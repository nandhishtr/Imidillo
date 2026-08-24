"""
FIWARE MCP Server for the Magdeburg Campus Mobility Assistant.
Exposes FIWARE Context Broker (NGSIv2) tools for querying real-time
sensor data: weather, parking, air quality, traffic, room sensors.
"""

import json
import sys
import os
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastmcp import FastMCP
from clients.fiware_client import FIWAREClient
from clients.imiq_events_client import IMIQEventsClient
from config import FIWARE_BASE_URL, FIWARE_API_KEY, IMIQ_EVENTS_URL

# Optional import of shared thresholds (file is being authored in parallel — tolerate absence).
try:
    from services.thresholds import (  # type: ignore
        CACHE_TTL_SECONDS as _SHARED_CACHE_TTL,
    )
except Exception:  # pragma: no cover - fallback when services.thresholds missing
    _SHARED_CACHE_TTL = 1800

mcp = FastMCP("fiware-sensors", instructions=(
    "Real-time IoT sensor data for OVGU campus and Magdeburg city via FIWARE Context Broker. "
    "Use this for live/real-time data only (weather, parking availability, air quality, traffic, water levels, "
    "and the OVGU campus Mensa's daily menu via the Mensa entity's todaysMenu attribute), "
    "plus the city event calendar (get_city_events). "
    "For static location data (buildings, restaurants, cafes, supermarkets) use the Neo4j server instead."
))

# ---------------------------------------------------------------------------
# Real-time entity types exposed via this server.
# Static location data (Building, POI, Stop, Street, Landmark, Area) lives in
# Neo4j — kept out of this server to avoid confusion.
# ---------------------------------------------------------------------------
from mcp_servers._sensor_types import REALTIME_TYPES
from mcp_servers._traffic_helpers import summarize_traffic_entity, haversine_m
_REALTIME_TYPES_SORTED = sorted(REALTIME_TYPES)

# ---------------------------------------------------------------------------
# Client (module-level singleton)
# ---------------------------------------------------------------------------
_client = FIWAREClient(FIWARE_BASE_URL, FIWARE_API_KEY)

# ---------------------------------------------------------------------------
# Type-list cache (15-minute TTL).  list_entity_types() is called on a hot
# path by the fiware agent to discover valid entity types; the answer changes
# rarely, so we cache the raw broker response and derive per-call views
# from it.  A single lock guards cache refill to prevent concurrent N+1
# storms on cold start.
# ---------------------------------------------------------------------------
_TYPE_CACHE_TTL_SECONDS = min(900, int(_SHARED_CACHE_TTL))  # 15 min, capped by shared ttl
_type_cache_lock = threading.Lock()
_type_cache: dict = {
    "fetched_at": 0.0,
    # Parsed entries: list of {"type_name": str, "attrs": [..], "count": Optional[int]}
    "entries": None,
    # Keep last error so callers get a useful message without retriggering broker calls
    "error": None,
}


def _parse_broker_types(raw_types) -> list[dict]:
    """Convert /types response (list of strings OR list of objects) into a uniform list.

    Returns entries of the form {"type_name": str, "attrs": [str], "count": Optional[int]}.
    Only entries whose type_name is in REALTIME_TYPES are kept.
    Per-type counts are taken from broker metadata when available; otherwise
    left as None (we refuse to N+1 fetch entities just to count them).
    """
    entries: list[dict] = []
    for t in raw_types or []:
        if isinstance(t, str):
            type_name = t
            attrs: list[str] = []
            count = None
        elif isinstance(t, dict):
            type_name = t.get("type") or t.get("id") or "unknown"
            raw_attrs = t.get("attrs")
            if isinstance(raw_attrs, dict):
                attrs = sorted(raw_attrs.keys())
            elif isinstance(raw_attrs, list):
                attrs = sorted(str(a) for a in raw_attrs)
            else:
                attrs = []
            # Broker-provided count (NGSIv2: `count`) — never entity enumeration
            count = t.get("count")
            if not isinstance(count, int):
                count = None
        else:
            continue

        if type_name not in REALTIME_TYPES:
            continue

        entries.append({"type_name": type_name, "attrs": attrs, "count": count})
    return entries


def _get_type_cache(force_refresh: bool = False) -> dict:
    """Return the cached type list, refreshing at most once per TTL window."""
    now = time.time()
    if not force_refresh:
        if _type_cache["entries"] is not None and (now - _type_cache["fetched_at"]) < _TYPE_CACHE_TTL_SECONDS:
            return _type_cache

    with _type_cache_lock:
        # Re-check inside lock (double-checked locking)
        now = time.time()
        if not force_refresh and _type_cache["entries"] is not None and (now - _type_cache["fetched_at"]) < _TYPE_CACHE_TTL_SECONDS:
            return _type_cache

        types_result = _client.get_types()
        if not types_result.get("success"):
            _type_cache["error"] = types_result
            # Keep any previously-cached entries so we serve stale-but-OK if broker flaps
            if _type_cache["entries"] is None:
                _type_cache["entries"] = []
            _type_cache["fetched_at"] = now
            return _type_cache

        _type_cache["entries"] = _parse_broker_types(types_result.get("types", []))
        _type_cache["fetched_at"] = now
        _type_cache["error"] = None
        return _type_cache


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_entity_types(entity_type: str = "", include_counts: bool = False) -> str:
    """Discover available FIWARE entity types, their attributes, and entity IDs.

    Returns ONLY the 9 real-time entity types (Weather, Parking, AirQuality,
    Traffic, Room, Vehicle, WaterLevel, DigitalTwin, Mensa). Static data
    (Building, POI, Stop, Street, Landmark, Area) lives in Neo4j — query
    neo4j_server for those. (The Mensa entity carries the OVGU campus Mensa's
    live daily menu in its `todaysMenu` attribute.)

    Args:
        entity_type: Optional. If provided, returns details (attributes +
                     entity IDs) only for that type (e.g. 'Parking'). If
                     empty, returns a summary of all 8 real-time types.
        include_counts: If True, include per-type entity counts on the
                        full-listing path. Counts are taken from broker
                        metadata; never via N+1 entity fetches. Entries
                        where the broker does not report a count expose
                        `count: null`.

    Returns:
        JSON. Shape depends on arguments:
        - entity_type="" -> {type_name: {"attributes": [...], "count": int|null}}
          (The full-listing path does NOT include entity_ids to avoid N+1.
           `count` here is the broker-reported total for the type, or null.)
        - entity_type="Parking" -> {"Parking": {"sample_count": int, "limit": int,
          "attributes": [...], "entity_ids": [...]}}
          (This path does 1 fetch for the requested type only. `sample_count`
          is the number of entities returned in this sample, up to `limit`.
          For the true total, paginate.)

    Note:
        Call this first if you're unsure which entity type exists. To get
        entity IDs, pass a specific entity_type — the full-list path omits
        IDs by design.
    """
    # Specific-type path: one entity query for that type (no N+1 — single fetch).
    if entity_type:
        if entity_type not in REALTIME_TYPES:
            return json.dumps({
                "error": "invalid_entity_type",
                "requested": entity_type,
                "valid": _REALTIME_TYPES_SORTED,
                "hint": "Static types (Building, POI, Stop, etc.) live in Neo4j.",
            })
        _SAMPLE_LIMIT = 100
        result = _client.query_entities(entity_type=entity_type, limit=_SAMPLE_LIMIT)
        if not result.get("success"):
            return json.dumps(result)
        entities = result.get("entities", [])
        attrs = set()
        ids = []
        for e in entities:
            ids.append(e.get("id", ""))
            attrs.update(k for k in e.keys() if k not in ("id", "type"))
        return json.dumps({
            entity_type: {
                "sample_count": len(entities),
                "limit": _SAMPLE_LIMIT,
                "attributes": sorted(attrs),
                "entity_ids": ids,
            }
        }, indent=2)

    # Full-listing path: cached type list, no N+1.
    cache = _get_type_cache()
    if not cache["entries"] and cache.get("error"):
        return json.dumps(cache["error"])

    info: dict = {}
    for entry in cache["entries"]:
        payload: dict = {"attributes": entry["attrs"]}
        if include_counts:
            payload["count"] = entry["count"]  # may be None if broker did not supply
        info[entry["type_name"]] = payload

    return json.dumps(info, indent=2)


@mcp.tool()
def query_entities(
    entity_type: str,
    entity_id: str = "",
    attrs: str = "",
    q: str = "",
    limit: int = 20
) -> str:
    """Query FIWARE entities by type with optional filters.

    Args:
        entity_type: Entity type to query. Must be one of the 9 real-time types:
                     Weather, Parking, AirQuality, Traffic, Room, Vehicle,
                     WaterLevel, DigitalTwin, Mensa (the campus Mensa's daily
                     menu — read its `todaysMenu` attribute).
        entity_id: Specific entity ID (e.g. 'Sensor:Weather:FacultyCS'). Leave empty for all.
        attrs: Comma-separated attribute names to retrieve (e.g. 'temperature,humidity').
               Leave empty for all attributes.
        q: NGSIv2 query filter string. Supports `<, >, <=, >=, ==, !=`. Compound
           filters join with `;` (logical AND). No regex/wildcards.
             Single:    q="humidity<80"
             Range:     q="humidity>20;humidity<30"
             Compound:  q="temperature>15;pressure<1010"
             Equality:  q="status==free"
           Leave empty for no filter.
        limit: Maximum entities to return (default 20, max 100)

    Returns:
        JSON with entities array, count, and success status.

    Examples:
        query_entities("Weather")
        query_entities("Weather", entity_id="Sensor:Weather:FacultyCS", attrs="temperature")
        query_entities("Parking", attrs="freeSpaces,totalSpaces")
        query_entities("Weather", q="humidity<80", attrs="humidity,temperature")
        query_entities("Weather", q="temperature>15;pressure<1010")
        query_entities("Mensa")  # campus Mensa daily menu — read todaysMenu
    """
    if entity_type and entity_type not in REALTIME_TYPES:
        return json.dumps({
            "error": "invalid_entity_type",
            "requested": entity_type,
            "valid": _REALTIME_TYPES_SORTED,
            "hint": "Static types (Building, POI, Stop, etc.) live in Neo4j.",
        })

    kwargs = {
        "entity_type": entity_type,
        "limit": min(limit, 100),
    }
    if entity_id:
        kwargs["entity_id"] = entity_id
    if attrs:
        kwargs["attrs"] = attrs.split(",")
    if q:
        kwargs["q"] = q

    result = _client.query_entities(**kwargs)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def get_entity_by_id(entity_id: str) -> str:
    """Fetch a single FIWARE entity by its exact ID.

    Args:
        entity_id: Full FIWARE entity ID. MUST be FIWARE format, e.g.
                   'Sensor:Weather:FacultyCS', 'ParkingSpot:ScienceHarbor',
                   'Room0'. For campus buildings/POIs, use
                   neo4j_server.execute_cypher instead — they do not have
                   FIWARE IDs.

    Returns:
        JSON with the entity data or error.
    """
    result = _client.get_entity_by_id(entity_id)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def query_by_location(
    latitude: float,
    longitude: float,
    sensor_type: str,
    radius: int = 500
) -> str:
    """Find the nearest sensor of a given type to a geographic location.

    Args:
        latitude: Latitude coordinate
        longitude: Longitude coordinate
        sensor_type: Entity type to search for. MUST be one of:
                     Weather, Parking, AirQuality, Traffic, Room, Vehicle,
                     WaterLevel, DigitalTwin, Mensa. Invalid values return a
                     structured error with the valid list.
        radius: Search radius in meters (default 500)

    Returns:
        JSON with the nearest matching entity.
    """
    if sensor_type not in REALTIME_TYPES:
        return json.dumps({
            "error": "invalid_sensor_type",
            "requested": sensor_type,
            "valid": _REALTIME_TYPES_SORTED,
            "hint": "Static POIs/buildings live in Neo4j — use neo4j_server.execute_cypher.",
        })

    result = _client.query_sensor_by_coordinates(
        latitude=latitude,
        longitude=longitude,
        sensor_type=sensor_type,
        radius=radius
    )
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def get_weather(limit: int = 10) -> str:
    """Get current weather data from all weather sensors on campus.

    Returns temperature, humidity, pressure, wind, and rain data from all stations.
    """
    result = _client.get_weather(limit=limit)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def get_parking(limit: int = 10) -> str:
    """Get current parking availability across all campus parking lots.

    Returns free spaces and total capacity for each lot.
    """
    result = _client.get_parking(limit=limit)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def get_air_quality(limit: int = 10) -> str:
    """Get current air quality measurements from all stations.

    Returns NO2, O3, PM10, PM2.5 levels.
    """
    result = _client.query_entities(entity_type="AirQuality", limit=limit)
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def get_traffic_flow(latitude: float, longitude: float, radius: int = 300) -> str:
    """Get road congestion AROUND a point from the Magdeburg FIWARE traffic sensors.

    Checks EVERY Traffic segment that currently reports a live `avgSpeed`
    within `radius` metres — not just the single nearest sensor, which may sit
    on a side road or report no speed at all. Aggregates to an overall
    `congestion` verdict plus a `nearby_slowdowns` list naming the slow
    street(s). When no live segment in range reports a slowdown,
    congestion='clear' (basis='no_slowdowns_reported'). Never fabricates a
    specific speed or delay number.
    """
    try:
        lat_f, lon_f = float(latitude), float(longitude)
    except (TypeError, ValueError):
        return json.dumps({"success": False, "error": "invalid_coords",
                           "detail": "latitude/longitude must be numeric"})
    if not (52.05 <= lat_f <= 52.20 and 11.55 <= lon_f <= 11.75):
        return json.dumps({"success": False, "error": "coords_outside_magdeburg",
                           "received": {"lat": lat_f, "lon": lon_f}})

    try:
        res = _client.query_entities(entity_type="Traffic", q="avgSpeed>0", limit=200)
    except Exception as e:
        return json.dumps({"success": False, "error": "fiware_unavailable", "detail": str(e)})
    if not isinstance(res, dict) or not res.get("success"):
        return json.dumps({
            "success": True, "found": False, "source": "fiware",
            "congestion": "clear", "basis": "no_slowdowns_reported",
            "radius_m": radius, "live_segments_in_radius": 0, "nearby_slowdowns": [],
            "note": "No live traffic readings available right now — present as clear / no delays.",
        })

    near = []
    for ent in res.get("entities", []):
        if not isinstance(ent, dict):
            continue
        loc = ent.get("location")
        if not (isinstance(loc, str) and "," in loc):
            continue
        parts = loc.split(",")
        try:
            la, lo = float(parts[0]), float(parts[1])
        except (ValueError, IndexError):
            continue
        if haversine_m(lat_f, lon_f, la, lo) <= radius:
            near.append(summarize_traffic_entity(ent))

    worst = "clear"
    slowdowns = []
    for summ in near:
        cong = summ.get("congestion")
        if cong == "heavy":
            worst = "heavy"
        elif cong == "moderate" and worst != "heavy":
            worst = "moderate"
        if cong in ("heavy", "moderate"):
            slowdowns.append({
                "street": str(summ.get("segment", "")).split(":", 1)[-1],
                "congestion": cong,
                "live_speed_kmh": summ.get("live_speed_kmh"),
                "speed_limit_kmh": summ.get("speed_limit_kmh"),
                "speed_ratio": summ.get("speed_ratio"),
            })
    slowdowns.sort(key=lambda s: s["speed_ratio"] if s.get("speed_ratio") is not None else 9)

    note = ("No slowdown reported within this area — present as clear / no delays."
            if not slowdowns else
            "Live slowdown(s) reported on the listed street(s); mention the worst one.")
    return json.dumps({
        "success": True, "found": True, "source": "fiware",
        "congestion": worst,
        "basis": "live_speed" if near else "no_slowdowns_reported",
        "radius_m": radius,
        "live_segments_in_radius": len(near),
        "nearby_slowdowns": slowdowns,
        "note": note,
    }, indent=2)


# ---------------------------------------------------------------------------
# City events (Magdeburg event calendar, scraped into the IMIQ platform)
# ---------------------------------------------------------------------------
_events_client = IMIQEventsClient(IMIQ_EVENTS_URL)

try:
    from zoneinfo import ZoneInfo
    _BERLIN_TZ = ZoneInfo("Europe/Berlin")
except Exception:  # no IANA tz data — fall back to server-local time
    _BERLIN_TZ = None

_EVENTS_MAX_DESC = 220


def _today_iso() -> str:
    from datetime import datetime
    now = datetime.now(_BERLIN_TZ) if _BERLIN_TZ else datetime.now()
    return now.date().isoformat()


@mcp.tool()
def get_city_events(from_date: str = "", to_date: str = "",
                    near_lat: float | None = None, near_lon: float | None = None,
                    radius: int = 2000, query: str = "", limit: int = 20) -> str:
    """What's on in Magdeburg: concerts, exhibitions, festivals, markets,
    family events — the city's event calendar.

    Dates are YYYY-MM-DD, INCLUSIVE; both default to today. Compute them from
    the current date you are given ("tomorrow", "this weekend" → the coming
    Saturday..Sunday). Pass the user's location as `near_lat`/`near_lon` for
    "events near me" (radius in meters), and a topic word as `query`
    ("konzert", "kinder", "markt") to filter by name/venue/description.

    Events carry `latitude`/`longitude` and are pinned on the map
    automatically. `start`/`end` are LOCAL Magdeburg wall-clock times; a
    00:00 start means all-day. Descriptions are German — summarize them in
    the user's language. `count_total` says how many matched before `limit`.
    """
    frm = (from_date or "").strip() or _today_iso()
    to = (to_date or "").strip() or frm

    events = _events_client.get_events(frm, to)
    if events is None:
        return json.dumps({"success": False, "error": "events_feed_unavailable",
                           "detail": "The city events feed is not reachable right now."})

    if near_lat is not None and near_lon is not None:
        try:
            la, lo = float(near_lat), float(near_lon)
            events = [e for e in events
                      if e.get("lat") is not None and e.get("lon") is not None
                      and haversine_m(la, lo, e["lat"], e["lon"]) <= radius]
        except (TypeError, ValueError):
            pass

    needle = (query or "").strip().lower()
    if needle:
        events = [e for e in events
                  if needle in f'{e.get("name", "")} {e.get("venue", "")} '
                               f'{e.get("description", "")}'.lower()]

    total = len(events)
    shaped = []
    for e in events[:max(1, int(limit))]:
        desc = e.get("description") or ""
        if len(desc) > _EVENTS_MAX_DESC:
            desc = desc[:_EVENTS_MAX_DESC].rsplit(" ", 1)[0] + "…"
        shaped.append({
            "name": e["name"],
            "venue": e.get("venue"),
            "address": e.get("address"),
            "start": e.get("start"),
            "end": e.get("end"),
            "price": e.get("price"),
            "latitude": e.get("lat"),
            "longitude": e.get("lon"),
            "url": e.get("url"),
            "description": desc,
        })

    return json.dumps({
        "success": True,
        "from": frm, "to": to,
        "count_total": total,
        "count_returned": len(shaped),
        "events": shaped,
        "note": ("No events in this range." if total == 0 else
                 "Curate: lead with the few most interesting, don't list all."),
    }, indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="stdio")
