"""
Nominatim (OSM) geocoding client — the PRIMARY geocoder fallback for places
the Neo4j graph doesn't know.

Chosen over ORS/Pelias for the fallback tier because it resolves German
civic/POI names correctly ("Ausländerbehörde Magdeburg" → Lübecker Straße,
where Pelias confidently returned a wrong downtown point) and, just as
important, returns NOTHING rather than a junk match when it doesn't know a
name. Searches are bounded to the Magdeburg viewbox.

Usage policy compliance (https://operations.osmfoundation.org/policies/nominatim/):
a descriptive User-Agent and at most 1 request/second, enforced with a simple
throttle. Volume is naturally tiny — this runs only for off-graph places,
behind a TTL cache.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, Optional

import httpx

try:
    from services.thresholds import CACHE_TTL_SECONDS as _DEFAULT_CACHE_TTL
except Exception:  # pragma: no cover
    _DEFAULT_CACHE_TTL = 1800

_USER_AGENT = "Dashbot-Magdeburg-Assistant/1.0 (OVGU IMIQ project)"

# Magdeburg viewbox: lon_min, lat_max, lon_max, lat_min (Nominatim order).
_VIEWBOX = "11.55,52.20,11.75,52.05"

_MIN_REQUEST_INTERVAL_S = 1.0  # Nominatim usage policy: max 1 req/s


class _TTLCache:
    def __init__(self, ttl_seconds: float) -> None:
        self.ttl = ttl_seconds
        self._store: Dict[str, tuple] = {}
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


class NominatimClient:
    """Sync Nominatim search client, bounded to Magdeburg."""

    def __init__(self, base_url: str = "https://nominatim.openstreetmap.org",
                 timeout_s: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._cache = _TTLCache(_DEFAULT_CACHE_TTL or 1800)
        self._throttle_lock = threading.Lock()
        self._last_request = 0.0
        self._client = httpx.Client(
            headers={"User-Agent": _USER_AGENT},
            timeout=httpx.Timeout(connect=4.0, read=timeout_s, write=4.0, pool=4.0),
        )

    def _throttled_get(self, params: dict) -> httpx.Response:
        with self._throttle_lock:
            wait = _MIN_REQUEST_INTERVAL_S - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
        return self._client.get(f"{self.base_url}/search", params=params)

    def geocode(self, place_name: str) -> Optional[dict]:
        """Resolve a free-text place name within Magdeburg.

        Returns ``{lat, lon, label, osm_type}`` for the best bounded match, or
        ``None`` when Nominatim doesn't know the name (which it honestly
        reports instead of fuzzy-matching junk). Never raises.
        """
        name = (place_name or "").strip()
        if not name:
            return None
        # Append the city unless the query already names it — improves recall
        # for bare POI names while `bounded` keeps results inside the viewbox.
        query = name if "magdeburg" in name.lower() else f"{name} Magdeburg"

        cached = self._cache.get(query)
        if cached is not None:
            return cached or None  # cached miss is stored as False

        try:
            resp = self._throttled_get({
                "q": query,
                "format": "jsonv2",
                "limit": 1,
                "viewbox": _VIEWBOX,
                "bounded": 1,
                "countrycodes": "de",
                "accept-language": "de",
            })
            if resp.status_code != 200:
                return None
            hits = resp.json()
        except Exception:
            return None

        if not isinstance(hits, list) or not hits:
            self._cache.put(query, False)
            return None
        hit = hits[0]
        try:
            result = {
                "lat": float(hit["lat"]),
                "lon": float(hit["lon"]),
                "label": hit.get("display_name", ""),
                "osm_type": hit.get("type", ""),
            }
        except (KeyError, TypeError, ValueError):
            return None
        self._cache.put(query, result)
        return result
