"""
Routing MCP Server for the Magdeburg Campus Mobility Assistant.
Exposes route calculation (walking, cycling, driving), geocoding,
and place-to-coordinate resolution.

Routes come from the IMIQ ranked-routes API (GraphHopper-backed, whole-city
coverage) — ONE call returns all road modes. The IMIQ router provides
summaries only (distance / duration; no geometry / street list / turn-by-turn),
so OpenRouteService is called per available mode purely to fetch each route's
POLYLINE GEOMETRY for the map — a visual overlay only; IMIQ stays the source of
truth for distance and duration. ORS also remains the geocoder fallback for
off-graph place names. Traffic data comes from the FIWARE sensor network
(radius check near the route), and public transit stays on the Neo4j NEXT_STOP
graph (see find_transit_route).
"""

import json
import math
import sys
import os
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastmcp import FastMCP
from models import Coordinates
from clients.fiware_client import FIWAREClient
from clients.imiq_client import IMIQRoutingClient
from clients.ors_client import ORSClient
from config import (
    IMIQ_ROUTING_URL,
    FIWARE_BASE_URL, FIWARE_API_KEY,
    ORS_API_KEY, ORS_BASE_URL, HTTP_TIMEOUT,
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE,
)
from mcp_servers._traffic_helpers import summarize_traffic_entity, haversine_m
from mcp_servers._place_resolver import resolve_place, resolve_place_candidates, decide_place
from mcp_servers._geocode import (
    geocode_fallback,
    geocode_fallback_candidates,
    looks_like_street_address,
    short_label,
)
from neo4j_tools import Neo4jTransitGraph
from neo4j import Query

mcp = FastMCP("routing", instructions=(
    "Route calculation for Magdeburg (IMIQ city routing service). "
    "Supports walking, cycling, and driving routes between coordinates. "
    "Use resolve_place_to_coordinates() or geocode() to convert place names to coordinates first, "
    "then use the route tools with those coordinates. "
    "All coordinates must lie within Magdeburg (lat 52.05-52.20, lon 11.55-11.75)."
))

# ---------------------------------------------------------------------------
# Clients (module-level singletons). Geocoding lives in mcp_servers._geocode
# (IMIQ geocoder first, Nominatim backup).
# ---------------------------------------------------------------------------
_imiq = IMIQRoutingClient(IMIQ_ROUTING_URL)
_fiware = FIWAREClient(FIWARE_BASE_URL, FIWARE_API_KEY)

# ORS is used here ONLY to fetch route polyline geometry for the map overlay;
# IMIQ remains the source of truth for distance/duration. Disabled when no API
# key is set (route cards then fall back to the dashed start->end straight line).
_ors = ORSClient(ORS_API_KEY, ORS_BASE_URL, HTTP_TIMEOUT)
_GEOMETRY_ENABLED = bool(ORS_API_KEY)

# ---------------------------------------------------------------------------
# Neo4j-first place resolution. The campus knowledge graph is the PRIMARY
# resolver (via the shared canonical resolver in _place_resolver.py, full-text
# based — no embedding model); the geocoder chain is only a LAST resort for
# genuine off-graph street addresses. `_neo4j` is kept solely for its driver:
# _run_read() issues the resolver's read-only Cypher through it.
# ---------------------------------------------------------------------------
_neo4j = Neo4jTransitGraph(NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE)

def _run_read(cypher: str, params: dict | None = None, timeout: float = 8.0) -> list[dict]:
    """Read-only Cypher via the routing server's own Neo4j driver — the read
    function the shared canonical place resolver runs through."""
    with _neo4j.driver.session(database=_neo4j.database) as session:
        result = session.run(Query(cypher, timeout=timeout), parameters=params or {})
        return [dict(record) for record in result]


def _resolve_via_neo4j(place_name: str) -> dict | None:
    """Resolve a place name to coordinates via the shared canonical resolver
    (Neo4j FIRST; curated campus nodes ranked before OSM imports). Returns
    ``{name, lat, lon, type}`` on a graph hit, or ``None`` when nothing matches
    — the caller then falls back to the geocoder chain (the last resort).

    This is the SAME resolver find_transit_route uses, so a place like "mensa"
    maps to ONE node across every tool instead of three disagreeing matches.
    """
    try:
        hit = resolve_place(_run_read, place_name)
    except Exception:
        # Resolution must never crash routing — fall back to the geocoder.
        return None
    if not hit or hit.get("lat") is None or hit.get("lon") is None:
        return None
    return {"name": hit.get("name"), "lat": hit["lat"], "lon": hit["lon"], "type": hit.get("type")}


# ---------------------------------------------------------------------------
# Disambiguation: resolve to a candidate SET, then decide (auto-pick nearest a
# spatial anchor, or report the candidates so the agent can ask "which one?").
# ---------------------------------------------------------------------------
def _anchor(lat, lon):
    """A validated (lat, lon) reference for picking the nearest branch, or None.
    Out-of-Magdeburg / non-numeric anchors are dropped (no bad auto-pick)."""
    if lat is None or lon is None:
        return None
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if _MGB_LAT_MIN <= lat_f <= _MGB_LAT_MAX and _MGB_LON_MIN <= lon_f <= _MGB_LON_MAX:
        return (lat_f, lon_f)
    return None


def _candidate_payload(c: dict) -> dict:
    """Shape one disambiguation candidate for the agent + map pin (drops the
    resolver's internal fields). `latitude`/`longitude` let api.py pin it."""
    out = {"name": c.get("name"), "type": c.get("type"),
           "latitude": c.get("lat"), "longitude": c.get("lon")}
    if c.get("district"):
        out["district"] = c["district"]
    if c.get("distance_m") is not None:
        out["distance_m"] = c["distance_m"]
    ns = c.get("nearest_stop") or {}
    if ns.get("name"):
        out["nearest_stop"] = ns["name"]
    return out


def _resolve_endpoint_decision(name: str, anchor=None) -> tuple:
    """Resolve ONE place name to a routing decision.

    Returns ``("resolved", {name, lat, lon, type, matched, alternatives})`` |
    ``("none", None)``.

    Street-address queries (suffix + house number) go straight to the
    geocoder — house numbers are geocoder territory, the graph knows streets.
    Everything else tries the Neo4j candidate set first, then the off-graph
    geocoder's candidate set through the SAME decision policy. Multiple
    branches always auto-pick (nearest to `anchor` when given, else best
    match); the runners-up ride along in `alternatives` so the agent can
    mention them — resolution never blocks on a "which one?" question."""
    if not name or not name.strip():
        return ("none", None)

    if looks_like_street_address(name):
        geo = geocode_fallback(name)
        if geo:
            return ("resolved", {"name": short_label(geo.get("label", "")) or name,
                                 "lat": geo["lat"], "lon": geo["lon"],
                                 "type": "geocoded", "matched": geo["source"]})
        # Fall through: not a geocodable address after all — try it as a name.

    try:
        cands = resolve_place_candidates(_run_read, name)
    except Exception:
        cands = []
    if cands:
        dec = decide_place(cands, anchor=anchor)
        if dec["status"] == "resolved":
            p = dec["place"]
            return ("resolved", {"name": p.get("name"), "lat": p["lat"], "lon": p["lon"],
                                 "type": p.get("type"), "matched": "neo4j",
                                 "alternatives": dec.get("alternatives") or []})

    geo_cands = geocode_fallback_candidates(name)
    if geo_cands:
        dec = decide_place(geo_cands, anchor=anchor)
        if dec["status"] == "resolved":
            p = dec["place"]
            return ("resolved", {"name": p.get("name") or short_label(p.get("label", "")) or name,
                                 "lat": p["lat"], "lon": p["lon"],
                                 "type": p.get("type") or "geocoded",
                                 "matched": p.get("source", "geocoder"),
                                 "alternatives": dec.get("alternatives") or []})
    return ("none", None)


# ---------------------------------------------------------------------------
# "Nearest X to me" — true nearest by REAL travel distance, not crow-flies.
# Magdeburg is split by the Elbe, so a branch closer as a straight line can be
# farther by road. Pull ALL branches of the name (via the brand grouping),
# shortlist the closest few by straight line, route to each, pick the genuine
# winner. _NEAREST_REFINE bounds the routing fan-out.
# ---------------------------------------------------------------------------
_NEAREST_REFINE = 4

_BRANCHES_Q = """
    MATCH (p:POI)
    WHERE p.latitude IS NOT NULL AND p.longitude IS NOT NULL
      AND (toLower(p.name) CONTAINS $q OR p.brand_key = $q OR p.type = $qtype)
    RETURN p.name AS name, p.type AS type, p.latitude AS lat,
           p.longitude AS lon, p.district AS district
    LIMIT 80
"""


def _all_branches(query: str) -> list[dict]:
    """Every POI matching a chain/name/type — by name substring, brand_key, or
    type. Wider than the resolver's top-N so 'nearest edeka' sees ALL Edekas,
    not just the best name matches. Coarse-deduped by location."""
    q = (query or "").strip().lower()
    if not q:
        return []
    qtype = "".join(w.capitalize() for w in q.split())  # 'fast food' -> 'FastFood'
    try:
        rows = _run_read(_BRANCHES_Q, {"q": q, "qtype": qtype}, 8.0)
    except Exception:
        return []
    out, seen = [], set()
    for r in rows:
        if r.get("lat") is None or r.get("lon") is None:
            continue
        key = (round(r["lat"], 4), round(r["lon"], 4))
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(r))
    return out


def _nearest_online_parking(lat: float, lon: float, radius_m: int = 800) -> dict:
    """Nearest live FIWARE parking garage to a destination.

    ALWAYS returns the closest garage with status=='Online' (an Offline
    sensor's `freeSpots` is meaningless), with its distance, so the agent can
    name it and how far it is ("~110 free at the Universitätsplatz garage,
    350 m away"). `within_radius` flags whether it's within ``radius_m`` (a
    short walk) so a more distant garage can be phrased honestly ("nearest live
    parking is ~1.2 km away"). Only returns ``{found: False}`` when NO garage
    reports live availability at all. Best-effort: any failure returns
    ``{found: False}`` rather than breaking routing.
    """
    try:
        res = _fiware.query_entities(entity_type="Parking", limit=50)
    except Exception:
        return {"found": False}
    if not isinstance(res, dict) or not res.get("success"):
        return {"found": False}
    best = None
    for e in res.get("entities", []):
        if not isinstance(e, dict) or str(e.get("status")) != "Online":
            continue
        loc = e.get("location")
        plat = plon = None
        if isinstance(loc, str) and "," in loc:
            try:
                parts = loc.split(",")
                plat, plon = float(parts[0]), float(parts[1])
            except (ValueError, IndexError):
                continue
        elif isinstance(loc, dict):
            coords = loc.get("coordinates")
            if isinstance(coords, list) and len(coords) >= 2:
                plat, plon = coords[1], coords[0]
        if plat is None:
            continue
        d = haversine_m(lat, lon, plat, plon)
        if best is None or d < best["distance_m"]:
            best = {
                "name": e.get("name") or str(e.get("id", "")).split(":", 1)[-1],
                "free_spots": e.get("freeSpots"),
                "total_spots": e.get("totalSpots"),
                "distance_m": round(d),
            }
    if best:
        return {"found": True, "within_radius": best["distance_m"] <= radius_m, **best}
    return {"found": False}


# Outdoor air-quality pollutants we surface (µg/m³). CO2-only indoor sensors are skipped.
_AQ_POLLUTANTS = ("no2", "pm25", "pm10", "o3")


def _nearest_air_quality(lat: float, lon: float, radius_m: int = 3000) -> dict:
    """Nearest live FIWARE air-quality station to a point (e.g. a route midpoint).

    Considers only stations reporting at least one outdoor pollutant
    (NO2/PM2.5/PM10/O3). Returns
    ``{found: True, station, distance_m, pollutants:{...}}`` for the closest in
    range, else ``{found: False}``. Best-effort: failure returns
    ``{found: False}`` rather than breaking routing.
    """
    try:
        res = _fiware.query_entities(entity_type="AirQuality", limit=50)
    except Exception:
        return {"found": False, "radius_m": radius_m}
    if not isinstance(res, dict) or not res.get("success"):
        return {"found": False, "radius_m": radius_m}
    best = None
    for e in res.get("entities", []):
        if not isinstance(e, dict):
            continue
        loc = e.get("location")
        plat = plon = None
        if isinstance(loc, str) and "," in loc:
            try:
                parts = loc.split(",")
                plat, plon = float(parts[0]), float(parts[1])
            except (ValueError, IndexError):
                continue
        elif isinstance(loc, dict):
            coords = loc.get("coordinates")
            if isinstance(coords, list) and len(coords) >= 2:
                plat, plon = coords[1], coords[0]
        if plat is None:
            continue
        d = haversine_m(lat, lon, plat, plon)
        if d > radius_m:
            continue
        pollutants = {k: e.get(k) for k in _AQ_POLLUTANTS if e.get(k) not in (None, "", [], {})}
        if not pollutants:
            continue
        if best is None or d < best["distance_m"]:
            best = {
                "station": e.get("name") or str(e.get("id", "")).split(":", 1)[-1],
                "distance_m": round(d),
                "pollutants": pollutants,
            }
    if best:
        return {"found": True, **best}
    return {"found": False, "radius_m": radius_m}


def _wx_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _weather_condition(temp, precip, wind) -> str:
    """Qualitative weather hint for the route card from the live Weather entity.
    `rain` when precipitation > 0; `windy` is heuristic (wind > 25, which reads
    as windy in km/h and won't false-fire on m/s data); else `clear`. The agent
    still receives the raw temperature/precipitation numbers."""
    if precip is not None and precip > 0:
        return "rain"
    if wind is not None and wind > 25:
        return "windy"
    return "clear"


def _nearest_weather(lat: float, lon: float, radius_m: int = 6000) -> dict:
    """Nearest live FIWARE Weather station to a point (e.g. a route midpoint).

    Returns ``{found, station, temperature, precipitation, wind, condition,
    distance_m}``. Campus weather is ~uniform, so if no station carries
    parseable coordinates we fall back to the first reporting station rather
    than returning nothing. Best-effort: failure returns ``{found: False}``.
    """
    try:
        res = _fiware.query_entities(entity_type="Weather", limit=50)
    except Exception:
        return {"found": False}
    if not isinstance(res, dict) or not res.get("success"):
        return {"found": False}
    best = None
    fallback = None
    for e in res.get("entities", []):
        if not isinstance(e, dict):
            continue
        temp = _wx_float(e.get("temperature"))
        if temp is None:
            continue
        rec = {
            "station": e.get("name") or str(e.get("id", "")).split(":", 1)[-1],
            "temperature": temp,
            "precipitation": _wx_float(e.get("precipitation")),
            "wind": _wx_float(e.get("windSpeed") or e.get("windspeed") or e.get("wind")),
        }
        if fallback is None:
            fallback = {**rec, "distance_m": None}
        loc = e.get("location")
        plat = plon = None
        if isinstance(loc, str) and "," in loc:
            try:
                parts = loc.split(",")
                plat, plon = float(parts[0]), float(parts[1])
            except (ValueError, IndexError):
                plat = plon = None
        elif isinstance(loc, dict):
            coords = loc.get("coordinates")
            if isinstance(coords, list) and len(coords) >= 2:
                plat, plon = coords[1], coords[0]
        if plat is None:
            continue
        d = haversine_m(lat, lon, plat, plon)
        if d > radius_m:
            continue
        if best is None or d < best["distance_m"]:
            best = {**rec, "distance_m": round(d)}
    chosen = best or fallback
    if not chosen:
        return {"found": False}
    chosen["condition"] = _weather_condition(
        chosen.get("temperature"), chosen.get("precipitation"), chosen.get("wind")
    )
    return {"found": True, **chosen}


def _dist_to_segment_m(lat: float, lon: float,
                       lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance in metres from a point to the start→end segment (local
    equirectangular projection — fine at city scale)."""
    klat = 111_320.0
    klon = 111_320.0 * math.cos(math.radians((lat1 + lat2) / 2.0))
    x, y = (lon - lon1) * klon, (lat - lat1) * klat
    dx, dy = (lon2 - lon1) * klon, (lat2 - lat1) * klat
    seg2 = dx * dx + dy * dy
    t = 0.0 if seg2 == 0 else max(0.0, min(1.0, (x * dx + y * dy) / seg2))
    return math.hypot(x - t * dx, y - t * dy)


def _traffic_near_route(start_lat: float, start_lon: float,
                        end_lat: float, end_lon: float) -> dict | None:
    """Live congestion near the route corridor (FIWARE Traffic segments).

    The IMIQ router returns no street list, so this checks every live
    Traffic segment within a corridor around the straight start→end line —
    NOT a radius around the midpoint, which for long trips swept in
    city-center streets the route never touches. The corridor half-width
    scales with the trip span (roads bow away from the straight line) but
    stays well under the city's breadth. Aggregation mirrors the fiware
    server's get_traffic_flow. Best-effort: any failure returns None so
    routing never breaks.
    """
    span_m = haversine_m(start_lat, start_lon, end_lat, end_lon)
    corridor_m = min(1500.0, max(700.0, span_m * 0.2))

    try:
        res = _fiware.query_entities(entity_type="Traffic", q="avgSpeed>0", limit=200)
    except Exception:
        return None
    if not isinstance(res, dict) or not res.get("success"):
        return None

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
        if _dist_to_segment_m(la, lo, start_lat, start_lon, end_lat, end_lon) <= corridor_m:
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

    return {
        "congestion": worst,
        "basis": "live_speed" if near else "no_slowdowns_reported",
        "method": "corridor_near_route",
        "corridor_m": round(corridor_m),
        "live_segments_near_route": len(near),
        "slowdowns": slowdowns,
    }

# ---------------------------------------------------------------------------
# Grounded bounds: Magdeburg bounding box.  Coordinates outside this box
# are rejected up-front — stops a swapped-lat/lon bug from computing a
# 110 km route through Saxony-Anhalt farmland.
# ---------------------------------------------------------------------------
_MGB_LAT_MIN, _MGB_LAT_MAX = 52.05, 52.20
_MGB_LON_MIN, _MGB_LON_MAX = 11.55, 11.75

# Per-mode route timeout (seconds) for get_all_routes parallel fan-out.
_ROUTE_MODE_TIMEOUT_S = 10


def _validate_magdeburg_coords(lat: float, lon: float) -> dict | None:
    """Reject coordinates outside the Magdeburg bounding box.

    Returns an error dict if out of range, None if OK.  Also catches the
    common swapped-lat/lon mistake (lon passed as lat): any non-numeric or
    NaN values also fail validation.
    """
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return {
            "error": "invalid_coords",
            "reason": "lat/lon must be numeric",
            "received": {"lat": lat, "lon": lon},
        }
    if lat_f != lat_f or lon_f != lon_f:  # NaN check
        return {"error": "invalid_coords", "reason": "NaN"}
    if not (_MGB_LAT_MIN <= lat_f <= _MGB_LAT_MAX and _MGB_LON_MIN <= lon_f <= _MGB_LON_MAX):
        return {
            "error": "coords_outside_magdeburg",
            "bounds": {
                "lat_min": _MGB_LAT_MIN, "lat_max": _MGB_LAT_MAX,
                "lon_min": _MGB_LON_MIN, "lon_max": _MGB_LON_MAX,
            },
            "received": {"lat": lat_f, "lon": lon_f},
            "hint": "Coordinates may be swapped (lon/lat) or outside Magdeburg.",
        }
    return None


def _validate_route_endpoints(start_lat: float, start_lon: float,
                              end_lat: float, end_lon: float) -> dict | None:
    """Validate both start and end coordinates; returns error dict or None."""
    err = _validate_magdeburg_coords(start_lat, start_lon)
    if err:
        err["which"] = "start"
        return err
    err = _validate_magdeburg_coords(end_lat, end_lon)
    if err:
        err["which"] = "end"
        return err
    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def geocode(place_name: str) -> str:
    """Convert a place name to geographic coordinates (in-house IMIQ geocoder
    first, OSM Nominatim as backup). Bounded to Magdeburg.

    Args:
        place_name: Place name to geocode, e.g. 'Hauptbahnhof', 'Alter Markt',
                    'Ernst-Reuter-Allee 22', 'Ausländerbehörde'

    Returns:
        JSON with latitude/longitude and the matched place label, or an error
        if not found. German place names resolve far better than English
        descriptions — retry with the German name on a miss.
    """
    hit = geocode_fallback(place_name)
    if hit:
        return json.dumps({
            "success": True,
            "place": place_name,
            "latitude": hit["lat"],
            "longitude": hit["lon"],
            "matched_name": short_label(hit.get("label", "")),
            "used_method": hit["source"],
        })
    return json.dumps({
        "success": False,
        "error": f"Could not geocode '{place_name}'",
        "hint": "If this is an English description, retry with the German "
                "name (e.g. \"Foreigners' Office\" -> 'Ausländerbehörde').",
    })


@mcp.tool()
def resolve_place_to_coordinates(place_name: str,
                                 near_lat: float | None = None,
                                 near_lon: float | None = None) -> str:
    """Resolve a campus/city place name to coordinates — Neo4j FIRST.

    Looks the name up in the Neo4j knowledge graph (stops, buildings incl.
    "Building NN" numbers + aliases, POIs by name/cuisine/properties,
    landmarks). Only if the graph has no confident match does it fall back to
    the city-wide geocoder — the LAST resort, for genuine off-graph street
    addresses.

    When a name matches SEVERAL distinct places (a chain with multiple
    branches — "Lidl", "World of Pizza"), the tool AUTO-PICKS: the branch
    nearest `near_lat`/`near_lon` when you pass the user's location, else the
    best match. The runners-up come back in `alternatives` — answer with the
    pick immediately (name its district/street) and at most mention the
    alternatives in one closing clause. Never ask "which one?" first.

    Args:
        place_name: Place name, e.g. 'mensa', 'Building 03', 'ENERCON', 'Lidl'.
        near_lat, near_lon: optional user location, to pick the nearest branch.

    Returns:
        Resolved: {"success": true, "place", "coordinates": [lat, lon], "latitude",
            "longitude", "used_method": "neo4j"|"imiq"|"nominatim", "matched_name"?,
            "type"?, "alternatives": [...]?}
        Not found: {"success": false, "error": "...", "tried": [...]}
    """
    anchor = _anchor(near_lat, near_lon)

    # 0. Street addresses (suffix + house number) are geocoder territory —
    #    a single, inherently unambiguous hit; no graph query needed.
    if looks_like_street_address(place_name):
        geo = geocode_fallback(place_name)
        if geo:
            return json.dumps({
                "success": True,
                "place": place_name,
                "coordinates": [geo["lat"], geo["lon"]],
                "latitude": geo["lat"],
                "longitude": geo["lon"],
                "used_method": geo["source"],
                "matched_name": short_label(geo.get("label", "")),
            })
        # Fall through: not a geocodable address after all — try it as a name.

    # 1. Neo4j knowledge graph FIRST — as a candidate SET, then decide.
    try:
        cands = resolve_place_candidates(_run_read, place_name)
    except Exception:
        cands = []
    if cands:
        dec = decide_place(cands, anchor=anchor)
        if dec["status"] == "resolved":
            hit = dec["place"]
            out = {
                "success": True,
                "place": place_name,
                "coordinates": [hit["lat"], hit["lon"]],
                "latitude": hit["lat"],
                "longitude": hit["lon"],
                "used_method": "neo4j",
                "matched_name": hit.get("name"),
                "type": hit.get("type"),
            }
            if dec.get("alternatives"):
                out["alternatives"] = [_candidate_payload(c) for c in dec["alternatives"]]
            return json.dumps(out)

    # 2. Off-graph → the geocoder's candidate set through the SAME decision
    #    policy (IMIQ geocoder first, Nominatim backup): auto-pick near the
    #    anchor, else the best match — alternatives ride along.
    geo_cands = geocode_fallback_candidates(place_name)
    if geo_cands:
        dec = decide_place(geo_cands, anchor=anchor)
        if dec["status"] == "resolved":
            hit = dec["place"]
            out = {
                "success": True,
                "place": place_name,
                "coordinates": [hit["lat"], hit["lon"]],
                "latitude": hit["lat"],
                "longitude": hit["lon"],
                "used_method": hit.get("source", "geocoder"),
                "matched_name": hit.get("name") or short_label(hit.get("label", "")),
                "type": hit.get("type"),
                "note": "Not found in Neo4j; resolved by city-wide geocoder — "
                        "matched_name says what it matched.",
            }
            if dec.get("alternatives"):
                out["alternatives"] = [_candidate_payload(c) for c in dec["alternatives"]]
            return json.dumps(out)

    return json.dumps({
        "success": False,
        "error": f"Place '{place_name}' not found",
        "tried": ["neo4j", "imiq_geocoder", "nominatim"],
        "hint": "If this is an English description, retry with the German "
                "name (e.g. \"Foreigners' Office\" -> 'Ausländerbehörde').",
    })


def _single_mode_route(mode: str, start_lat: float, start_lon: float,
                       end_lat: float, end_lon: float) -> str:
    """Shared body for the three single-mode tools: validate, compute via
    IMIQ (with only this mode's enrichments), shape as a {"success": ...}
    payload like the tools always returned."""
    err = _validate_route_endpoints(start_lat, start_lon, end_lat, end_lon)
    if err:
        return json.dumps(err)

    start = Coordinates(lat=start_lat, lon=start_lon)
    end = Coordinates(lat=end_lat, lon=end_lon)
    routes = _compute_routes(start, end, want=(mode,))
    result = dict(routes.get(mode) or {})
    if not result.pop("available", False):
        return json.dumps({
            "success": False,
            "error": result.get("error", f"{mode.capitalize()} route calculation failed"),
        })
    result["success"] = True
    result["profile"] = mode
    # Endpoint echo — the UI card layer synthesizes a start→end map line from
    # these (the IMIQ router returns no route geometry).
    result["start"] = {"lat": start_lat, "lon": start_lon}
    result["end"] = {"lat": end_lat, "lon": end_lon}
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def get_walking_route(
    start_lat: float, start_lon: float,
    end_lat: float, end_lon: float
) -> str:
    """Calculate a walking route between two coordinate points.

    Both endpoints must lie within the Magdeburg bounding box
    (lat 52.05-52.20, lon 11.55-11.75). The router only offers walking for
    distances up to roughly ~3 km — beyond that it reports unavailable.

    Args:
        start_lat: Origin latitude
        start_lon: Origin longitude
        end_lat: Destination latitude
        end_lon: Destination longitude

    Returns:
        JSON with distance, duration, and air quality along the way.
    """
    return _single_mode_route("walking", start_lat, start_lon, end_lat, end_lon)


@mcp.tool()
def get_cycling_route(
    start_lat: float, start_lon: float,
    end_lat: float, end_lon: float
) -> str:
    """Calculate a cycling route between two coordinate points.

    Both endpoints must lie within the Magdeburg bounding box
    (lat 52.05-52.20, lon 11.55-11.75).

    Args:
        start_lat: Origin latitude
        start_lon: Origin longitude
        end_lat: Destination latitude
        end_lon: Destination longitude

    Returns:
        JSON with distance, duration, and air quality along the way.
    """
    return _single_mode_route("cycling", start_lat, start_lon, end_lat, end_lon)


@mcp.tool()
def get_driving_route(
    start_lat: float, start_lon: float,
    end_lat: float, end_lon: float
) -> str:
    """Calculate a driving route between two coordinate points.

    ETA is free-flow distance/duration — live road congestion comes from the
    FIWARE traffic sensors and is embedded in the result's `traffic` field
    (segments near the route corridor), with the nearest live parking garage
    to the destination in `destination_parking`. No turn-by-turn directions —
    the router returns route summaries only.

    Both endpoints must lie within the Magdeburg bounding box
    (lat 52.05-52.20, lon 11.55-11.75).

    Args:
        start_lat: Origin latitude
        start_lon: Origin longitude
        end_lat: Destination latitude
        end_lon: Destination longitude

    Returns:
        JSON with distance, duration, nearby traffic, and destination parking.
    """
    return _single_mode_route("driving", start_lat, start_lon, end_lat, end_lon)


# ---------------------------------------------------------------------------
# Core route computation: ONE IMIQ call for all road modes + parallel
# live-context enrichments (air quality / traffic / destination parking).
# ---------------------------------------------------------------------------

_ALL_MODES = ("walking", "cycling", "driving")


def _future_result(future, default=None):
    try:
        return future.result(timeout=_ROUTE_MODE_TIMEOUT_S)
    except Exception:
        return default


def _ors_geometry(mode: str, start: Coordinates, end: Coordinates):
    """Best-effort ORS polyline for ONE road mode.

    IMIQ returns no geometry, so we ask ORS for the SHAPE of the path only —
    the distance/duration shown to the user stay IMIQ's. Returns the route as
    an ENCODED polyline string (compact; decoded to ``[[lat, lon], ...]`` only
    at card-build time in api.py, never placed in the agent's context), or
    None. Geometry is a visual nicety and must never break routing.
    """
    try:
        res = _ors.get_route(start, end, profile=mode)
    except Exception:
        return None
    if isinstance(res, dict) and res.get("success"):
        return res.get("geometry") or None
    return None


def _compute_routes(start: Coordinates, end: Coordinates,
                    want: tuple = _ALL_MODES) -> dict:
    """All road modes between two points via one IMIQ ranked-routes call.

    Returns ``{walking, cycling, driving}`` where each mode is
    ``{"available": True, distance, duration, distance_meters,
    duration_seconds, source}`` or ``{"available": False, "error": ...}``
    (the IMIQ router omits `foot` beyond roughly ~3 km).

    Enrichments (fetched in parallel, only for modes in `want` that are
    available; failures degrade to absent fields, never break routing):
      * every available mode -> `geometry` (ORS polyline for the map overlay)
      * walking/cycling -> `air_quality` and `weather` at the route midpoint
      * driving -> `traffic` (live segments near the corridor) and
        `destination_parking`
    """
    res = _imiq.ranked_routes(start, end)
    if not res.get("success"):
        err = res.get("error", "routing failed")
        return {m: {"available": False, "error": err} for m in _ALL_MODES}

    routes: dict = {}
    for mode in _ALL_MODES:
        r = (res.get("routes") or {}).get(mode) or {}
        if r.get("success"):
            routes[mode] = {
                "available": True,
                "source": r.get("source", "imiq"),
                "distance": r.get("distance"),
                "duration": r.get("duration"),
                "distance_meters": r.get("distance_meters", 0),
                "duration_seconds": r.get("duration_seconds", 0),
            }
            if r.get("geometry") is not None:  # present once IMIQ exposes it
                routes[mode]["geometry"] = r["geometry"]
        else:
            routes[mode] = {"available": False,
                            "error": r.get("error", "route calculation failed")}

    # Modes that get an ORS polyline overlay: available + requested + not
    # already carrying IMIQ geometry (which would take precedence). Skipped
    # entirely when ORS isn't configured.
    geom_modes = ([m for m in _ALL_MODES
                   if routes[m]["available"] and m in want and not routes[m].get("geometry")]
                  if _GEOMETRY_ENABLED else [])

    want_air = any(routes[m]["available"] and m in want for m in ("walking", "cycling"))
    want_drive = routes["driving"]["available"] and "driving" in want
    if not (geom_modes or want_air or want_drive):
        return routes

    mid_lat = (start.lat + end.lat) / 2.0
    mid_lon = (start.lon + end.lon) / 2.0
    with ThreadPoolExecutor(max_workers=6) as pool:
        geom_futures = {m: pool.submit(_ors_geometry, m, start, end) for m in geom_modes}
        f_air = pool.submit(_nearest_air_quality, mid_lat, mid_lon) if want_air else None
        f_weather = pool.submit(_nearest_weather, mid_lat, mid_lon) if want_air else None
        f_traffic = (pool.submit(_traffic_near_route, start.lat, start.lon, end.lat, end.lon)
                     if want_drive else None)
        f_parking = pool.submit(_nearest_online_parking, end.lat, end.lon) if want_drive else None
        for m, fut in geom_futures.items():
            geom = _future_result(fut)
            if geom:
                routes[m]["geometry"] = geom
        air = _future_result(f_air) if f_air else None
        weather = _future_result(f_weather) if f_weather else None
        traffic = _future_result(f_traffic) if f_traffic else None
        parking = _future_result(f_parking) if f_parking else None

    for mode in ("walking", "cycling"):
        if not routes[mode]["available"]:
            continue
        if air is not None:
            routes[mode]["air_quality"] = air
        if weather is not None:
            routes[mode]["weather"] = weather
    if want_drive:
        if traffic is not None:
            routes["driving"]["traffic"] = traffic
        routes["driving"]["destination_parking"] = parking if parking is not None else {"found": False}
    return routes


@mcp.tool()
def get_all_routes(
    start_lat: float, start_lon: float,
    end_lat: float, end_lon: float
) -> str:
    """Calculate walking, cycling, and driving routes between two points.

    Both endpoints must lie within the Magdeburg bounding box
    (lat 52.05-52.20, lon 11.55-11.75).

    All three modes come from ONE routing call. A mode the router cannot
    serve (e.g. walking beyond ~3 km) is reported as
    `{"available": false, "error": ...}` and the others still return.

    Args:
        start_lat: Origin latitude
        start_lon: Origin longitude
        end_lat: Destination latitude
        end_lon: Destination longitude

    Returns:
        JSON with routes for each transport mode.
    """
    err = _validate_route_endpoints(start_lat, start_lon, end_lat, end_lon)
    if err:
        return json.dumps(err)

    start = Coordinates(lat=start_lat, lon=start_lon)
    end = Coordinates(lat=end_lat, lon=end_lon)
    routes = _compute_routes(start, end)
    return json.dumps({
        "success": True,
        "start": {"lat": start_lat, "lon": start_lon},
        "end": {"lat": end_lat, "lon": end_lon},
        "routes": routes,
    }, indent=2, default=str)


# ---------------------------------------------------------------------------
# Compound tool: `get_routes_for_places` collapses resolve-origin +
# resolve-destination + route into one MCP call. Each side resolves via the
# canonical candidate set (disambiguating chains), then ONE IMIQ call returns
# every road mode. The agent's prompt prefers this for "how do I get X → Y".
# ---------------------------------------------------------------------------

@mcp.tool()
def get_routes_for_places(origin_name: str, destination_name: str,
                          near_lat: float | None = None,
                          near_lon: float | None = None) -> str:
    """Single-call route planner: resolves both place names AND computes
    walking, cycling, and driving. Use this for any 'how do I get from X
    to Y' query — it replaces the resolve-resolve-route sequence with one
    round-trip.

    Disambiguation: if a name matches several distinct places (a chain like
    "Lidl" / "World of Pizza"), the tool AUTO-PICKS — the origin as the branch
    nearest the user (pass their location as `near_lat`/`near_lon`), the
    destination as the branch nearest the user, else nearest the resolved
    origin. Runners-up ride along in the destination's `alternatives`; answer
    with the picked route immediately and at most mention an alternative in
    one closing clause. Never ask "which one?" first.

    Args:
        origin_name: Origin place name (e.g. 'Hauptbahnhof', 'Building 03').
            MUST be a place the user actually stated, discussed, or their
            shared-location hint — NEVER a guessed or assumed starting point.
            No known origin → ask the user first instead of calling this.
        destination_name: Destination place name.
        near_lat, near_lon: optional user location, to pick the nearest branch.

    Returns:
        JSON with origin/destination resolved info plus all three modes.
    """
    user_anchor = _anchor(near_lat, near_lon)

    # 1. Origin — anchored ONLY on the user (don't guess which branch they START
    #    from off the destination).
    o_status, o_data = _resolve_endpoint_decision(origin_name, user_anchor)
    if o_status == "none":
        return json.dumps({"success": False, "error": "place_not_found",
                           "which": "origin", "place": origin_name})

    # 2. Destination — anchored on the user, else on the resolved origin (the
    #    branch nearest the start).
    dest_anchor = user_anchor or (o_data["lat"], o_data["lon"])
    d_status, d_data = _resolve_endpoint_decision(destination_name, dest_anchor)
    if d_status == "none":
        return json.dumps({"success": False, "error": "place_not_found",
                           "which": "destination", "place": destination_name})

    # 3. Validate both endpoints lie inside Magdeburg.
    for which, resolved in (("origin", o_data), ("destination", d_data)):
        if _validate_magdeburg_coords(resolved["lat"], resolved["lon"]) is not None:
            return json.dumps({"success": False, "error": "coordinates_outside_magdeburg",
                               "which": which})

    start = Coordinates(lat=o_data["lat"], lon=o_data["lon"])
    end = Coordinates(lat=d_data["lat"], lon=d_data["lon"])

    # 4. ONE IMIQ call returns every road mode (plus enrichments).
    routes = _compute_routes(start, end)

    def _endpoint_payload(input_str: str, resolved: dict) -> dict:
        return {
            "input": input_str,
            "name": resolved.get("name"),
            "lat": resolved.get("lat"),
            "lon": resolved.get("lon"),
            "type": resolved.get("type"),
            "matched": resolved.get("matched"),
        }

    result = {
        "success": True,
        "origin": _endpoint_payload(origin_name, o_data),
        "destination": _endpoint_payload(destination_name, d_data),
        "routes": routes,
    }
    # Destination runners-up only (an origin is never questioned — it's where
    # the user IS): lets the agent close with "there's also one in <district>".
    if d_data.get("alternatives"):
        result["destination"]["alternatives"] = [
            _candidate_payload(c) for c in d_data["alternatives"]]
    return json.dumps(result, indent=2, default=str)


@mcp.tool()
def find_nearest(query: str, near_lat: float, near_lon: float) -> str:
    """Find the genuinely NEAREST <query> to the user by REAL travel distance
    (not straight-line) and return the route to it.

    Use for "which X is closest/nearest to me?" when you know the user's location
    (their shared GPS, or a place they named that you resolved to coordinates).
    It pulls EVERY branch of <query> (chains via the brand grouping), shortlists
    the closest by straight line, ROUTES from the user to each of those, and
    picks the one with the shortest real travel — Magdeburg is split by the Elbe,
    so the straight-line nearest isn't always the road nearest. Returns full
    walking/cycling/driving routes (with map geometry) to the winner plus the
    alternatives it compared.

    Args:
        query: place / chain / type, e.g. 'edeka', 'Lidl', 'pharmacy'
        near_lat, near_lon: the user's location in Magdeburg.

    Returns:
        JSON {success, query, nearest_by, branches_found, origin,
        destination (winner + district), routes:{walking,cycling,driving},
        alternatives:[...]}, or an error (need_user_location / not_found).
    """
    anchor = _anchor(near_lat, near_lon)
    if anchor is None:
        return json.dumps({"success": False, "error": "need_user_location",
                           "message": "Need the user's Magdeburg location (near_lat/near_lon) to find the nearest."})

    candidates = _all_branches(query)
    if not candidates:
        candidates = [{"name": c.get("name"), "type": c.get("type"), "lat": c["lat"],
                       "lon": c["lon"], "district": c.get("district")}
                      for c in resolve_place_candidates(_run_read, query)
                      if c.get("lat") is not None and c.get("lon") is not None]
    if not candidates:
        geo = geocode_fallback(query)
        if not geo:
            return json.dumps({"success": False, "error": "not_found", "place": query})
        candidates = [{"name": short_label(geo.get("label", "")) or query, "type": "geocoded",
                       "lat": geo["lat"], "lon": geo["lon"], "district": None}]

    # Shortlist by straight line, then route to each of the closest few.
    candidates.sort(key=lambda c: haversine_m(anchor[0], anchor[1], c["lat"], c["lon"]))
    short = candidates[:_NEAREST_REFINE]
    start = Coordinates(lat=anchor[0], lon=anchor[1])

    def _road(c):
        try:
            res = _imiq.ranked_routes(start, Coordinates(lat=c["lat"], lon=c["lon"]))
        except Exception:
            return None
        if not res.get("success"):
            return None
        modes = res.get("routes") or {}
        for m in ("walking", "driving", "cycling"):
            r = modes.get(m) or {}
            if r.get("success") and isinstance(r.get("distance_meters"), (int, float)):
                return {"distance_m": round(r["distance_meters"]),
                        "duration_s": r.get("duration_seconds"), "mode": m}
        return None

    with ThreadPoolExecutor(max_workers=max(1, min(_NEAREST_REFINE, len(short)))) as pool:
        roads = list(pool.map(_road, short))

    scored = [(c, d) for c, d in zip(short, roads) if d]
    if scored:
        scored.sort(key=lambda cd: cd[1]["distance_m"])
        winner = scored[0][0]
        nearest_by = "road_distance"
    else:
        winner = short[0]          # routing unavailable → straight-line nearest
        nearest_by = "straight_line"

    routes = _compute_routes(start, Coordinates(lat=winner["lat"], lon=winner["lon"]))

    alternatives = [
        {"name": c.get("name"), "district": c.get("district"),
         "distance_m": d["distance_m"] if d else None,
         "duration_s": d.get("duration_s") if d else None}
        for c, d in zip(short, roads)
    ]

    return json.dumps({
        "success": True,
        "query": query,
        "nearest_by": nearest_by,
        "branches_found": len(candidates),
        "origin": {"input": "your location", "name": "your location",
                   "lat": anchor[0], "lon": anchor[1]},
        "destination": {"input": query, "name": winner.get("name"),
                        "lat": winner["lat"], "lon": winner["lon"],
                        "type": winner.get("type"), "district": winner.get("district")},
        "routes": routes,
        "alternatives": alternatives,
    }, indent=2, default=str)


# Traffic tools were TomTom-only and have been removed. Real-time traffic now
# lives in the FIWARE sensor network — external MCP clients should query the
# `fiware` server's Traffic entities (avgSpeed / speedLimit) instead.


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="stdio")
