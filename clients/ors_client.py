"""
OpenRouteService client — route polyline geometry for the map overlay.

Routing distances/durations come from the IMIQ router and geocoding from the
IMIQ/Nominatim chain; ORS is called only for the SHAPE of a route path
(`get_route`), plus the module-level `decode_geometry` helper api.py uses to
decode encoded polylines at card-build time.

COORDINATE ORDER: All internal APIs accept `lat, lon` (decimal degrees, WGS84).
ORS API (GeoJSON) uses `[lon, lat]` arrays — conversion happens inside this
client only. Callers ALWAYS pass `lat, lon`.
"""

import asyncio
import threading
import time
from typing import Any, Callable, Dict, Optional

import httpx

from models import Coordinates

try:
    from services.thresholds import CACHE_TTL_SECONDS as _DEFAULT_CACHE_TTL
except Exception:  # pragma: no cover
    _DEFAULT_CACHE_TTL = 300

try:
    from tenacity import (
        AsyncRetrying,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential_jitter,
    )
    _HAS_TENACITY = True
except Exception:  # pragma: no cover
    _HAS_TENACITY = False


_SHARED_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
_SHARED_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=50)
_DEFAULT_HEADERS = {"Accept-Encoding": "gzip"}

_RESPONSE_TTL_SECONDS = 300


# --- Bounds check ----------------------------------------------------------
_MAG_LAT_MIN, _MAG_LAT_MAX = 52.05, 52.20
_MAG_LON_MIN, _MAG_LON_MAX = 11.55, 11.75


def _assert_magdeburg_bounds(lat: float, lon: float) -> None:
    """Raise ValueError when (lat, lon) is outside Magdeburg (guards coord swaps)."""
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"coordinates must be numeric, got lat={lat!r}, lon={lon!r}") from exc
    if not (_MAG_LAT_MIN <= lat_f <= _MAG_LAT_MAX and _MAG_LON_MIN <= lon_f <= _MAG_LON_MAX):
        raise ValueError(
            f"coordinates ({lat_f}, {lon_f}) outside Magdeburg bounds "
            f"lat[{_MAG_LAT_MIN}-{_MAG_LAT_MAX}], lon[{_MAG_LON_MIN}-{_MAG_LON_MAX}]"
        )


# --- TTL response cache ----------------------------------------------------
class _TTLCache:
    def __init__(self, ttl_seconds: float) -> None:
        self.ttl = ttl_seconds
        self._store: Dict[tuple, tuple] = {}
        self._lock = threading.Lock()

    def get(self, key):
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expiry, value = entry
            if expiry < now:
                self._store.pop(key, None)
                return None
            return value

    def put(self, key, value):
        with self._lock:
            self._store[key] = (time.monotonic() + self.ttl, value)


_ors_cache = _TTLCache(_DEFAULT_CACHE_TTL if _DEFAULT_CACHE_TTL else _RESPONSE_TTL_SECONDS)


def _cache_key(endpoint: str, payload: Dict[str, Any]) -> tuple:
    return (endpoint, tuple(sorted((k, str(v)) for k, v in payload.items())))


# --- Retry helpers --------------------------------------------------------
_RETRY_ATTEMPTS = 3
_RETRY_WAITS = (0.5, 1.0, 2.0)


def _is_retriable_status(status: int) -> bool:
    return 500 <= status < 600


async def _aretry(coro_factory: Callable[[], Any]):
    if _HAS_TENACITY:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(_RETRY_ATTEMPTS),
            wait=wait_exponential_jitter(initial=0.5, max=2.0),
            retry=retry_if_exception_type(httpx.TransportError),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                response = await coro_factory()
                if _is_retriable_status(response.status_code):
                    raise httpx.TransportError(f"server error {response.status_code}")
                return response
        return None  # pragma: no cover
    last_exc: Optional[BaseException] = None
    for i in range(_RETRY_ATTEMPTS):
        try:
            response = await coro_factory()
            if _is_retriable_status(response.status_code) and i < _RETRY_ATTEMPTS - 1:
                await asyncio.sleep(_RETRY_WAITS[i])
                continue
            return response
        except httpx.TransportError as exc:
            last_exc = exc
            if i < _RETRY_ATTEMPTS - 1:
                await asyncio.sleep(_RETRY_WAITS[i])
                continue
            raise
    if last_exc:
        raise last_exc
    return None  # pragma: no cover


# --- sync-from-async bridge -----------------------------------------------
def _run_sync(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        result: Dict[str, Any] = {}

        def runner():
            result["value"] = asyncio.run(coro)

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join()
        return result.get("value")
    return asyncio.run(coro)


# --- formatting helpers ----------------------------------------------------
def _fmt_distance(distance_m: float) -> str:
    if distance_m >= 1000:
        return f"{distance_m/1000:.1f} km"
    return f"{int(distance_m)} m"


def _fmt_duration(duration_s: float) -> str:
    if duration_s >= 3600:
        hours = int(duration_s // 3600)
        mins = int((duration_s % 3600) // 60)
        return f"{hours}h {mins}min"
    return f"{int(duration_s // 60)} min"


# --- geometry decoding ------------------------------------------------------
# ORS returns route geometry as an encoded polyline string (Google algorithm,
# precision 5 for 2D). Decode it to [[lat, lon], ...] so map clients (the
# dashboard's Leaflet) can draw it directly without a client-side decoder.
def _decode_polyline(encoded: str, precision: int = 5) -> list:
    """Decode a Google/ORS encoded polyline into [[lat, lon], ...]."""
    coords, index, lat, lng = [], 0, 0, 0
    factor = float(10 ** precision)
    length = len(encoded)
    while index < length:
        for _is_lng in (False, True):
            shift, result = 0, 0
            while True:
                if index >= length:
                    return coords
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if _is_lng:
                lng += delta
            else:
                lat += delta
        coords.append([lat / factor, lng / factor])
    return coords


def decode_geometry(geometry):
    """Normalize an ORS geometry (encoded polyline str OR GeoJSON dict) to
    [[lat, lon], ...]. Returns None when missing/unparseable.

    Public so api.py can decode the compact encoded polyline at card-build
    time — that way the heavy coordinate array is sent to the map widget but
    NEVER placed in the agent's tool-result context."""
    try:
        if isinstance(geometry, str) and geometry:
            pts = _decode_polyline(geometry, 5)
            # 2D ORS geometry is precision 5; if the first point lands far
            # outside the Magdeburg region, retry at precision 6 before giving up.
            if pts and not (51.8 <= pts[0][0] <= 52.4 and 11.3 <= pts[0][1] <= 12.0):
                alt = _decode_polyline(geometry, 6)
                if alt and (51.8 <= alt[0][0] <= 52.4 and 11.3 <= alt[0][1] <= 12.0):
                    return alt
            return pts or None
        if isinstance(geometry, dict):
            coords = geometry.get("coordinates")
            if isinstance(coords, list) and coords:
                # GeoJSON is [lon, lat] -> [lat, lon].
                return [[c[1], c[0]] for c in coords if isinstance(c, list) and len(c) >= 2]
    except Exception:
        return None
    return None


class ORSClient:

    _shared_async_client: Optional[httpx.AsyncClient] = None
    _shared_lock = threading.Lock()

    def __init__(self, api_key: str, base_url: str = "https://api.openrouteservice.org",
                 http_timeout: int = 10):
        self.api_key = api_key
        self.base_url = base_url
        self.http_timeout = http_timeout
        self._headers = {
            **_DEFAULT_HEADERS,
            "Authorization": api_key,
            "Content-Type": "application/json",
        }
        self.profiles = {
            "walking": "foot-walking",
            "cycling": "cycling-regular",
            "driving": "driving-car",
            "wheelchair": "wheelchair",
        }

    @classmethod
    def _async_client(cls) -> httpx.AsyncClient:
        # Event-loop-aware cache: if the cached client was bound to a now-dead
        # loop (e.g. created in a warmup thread's asyncio.run()), recreate it
        # on the current loop. Without this the client fails with
        # "Event loop is closed" on every subsequent use.
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        with cls._shared_lock:
            bound_loop = (
                getattr(cls._shared_async_client, "_bound_loop", None)
                if cls._shared_async_client is not None
                else None
            )
            if (cls._shared_async_client is None
                    or cls._shared_async_client.is_closed
                    or (current_loop is not None and bound_loop is not current_loop)):
                cls._shared_async_client = httpx.AsyncClient(
                    timeout=_SHARED_TIMEOUT,
                    limits=_SHARED_LIMITS,
                    headers=_DEFAULT_HEADERS,
                )
                cls._shared_async_client._bound_loop = current_loop
            return cls._shared_async_client

    # ---- get_route -------------------------------------------------------
    def _build_route_payload(self, start_coords: Coordinates, end_coords: Coordinates,
                             instructions: bool = False) -> Dict[str, Any]:
        # Validate before constructing.
        _assert_magdeburg_bounds(start_coords.lat, start_coords.lon)
        _assert_magdeburg_bounds(end_coords.lat, end_coords.lon)
        return {
            "coordinates": [
                [start_coords.lon, start_coords.lat],   # GeoJSON: [lon, lat]
                [end_coords.lon, end_coords.lat],
            ],
            "instructions": instructions,
            "geometry": True,
        }

    @staticmethod
    def _parse_route_response(data: Dict[str, Any], profile: str) -> Dict[str, Any]:
        if not data.get("routes"):
            return {"success": False, "error": "No routes found"}
        route = data["routes"][0]
        summary = route.get("summary", {})
        if not summary and "properties" in route:
            summary = route["properties"].get("summary", {})
        # H23 — validate summary has positive distance + duration.
        distance_m = summary.get("distance")
        duration_s = summary.get("duration")
        if not (isinstance(distance_m, (int, float)) and distance_m > 0
                and isinstance(duration_s, (int, float)) and duration_s > 0):
            return {"found": False, "error": "ors_schema_mismatch"}
        geometry = route.get("geometry", {})
        return {
            "success": True,
            "profile": profile,
            "distance": _fmt_distance(distance_m),
            "distance_meters": distance_m,
            "duration": _fmt_duration(duration_s),
            "duration_seconds": duration_s,
            "geometry": geometry,
            "summary": summary,
        }

    async def aget_route(self, start_coords: Coordinates, end_coords: Coordinates,
                         profile: str = "walking") -> Optional[Dict]:
        ors_profile = self.profiles.get(profile, "foot-walking")
        url = f"{self.base_url}/v2/directions/{ors_profile}"
        try:
            payload = self._build_route_payload(start_coords, end_coords, instructions=False)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        cache_payload = {
            "profile": ors_profile,
            "start_lat": start_coords.lat, "start_lon": start_coords.lon,
            "end_lat": end_coords.lat, "end_lon": end_coords.lon,
            "instructions": "0",
        }
        key = _cache_key(url, cache_payload)
        cached = _ors_cache.get(key)
        if cached is not None:
            return cached
        client = self._async_client()
        try:
            response = await _aretry(
                lambda: client.post(url, json=payload, headers=self._headers)
            )
        except httpx.TimeoutException:
            return {"success": False, "error": "ORS request timed out"}
        except Exception as exc:  # noqa: BLE001
            print(f"ORS route error: {exc}")
            return {"success": False, "error": str(exc)}
        if response.status_code != 200:
            return {"success": False, "error": f"ORS API error: {response.status_code}"}
        try:
            data = response.json()
        except Exception as exc:
            return {"success": False, "error": f"ORS returned invalid JSON: {exc}"}
        result = self._parse_route_response(data, profile)
        if result.get("success"):
            _ors_cache.put(key, result)
        return result

    def get_route(self, start_coords: Coordinates, end_coords: Coordinates,
                  profile: str = "walking") -> Optional[Dict]:
        return _run_sync(self.aget_route(start_coords, end_coords, profile))

    def close(self):
        # Pool lives for the process lifetime; nothing to free here.
        pass


if __name__ == "__main__":
    import os

    api_key = os.getenv("ORS_API_KEY", "")
    if not api_key:
        print("Set ORS_API_KEY environment variable")
        exit(1)

    client = ORSClient(api_key)
    hauptbahnhof = Coordinates(lat=52.1305, lon=11.6265)
    universitaetsplatz = Coordinates(lat=52.1396, lon=11.6457)
    route = client.get_route(hauptbahnhof, universitaetsplatz, "walking")
    print(f"Walking route: {route and route.get('success')}")
    if route and route.get("success"):
        print(f"  Distance: {route['distance']}")
        print(f"  Duration: {route['duration']}")
        decoded = decode_geometry(route.get("geometry"))
        print(f"  Geometry points: {len(decoded) if decoded else 0}")

    client.close()
