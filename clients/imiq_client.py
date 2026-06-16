"""
IMIQ ranked-routes client — the routing engine for walking / cycling / driving.

Wraps the university's personalised multimodal routing API
(POST {base}/ranked-routes, GraphHopper-backed, whole-Magdeburg coverage).
One call returns ALL road modes at once, replacing the per-mode ORS fan-out.

The API is personalised via a "cognitive passport"; Dashbot doesn't want
personalisation, so every request sends a fixed NEUTRAL passport whose beliefs
unlock all modes, and the per-agent ranking fields (rank / utility /
dimension_scores) are ignored — only each mode's summary is read.

Known API limits (probed 2026-06-11):
  * Modes returned: car, bike, foot. `foot` is omitted by the router beyond
    roughly ~3 km — reported here as available=False with a reason.
  * Public transit is advertised in `available_modes` but never produces a
    route (no GTFS behind it) — transit stays on the Neo4j NEXT_STOP graph.
  * NO route geometry, street names, or turn-by-turn — summaries only.
  * NO geocoding: callers resolve names to coordinates first.

COORDINATE ORDER: lat, lon (the IMIQ API uses named lat/lon fields, no
GeoJSON ordering pitfalls).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, Optional

import httpx

from models import Coordinates

try:  # shared TTL, optional import pattern used across clients
    from services.thresholds import CACHE_TTL_SECONDS as _DEFAULT_CACHE_TTL
except Exception:  # pragma: no cover
    _DEFAULT_CACHE_TTL = 300

# Beliefs unlock every mode; empty values = no preference ranking we'd trust.
_NEUTRAL_PASSPORT: Dict[str, Any] = {
    "id": "dashbot",
    "values": {},
    "beliefs": {"owns_car": True, "owns_bike": True, "has_pt_access": True},
}

# IMIQ mode_key -> Dashbot mode name (the shape the tools/cards/prompt use).
_MODE_MAP = {"foot": "walking", "bike": "cycling", "car": "driving"}

_RETRY_ATTEMPTS = 3
_RETRY_WAITS = (0.5, 1.0)

_SHARED_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
_SHARED_LIMITS = httpx.Limits(max_keepalive_connections=10, max_connections=20)


# --- Magdeburg bounds (same box as routing_server / ors_client) -------------
_MAG_LAT_MIN, _MAG_LAT_MAX = 52.05, 52.20
_MAG_LON_MIN, _MAG_LON_MAX = 11.55, 11.75


def _assert_magdeburg_bounds(lat: float, lon: float) -> None:
    """Raise ValueError when (lat, lon) is outside Magdeburg (guards coord swaps).
    The IMIQ API itself accepts any coordinates on Earth, so this guard is ours."""
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


_cache = _TTLCache(_DEFAULT_CACHE_TTL or 300)


class IMIQRoutingClient:
    """Sync client for the IMIQ ranked-routes endpoint."""

    _shared_client: Optional[httpx.Client] = None
    _shared_lock = threading.Lock()

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    @classmethod
    def _client(cls) -> httpx.Client:
        with cls._shared_lock:
            if cls._shared_client is None or cls._shared_client.is_closed:
                cls._shared_client = httpx.Client(
                    timeout=_SHARED_TIMEOUT,
                    limits=_SHARED_LIMITS,
                    headers={"Accept-Encoding": "gzip"},
                )
            return cls._shared_client

    def _post_ranked_routes(self, payload: dict) -> Optional[dict]:
        url = f"{self.base_url}/ranked-routes"
        last_exc: Optional[BaseException] = None
        for i in range(_RETRY_ATTEMPTS):
            try:
                resp = self._client().post(url, json=payload)
                if 500 <= resp.status_code < 600 and i < _RETRY_ATTEMPTS - 1:
                    time.sleep(_RETRY_WAITS[i])
                    continue
                if resp.status_code != 200:
                    return {"_error": f"IMIQ API error: HTTP {resp.status_code}"}
                return resp.json()
            except httpx.TimeoutException:
                return {"_error": "IMIQ routing request timed out"}
            except httpx.TransportError as exc:
                last_exc = exc
                if i < _RETRY_ATTEMPTS - 1:
                    time.sleep(_RETRY_WAITS[i])
                    continue
            except Exception as exc:  # noqa: BLE001 — invalid JSON etc.
                return {"_error": f"IMIQ routing failed: {exc}"}
        return {"_error": f"IMIQ routing unreachable: {last_exc}"}

    def ranked_routes(self, start: Coordinates, end: Coordinates) -> Dict[str, Any]:
        """All road modes between two points in ONE call.

        Returns ``{"success": True, "routes": {walking, cycling, driving}}``
        where each mode is ``{"success": True, distance, distance_meters,
        duration, duration_seconds, source: "imiq"}`` or ``{"success": False,
        "error": ...}`` (e.g. walking beyond the router's distance cap).
        On a transport-level failure returns ``{"success": False, "error"}``.
        """
        try:
            _assert_magdeburg_bounds(start.lat, start.lon)
            _assert_magdeburg_bounds(end.lat, end.lon)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        key = (round(start.lat, 6), round(start.lon, 6),
               round(end.lat, 6), round(end.lon, 6))
        cached = _cache.get(key)
        if cached is not None:
            return cached

        data = self._post_ranked_routes({
            "start": {"lat": start.lat, "lon": start.lon},
            "stop": {"lat": end.lat, "lon": end.lon},
            "cognitive_passport": _NEUTRAL_PASSPORT,
            "include_unavailable": True,
        })
        if not isinstance(data, dict) or data.get("_error"):
            return {"success": False,
                    "error": (data or {}).get("_error", "IMIQ routing failed")}

        routes: Dict[str, Any] = {}
        for rt in data.get("routes", []):
            if not isinstance(rt, dict):
                continue
            mode = _MODE_MAP.get(rt.get("mode_key"))
            if mode is None or mode in routes:
                continue  # unknown or duplicate mode — first occurrence wins
            summary = rt.get("summary") or {}
            dist = summary.get("distance_meters")
            dur = summary.get("duration_seconds")
            if not (isinstance(dist, (int, float)) and dist > 0
                    and isinstance(dur, (int, float)) and dur > 0):
                routes[mode] = {"success": False, "error": "imiq_schema_mismatch"}
                continue
            routes[mode] = {
                "success": True,
                "profile": mode,
                "distance": _fmt_distance(dist),
                "distance_meters": dist,
                "duration": _fmt_duration(dur),
                "duration_seconds": dur,
                "source": "imiq",
            }
            # Geometry pass-through: the API doesn't return route geometry
            # today (2026-06), but GraphHopper computes it — if the IMIQ team
            # exposes it (encoded polyline or GeoJSON under `geometry` /
            # `points`, on the route or its single leg), forward it so the
            # map draws real paths instead of the dashed straight line.
            geom = rt.get("geometry") or rt.get("points")
            if geom is None:
                legs = rt.get("legs") or []
                if len(legs) == 1 and isinstance(legs[0], dict):
                    geom = legs[0].get("geometry") or legs[0].get("points")
            if geom is not None:
                routes[mode]["geometry"] = geom

        # Modes the router didn't return at all (foot beyond its distance cap).
        for mode in _MODE_MAP.values():
            routes.setdefault(mode, {
                "success": False,
                "error": ("not offered by the router for this distance"
                          if mode == "walking" else "not returned by the router"),
            })

        result = {"success": True, "routes": routes,
                  "straight_line_distance_m": data.get("straight_line_distance_m")}
        if any(r.get("success") for r in routes.values()):
            _cache.put(key, result)
        return result

    def get_route(self, start: Coordinates, end: Coordinates,
                  profile: str = "walking") -> Optional[Dict]:
        """Single-mode lookup with the same return shape get_route had on the
        ORS client, so existing call sites swap over without reshaping."""
        res = self.ranked_routes(start, end)
        if not res.get("success"):
            return {"success": False, "error": res.get("error", "routing failed")}
        mode = res["routes"].get(profile)
        if mode is None:
            return {"success": False, "error": f"unknown profile {profile!r}"}
        return mode

    def close(self):
        pass  # shared client lives for the process lifetime


if __name__ == "__main__":
    from config import IMIQ_ROUTING_URL

    client = IMIQRoutingClient(IMIQ_ROUTING_URL)
    res = client.ranked_routes(
        Coordinates(lat=52.1305, lon=11.6265),   # Hauptbahnhof
        Coordinates(lat=52.1396, lon=11.6457),   # Universitätsplatz
    )
    import json as _json
    print(_json.dumps(res, indent=2))
