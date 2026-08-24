"""
Shared SentenceTransformer singleton + LRU encode cache.

Services should read through ``get_shared_encoder()`` so the underlying
model is loaded exactly once. The ``EncoderProxy`` wraps any
SentenceTransformer-compatible object and adds a 512-entry LRU over
single-string encodings so repeated queries don't re-encode.
"""

import threading
from collections import OrderedDict
from typing import Any, Optional

import numpy as np


_shared_encoder: Optional["EncoderProxy"] = None
_shared_encoder_lock = threading.Lock()


class EncoderProxy:
    """Thin wrapper around a SentenceTransformer-like object.

    Transparently forwards ``encode`` calls, but memoizes single-string
    lookups (the hot path for cache-get / resolver / KB query paths) in
    a bounded LRU so repeated queries skip the model entirely.
    """

    def __init__(self, encoder: Any, lru_size: int = 512):
        self._encoder = encoder
        self._lru_size = lru_size
        self._lru: "OrderedDict[tuple, np.ndarray]" = OrderedDict()
        self._lru_lock = threading.Lock()

    def encode(self, text, normalize_embeddings: bool = False, **kwargs):
        # Only memoize single-string, normalize=True calls (the hot path).
        # Lists / batched encodes go straight to the underlying model.
        if isinstance(text, str) and normalize_embeddings and not kwargs:
            key = (text, True)
            with self._lru_lock:
                hit = self._lru.get(key)
                if hit is not None:
                    self._lru.move_to_end(key)
                    return hit
            vec = self._encoder.encode(text, normalize_embeddings=True)
            with self._lru_lock:
                self._lru[key] = vec
                self._lru.move_to_end(key)
                while len(self._lru) > self._lru_size:
                    self._lru.popitem(last=False)
            return vec
        return self._encoder.encode(text, normalize_embeddings=normalize_embeddings, **kwargs)

    # Fall through for any other attribute (tokenizer, device, etc.)
    def __getattr__(self, name):
        return getattr(self._encoder, name)


def get_shared_encoder() -> Optional["EncoderProxy"]:
    """Return the shared encoder if one has been registered, else None."""
    return _shared_encoder


def ensure_shared_encoder(encoder: Any) -> "EncoderProxy":
    """Return the shared encoder, registering ``encoder`` if none is set.

    If a shared encoder is already registered it is returned unchanged —
    the caller's ``encoder`` is ignored so we don't load the model twice.
    """
    global _shared_encoder
    with _shared_encoder_lock:
        if _shared_encoder is None:
            _shared_encoder = EncoderProxy(encoder)
        return _shared_encoder
