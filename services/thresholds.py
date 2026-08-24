"""
Canonical thresholds for all semantic / similarity matching.

Single source of truth so thresholds stay consistent across the place
resolver and semantic cache.  Changing a value here changes it everywhere.
"""

from typing import Tuple

# --- Similarity thresholds -------------------------------------------------

SEMANTIC_CACHE = 0.95        # semantic_cache hit threshold
TOP_GAP_MIN = 0.10           # require top-1 beats top-2 by this margin

# Canonical place resolver (mcp_servers/_place_resolver.py): minimum name
# similarity between the user's query and a full-text candidate's name/aliases.
# Below this the candidate is rejected so callers fall through to the ORS
# geocoder — Lucene fuzzy search ALWAYS returns something, and a weak in-graph
# hit must not shadow a correct city-wide geocoder match for off-graph places.
# Calibrated 2026-06 against the live graph: genuine in-graph names score
# >= 0.86 (exact/containment/close typo), off-graph junk peaks ~0.6.
RESOLVER_MIN_SIMILARITY = 0.70

# --- Cache keying ----------------------------------------------------------

CACHE_LOCATION_GRID_M = 100  # bucket user location to 100m grid for cache key
CACHE_TTL_SECONDS = 1800     # 30 minutes

# --- Proactive freshness ---------------------------------------------------

PROACTIVE_STALENESS_SECONDS = 600  # 10 minutes


def bucket_location(lat, lon, grid_m: int = CACHE_LOCATION_GRID_M) -> Tuple[float, float]:
    """Bucket a (lat, lon) pair to a ~grid_m grid.

    Returns a (lat_bucket, lon_bucket) tuple of floats rounded to the grid.
    Used as a component of the semantic-cache composite key so user A's
    "nearest parking" answer at location A cannot be returned to user B
    at location B.

    At Magdeburg's latitude (~52.12 N) 1 degree latitude ~ 111 km,
    1 degree longitude ~ 68.6 km, so 100 m ~ 0.0009 deg lat and
    ~0.00146 deg lon.  We use a single step derived from latitude for
    simplicity; a 100 m bucket is small enough that lat/lon asymmetry
    is below the noise floor for cache-isolation purposes.
    """
    if lat is None or lon is None:
        return (None, None)

    step = grid_m / 111_000.0  # degrees per grid_m at the equator, good enough
    lat_bucket = round(float(lat) / step) * step
    lon_bucket = round(float(lon) / step) * step
    # round() floats keep long tails, normalize to 6 decimals for stable hashes
    return (round(lat_bucket, 6), round(lon_bucket, 6))
