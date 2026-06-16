"""
Canonical thresholds for all semantic / similarity matching.

Single source of truth so thresholds stay consistent across the
coordinate resolver and semantic cache.  Changing a value here
changes it everywhere.
"""

from typing import Tuple

# --- Similarity thresholds -------------------------------------------------

BUILDING_EXACT = 0.75        # coordinate_resolver building match
STOP_EXACT = 0.75            # coordinate_resolver stop match
POI_EXACT = 0.70
SEMANTIC_CACHE = 0.95        # semantic_cache hit threshold
TYPO_FALLBACK = 0.30         # permissive fallback when exact has no hit
TOP_GAP_MIN = 0.10           # require top-1 beats top-2 by this margin

# Canonical place resolver (mcp_servers/_place_resolver.py): minimum name
# similarity between the user's query and a full-text candidate's name/aliases.
# Below this the candidate is rejected so callers fall through to the ORS
# geocoder — Lucene fuzzy search ALWAYS returns something, and a weak in-graph
# hit must not shadow a correct city-wide geocoder match for off-graph places.
# Calibrated 2026-06 against the live graph: genuine in-graph names score
# >= 0.86 (exact/containment/close typo), off-graph junk peaks ~0.6.
RESOLVER_MIN_SIMILARITY = 0.70

# Geocoder fallback chain (mcp_servers/_geocode.py): minimum name similarity
# between the query and the LABEL Pelias matched, before an ORS geocode hit
# is trusted. Pelias fuzzy-matches aggressively (it placed 'Ausländerbehörde'
# at a wrong downtown point, and matched "Foreigners' Office" to an office
# furniture shop at 0.47); Nominatim runs first and is trusted as-is because
# it returns nothing rather than junk. A genuine Pelias name hit contains the
# query verbatim in its label (≥0.95 containment), so 0.60 costs no recall.
GEOCODER_MIN_SIMILARITY = 0.60

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
