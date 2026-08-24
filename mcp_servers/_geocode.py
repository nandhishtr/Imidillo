"""
Shared geocoder fallback chain for off-graph place names — used by every tool
whose place didn't resolve in the Neo4j graph (routing_server tools and
find_transit_route's nearest-stop fallback).

Order and reasoning:
  1. **IMIQ geocoder** — the university's own geocoding service (same
     platform as the FIWARE broker): no rate-limit throttle, no API key,
     resolves German civic/POI names correctly, and returns NOTHING rather
     than junk for names it doesn't know. The client scopes queries to
     Magdeburg and drops out-of-viewbox hits (the service itself is
     world-wide).
  2. **Nominatim (OSM)** — public-instance backup for when the IMIQ service
     is down. Bounded to the Magdeburg viewbox, same no-junk behaviour,
     but throttled to 1 req/s by the OSM usage policy.

The former third tier — ORS/Pelias gated by a name-similarity check — was
dropped when the IMIQ geocoder landed: behind two honest geocoders, a
junk-prone third that needed its own trust gate added nothing. (ORS itself
is still used elsewhere, for route polyline geometry.)

Two entry points:
  * ``geocode_fallback`` — single best hit, ``{lat, lon, label, source}``.
  * ``geocode_fallback_candidates`` — ALL distinct in-bounds matches shaped
    like `_place_resolver` candidates, so off-graph places flow through the
    SAME ``decide_place`` policy as graph places (auto-pick near an anchor,
    else ask "which one?" with districts).

``looks_like_street_address`` lets resolvers send address-shaped queries
(street suffix + house number) straight here: house numbers are geocoder
territory — the graph knows streets, not addresses.
"""

from __future__ import annotations

import re
from typing import Optional

from clients.imiq_geocode_client import IMIQGeocodeClient
from clients.nominatim_client import NominatimClient
from config import IMIQ_GEOCODE_URL
from mcp_servers._place_resolver import _same_place

_MGB_LAT_MIN, _MGB_LAT_MAX = 52.05, 52.20
_MGB_LON_MIN, _MGB_LON_MAX = 11.55, 11.75

_imiq_geocoder = IMIQGeocodeClient(IMIQ_GEOCODE_URL)
_nominatim = NominatimClient()

# Street-address shape: a word carrying a German street suffix (fused
# "Erzbergerstraße" or standalone "Breiter Weg") followed by a house number,
# optionally trailed by the city. Deliberately conservative — campus forms
# ("building 27", "gebäude 3", "tram 1") carry no street suffix and never
# match. A false negative costs nothing (the graph-miss path still geocodes);
# a false positive costs one extra geocoder round-trip (the graph remains
# the backstop), so precision here is a latency optimisation, not a gate.
_ADDRESS_RE = re.compile(
    r"(?:^|[\s,])[\w.\-]*"
    r"(?:straße|strasse|str\.?|weg|platz|allee|ring|gasse|damm|ufer|chaussee|markt|steig|stieg)"
    r"\s+\d{1,4}\s*[a-z]?"
    r"(?:\s*,?\s*magdeburg)?\s*$",
    re.IGNORECASE,
)


def looks_like_street_address(place_name: str) -> bool:
    """True when the query is address-shaped (street + house number) — the
    caller should geocode FIRST instead of burning a graph lookup that the
    resolver's mandatory-numeric-token rule will reject anyway."""
    return bool(_ADDRESS_RE.search((place_name or "").strip()))


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


def _candidate(hit: dict, source: str, fallback_name: str) -> dict:
    """Shape one geocoder hit like a `_place_resolver` candidate so it can run
    through `decide_place` and each server's `_candidate_payload` unchanged.
    `confidence` stays below 0.9 and `_curated` False, so the curated-peer
    pool logic in `decide_place` never mistakes geocoder hits for campus
    nodes. No `nearest_stop` — the transit path looks that up after deciding.
    """
    return {
        "name": hit.get("name") or short_label(hit.get("label", ""), 1) or fallback_name,
        "type": hit.get("osm_type") or "geocoded",
        "lat": hit["lat"],
        "lon": hit["lon"],
        "district": hit.get("district"),
        "label": hit.get("label", ""),
        "source": source,
        "confidence": 0.75,
        "_curated": False,
    }


def geocode_fallback_candidates(place_name: str, limit: int = 5) -> list[dict]:
    """All DISTINCT in-bounds geocoder matches for an off-graph place name,
    ranked, deduplicated to physical places (the same `_same_place` rule the
    graph resolver uses — one branch listed under two OSM tags collapses to
    one, genuine chain branches survive). Empty list when nothing matches.
    Never raises.
    """
    name = (place_name or "").strip()
    if not name:
        return []

    # 1. IMIQ geocoder — full candidate list.
    try:
        hits = _imiq_geocoder.geocode_candidates(name, limit=limit)
    except Exception:
        hits = []
    cands: list[dict] = []
    for h in hits:
        if not _in_magdeburg(h.get("lat"), h.get("lon")):
            continue
        c = _candidate(h, "imiq", name)
        if any(_same_place(c, kept) for kept in cands):
            continue
        cands.append(c)
    if cands:
        return cands

    # 2. Nominatim — backup when the IMIQ service is down or has a gap.
    #    Its client returns a single best hit, so no dedup needed.
    try:
        hit = _nominatim.geocode(name)
    except Exception:
        hit = None
    if hit and _in_magdeburg(hit.get("lat"), hit.get("lon")):
        return [_candidate(hit, "nominatim", name)]

    return []


def geocode_fallback(place_name: str) -> Optional[dict]:
    """Geocode an off-graph Magdeburg place name to its single best match.

    Returns ``{lat, lon, label, source: "imiq" | "nominatim"}`` (plus
    ``district`` when derivable) or ``None`` when neither geocoder produces
    a trustworthy in-bounds match. Callers that want to disambiguate between
    multiple matches should use ``geocode_fallback_candidates`` +
    ``decide_place`` instead. Never raises.
    """
    cands = geocode_fallback_candidates(place_name)
    if not cands:
        return None
    top = cands[0]
    out = {"lat": top["lat"], "lon": top["lon"],
           "label": top.get("label", ""), "source": top["source"]}
    if top.get("district"):
        out["district"] = top["district"]
    return out
