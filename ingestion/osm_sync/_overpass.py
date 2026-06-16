"""
Lightweight Overpass API client for Magdeburg OSM ingestion — no osmnx/geopandas.

Fetches OSM features (nodes/ways/relations) straight from the Overpass API via
httpx and normalises them to plain dicts. Pairs with shapely (point-in-polygon)
and the neo4j driver so the whole ingestion runs without the heavy geo stack.
Scope is the Magdeburg city boundary (the kreisfreie Stadt, admin_level=6 —
verified: a 'Lidl' count of 10 here matches the existing graph).
"""
from __future__ import annotations

import time
from typing import Optional

import httpx

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_UA = {"User-Agent": "DashbotIngest/1.0 (OVGU IMIQ research)"}

# Bind Magdeburg to `.a` for every query body.
MAGDEBURG_AREA = 'area[name="Magdeburg"][admin_level=6]->.a;'

# Overpass is rate-limited; back off generously on 429/504/timeouts.
_RETRY_WAITS = (0.0, 3.0, 8.0, 20.0)


def overpass(ql_body: str, timeout: int = 180) -> dict:
    """Run an Overpass QL body (the part AFTER the settings + area) scoped to
    Magdeburg and return parsed JSON. `ql_body` should reference `area.a`."""
    query = f"[out:json][timeout:{timeout}];{MAGDEBURG_AREA}{ql_body}"
    last = "?"
    for wait in _RETRY_WAITS:
        if wait:
            time.sleep(wait)
        try:
            r = httpx.get(OVERPASS_URL, params={"data": query}, headers=_UA,
                          timeout=timeout + 30)
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 503, 504):
                last = f"HTTP {r.status_code}"
                continue
            r.raise_for_status()
        except (httpx.TimeoutException, httpx.TransportError) as e:  # noqa: PERF203
            last = repr(e)
            continue
    raise RuntimeError(f"Overpass failed after retries: {last}")


def element_point(el: dict) -> Optional[tuple]:
    """(lat, lon) for an element: a node's own coords, else the `center` of a
    way/relation (needs `out center;`). None when neither is present."""
    if el.get("type") == "node" and "lat" in el and "lon" in el:
        return (el["lat"], el["lon"])
    c = el.get("center")
    if isinstance(c, dict) and "lat" in c and "lon" in c:
        return (c["lat"], c["lon"])
    return None


def osm_id(el: dict) -> str:
    """Stable id matching the existing graph convention, e.g. 'node/123456'."""
    return f"{el.get('type')}/{el.get('id')}"
