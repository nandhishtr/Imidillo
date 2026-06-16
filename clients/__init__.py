"""
Package exports for external API clients. Exposes FIWAREClient, ORSClient,
and IMIQRoutingClient for external service integration.

- IMIQRoutingClient: ranked_routes / get_route — walking, cycling, and driving
  via the IMIQ city router (GraphHopper). THE routing engine.
- ORSClient: geocode / ageocode — kept ONLY as the geocoder fallback for
  off-graph place names (its route methods are no longer used in production).
- FIWAREClient: see fiware_client module for sync + async variants.

Sync methods remain fully callable (they bridge to async internally via a
shared httpx.AsyncClient pool where applicable). Callers ALWAYS pass
`lat, lon` in that order; coordinate-swap to GeoJSON `[lon, lat]` happens
inside each client only.
"""

from .fiware_client import FIWAREClient
from .imiq_client import IMIQRoutingClient
from .ors_client import ORSClient

__all__ = [
    'FIWAREClient',
    'IMIQRoutingClient',
    'ORSClient',
]
