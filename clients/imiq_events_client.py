"""
IMIQ city-events client — the platform's Magdeburg event calendar
(scraped from dates-md), served at ``GET {base}?from=YYYY-MM-DD&to=YYYY-MM-DD``.

Each raw item carries event_name, venue_name, address, geo_lat/geo_lon,
start_iso/end_iso, description (German), price, image_url, source_url.

TIMEZONE QUIRK (probed live 2026-08-09): the timestamps are labeled +00:00
but hold LOCAL wall-clock times — a gallery that "ends 18:00+00:00" closes
at 18:00 in Magdeburg. Treat them as naive local times; NEVER convert.
A start time of 00:00 means "all day" (no start time scraped).
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


def _wall_time(iso: str) -> str:
    """'2026-08-09T14:00:00+00:00' -> '2026-08-09 14:00' (no tz math — the
    feed's offsets are mislabeled, the digits ARE the local time)."""
    return (iso or "")[:16].replace("T", " ")


class IMIQEventsClient:
    """Sync client for the IMIQ events feed. Never raises."""

    def __init__(self, base_url: str, timeout_s: float = 8.0):
        self.base_url = base_url.rstrip("/")
        self._cache = _TTLCache(_DEFAULT_CACHE_TTL or 1800)
        self._client = httpx.Client(
            headers={"User-Agent": _USER_AGENT},
            timeout=httpx.Timeout(connect=4.0, read=timeout_s, write=4.0, pool=4.0),
        )

    def get_events(self, from_date: str, to_date: str) -> Optional[List[dict]]:
        """Events overlapping [from_date, to_date] (YYYY-MM-DD, inclusive).

        Returns a list of shaped events sorted by start time, ``[]`` for an
        honest empty range, or ``None`` when the service is unreachable —
        so callers can distinguish "quiet day" from "feed down".
        """
        key = f"{from_date}..{to_date}"
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        try:
            resp = self._client.get(
                self.base_url, params={"from": from_date, "to": to_date}
            )
            if resp.status_code != 200:
                return None
            raw = resp.json()
        except Exception:
            return None
        if not isinstance(raw, list):
            return None  # error payloads come back as a dict — treat as outage

        events: List[dict] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = (item.get("event_name") or "").strip()
            if not name:
                continue
            lat, lon = item.get("geo_lat"), item.get("geo_lon")
            sources = item.get("source_url") or []
            events.append({
                "name": name,
                "venue": (item.get("venue_name") or "").strip(),
                "address": (item.get("address") or "").strip(),
                "start": _wall_time(item.get("start_iso") or ""),
                "end": _wall_time(item.get("end_iso") or ""),
                "price": item.get("price"),
                "lat": float(lat) if isinstance(lat, (int, float)) else None,
                "lon": float(lon) if isinstance(lon, (int, float)) else None,
                "url": sources[0] if isinstance(sources, list) and sources else None,
                "description": " ".join((item.get("description") or "").split()),
            })

        events.sort(key=lambda e: e["start"])
        self._cache.put(key, events)
        return events

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
