"""
IMIQ geocoding client — the in-house geocoder for places the Neo4j graph
doesn't know. PRIMARY tier of the off-graph fallback chain (Nominatim is the
backup), replacing the public OSM Nominatim instance in that role.

Runs on the university's own IMIQ platform (same host family as the FIWARE
broker), so there is no usage-policy throttle and no external API key. It
shares Nominatim's crucial virtue: it returns NOTHING rather than a junk
match for names it doesn't know (verified: nonsense queries -> []), and it
resolves German civic/POI names correctly ("Ausländerbehörde Magdeburg" ->
Lübecker Straße).

Two things the service does NOT do, which this client compensates for:
  * It is NOT city-scoped — a bare chain name ("lidl") returns hits across
    Europe. The client appends "Magdeburg" to unscoped queries and drops
    hits outside the Magdeburg viewbox.
  * No typo tolerance ("hauptbanhof" -> []). Fine here: typos are handled
    upstream by the graph resolver's Lucene fuzzy match; the geocoder tier
    only sees names the graph doesn't know.

API: POST {base}/geocode with body {"address": str, "limit": int} ->
JSON list of {rank, confidence, lat, lon, display_name, name, street,
housenumber, postcode, city, state, country, country_code, osm_id,
osm_type, type, extent}. NOTE: `confidence` is rank-derived (1.0, 0.85,
0.7, ...), not a match-quality signal — never gate on it.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

import httpx

try:
    from services.thresholds import CACHE_TTL_SECONDS as _DEFAULT_CACHE_TTL
except Exception:  # pragma: no cover
    _DEFAULT_CACHE_TTL = 1800

_USER_AGENT = "Dashbot-Magdeburg-Assistant/1.0 (OVGU IMIQ project)"

# Magdeburg viewbox (H26 box, same as the route-endpoint validation): hits
# outside it are discarded — the service happily geocodes the whole planet.
_MGB_LAT_MIN, _MGB_LAT_MAX = 52.05, 52.20
_MGB_LON_MIN, _MGB_LON_MAX = 11.55, 11.75

# Ask for a few hits even when the caller wants one: if rank 1 lands outside
# the viewbox (out-of-town chain branch), a valid Magdeburg hit at rank 2-5
# still resolves the query instead of failing it.
_SEARCH_LIMIT = 5


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


def _extract_district(display_name: str, name: str) -> Optional[str]:
    """Best-effort Stadtteil from a display label like 'Lidl, 49, Alt Salbke,
    Salbke, Magdeburg, Sachsen-Anhalt, 39122, Deutschland' -> 'Salbke'.

    The part immediately before 'Magdeburg' is the district (or quarter);
    skipped when it is just the name/housenumber again. Used for the
    "which one — the one in Stadtfeld or Sudenburg?" disambiguation wording.
    """
    parts = [p.strip() for p in (display_name or "").split(",") if p.strip()]
    try:
        idx = parts.index("Magdeburg")
    except ValueError:
        return None
    if idx < 1:
        return None
    candidate = parts[idx - 1]
    if candidate.lower() == (name or "").lower() or candidate.isdigit():
        return None
    return candidate


class IMIQGeocodeClient:
    """Sync client for the IMIQ geocoding service, bounded to Magdeburg."""

    def __init__(self, base_url: str, timeout_s: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._cache = _TTLCache(_DEFAULT_CACHE_TTL or 1800)
        self._client = httpx.Client(
            headers={"User-Agent": _USER_AGENT},
            timeout=httpx.Timeout(connect=4.0, read=timeout_s, write=4.0, pool=4.0),
        )

    def _search(self, query: str) -> Optional[List[dict]]:
        """Raw scoped search: in-viewbox hits in rank order, [] for an honest
        miss, None on transport/shape errors (so the caller can distinguish
        "service says no" from "service unreachable"). Never raises."""
        cached = self._cache.get(query)
        if cached is not None:
            return cached or []  # cached miss is stored as False

        try:
            resp = self._client.post(
                f"{self.base_url}/geocode",
                json={"address": query, "limit": _SEARCH_LIMIT},
            )
            if resp.status_code != 200:
                return None
            raw = resp.json()
        except Exception:
            return None
        if not isinstance(raw, list):
            return None  # error payloads come back as a dict — treat as outage

        hits: List[dict] = []
        for h in raw:
            if not isinstance(h, dict):
                continue
            try:
                lat, lon = float(h["lat"]), float(h["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            if not (_MGB_LAT_MIN <= lat <= _MGB_LAT_MAX
                    and _MGB_LON_MIN <= lon <= _MGB_LON_MAX):
                continue
            label = h.get("display_name") or ""
            name = h.get("name") or ""
            # A query that matches nothing but the appended city token can
            # return the Magdeburg admin relation itself ("building 27
            # magdeburg" -> the city centroid). That's a junk pin, not a
            # resolution — drop it unless the user literally asked for
            # Magdeburg. District relations (Stadtfeld Ost, ...) stay.
            if (h.get("type") == "administrative"
                    and name.strip().lower() == "magdeburg"
                    and query.strip().lower() != "magdeburg"):
                continue
            hits.append({
                "lat": lat,
                "lon": lon,
                "label": label,
                "name": name,
                "osm_type": h.get("type", ""),
                "district": _extract_district(label, name),
            })

        # Only a well-formed empty answer is a cacheable negative; transport
        # errors above returned early and are retried on the next call.
        self._cache.put(query, hits if hits else False)
        return hits

    def _scoped(self, place_name: str) -> Optional[str]:
        name = (place_name or "").strip()
        if not name:
            return None
        # Append the city unless the query already names it — the service is
        # world-wide, and the viewbox filter alone would turn every unscoped
        # chain-name query into a miss instead of a Magdeburg hit.
        return name if "magdeburg" in name.lower() else f"{name} Magdeburg"

    def geocode(self, place_name: str) -> Optional[dict]:
        """Resolve a free-text place name within Magdeburg.

        Returns ``{lat, lon, label, name, osm_type, district}`` for the best
        in-viewbox match, or ``None`` when the service doesn't know the name
        (honest miss) or is unreachable. Never raises.
        """
        query = self._scoped(place_name)
        if not query:
            return None
        hits = self._search(query)
        return hits[0] if hits else None

    def geocode_candidates(self, place_name: str, limit: int = 5) -> List[dict]:
        """All in-viewbox matches in rank order (same shape as `geocode`),
        capped to `limit`. Empty list on miss or outage. Never raises."""
        query = self._scoped(place_name)
        if not query:
            return []
        hits = self._search(query)
        return (hits or [])[:max(1, limit)]

    def reverse(self, lat: float, lon: float) -> Optional[dict]:
        """Reverse-geocode coordinates to the nearest address.

        POST {base}/reverse with {"lat", "lon"} -> a single hit with street,
        housenumber, postcode, city, display_name, type. Returns
        ``{address, street, housenumber, district, label}`` where `address` is
        the short human form ("Erzbergerstraße 13, Altstadt"), or None on a
        miss/outage. Cached by ~11 m coordinate bucket (5 decimals): a
        stationary user costs one upstream call per TTL. Never raises.
        """
        key = f"rev:{round(lat, 5)},{round(lon, 5)}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached or None  # cached miss is stored as False

        try:
            resp = self._client.post(
                f"{self.base_url}/reverse", json={"lat": lat, "lon": lon}
            )
            if resp.status_code != 200:
                return None
            hit = resp.json()
        except Exception:
            return None
        if not isinstance(hit, dict) or not (hit.get("street") or hit.get("display_name")):
            self._cache.put(key, False)
            return None

        street = (hit.get("street") or "").strip()
        housenumber = (str(hit.get("housenumber") or "")).strip()
        label = hit.get("display_name") or ""
        district = _extract_district(label, hit.get("name") or street)
        street_part = f"{street} {housenumber}".strip() or (hit.get("name") or "").strip()
        if not street_part:
            self._cache.put(key, False)
            return None
        address = f"{street_part}, {district}" if district else street_part

        result = {
            "address": address,
            "street": street,
            "housenumber": housenumber,
            "district": district,
            "label": label,
        }
        self._cache.put(key, result)
        return result

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
