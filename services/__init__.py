"""
Package exports for business logic services. Exposes the SemanticCache,
the shared encoder helpers, and canonical thresholds.
"""

from .semantic_cache import SemanticCache
from ._embedder import get_shared_encoder, ensure_shared_encoder
from . import thresholds

__all__ = [
    'SemanticCache',
    'get_shared_encoder',
    'ensure_shared_encoder',
    'thresholds',
]
