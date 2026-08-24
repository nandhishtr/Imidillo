"""
Semantic cache nodes for the LangGraph pipeline.

Wraps the existing SemanticCache to check/store full query-response pairs.
The cache key is composite (query + user_id + bucketed location) to prevent
User A's location-scoped answer from leaking to User B.

On cache hit, `final_response` is populated so the graph-level conditional
edge can short-circuit straight to END and downstream nodes see
`cache_hit=True` and can bail out.
"""

from typing import Optional, Tuple

from graph.nodes._models import CacheCheckResult

# Try to pull shared thresholds; fall back to safe defaults so the node is
# still callable even if services/thresholds.py isn't on the path yet.
try:
    from services.thresholds import bucket_location
except Exception:  # pragma: no cover - fallback path only
    def bucket_location(lat, lon, grid_m: int = 100) -> Tuple[Optional[float], Optional[float]]:
        if lat is None or lon is None:
            return (None, None)
        step = grid_m / 111_000.0
        return (round(round(float(lat) / step) * step, 6),
                round(round(float(lon) / step) * step, 6))


def _extract_location(state: dict) -> Optional[Tuple[float, float]]:
    """Return a raw (lat, lon) tuple from state, or None if unavailable.

    Tries `user_location` dict first, then top-level `lat`/`lon` fields.
    """
    loc = state.get("user_location")
    if isinstance(loc, dict):
        lat = loc.get("lat")
        lon = loc.get("lon")
        if lat is not None and lon is not None:
            try:
                return (float(lat), float(lon))
            except (TypeError, ValueError):
                pass

    lat = state.get("lat")
    lon = state.get("lon")
    if lat is not None and lon is not None:
        try:
            return (float(lat), float(lon))
        except (TypeError, ValueError):
            pass

    return None


def _extract_user_id(state: dict) -> str:
    """Pull user_id from state with anonymous fallback."""
    uid = state.get("user_id")
    if uid is None or uid == "":
        return "__anon__"
    return str(uid)


def _cache_get_compat(semantic_cache, query: str, user_id: str,
                       location: Optional[Tuple[float, float]]):
    """Call semantic_cache.get() with the new composite-key kwargs.

    Falls back to the legacy single-arg signature if the parallel agent's
    update hasn't landed yet.  When the legacy path is used we bucket the
    location into the query text so at least inter-location isolation is
    preserved — not a perfect fix but strictly better than the old key.
    """
    try:
        return semantic_cache.get(query, user_id=user_id, location=location)
    except TypeError:
        bucketed = bucket_location(*location) if location else (None, None)
        composite_query = f"{query}|u={user_id}|loc={bucketed[0]},{bucketed[1]}"
        return semantic_cache.get(composite_query)


def _cache_put_compat(semantic_cache, query: str, value: dict,
                       user_id: str, location: Optional[Tuple[float, float]]):
    """Mirror of _cache_get_compat for puts."""
    try:
        semantic_cache.put(query, value, user_id=user_id, location=location)
    except TypeError:
        bucketed = bucket_location(*location) if location else (None, None)
        composite_query = f"{query}|u={user_id}|loc={bucketed[0]},{bucketed[1]}"
        semantic_cache.put(composite_query, value)


def create_cache_nodes(semantic_cache):
    """Create cache_check and cache_store node functions with a bound cache.
    If semantic_cache is None, both nodes are pass-through (no caching).
    """

    def cache_check(state: dict) -> dict:
        """Check for a cached response using composite (query, user, location) key."""
        if semantic_cache is None:
            return CacheCheckResult(cache_hit=False).model_dump(exclude_none=True)

        query = state.get("query", "")
        user_id = _extract_user_id(state)
        location = _extract_location(state)

        try:
            cached = _cache_get_compat(semantic_cache, query, user_id, location)
        except Exception as e:
            print(f"[CACHE] get() failed: {e}")
            cached = None

        if cached is not None:
            response = cached.get("response", "") or ""
            print(f"[CACHE] Hit for: {query[:50]}... (user={user_id})")
            result = CacheCheckResult(
                cache_hit=True,
                response=response,
                final_response=response,
            )
            return result.model_dump(exclude_none=True)

        print(f"[CACHE] Miss (user={user_id})")
        return CacheCheckResult(cache_hit=False).model_dump(exclude_none=True)

    def cache_store(state: dict) -> dict:
        """Store the synthesised response in cache after synthesis."""
        if semantic_cache is None:
            return {}

        # Don't re-store a hit — it was already in the cache.
        if state.get("cache_hit", False):
            return {}

        query = state.get("query", "")
        # Prefer `response` (legacy synthesis output) then `final_response`.
        response = state.get("response") or state.get("final_response") or ""

        if not response:
            return {}

        user_id = _extract_user_id(state)
        location = _extract_location(state)

        try:
            _cache_put_compat(
                semantic_cache,
                query,
                {"response": response},
                user_id,
                location,
            )
            stats = semantic_cache.get_stats()
            print(f"[CACHE] Stored ({stats['size']} entries, "
                  f"{stats['hit_rate']:.0%} hit rate)")
        except Exception as e:
            print(f"[CACHE] put() failed: {e}")

        return {}

    return cache_check, cache_store
