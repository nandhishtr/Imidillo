"""
Configuration module for the Magdeburg Campus Assistant.
Loads environment variables and exposes constants for all external
services (FIWARE, OpenAI, Neo4j, ORS) and the LangGraph agent.
"""

import os
import threading
from pathlib import Path
from typing import Any, Optional
from dotenv import load_dotenv

env_path = Path(__file__).parent / '.env'
load_dotenv(dotenv_path=env_path)


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in ("true", "1", "yes")


def _parse_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        print(f"WARNING: Invalid integer value '{value}', using default {default}")
        return default


def _parse_float(value: str, default: float) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        print(f"WARNING: Invalid float value '{value}', using default {default}")
        return default


# ---------------------------------------------------------------------------
# External services
# ---------------------------------------------------------------------------
FIWARE_BASE_URL = os.getenv("FIWARE_BASE_URL", "https://imiq-public.et.uni-magdeburg.de/api/orion")
FIWARE_API_KEY = os.getenv("FIWARE_API_KEY", "")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")

NEO4J_URI = os.getenv("NEO4J_URI", "neo4j://127.0.0.1:7687")
NEO4J_USERNAME = os.getenv("NEO4J_USERNAME", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "")
NEO4J_DATABASE = os.getenv("NEO4J_DATABASE", "neo4j")

ORS_API_KEY = os.getenv("ORS_API_KEY", "")
ORS_BASE_URL = os.getenv("ORS_BASE_URL", "https://api.openrouteservice.org")

# IMIQ ranked-routes API (GraphHopper-backed) — walking/cycling/driving routes.
IMIQ_ROUTING_URL = os.getenv(
    "IMIQ_ROUTING_URL", "https://imiq-app.et.uni-magdeburg.de/api/routing"
)

# Geocoder fallback chain for off-graph place names: Nominatim (OSM) first,
# then the ORS/Pelias geocoder gated by a matched-label similarity check.
NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org")


# ---------------------------------------------------------------------------
# Application settings
# ---------------------------------------------------------------------------
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
MAX_CONVERSATION_HISTORY = _parse_int(os.getenv("MAX_CONVERSATION_HISTORY", "6"), 6)
HTTP_TIMEOUT = _parse_int(os.getenv("HTTP_TIMEOUT", "10"), 10)

MAGDEBURG_LAT = _parse_float(os.getenv("MAGDEBURG_LAT", "52.1205"), 52.1205)
MAGDEBURG_LON = _parse_float(os.getenv("MAGDEBURG_LON", "11.6276"), 11.6276)


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------
SINGLE_AGENT_MODEL = os.getenv("SINGLE_AGENT_MODEL", "gpt-5.4-thinking")
AGENT_TIMEOUT = _parse_int(os.getenv("AGENT_TIMEOUT", "90"), 90)  # wall-clock seconds
AGENT_VERBOSE_LOGGING = _parse_bool(os.getenv("AGENT_VERBOSE_LOGGING", "false"))


# ---------------------------------------------------------------------------
# Semantic cache
# ---------------------------------------------------------------------------
SEMANTIC_CACHE_ENABLED = _parse_bool(os.getenv("SEMANTIC_CACHE_ENABLED", "true"))
SEMANTIC_CACHE_THRESHOLD = _parse_float(os.getenv("SEMANTIC_CACHE_THRESHOLD", "0.88"), 0.88)
SEMANTIC_CACHE_TTL = _parse_int(os.getenv("SEMANTIC_CACHE_TTL", "3600"), 3600)
SEMANTIC_CACHE_MAX_SIZE = _parse_int(os.getenv("SEMANTIC_CACHE_MAX_SIZE", "500"), 500)


# ---------------------------------------------------------------------------
# Optional infrastructure
# ---------------------------------------------------------------------------
# Distributed rate limiter backend (optional; falls back to in-memory if empty)
REDIS_URL = os.getenv("REDIS_URL", "")


# ---------------------------------------------------------------------------
# Embedding encoder singleton (L10)
# ---------------------------------------------------------------------------
# SentenceTransformer models are expensive to load (150+ MB, 2-5 s init).
# A process-wide lazy singleton with double-checked locking guarantees we
# load the model exactly once across every service that needs embeddings
# (semantic_cache, coordinate_resolver, neo4j_tools).
_encoder: Optional[Any] = None
_encoder_lock = threading.Lock()


def get_encoder() -> Any:
    """Return the process-wide SentenceTransformer instance.

    Loads the model on first call under a lock; subsequent calls are
    lock-free. Safe to call from worker threads and async contexts.
    Callers should treat the returned object as read-only.
    """
    global _encoder
    if _encoder is None:
        with _encoder_lock:
            if _encoder is None:
                print(f"Loading embedding model ({EMBEDDING_MODEL})...")
                from sentence_transformers import SentenceTransformer
                _encoder = SentenceTransformer(EMBEDDING_MODEL)
    return _encoder


def validate_config() -> bool:
    required_vars = {
        "FIWARE_API_KEY": FIWARE_API_KEY,
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "NEO4J_PASSWORD": NEO4J_PASSWORD,
        "ORS_API_KEY": ORS_API_KEY,
    }

    missing = [key for key, value in required_vars.items() if not value]

    if missing:
        print("WARNING: Missing required environment variables:")
        for var in missing:
            print(f"   - {var}")
        print("\nCreate a .env file with these variables or set them in your environment.")
        print("   See .env.example for a template.\n")
        return False

    return True


if __name__ != "__main__":
    validate_config()


if __name__ == "__main__":
    print("=" * 60)
    print("Configuration Settings")
    print("=" * 60)

    print("\nFIWARE:")
    print(f"   Base URL: {FIWARE_BASE_URL}")
    print(f"   API Key: {'Set' if FIWARE_API_KEY else 'Missing'}")

    print("\nOpenAI:")
    print(f"   Base URL: {OPENAI_BASE_URL}")
    print(f"   API Key: {'Set' if OPENAI_API_KEY else 'Missing'}")

    print("\nNeo4j:")
    print(f"   URI: {NEO4J_URI}")
    print(f"   Username: {NEO4J_USERNAME}")
    print(f"   Password: {'Set' if NEO4J_PASSWORD else 'Missing'}")
    print(f"   Database: {NEO4J_DATABASE}")

    print("\nOpenRouteService:")
    print(f"   Base URL: {ORS_BASE_URL}")
    print(f"   API Key: {'Set' if ORS_API_KEY else 'Missing'}")

    print("\nApplication:")
    print(f"   Max History: {MAX_CONVERSATION_HISTORY}")
    print(f"   HTTP Timeout: {HTTP_TIMEOUT}s")

    print("\nMagdeburg:")
    print(f"   Latitude: {MAGDEBURG_LAT}")
    print(f"   Longitude: {MAGDEBURG_LON}")

    print("\nAgent:")
    print(f"   Model: {SINGLE_AGENT_MODEL}")
    print(f"   Timeout: {AGENT_TIMEOUT}s")
    print(f"   Verbose Logging: {'Yes' if AGENT_VERBOSE_LOGGING else 'No'}")

    print("\n" + "=" * 60)

    print("\nValidation:")
    if validate_config():
        print("All required environment variables are set!")
    else:
        print("Some required environment variables are missing.")
