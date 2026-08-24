"""
Shared domain types used across the codebase.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Coordinates:
    """Immutable geographic point with explicit lat/lon fields.

    Eliminates coordinate-swap bugs by replacing raw (lon, lat) / (lat, lon)
    tuples with named fields.  Every client accesses .lat and .lon directly
    and applies its own API convention internally.
    """
    lat: float
    lon: float
