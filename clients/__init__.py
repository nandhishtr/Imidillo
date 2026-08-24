"""
Package exports for external API clients. Exposes FIWAREClient, ORSClient,
IMIQRoutingClient, and IMIQGeocodeClient for external service integration.

- IMIQRoutingClient: ranked_routes / get_route — walking, cycling, and driving
  via the IMIQ city router (GraphHopper). THE routing engine.
- IMIQGeocodeClient: geocode / geocode_candidates — the in-house geocoder,
  PRIMARY fallback tier for off-graph place names (Nominatim is the backup).
- ORSClient: route polyline geometry only (map overlay), plus the
  `decode_geometry` helper api.py uses at card-build time.
- FIWAREClient: see fiware_client module for sync + async variants.

Sync methods remain fully callable (they bridge to async internally via a
shared httpx.AsyncClient pool where applicable). Callers ALWAYS pass
`lat, lon` in that order; coordinate-swap to GeoJSON `[lon, lat]` happens
inside each client only.
"""

from .elevenlabs_client import ElevenLabsClient
from .fiware_client import FIWAREClient
from .imiq_client import IMIQRoutingClient
from .imiq_geocode_client import IMIQGeocodeClient
from .ors_client import ORSClient

__all__ = [
    'ElevenLabsClient',
    'FIWAREClient',
    'IMIQGeocodeClient',
    'IMIQRoutingClient',
    'ORSClient',
]
