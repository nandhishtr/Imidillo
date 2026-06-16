"""
Shared geocoder fallback chain for off-graph place names — used by every tool
whose place didn't resolve in the Neo4j graph (routing_server tools and
find_transit_route's nearest-stop fallback).

Order and reasoning:
  1. **Nominatim (OSM)** — bounded to the Magdeburg viewbox. Resolves German
     civic/POI names correctly ("Ausländerbehörde" → Lübecker Straße, where
     Pelias confidently returned a wrong downtown point) and returns NOTHING
     rather than junk for names it doesn't know.
  2. **ORS / Pelias** — backup, but only trusted when the label it matched
     actually resembles the query (name-similarity gate). An honest
     "not found" beats a confident wrong pin: the agent can then retry with
     the German name or ask the user.

Every hit is validated against the Magdeburg bounding box and returns
``{lat, lon, label, source}`` so tool outputs can SAY what was matched.
"""

from __future__ import annotations

from typing import Optional

from clients.nominatim_client import NominatimClient
from clients.ors_client import ORSClient
from config import ORS_API_KEY, ORS_BASE_URL, HTTP_TIMEOUT, MAGDEBURG_LAT, MAGDEBURG_LON
from mcp_servers._place_resolver import name_similarity

try:
    from services.thresholds import GEOCODER_MIN_SIMILARITY as _MIN_LABEL_SIM
except Exception:  # pragma: no cover
    _MIN_LABEL_SIM = 0.45

_MGB_LAT_MIN, _MGB_LAT_MAX = 52.05, 52.20
_MGB_LON_MIN, _MGB_LON_MAX = 11.55, 11.75

_nominatim = NominatimClient()
_ors = ORSClient(ORS_API_KEY, ORS_BASE_URL, HTTP_TIMEOUT)


def short_label(label: str, max_parts: int = 3) -> str:
    """Trim a geocoder display label ('Ausländerbehörde Magdeburg, 53-63,
    Lübecker Straße, Neue Neustadt, Magdeburg, Sachsen-Anhalt, 39124,
    Deutschland') to its informative head for cards and agent narration."""
    parts = [p.strip() for p in (label or "").split(",") if p.strip()]
    return ", ".join(parts[:max_parts])


def _in_magdeburg(lat, lon) -> bool:
    try:
        return (_MGB_LAT_MIN <= float(lat) <= _MGB_LAT_MAX
                and _MGB_LON_MIN <= float(lon) <= _MGB_LON_MAX)
    except (TypeError, ValueError):
        return False


def geocode_fallback(place_name: str) -> Optional[dict]:
    """Geocode an off-graph Magdeburg place name.

    Returns ``{lat, lon, label, source: "nominatim" | "ors"}`` or ``None``
    when neither geocoder produces a trustworthy in-bounds match. Never
    raises.
    """
    name = (place_name or "").strip()
    if not name:
        return None

    # 1. Nominatim — trusted as-is (bounded search, no-junk behaviour).
    try:
        hit = _nominatim.geocode(name)
    except Exception:
        hit = None
    if hit and _in_magdeburg(hit.get("lat"), hit.get("lon")):
        return {"lat": hit["lat"], "lon": hit["lon"],
                "label": hit.get("label", ""), "source": "nominatim"}

    # 2. ORS/Pelias — trusted ONLY when its matched label resembles the query.
    try:
        detail = _ors.geocode_detailed(name, focus_lat=MAGDEBURG_LAT,
                                       focus_lon=MAGDEBURG_LON)
    except Exception:
        detail = None
    if not detail or not _in_magdeburg(detail.get("lat"), detail.get("lon")):
        return None
    label = detail.get("label", "") or ""
    # Bbox corners overlap neighboring municipalities (Biederitz, Barleben) —
    # a Pelias label that doesn't name Magdeburg is not a Magdeburg place.
    if "magdeburg" not in label.lower():
        return None
    if name_similarity(name, label) < _MIN_LABEL_SIM:
        return None  # Pelias matched something that isn't what was asked for
    return {"lat": detail["lat"], "lon": detail["lon"],
            "label": label, "source": "ors"}
