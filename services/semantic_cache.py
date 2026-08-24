"""
Semantic similarity cache for LLM decision results.
Uses pre-normalized BGE embeddings with numpy dot-product for O(n) lookup.

Composite key (H1, H13):
  Every entry is keyed by (user_id, location_bucket, date_hour) in addition
  to the query embedding.  Similarity is only computed against candidates
  sharing the same bucket, so User A's answer cannot leak into User B's
  session and "nearest parking" at location A is not reused at location B.

Design decision — high threshold (0.95 default):
  "Where is Building 40?" vs "Where is Building 18?" score ~0.90-0.93 rejected.
  "Where is Building 40?" vs "Where's Building 40?" score ~0.97+ accepted.
  Trades hit-rate for correctness.
"""

import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from .thresholds import (
    CACHE_TTL_SECONDS,
    SEMANTIC_CACHE,
    bucket_location,
)
from ._embedder import ensure_shared_encoder, get_shared_encoder

_ANON_USER = "__anon__"


def _date_hour() -> str:
    """UTC date-hour string, used as a coarse freshness component of the key."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%d-%H")


def _bucket_key(
    user_id: Optional[str],
    location: Optional[Tuple[float, float]],
    date_hour: Optional[str] = None,
) -> Tuple[str, Tuple[Optional[float], Optional[float]], str]:
    """Return the (user_id, location_bucket, date_hour) tuple used to isolate entries."""
    u = user_id if user_id else _ANON_USER
    if location is not None:
        loc_bucket = bucket_location(location[0], location[1])
    else:
        loc_bucket = (None, None)
    dh = date_hour if date_hour is not None else _date_hour()
    return (u, loc_bucket, dh)


class SemanticCache:

    def __init__(
        self,
        encoder,
        threshold: float = SEMANTIC_CACHE,
        ttl_seconds: int = CACHE_TTL_SECONDS,
        max_size: int = 500,
    ):
        # Register / reuse the process-wide encoder so we don't load the
        # SentenceTransformer model multiple times across services.
        self._encoder = ensure_shared_encoder(encoder)
        self._threshold = threshold
        self._ttl = ttl_seconds
        self._max_size = max_size

        self._lock = threading.RLock()
        self._embeddings: List[np.ndarray] = []
        self._values: List[Dict[str, Any]] = []
        self._timestamps: List[float] = []
        self._buckets: List[tuple] = []
        self._access_order: List[int] = []

        self._hits = 0
        self._misses = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get(
        self,
        query: str,
        user_id: Optional[str] = None,
        location: Optional[Tuple[float, float]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Look up a cached response.

        Candidates are filtered to the same ``(user_id, location_bucket,
        date_hour)`` bucket before similarity is computed — so no cross-user
        and no cross-location leaks are possible.
        """
        bucket = _bucket_key(user_id, location)
        query_embedding = self._encoder.encode(query, normalize_embeddings=True)

        now = time.time()
        with self._lock:
            if not self._embeddings:
                self._misses += 1
                return None

            # Filter to the same bucket first (H1/H13 isolation).
            candidate_idxs = [i for i, b in enumerate(self._buckets) if b == bucket]
            if not candidate_idxs:
                self._misses += 1
                return None

            matrix = np.stack([self._embeddings[i] for i in candidate_idxs])
            similarities = matrix @ query_embedding

            local_best = int(np.argmax(similarities))
            best_idx = candidate_idxs[local_best]
            best_score = float(similarities[local_best])

            if best_score < self._threshold:
                self._misses += 1
                return None

            if now - self._timestamps[best_idx] > self._ttl:
                self._remove_index(best_idx)
                self._misses += 1
                return None

            if best_idx in self._access_order:
                self._access_order.remove(best_idx)
            self._access_order.append(best_idx)

            self._hits += 1
            return dict(self._values[best_idx])

    def put(
        self,
        query: str,
        value: Dict[str, Any],
        user_id: Optional[str] = None,
        location: Optional[Tuple[float, float]] = None,
    ) -> None:
        """Store a response under the given (user_id, location) bucket."""
        bucket = _bucket_key(user_id, location)
        embedding = self._encoder.encode(query, normalize_embeddings=True)

        with self._lock:
            self._purge_expired()

            while len(self._embeddings) >= self._max_size:
                if self._access_order:
                    self._remove_index(self._access_order[0])
                else:
                    self._remove_index(0)

            new_idx = len(self._embeddings)
            self._embeddings.append(embedding)
            self._values.append(dict(value))
            self._timestamps.append(time.time())
            self._buckets.append(bucket)
            self._access_order.append(new_idx)

    def get_stats(self) -> Dict[str, Any]:
        with self._lock:
            total = self._hits + self._misses
            return {
                "size": len(self._embeddings),
                "hits": self._hits,
                "misses": self._misses,
                "hit_rate": round(self._hits / max(1, total), 3),
            }

    # ------------------------------------------------------------------
    # Internal helpers — all assume self._lock is held
    # ------------------------------------------------------------------

    def _purge_expired(self) -> None:
        now = time.time()
        valid = [i for i, ts in enumerate(self._timestamps) if now - ts <= self._ttl]
        if len(valid) == len(self._embeddings):
            return
        self._rebuild(valid)

    def _remove_index(self, idx: int) -> None:
        valid = [i for i in range(len(self._embeddings)) if i != idx]
        self._rebuild(valid)

    def _rebuild(self, keep_indices: List[int]) -> None:
        keep_set = set(keep_indices)
        old_to_new = {old: new for new, old in enumerate(keep_indices)}

        # Build new lists locally first, then atomically swap references under
        # the lock. Prevents readers from seeing half-rebuilt state if any
        # comprehension raises partway through.
        new_embeddings = [self._embeddings[i] for i in keep_indices]
        new_values = [self._values[i] for i in keep_indices]
        new_timestamps = [self._timestamps[i] for i in keep_indices]
        new_buckets = [self._buckets[i] for i in keep_indices]
        new_access_order = [
            old_to_new[i] for i in self._access_order if i in keep_set
        ]

        with self._lock:
            self._embeddings = new_embeddings
            self._values = new_values
            self._timestamps = new_timestamps
            self._buckets = new_buckets
            self._access_order = new_access_order


# Re-export so callers can reach the shared encoder helpers via semantic_cache
__all__ = [
    "SemanticCache",
    "get_shared_encoder",
    "ensure_shared_encoder",
]
