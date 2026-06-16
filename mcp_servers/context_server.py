"""
Context Bridge MCP Server for the Magdeburg Campus Mobility Assistant.
Bridges Neo4j (location resolution), FIWARE (real-time sensors), and the
IMIQ router (walking distance) to provide unified spatial context in one
tool call.
"""

import json
import sys
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastmcp import FastMCP
from neo4j import GraphDatabase, Query
from clients.fiware_client import FIWAREClient
from clients.imiq_client import IMIQRoutingClient, _fmt_distance, _fmt_duration
from models import Coordinates
from mcp_servers._traffic_helpers import haversine_m
from config import (
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE,
    FIWARE_BASE_URL, FIWARE_API_KEY,
    IMIQ_ROUTING_URL,
)

# Optional import of shared thresholds (authored in parallel — tolerate absence).
try:
    from services.thresholds import PROACTIVE_STALENESS_SECONDS as _STALE_S  # type: ignore
except Exception:  # pragma: no cover
    _STALE_S = 600

mcp = FastMCP("context-bridge", instructions=(
    "Spatial context bridge for Magdeburg. Resolves a location name to coordinates "
    "via the Neo4j knowledge graph, then queries nearby real-time sensor data "
    "(parking, weather, air quality, traffic) from FIWARE and walking distances "
    "from the IMIQ router — all in one call."
))

# ---------------------------------------------------------------------------
# Clients (module-level singletons)
# ---------------------------------------------------------------------------
_neo4j_driver = GraphDatabase.driver(
    NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD),
    connection_acquisition_timeout=5.0,
    connection_timeout=3.0,
    max_connection_pool_size=50,
    # Aura/cloud load balancers silently drop idle TCP connections; without
    # these, the first query after an idle gap fails with "defunct connection"
    # and stalls the agent until its 90s timeout.
    max_connection_lifetime=300,
    keep_alive=True,
    liveness_check_timeout=60,
)
_fiware = FIWAREClient(FIWARE_BASE_URL, FIWARE_API_KEY)
_imiq = IMIQRoutingClient(IMIQ_ROUTING_URL)

_DEFAULT_QUERY_TIMEOUT = 8.0


def _q(cypher: str, timeout: float = _DEFAULT_QUERY_TIMEOUT) -> Query:
    """Wrap a Cypher string in a Query object with a per-query timeout."""
    return Query(cypher, timeout=timeout)

# Real-time entity types to query from FIWARE.
# Canonical set lives in mcp_servers/_sensor_types.py — but this server
# only queries a bridge-relevant subset (omits Room, Vehicle, DigitalTwin
# which have no useful "nearby" semantics for a location bridge).
from mcp_servers._sensor_types import REALTIME_TYPES as _ALL_REALTIME_TYPES
_SENSOR_TYPES = [t for t in ["Parking", "Weather", "AirQuality", "Traffic", "WaterLevel"]
                 if t in _ALL_REALTIME_TYPES]

# Per-sensor fetch timeout (seconds). Reduced from 10s so 5 sensors x timeout
# can't block a user request for 50s. Each sensor is fetched in parallel; the
# overall wall-clock cap is enforced via as_completed() plus a total budget.
_SENSOR_TIMEOUT_S = 3
# Overall wall-clock budget for the full sensor fan-out (seconds). Prevents a
# straggler pool from serialising behind worker exhaustion.
_SENSOR_OVERALL_BUDGET_S = _SENSOR_TIMEOUT_S + 1


def _neo4j_read(cypher: str, params: dict = None, timeout: float = _DEFAULT_QUERY_TIMEOUT) -> list[dict]:
    with _neo4j_driver.session(database=NEO4J_DATABASE) as session:
        result = session.run(_q(cypher, timeout=timeout), parameters=params or {})
        return [dict(record) for record in result]


def _resolve_location(name: str) -> dict | None:
    """Resolve a location name to coordinates via Neo4j (searches all node types)."""
    search = name.lower()
    rows = _neo4j_read("""
        MATCH (n)
        WHERE (toLower(n.name) CONTAINS $search
           OR ANY(a IN COALESCE(n.aliases, []) WHERE toLower(a) CONTAINS $search))
          AND n.latitude IS NOT NULL AND n.longitude IS NOT NULL
        RETURN labels(n)[0] AS type, n.name AS name, n.latitude AS lat, n.longitude AS lon
        LIMIT 1
    """, {"search": search})
    if rows:
        return rows[0]
    return None


def _parse_fiware_location(loc) -> tuple:
    """Extract (lat, lon) from various FIWARE location formats."""
    if isinstance(loc, dict):
        # GeoJSON: {"type": "Point", "coordinates": [lon, lat]}
        coords = loc.get("coordinates")
        if isinstance(coords, list) and len(coords) >= 2:
            return coords[1], coords[0]
        # Direct: {"latitude": ..., "longitude": ...}
        if "latitude" in loc:
            return loc["latitude"], loc["longitude"]
    if isinstance(loc, str) and "," in loc:
        # String: "52.137601, 11.637394"
        parts = loc.split(",")
        try:
            return float(parts[0].strip()), float(parts[1].strip())
        except ValueError:
            pass
    return None, None


def _query_fiware_sensor(lat: float, lon: float, sensor_type: str, radius: int) -> dict:
    """Query FIWARE for the nearest sensor of a given type."""
    result = _fiware.query_sensor_by_coordinates(
        latitude=lat, longitude=lon, sensor_type=sensor_type, radius=radius
    )
    if result.get("success"):
        entity = result["entity"]
        return {
            "found": True,
            "type": sensor_type,
            "id": entity.get("id", ""),
            "data": {k: v for k, v in entity.items() if k not in ("id", "type")},
        }
    return {"found": False, "type": sensor_type}


def _get_walking_distance(origin_lat: float, origin_lon: float,
                          dest_lat: float, dest_lon: float) -> dict | None:
    """Get walking distance and duration between two points via the IMIQ
    router. The router omits foot routes beyond roughly ~3 km — in that case
    (or any router failure) fall back to a straight-line estimate (x1.3
    street detour factor at 5 km/h), flagged as `estimated`."""
    route = _imiq.get_route(
        Coordinates(lat=origin_lat, lon=origin_lon),
        Coordinates(lat=dest_lat, lon=dest_lon),
        profile="walking",
    )
    if route and route.get("success"):
        return {
            "distance": route["distance"],
            "distance_meters": route["distance_meters"],
            "duration": route["duration"],
            "duration_seconds": route["duration_seconds"],
        }
    try:
        est_m = haversine_m(origin_lat, origin_lon, dest_lat, dest_lon) * 1.3
    except Exception:
        return None
    est_s = est_m / (5000.0 / 3600.0)  # 5 km/h walking pace
    return {
        "distance": _fmt_distance(est_m),
        "distance_meters": round(est_m),
        "duration": _fmt_duration(est_s),
        "duration_seconds": round(est_s),
        "estimated": True,
    }


@mcp.tool()
def get_nearby_context(location: str, radius: int = 1000) -> str:
    """Get real-time spatial context for a location: nearby parking, weather,
    air quality, traffic, and walking distances — all in one call.

    Resolves the location name to coordinates via the Neo4j knowledge graph,
    then queries FIWARE for nearby sensors and ORS for walking distances.
    Sensor fetches run in parallel with a 3-second per-sensor timeout so a
    single slow broker response cannot stall the whole call — any sensor
    that times out is reported as `{"found": false, "error": "timeout"}`
    and the remaining sensors still return (partial-results pattern).

    Fallback behaviour:
        If no parking sensor is found within `radius`, returns up to 3
        nearest parking lots by straight-line (haversine) distance. These
        fallback lots MAY EXCEED `radius` — the whole point of the fallback
        is to surface the closest lot even if it is further than the
        requested search area. Every lot is flagged with `within_radius`
        so the caller can distinguish in-radius hits from fallback hits.
        Walk-distance (ORS) is computed ONLY for the single in-radius
        sensor — not for fallback lots — to keep latency bounded.

    Args:
        location: Place name, e.g. 'Alter Markt', 'Building 3', 'Opernhaus', 'mensa'
        radius: Search radius in meters (default 1000)

    Returns:
        JSON with location coordinates and nearby real-time sensor data
        including walking distance to parking and a `parking_fallback`
        block with `within_radius: bool` per item when applicable.
    """
    # Step 1: Resolve location to coordinates via Neo4j
    resolved = _resolve_location(location)
    if not resolved:
        return json.dumps({
            "error": f"Could not find location '{location}' in the knowledge graph.",
        })

    lat, lon = resolved["lat"], resolved["lon"]

    # Step 2: Query all sensor types from FIWARE in parallel with a 3s per-sensor
    # timeout and an overall wall-clock budget. Missing / timed-out sensors are
    # returned as partial results so one slow broker doesn't block the whole call.
    sensor_results: dict = {}
    deadline = time.monotonic() + _SENSOR_OVERALL_BUDGET_S
    with ThreadPoolExecutor(max_workers=len(_SENSOR_TYPES)) as pool:
        futures = {
            pool.submit(_query_fiware_sensor, lat, lon, st, radius): st
            for st in _SENSOR_TYPES
        }
        try:
            for future in as_completed(futures, timeout=_SENSOR_OVERALL_BUDGET_S):
                sensor_type = futures[future]
                try:
                    sensor_results[sensor_type] = future.result(timeout=_SENSOR_TIMEOUT_S)
                except FuturesTimeoutError:
                    sensor_results[sensor_type] = {
                        "found": False, "type": sensor_type, "error": "timeout",
                    }
                except Exception as e:
                    sensor_results[sensor_type] = {
                        "found": False, "type": sensor_type, "error": str(e),
                    }
        except FuturesTimeoutError:
            # Overall budget exceeded — mark any still-pending sensors as timed out.
            pass
        # Anything not yet recorded is a straggler we won't wait on.
        for future, sensor_type in futures.items():
            if sensor_type in sensor_results:
                continue
            if future.done():
                try:
                    sensor_results[sensor_type] = future.result(timeout=0)
                    continue
                except Exception as e:
                    sensor_results[sensor_type] = {
                        "found": False, "type": sensor_type, "error": str(e),
                    }
                    continue
            # Cancel what we can; mark as partial.
            future.cancel()
            sensor_results[sensor_type] = {
                "found": False, "type": sensor_type, "error": "timeout",
            }

    # Degraded flag: any sensor did not return a definitive answer within budget.
    partial = any(r.get("error") == "timeout" for r in sensor_results.values())

    # Step 3: Parking fallback — if no parking within radius, query ALL parking
    # lots globally and return the closest one(s) with walking distance.
    parking = sensor_results.get("Parking", {})
    walking_to_parking = None
    parking_fallback = None

    if parking.get("found"):
        parking_data = parking.get("data", {})
        parking_loc = parking_data.get("location", {})
        p_lat, p_lon = _parse_fiware_location(parking_loc)
        if p_lat and p_lon:
            walking_to_parking = _get_walking_distance(lat, lon, p_lat, p_lon)
    else:
        # No parking in radius. Pull all Parking entities and compute distances.
        import math
        all_parking = _fiware.query_entities(entity_type="Parking", limit=50)
        if all_parking.get("success"):
            candidates = []
            for e in all_parking.get("entities", []):
                p_lat, p_lon = _parse_fiware_location(e.get("location"))
                if p_lat is None or p_lon is None:
                    continue
                # Approx distance (haversine-ish, fine for <10km)
                dlat = math.radians(p_lat - lat)
                dlon = math.radians(p_lon - lon)
                a = math.sin(dlat/2)**2 + math.cos(math.radians(lat)) * math.cos(math.radians(p_lat)) * math.sin(dlon/2)**2
                dist_m = 6371000 * 2 * math.asin(math.sqrt(a))
                candidates.append({
                    "id": e.get("id"),
                    "name": e.get("name"),
                    "freeSpots": e.get("freeSpots"),
                    "totalSpots": e.get("totalSpots"),
                    "status": e.get("status"),
                    "distance_meters_straight": round(dist_m),
                    "within_radius": dist_m <= radius,
                    "location": e.get("location"),
                })
            candidates.sort(key=lambda c: c["distance_meters_straight"])
            parking_fallback = candidates[:3]  # top 3 nearest (may exceed radius)

    # Build response
    context = {
        "location": {
            "name": resolved["name"],
            "type": resolved["type"],
            "latitude": lat,
            "longitude": lon,
        },
        "sensors": {},
    }

    for sensor_type, result in sensor_results.items():
        if result.get("found"):
            entry = {
                "id": result["id"],
                "data": result["data"],
                "within_radius": True,
            }
            if sensor_type == "Parking" and walking_to_parking:
                entry["walking_from_location"] = walking_to_parking
            context["sensors"][sensor_type] = entry

    # Attach parking fallback if no parking was in the radius
    if parking_fallback and "Parking" not in context["sensors"]:
        context["parking_fallback"] = {
            "note": (
                f"No parking lot is within {radius}m of {resolved['name']}. "
                f"Nearest parking lots by straight-line distance (may exceed radius; "
                f"walking distance NOT computed for fallback lots):"
            ),
            "lots": parking_fallback,
        }

    if not context["sensors"] and not parking_fallback:
        context["note"] = f"No real-time sensors found within {radius}m of {resolved['name']}."

    if partial:
        context["partial_results"] = True
        context["partial_note"] = (
            f"One or more sensor lookups exceeded {_SENSOR_TIMEOUT_S}s and were "
            "returned as timeouts. Results are partial — re-query if you need the missing sensors."
        )

    return json.dumps(context, indent=2, default=str)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="stdio")
