"""
FastAPI REST server for the Magdeburg Campus Assistant.
Provides HTTP endpoints for chat and session management.
All queries are processed through the LangGraph multi-agent pipeline.
"""

import sys
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import asyncio

# L11: prefer uvloop on platforms that support it (Linux / macOS).
# Installed as CMD on Docker and ignored silently on Windows (no wheel).
try:
    import uvloop  # type: ignore
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

from contextlib import asynccontextmanager
from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional, Dict
from collections import defaultdict, deque
import json
import logging
import os
import re
import secrets
import threading
import time

from APP import get_app
from models import Coordinates
from config import REDIS_URL, AGENT_TIMEOUT
from clients.ors_client import decode_geometry
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("magdeburg_api")

logger.info("Starting Magdeburg Assistant API v6.0 (LangGraph single-agent on MCP tools)...")

# Initialize all dependencies (clients + LangGraph single-agent pipeline).
# The MCP tool servers + agent are built in the lifespan (MCP is the only tool path).
ctx = get_app()

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
SESSION_TIMEOUT_SECONDS = 1800  # 30 minutes
_session_last_active: Dict[str, float] = {}
_session_histories: Dict[str, list] = {}
_session_tokens: Dict[str, str] = {}
_session_active_lock = threading.Lock()


def _touch_session(session_id: str) -> None:
    with _session_active_lock:
        _session_last_active[session_id] = time.time()


def _auto_cleanup_sessions() -> None:
    now = time.time()
    with _session_active_lock:
        expired = [
            sid for sid, last in _session_last_active.items()
            if now - last > SESSION_TIMEOUT_SECONDS
        ]
        for sid in expired:
            _session_last_active.pop(sid, None)
            _session_histories.pop(sid, None)
            _session_tokens.pop(sid, None)
    if expired:
        logger.info(f"Auto-cleaned {len(expired)} expired session(s)")


def verify_session_token(
    session_id: str,
    x_session_token: Optional[str] = Header(default=None, alias="X-Session-Token"),
) -> str:
    """FastAPI dependency that validates a session's auth token.

    Returns 404 if the session_id is unknown, 401 if the token is missing
    or does not match. Uses secrets.compare_digest for constant-time compare.
    """
    with _session_active_lock:
        expected = _session_tokens.get(session_id)
    if expected is None:
        raise HTTPException(status_code=404, detail="unknown session_id")
    if not x_session_token or not secrets.compare_digest(expected, x_session_token):
        raise HTTPException(status_code=401, detail="invalid or missing session token")
    return session_id


async def _cleanup_loop():
    while True:
        await asyncio.sleep(300)
        try:
            _auto_cleanup_sessions()
        except Exception:
            logger.exception("cleanup loop iteration failed")


def _get_conversation_history(session_id: str) -> list:
    with _session_active_lock:
        return list(_session_histories.get(session_id, []))


MAX_HISTORY_MESSAGES = 80
MAX_HISTORY_BYTES = 60_000  # ~60 KB budget per session, trimmed from oldest


def _add_to_history(session_id: str, query: str, response: str) -> None:
    # H29: redact PII before persisting so it cannot leak into later turns
    # replayed as LLM context, or into checkpointer logs.
    safe_query = _redact_pii(query)
    safe_response = _redact_pii(response)
    with _session_active_lock:
        if session_id not in _session_histories:
            _session_histories[session_id] = []
        history = _session_histories[session_id]
        history.append({"role": "user", "content": safe_query})
        history.append({"role": "assistant", "content": safe_response})
        # Cap by message count first, then by total byte size so long tool
        # payloads can't balloon memory even under the count cap.
        if len(history) > MAX_HISTORY_MESSAGES:
            del history[:len(history) - MAX_HISTORY_MESSAGES]
        total = sum(len(m["content"]) for m in history)
        while total > MAX_HISTORY_BYTES and len(history) > 2:
            total -= len(history.pop(0)["content"])


# ---------------------------------------------------------------------------
# Rate limiting — Redis-backed when REDIS_URL is set, otherwise per-worker
# in-memory token-bucket as a fallback.
# ---------------------------------------------------------------------------
RATE_LIMIT_MAX = 20       # requests per window
RATE_LIMIT_WINDOW = 60.0  # seconds
_rate_limits: Dict[str, deque] = defaultdict(deque)
_rate_limit_lock = threading.Lock()


def _check_rate_limit_inmemory(client_id: str) -> bool:
    now = time.time()
    with _rate_limit_lock:
        dq = _rate_limits[client_id]
        while dq and now - dq[0] > RATE_LIMIT_WINDOW:
            dq.popleft()
        if len(dq) >= RATE_LIMIT_MAX:
            return False
        dq.append(now)
        return True


class RateLimiter:
    """Thin abstraction over an in-memory or Redis-backed rate limiter.

    The `check(client_ip)` method returns True if the request is allowed.
    On Redis connection errors mid-request we fall back to the in-memory
    limiter so requests never fail closed because of cache outages.
    """

    def __init__(self):
        self._redis = None
        self._ready = False
        self._window = int(RATE_LIMIT_WINDOW)
        self._max = RATE_LIMIT_MAX
        if REDIS_URL:
            try:
                import redis.asyncio as aioredis  # type: ignore
                self._redis = aioredis.Redis.from_url(REDIS_URL, socket_timeout=1.0)
                logger.info("Rate limiter: using Redis backend (%s)", REDIS_URL)
            except Exception as e:
                logger.warning(
                    "Rate limiter: Redis backend unavailable (%s); using in-memory fallback",
                    e,
                )
                self._redis = None
        if self._redis is None:
            logger.warning(
                "Rate limiter is per-worker only (REDIS_URL not set). "
                "Multi-worker deployments will allow N× the configured rate."
            )

    async def check(self, client_ip: str) -> bool:
        # Lazy-ping Redis on first call so sync __init__ stays fast.
        if self._redis is not None and not self._ready:
            try:
                await self._redis.ping()
                self._ready = True
                logger.info("Rate limiter: Redis backend ready")
            except Exception as e:
                logger.warning(
                    "Rate limiter: Redis ping failed (%s); falling back to in-memory", e
                )
                self._redis = None
        if self._redis is not None:
            try:
                window = int(time.time() // self._window)
                key = f"rate_limit:{client_ip}:{window}"
                count = await self._redis.incr(key)
                if count == 1:
                    await self._redis.expire(key, self._window)
                return int(count) <= self._max
            except Exception as e:
                logger.warning("Redis rate-limit check failed (%s); falling back", e)
                return _check_rate_limit_inmemory(client_ip)
        return _check_rate_limit_inmemory(client_ip)


_rate_limiter = RateLimiter()


# ---------------------------------------------------------------------------
# Nearest-stop cache — avoids a Neo4j round-trip on every message when the
# user barely moves between turns. Keyed by rounded coordinates (~111 m at
# 3 decimal places) with a 5-minute TTL.
# ---------------------------------------------------------------------------
NEAREST_STOP_TTL = 300.0
NEAREST_STOP_PRECISION = 3
_nearest_stop_cache: Dict[tuple, tuple] = {}
_nearest_stop_cache_lock = threading.Lock()


logger.info("API Ready!")

# ---------------------------------------------------------------------------
# PII redaction (H29)
# ---------------------------------------------------------------------------
# Used on any text that may end up in logs, cached entries, or replayed
# conversation history. Deliberately conservative regexes — prefer
# over-masking to leaking a valid phone/email/address.
_EMAIL_RE = re.compile(r'\b[\w._%+-]+@[\w.-]+\.[A-Za-z]{2,}\b')
_PHONE_RE = re.compile(r'\b\+?\d[\d\s()\-]{7,}\d\b')
# Partial street addresses: "Universitaetsplatz 2", "Hauptstrasse 15a".
# Streetname token + 1-4 digit number + optional letter suffix.
_STREET_RE = re.compile(
    r'\b[A-ZÄÖÜ][A-Za-zäöüß\-]{3,}(?:strasse|straße|str\.|platz|weg|allee|ring|gasse)\s+\d{1,4}[a-zA-Z]?\b',
    re.IGNORECASE,
)


def _redact_pii(text: str) -> str:
    """Mask email, phone, and partial street addresses.

    Called before logging and before storing user turns in the in-memory
    conversation history so leaked PII cannot re-enter the LLM context on
    subsequent turns.
    """
    if not text:
        return text
    redacted = _EMAIL_RE.sub("<email>", text)
    redacted = _PHONE_RE.sub("<phone>", redacted)
    redacted = _STREET_RE.sub("<address>", redacted)
    return redacted


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
_here = os.path.dirname(os.path.abspath(__file__))
_static_dir = os.path.join(_here, "static")
_templates_dir = os.path.join(_here, "templates")

_cleanup_task = None
_mcp_stack = None  # AsyncExitStack holding the persistent MCP sessions (opened in lifespan)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cleanup_task, _mcp_stack
    _cleanup_task = asyncio.create_task(_cleanup_loop())

    # MCP is the ONLY tool path (no in-process fallback). Open persistent stdio
    # sessions to the 4 MCP servers and build the single gpt-5.4 agent on them.
    # If this fails, the app fails to start — by design.
    from contextlib import AsyncExitStack
    from graph.mcp_client import open_mcp_tools
    from graph.graph import build_graph

    from graph.agent import build_single_agent

    _mcp_stack = AsyncExitStack()
    await _mcp_stack.__aenter__()
    tools, per_server = await open_mcp_tools(_mcp_stack)
    # Build the agent ONCE and keep a handle on it: the /chat streaming path
    # streams this agent directly (token-by-token), and the graph wraps the
    # SAME agent for the non-streaming path.
    agent, _tool_names = build_single_agent(tools)
    ctx.single_agent = agent
    ctx.graph_app = build_graph(
        neo4j_graph=ctx.neo4j_graph,
        fiware_client=ctx.fiware_client,
        ors_client=ctx.ors_client,
        semantic_cache=ctx.semantic_cache,
        checkpointer=ctx.checkpointer,
        tools=tools,
        agent=agent,
    )
    logger.info(f"[MCP] single gpt-5.4 agent ready on {len(tools)} MCP tools: {per_server}")

    try:
        yield
    finally:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
        if _mcp_stack is not None:
            try:
                await _mcp_stack.aclose()
            except Exception:
                pass


app = FastAPI(
    title="Magdeburg Assistant API",
    description="AI Assistant for OVGU Campus and Magdeburg City - single gpt-5.4 LangGraph agent (optional MCP tool servers)",
    version="6.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    # Restrict to local dev + OVGU domains. Update via env var for other environments.
    allow_origins=["http://localhost:*", "http://127.0.0.1:*", "https://*.ovgu.de"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# L35: gzip JSON bodies and streamed SSE payloads above 1 KB. Small
# replies (status/health) are skipped automatically by the middleware.
app.add_middleware(GZipMiddleware, minimum_size=1000)

if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------
class UserLocation(BaseModel):
    lat: float = Field(..., ge=-90, le=90)
    lon: float = Field(..., ge=-180, le=180)

class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000)
    session_id: Optional[str] = Field(None, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    language: Optional[str] = Field("en", pattern=r"^(en|de)$")
    user_location: Optional[UserLocation] = None
    # Client's location-sharing state when no coordinates are sent:
    # "off" | "denied" | "unavailable" | "timeout" | "unsupported" (or "on").
    location_status: Optional[str] = Field(None, max_length=32)
    stream: bool = False


# ---------------------------------------------------------------------------
# Location helpers
# ---------------------------------------------------------------------------
def is_route_question_without_origin(message: str) -> bool:
    message_lower = message.lower()

    route_keywords = [
        "how do i get to", "how to get to", "how can i get to",
        "how can i go to", "how do i go to",
        "directions to", "route to", "way to",
        "how far is", "distance to",
        "take me to", "navigate to",
        "get to", "go to"
    ]

    origin_keywords = [
        "from", "starting from", "starting at",
        "i'm at", "im at", "i am at",
        "currently at", "at the"
    ]

    has_route_keyword = any(kw in message_lower for kw in route_keywords)
    has_origin = any(kw in message_lower for kw in origin_keywords)

    return has_route_keyword and not has_origin


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse, tags=["meta"])
async def root():
    index_path = os.path.join(_templates_dir, "index.html")
    if os.path.isfile(index_path):
        # no-cache so the browser always revalidates the HTML and therefore
        # picks up new ?v= asset versions immediately (the static JS/CSS stay
        # cache-busted by their version query string).
        return FileResponse(index_path, media_type="text/html",
                            headers={"Cache-Control": "no-cache"})
    return HTMLResponse("<h1>Magdeburg Assistant API</h1><p>No chat UI installed.</p>")

@app.get("/status", tags=["meta"])
async def status():
    return {
        "status": "online",
        "version": "6.0.0",
        "features": ["langgraph", "single_agent", "mcp_optional"]
    }

@app.get("/health", tags=["meta"])
async def health():
    neo4j_ok = await asyncio.to_thread(ctx.neo4j_graph.test_connection)
    return {
        "status": "healthy" if neo4j_ok else "degraded",
        "neo4j": "connected" if neo4j_ok else "disconnected",
        "pipeline": "langgraph",
        "version": "6.0.0"
    }

# ---------------------------------------------------------------------------
# Card extraction — transforms tool outputs (in-process or MCP) into UI card payloads.
# Cards are emitted as SSE events before text tokens so the widget can
# render them above the response bubble.
# ---------------------------------------------------------------------------
def _coerce_output_str(output):
    if output is None:
        return None
    content = getattr(output, "content", output)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if "text" in block and isinstance(block["text"], str):
                    parts.append(block["text"])
                elif "content" in block:
                    parts.append(str(block["content"]))
        return "".join(parts) if parts else None
    if content is None:
        return None
    return str(content)


def _card_transit_route(data):
    if not isinstance(data, dict) or "segments" not in data:
        return None
    origin = data.get("origin") or {}
    dest = data.get("destination") or {}
    return {
        "type": "transit_route",
        "origin": origin.get("search_term") or origin.get("stop"),
        "origin_stop": origin.get("stop"),
        "origin_walk_m": int(origin.get("walk_meters") or 0),
        "destination": dest.get("search_term") or dest.get("stop"),
        "destination_stop": dest.get("stop"),
        "destination_walk_m": int(dest.get("walk_meters") or 0),
        "total_stops": data.get("total_stops"),
        "total_transfers": data.get("total_transfers") or 0,
        "transfer_points": data.get("transfer_points") or [],
        "segments": [
            {
                "line": s.get("line"),
                "direction": s.get("direction"),
                "from": s.get("from"),
                "to": s.get("to"),
                "num_stops": s.get("num_stops"),
                "stops": s.get("stops") or [],
            }
            for s in (data.get("segments") or [])
        ],
    }


def _coords_pair(obj):
    """A [lat, lon] point from a {lat/latitude, lon/longitude} dict, or None."""
    if not isinstance(obj, dict):
        return None
    lat = next((obj[k] for k in ("lat", "latitude") if isinstance(obj.get(k), (int, float))), None)
    lon = next((obj[k] for k in ("lon", "longitude") if isinstance(obj.get(k), (int, float))), None)
    if lat is None or lon is None:
        return None
    return [lat, lon]


# Qualitative air-quality band for the card (the agent still gets raw µg/m³).
# Simplified WHO/EU-style breakpoints over the pollutants we carry (µg/m³):
# (good_max, moderate_max). The card shows the WORST band across the pollutants
# that are present.
_AQ_BREAKPOINTS = {
    "pm25": (15, 35),
    "pm10": (30, 50),
    "no2": (40, 100),
    "o3": (100, 160),
}


def _aq_band(pollutants):
    """'good' | 'moderate' | 'poor' from a {no2, pm25, pm10, o3} dict, or None."""
    if not isinstance(pollutants, dict):
        return None
    rank = {"good": 0, "moderate": 1, "poor": 2}
    worst = None
    for key, (good_max, mod_max) in _AQ_BREAKPOINTS.items():
        try:
            v = float(pollutants.get(key))
        except (TypeError, ValueError):
            continue
        band = "good" if v <= good_max else ("moderate" if v <= mod_max else "poor")
        if worst is None or rank[band] > rank[worst]:
            worst = band
    return worst


def _card_route(data, mode, endpoints=None):
    if not isinstance(data, dict) or data.get("success") is False or data.get("available") is False:
        return None

    def _num(x):
        return x if isinstance(x, (int, float)) else None

    distance = _num(data.get("distance_meters")) or _num(data.get("distance_m"))
    duration = _num(data.get("duration_seconds")) or _num(data.get("duration_s"))
    if distance is None and duration is None:
        return None
    traffic = data.get("traffic") if isinstance(data.get("traffic"), dict) else {}
    card = {
        "type": "route",
        "mode": mode,
        "distance_m": int(distance) if distance is not None else None,
        "duration_s": int(duration) if duration is not None else None,
        "traffic_delay_s": data.get("traffic_delay_seconds"),
        "congestion": traffic.get("congestion"),
        "directions": (data.get("directions") or data.get("instructions") or [])[:8],
    }
    # Live per-mode enrichments for the card. The agent still receives the raw
    # tool result with exact numbers; these are the compact, icon-friendly
    # forms. Driving → nearest live parking (congestion is already set above);
    # walking/cycling → qualitative air band + a weather hint.
    if mode == "driving":
        pk = data.get("destination_parking")
        if isinstance(pk, dict) and pk.get("found") and pk.get("free_spots") is not None:
            card["parking"] = {
                "name": pk.get("name"),
                "free": pk.get("free_spots"),
                "distance_m": pk.get("distance_m"),
                "within_radius": pk.get("within_radius"),
            }
    else:
        aq = data.get("air_quality")
        if isinstance(aq, dict) and aq.get("found"):
            band = _aq_band(aq.get("pollutants"))
            if band:
                card["air"] = {"level": band, "station": aq.get("station")}
        wx = data.get("weather")
        if isinstance(wx, dict) and wx.get("found"):
            card["weather"] = {
                "condition": wx.get("condition"),
                "temp_c": wx.get("temperature"),
            }
    # Decode a polyline geometry → [[lat, lon], ...] HERE (not in the tool
    # result) so the coordinate array reaches the map widget but never the
    # agent's context. Downsampled to keep the SSE card small.
    coords = decode_geometry(data.get("geometry"))
    if isinstance(coords, list) and len(coords) > 1:
        card["geometry"] = _simplify_coords(coords)
    else:
        # The IMIQ router returns no geometry — synthesize a start→end line
        # (rendered dashed by the widget) so the map still shows the trip.
        start = _coords_pair((endpoints or {}).get("start") or data.get("start"))
        end = _coords_pair((endpoints or {}).get("end") or data.get("end"))
        if start and end and start != end:
            card["geometry"] = [start, end]
            card["straight_line"] = True
    return card


def _simplify_coords(coords, max_points=80):
    """Uniformly downsample a coordinate list to <= max_points, always keeping
    the first and last point. ~80 points is plenty for a city-scale polyline."""
    n = len(coords)
    if n <= max_points:
        return coords
    step = n / float(max_points)
    out = [coords[int(i * step)] for i in range(max_points)]
    out[-1] = coords[-1]
    return out


def _card_place(data, kind="place"):
    """Build a map-pin card from a place-resolution tool result (get_building /
    resolve_place_to_coordinates). Carries the Neo4j coordinates so the
    dashboard map can drop a marker at the resolved location."""
    if not isinstance(data, dict) or data.get("found") is False or data.get("success") is False:
        return None
    lat = data.get("latitude")
    lon = data.get("longitude")
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        coords = data.get("coordinates")
        if isinstance(coords, list) and len(coords) >= 2:
            lat, lon = coords[0], coords[1]
    if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
        return None
    name = data.get("name") or data.get("matched_name") or data.get("place")
    return {"type": "place", "name": name, "lat": lat, "lon": lon, "kind": kind}


def _route_cards_from_modes(data):
    """Extract per-mode route cards from a {routes: {walking, cycling, driving}}
    payload (get_all_routes / get_routes_for_places). The endpoints live on the
    parent payload (origin/destination for the compound planner, start/end for
    get_all_routes) and feed the synthesized map line when geometry is absent."""
    routes = data.get("routes") if isinstance(data.get("routes"), dict) else data
    endpoints = {
        "start": data.get("origin") or data.get("start"),
        "end": data.get("destination") or data.get("end"),
    }
    cards = []
    for m in ("walking", "cycling", "driving"):
        sub = routes.get(m) if isinstance(routes, dict) else None
        if isinstance(sub, dict):
            c = _card_route(sub, m, endpoints=endpoints)
            if c:
                cards.append(c)
    return cards


# --- Generic place-pin extraction -------------------------------------------
# Pins ANY place (a name + valid Magdeburg coordinates) the agent surfaces via
# open-ended tools (execute_cypher POI/building lookups, the context bridge) —
# not just the few tools with bespoke handlers. Coordinate-driven, so it works
# for restaurants, cafés, landmarks, etc. without tool-by-tool wiring.
_MGB_PIN_BOUNDS = (52.0, 52.3, 11.4, 11.9)  # lat_min, lat_max, lon_min, lon_max
_LAT_KEYS = ("latitude", "lat")
_LON_KEYS = ("longitude", "lon", "lng")
_NAME_KEYS = ("name", "title", "label", "matched_name")


def _in_mgb(lat, lon):
    a, b, c, d = _MGB_PIN_BOUNDS
    return a <= lat <= b and c <= lon <= d


def _place_from_dict(obj):
    """A {name, lat, lon} pin from a flat dict that carries valid Magdeburg
    coordinates, else None."""
    if not isinstance(obj, dict):
        return None
    lat = next((obj[k] for k in _LAT_KEYS if isinstance(obj.get(k), (int, float))), None)
    lon = next((obj[k] for k in _LON_KEYS if isinstance(obj.get(k), (int, float))), None)
    if lat is None or lon is None or not _in_mgb(lat, lon):
        return None
    name = next((obj[k] for k in _NAME_KEYS
                 if isinstance(obj.get(k), str) and obj.get(k).strip()), None)
    return {"type": "place", "name": name, "lat": lat, "lon": lon, "kind": "place"}


def _places_from_record(rec):
    """Find pins in one result record: the record itself, else any node-valued
    column one level down (handles both `RETURN p.lat AS latitude` and `RETURN p`)."""
    p = _place_from_dict(rec)
    if p:
        return [p]
    found = []
    if isinstance(rec, dict):
        for v in rec.values():
            p2 = _place_from_dict(v)
            if p2:
                found.append(p2)
    return found


def _card_places_generic(data, cap=12):
    """Extract map pins from an arbitrary tool result: a list of records
    (execute_cypher) or a single object with a `location` (context bridge).
    Deduped by rounded coords, capped to keep the SSE payload small."""
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        loc = data.get("location")
        records = [loc] if isinstance(loc, dict) else [data]
    else:
        return []
    cards, seen = [], set()
    for rec in records:
        for p in _places_from_record(rec):
            key = (round(p["lat"], 5), round(p["lon"], 5))
            if key in seen:
                continue
            seen.add(key)
            cards.append(p)
            if len(cards) >= cap:
                return cards
    return cards


def _extract_cards_from_tool(tool_name, raw_output):
    content = _coerce_output_str(raw_output)
    if not content:
        return []
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.debug(f"[CARD] tool {tool_name} returned non-JSON content; skipping")
        return []
    if isinstance(data, dict) and data.get("error"):
        return []

    # Disambiguation: a resolve/route tool reported several distinct matches →
    # pin each candidate so the user sees (and can pick) the options on the map.
    if isinstance(data, dict) and data.get("ambiguous") and isinstance(data.get("candidates"), list):
        return _card_places_generic(data["candidates"])

    name = (tool_name or "").lower()

    if "find_transit_route" in name:
        c = _card_transit_route(data)
        return [c] if c else []
    # Compound planners first (their names also contain "routes"); each carries
    # a {routes: {walking, cycling, driving}} block with decoded geometry.
    if "get_routes_for_places" in name and isinstance(data, dict):
        return _route_cards_from_modes(data)
    if "get_all_routes" in name and isinstance(data, dict):
        return _route_cards_from_modes(data)
    # find_nearest: route to the winning branch + a clickable pin on it.
    if "find_nearest" in name and isinstance(data, dict):
        cards = _route_cards_from_modes(data)
        dest = data.get("destination") or {}
        if isinstance(dest.get("lat"), (int, float)) and isinstance(dest.get("lon"), (int, float)):
            pin = _card_place({"latitude": dest["lat"], "longitude": dest["lon"],
                               "name": dest.get("name")}, kind="place")
            if pin:
                cards.append(pin)
        return cards
    if "walking_route" in name:
        c = _card_route(data, "walking")
        return [c] if c else []
    if "cycling_route" in name:
        c = _card_route(data, "cycling")
        return [c] if c else []
    if "driving_route" in name:
        c = _card_route(data, "driving")
        return [c] if c else []
    # Place lookups → map pin at the resolved coordinates.
    if "get_building" in name:
        c = _card_place(data, kind="building")
        return [c] if c else []
    if "resolve_place_to_coordinates" in name:
        c = _card_place(data, kind="place")
        return [c] if c else []
    # Open-ended results → pin EVERY place that carries coordinates (POIs,
    # buildings, landmarks via Cypher; the context bridge's resolved location).
    # Generic, so any place the agent surfaces shows on the map.
    if "execute_cypher" in name or "get_nearby_context" in name:
        return _card_places_generic(data)
    return []


def _extract_cards_from_messages(messages):
    """Build UI cards from the single agent's tool-call results.

    Walks the ReAct message list the agent returns in state["messages"]:
    first maps each tool_call_id -> tool name from the AIMessages, then
    feeds every ToolMessage's (name, content) to _extract_cards_from_tool.

    This is the MCP single-agent replacement for the old
    _extract_cards_from_agent_output, which read the now-unused
    `agent_results` state field and therefore emitted no cards after the
    multi-agent -> single-agent refactor.
    """
    if not isinstance(messages, list):
        return []

    # tool_call_id -> tool name, harvested from AIMessage.tool_calls.
    id_to_name = {}
    for m in messages:
        for tc in (getattr(m, "tool_calls", None) or []):
            tc_id = tc.get("id")
            tc_name = tc.get("name")
            if tc_id and tc_name:
                id_to_name[tc_id] = tc_name

    cards = []
    for m in messages:
        if type(m).__name__ != "ToolMessage":
            continue
        name = id_to_name.get(getattr(m, "tool_call_id", None)) or getattr(m, "name", None)
        try:
            cards.extend(_extract_cards_from_tool(name, getattr(m, "content", None)))
        except Exception as e:
            logger.debug(f"[CARD] extraction from tool message failed: {e}")
    return cards


def _card_dedup_key(card):
    t = card.get("type")
    if t == "transit_route":
        return f"tr:{card.get('origin_stop')}->{card.get('destination_stop')}"
    if t == "route":
        return f"rt:{card.get('mode')}:{card.get('distance_m')}:{card.get('duration_s')}"
    return json.dumps(card, sort_keys=True)


def _build_graph_input(message: str, session_id: str, user_location, conversation_history,
                       location_status=None):
    return {
        "query": message,
        "session_id": session_id,
        "messages": [],
        "user_location": user_location,
        "location_status": location_status,
        "conversation_history": conversation_history,
        "response": None,
        "cache_hit": False,
    }


class _ThinkStripper:
    """Stateful streaming filter that drops <think>...</think> spans.
    Buffers the tail so tokens split mid-tag are not leaked.
    Tracks nesting depth so nested <think> tags are handled."""

    _SAFE_TAIL = 8  # longer than len("</think>")

    def __init__(self):
        self._buf = ""
        self._depth = 0

    def feed(self, token: str) -> str:
        self._buf += token
        out = []
        while True:
            if self._depth > 0:
                close_idx = self._buf.find("</think>")
                open_idx = self._buf.find("<think>")
                if close_idx == -1 and open_idx == -1:
                    if len(self._buf) > self._SAFE_TAIL:
                        self._buf = self._buf[-self._SAFE_TAIL:]
                    break
                if open_idx != -1 and (close_idx == -1 or open_idx < close_idx):
                    self._buf = self._buf[open_idx + len("<think>"):]
                    self._depth += 1
                else:
                    self._buf = self._buf[close_idx + len("</think>"):].lstrip()
                    self._depth -= 1
            else:
                idx = self._buf.find("<think>")
                if idx == -1:
                    if len(self._buf) > self._SAFE_TAIL:
                        out.append(self._buf[:-self._SAFE_TAIL])
                        self._buf = self._buf[-self._SAFE_TAIL:]
                    break
                out.append(self._buf[:idx])
                self._buf = self._buf[idx + len("<think>"):]
                self._depth = 1
        return "".join(out)

    def flush(self) -> str:
        if self._depth > 0:
            self._buf = ""
            return ""
        tail = self._buf
        self._buf = ""
        return tail


async def _compute_proactive_context(user_location) -> str:
    """Best-effort nearby live context (weather/parking/traffic) for the
    streaming path — mirrors the graph's proactive bridge node."""
    if not user_location:
        return ""
    try:
        from graph.nodes.proactive_node import create_proactive_node
        node = create_proactive_node(ctx.fiware_client)
        res = await node({"user_location": user_location})
        return (res or {}).get("proactive_context", "") or ""
    except Exception:
        return ""


async def _compose_user_message(query, user_location, conversation_history,
                                location_status=None) -> str:
    """Assemble the agent's user message exactly like the single_agent graph
    node does (recent history + current time + location + proactive context
    + question)."""
    from graph.agent import _format_history, _format_location_status, _format_now
    parts = []
    history_text = _format_history(conversation_history or [])
    if history_text:
        parts.append(f"Recent conversation:\n{history_text}")
    parts.append(_format_now())
    location_text = _format_location_status(user_location, location_status)
    if location_text:
        parts.append(location_text)
    proactive_context = await _compute_proactive_context(user_location)
    if proactive_context:
        parts.append(proactive_context)
    parts.append(f"Question: {query}")
    return "\n\n".join(parts)


async def _stream_chat(message: str, orig_message: str, session_id: str,
                       user_location, conversation_history, location_status=None):
    """Stream the agent's answer token-by-token (ChatGPT/Claude-style).

    Streams the gpt-5.4 ReAct agent DIRECTLY with stream_mode=["values",
    "messages"] so the final-answer tokens surface as the model writes them
    (the outer graph couldn't propagate them because the agent ran inside an
    opaque ainvoke node). The thinking model's <think>…</think> spans are
    stripped so only the answer shows; the final tool transcript still yields
    the map cards.

    A background producer feeds tokens onto a queue while the consumer emits
    SSE heartbeats during the (tool-calling) gap before the first token. Falls
    back to the one-shot graph path if the agent handle is unavailable. Note:
    the streaming path skips the semantic cache (disabled in prod anyway).
    """
    agent = getattr(ctx, "single_agent", None)
    if agent is None:
        async for ev in _stream_chat_oneshot(
            message, orig_message, session_id, user_location, conversation_history, location_status
        ):
            yield ev
        return

    user_msg = await _compose_user_message(message, user_location, conversation_history,
                                           location_status)
    inputs = {"messages": [HumanMessage(content=user_msg)]}
    queue: asyncio.Queue = asyncio.Queue()

    async def _producer():
        stripper = _ThinkStripper()
        final_msgs = None
        try:
            async for mode, data in agent.astream(inputs, stream_mode=["values", "messages"]):
                if mode == "messages":
                    chunk = data[0] if isinstance(data, tuple) else data
                    if isinstance(chunk, AIMessageChunk):
                        piece = _coerce_output_str(chunk)
                        if piece:
                            visible = stripper.feed(piece)
                            if visible:
                                await queue.put(("token", visible))
                elif mode == "values":
                    if isinstance(data, dict) and data.get("messages"):
                        final_msgs = data["messages"]
            tail = stripper.flush()
            if tail:
                await queue.put(("token", tail))
            await queue.put(("final", final_msgs))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("agent token-stream failed")
            await queue.put(("error", str(e)))
        finally:
            await queue.put(("__end__", None))

    prod_task = asyncio.create_task(_producer())
    KEEPALIVE_INTERVAL = 2.5
    start = time.monotonic()
    acc: list = []
    final_msgs = None
    errored = False

    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_INTERVAL)
            except asyncio.TimeoutError:
                if time.monotonic() - start > AGENT_TIMEOUT:
                    logger.warning(f"[STREAM] agent exceeded {AGENT_TIMEOUT}s (session={session_id})")
                    if not acc:
                        msg = "Sorry, that took longer than expected to look up. Please try again."
                        acc.append(msg)
                        yield f"data: {json.dumps({'type':'token','content':msg})}\n\n"
                    break
                yield f"event: heartbeat\ndata: {json.dumps({'ts': int(time.time())})}\n\n"
                continue

            if kind == "__end__":
                break
            if kind == "token":
                acc.append(payload)
                yield f"data: {json.dumps({'type':'token','content':payload})}\n\n"
            elif kind == "final":
                final_msgs = payload
            elif kind == "error":
                errored = True
                if not acc:
                    yield f"data: {json.dumps({'type':'error','content':'stream error'})}\n\n"

        full_text = "".join(acc).strip()

        # Nothing visible streamed (e.g. the answer only landed in the final
        # message) — recover it so the user still sees an answer.
        if not full_text and final_msgs:
            for m in reversed(final_msgs):
                if isinstance(m, AIMessage):
                    c = _coerce_output_str(m)
                    if c and c.strip():
                        full_text = c.strip()
                        yield f"data: {json.dumps({'type':'token','content':full_text})}\n\n"
                        break

        # Map cards from the final tool transcript.
        if final_msgs:
            try:
                cards = _extract_cards_from_messages(final_msgs)
            except Exception as card_err:
                logger.debug(f"[CARD] extraction from tool messages failed: {card_err}")
                cards = []
            emitted_card_keys = set()
            for card in cards:
                key = _card_dedup_key(card)
                if key in emitted_card_keys:
                    continue
                emitted_card_keys.add(key)
                yield f"data: {json.dumps({'type':'card','card':card})}\n\n"

        yield f"data: {json.dumps({'type':'done','session_id':session_id})}\n\n"

        if full_text and not errored:
            _add_to_history(session_id, orig_message, full_text)

    finally:
        if not prod_task.done():
            prod_task.cancel()


async def _stream_chat_oneshot(message: str, orig_message: str, session_id: str,
                       user_location, conversation_history, location_status=None):
    """One-shot fallback: run the full graph via ainvoke and send the answer in
    a single chunk. Used only when the streaming agent handle is unavailable.

    Yield SSE chunks shaped for the dashbot widget.

    Runs the graph via `ainvoke()` in a background task (same call APP.py
    uses — known-good) and emits SSE heartbeats while waiting so the
    widget's spinner stays alive. When the graph finishes, we extract the
    final response + cards from the terminal state and stream them as
    one SSE `token` event followed by a `done` event.

    Why not astream / astream_events: both APIs in LangGraph 0.6.x fail
    to propagate events from `create_react_agent` sub-agents up to the
    parent iterator. The parent sees only the first 1-2 top-level
    updates, then StopAsyncIteration while sub-agents are still running
    — the widget receives nothing and shows "Sorry, I could not generate
    a response." Reverting to ainvoke trades token-level streaming for
    reliability; reintroduce token streaming only if we migrate to
    LangGraph's newer multi-mode streaming (`stream_mode=["messages"]`)
    and confirm it propagates correctly.
    """
    input_state = _build_graph_input(message, session_id, user_location, conversation_history,
                                     location_status)
    # Fresh thread_id per invocation so LangGraph's checkpointer doesn't
    # replay stale state from the previous turn. Conversation memory is
    # managed separately via `_session_histories` and passed in via
    # `conversation_history`, so we don't need checkpointer persistence.
    # This sidesteps a 60s fiware_agent hang observed on repeat queries
    # within the same session (confirmed by the 2026-04-23 parking query
    # log — first query 18s, second query 63s in the same thread_id).
    import uuid as _uuid
    config = {"configurable": {"thread_id": f"{session_id}:{_uuid.uuid4().hex[:8]}"}}

    KEEPALIVE_INTERVAL = 2.5
    heartbeat_count = 0

    # Kick off the graph in a background task so we can heartbeat while
    # it runs. asyncio.shield prevents wait_for's timeout from cancelling
    # the inner task — we only want timeout for the WAITER, not the work.
    graph_task = asyncio.create_task(
        ctx.graph_app.ainvoke(input_state, config=config)
    )

    try:
        while not graph_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(graph_task),
                    timeout=KEEPALIVE_INTERVAL,
                )
            except asyncio.TimeoutError:
                heartbeat_count += 1
                logger.info(
                    f"[STREAM-DIAG] heartbeat #{heartbeat_count} "
                    f"(graph running, session={session_id})"
                )
                yield f"event: heartbeat\ndata: {json.dumps({'ts': int(time.time())})}\n\n"

        # Graph finished — materialise its result.
        result = graph_task.result()
        logger.info(
            f"[STREAM-DIAG] ainvoke complete after {heartbeat_count} heartbeats, "
            f"keys={list(result.keys()) if isinstance(result, dict) else type(result).__name__}"
        )

        response_text = None
        if isinstance(result, dict):
            response_text = result.get("final_response") or result.get("response")

            # Cards come from the agent's tool results (find_transit_route +
            # routing tools). Walk the ReAct transcript the single agent
            # returns in `messages` and build cards from those tool outputs.
            try:
                cards = _extract_cards_from_messages(result.get("messages"))
            except Exception as card_err:
                logger.debug(f"[CARD] extraction from tool messages failed: {card_err}")
                cards = []
            emitted_card_keys = set()
            for card in cards:
                key = _card_dedup_key(card)
                if key in emitted_card_keys:
                    continue
                emitted_card_keys.add(key)
                yield f"data: {json.dumps({'type':'card','card':card})}\n\n"

        if response_text:
            yield f"data: {json.dumps({'type':'token','content':response_text})}\n\n"
            _add_to_history(session_id, orig_message, response_text)
        else:
            logger.warning(
                f"[STREAM-DIAG] graph produced no response. "
                f"keys={list(result.keys()) if isinstance(result, dict) else 'n/a'}"
            )

        done_payload = {"type": "done", "session_id": session_id}
        yield f"data: {json.dumps(done_payload)}\n\n"

    except Exception as e:
        logger.exception("stream chat failed")
        yield f"data: {json.dumps({'type':'error','content':'stream error'})}\n\n"
        yield f"data: {json.dumps({'type':'done','session_id':session_id})}\n\n"
    except BaseException as be:
        # Client disconnect / cancellation. Kill the graph task so the
        # MCP subprocesses and Neo4j sessions aren't kept busy after the
        # widget walked away.
        logger.warning(
            f"[STREAM-DIAG] stream interrupted by {type(be).__name__}: {be} "
            f"(heartbeats={heartbeat_count})"
        )
        if not graph_task.done():
            graph_task.cancel()
        raise
    finally:
        if not graph_task.done():
            graph_task.cancel()


def _maybe_find_nearest_stop(user_coords: Coordinates):
    """Run Neo4j nearest-stop lookup; return dict or None on failure.

    Thread-safe, blocking — intended for asyncio.to_thread() so the
    request-path event loop is not blocked while Neo4j responds (L24).
    Results are cached for NEAREST_STOP_TTL seconds keyed by rounded
    coordinates so a stationary user doesn't re-hit Neo4j every turn.
    """
    key = (
        round(user_coords.lat, NEAREST_STOP_PRECISION),
        round(user_coords.lon, NEAREST_STOP_PRECISION),
    )
    now = time.time()
    with _nearest_stop_cache_lock:
        cached = _nearest_stop_cache.get(key)
        if cached and now - cached[0] < NEAREST_STOP_TTL:
            return cached[1]
    try:
        result = ctx.neo4j_graph.find_nearest_stop(user_coords)
    except Exception as e:
        logger.warning(f"[nearest_stop] lookup failed: {e}")
        return None
    with _nearest_stop_cache_lock:
        _nearest_stop_cache[key] = (now, result)
    return result


@app.post("/chat", tags=["chat"])
async def chat_endpoint(
    request: ChatRequest,
    http_request: Request,
    background_tasks: BackgroundTasks,
    x_session_token: Optional[str] = Header(default=None, alias="X-Session-Token"),
):
    client_ip = http_request.client.host if http_request.client else "unknown"
    if not await _rate_limiter.check(client_ip):
        raise HTTPException(status_code=429, detail="Rate limit exceeded")

    session_id = request.session_id
    if session_id:
        # If a session_id is provided, the token is mandatory.
        with _session_active_lock:
            expected = _session_tokens.get(session_id)
        if expected is None:
            raise HTTPException(status_code=404, detail="unknown session_id")
        if not x_session_token or not secrets.compare_digest(expected, x_session_token):
            raise HTTPException(status_code=401, detail="invalid or missing session token")
    else:
        import uuid
        session_id = str(uuid.uuid4())

    # L24: fire the nearest-stop lookup in a background thread **in parallel**
    # with the rest of the pipeline so it never adds to the critical path.
    # The result is only used to rewrite the message when we detect a
    # route question without an explicit origin.
    nearest_stop_task = None
    user_location = None
    if request.user_location:
        loc = request.user_location
        user_location = {"lat": loc.lat, "lon": loc.lon}
        user_coords = Coordinates(lat=loc.lat, lon=loc.lon)
        logger.info(f"User location: {user_coords.lat}, {user_coords.lon}")
        nearest_stop_task = asyncio.create_task(
            asyncio.to_thread(_maybe_find_nearest_stop, user_coords)
        )

    # Location-sharing state for the agent: trust the client's explicit status,
    # else infer it from whether coordinates were actually sent.
    location_status = (request.location_status
                       or ("on" if user_location else "off")).strip().lower()

    message = request.message
    # Only block on nearest-stop resolution if we genuinely need it
    # (route question without an origin). Otherwise let the task keep
    # running — the graph's location node can pick up user_location itself.
    if nearest_stop_task is not None and is_route_question_without_origin(message):
        try:
            nearest_stop = await asyncio.wait_for(nearest_stop_task, timeout=1.5)
        except (asyncio.TimeoutError, Exception) as e:
            logger.info(f"[nearest_stop] skipped ({type(e).__name__})")
            nearest_stop = None
        if nearest_stop:
            # Keep the FULL canonical stop name (do NOT strip "Magdeburg "): the
            # exact name resolves to the stop by exact match, whereas the bare
            # token can match several places and make the agent ask the user to
            # disambiguate their OWN position — e.g. "Opernhaus" alone matches a
            # tram stop, a place, AND a nearby shop ("which Opernhaus?").
            stop_name = nearest_stop['name']
            message = f"{message} (I'm currently near {stop_name})"
            logger.info(f"Modified message: {message}")
            logger.info(f"Location: near {nearest_stop['name']}")
    elif nearest_stop_task is not None:
        # Detach: let it run, but don't crash on GC warnings.
        def _swallow(task):
            try:
                task.result()
            except Exception:
                pass
        nearest_stop_task.add_done_callback(_swallow)

    # H29: redact PII from any user-originated text before logging.
    safe_user_input = _redact_pii(request.message)
    logger.info(f"User: {safe_user_input} | Session: {session_id} (stream={request.stream})")

    _touch_session(session_id)
    conversation_history = _get_conversation_history(session_id)

    if request.stream:
        return StreamingResponse(
            _stream_chat(message, request.message, session_id, user_location, conversation_history,
                         location_status),
            media_type="text/event-stream",
            # Content-Encoding: identity opts this stream OUT of GZipMiddleware,
            # which otherwise buffers the whole SSE response in its gzip
            # compressor and only flushes at the end (Starlette's GZipResponder
            # never flushes per chunk) — that defeats token streaming entirely.
            # X-Accel-Buffering disables proxy buffering (nginx/traefik).
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Content-Encoding": "identity",
            },
        )

    try:
        try:
            # See _stream_chat for rationale on per-invocation thread_id.
            import uuid as _uuid
            result = await asyncio.wait_for(
                ctx.graph_app.ainvoke(
                    _build_graph_input(message, session_id, user_location, conversation_history,
                                       location_status),
                    config={"configurable": {"thread_id": f"{session_id}:{_uuid.uuid4().hex[:8]}"}},
                ),
                timeout=30.0,
            )
        except asyncio.TimeoutError:
            logger.warning(f"/chat request exceeded 30s deadline (session={session_id})")
            raise HTTPException(status_code=504, detail="Request exceeded 30s deadline")

        response_text = result.get("response", "I'm sorry, I couldn't process your request.")

        # L23: fast return. History persistence, PII redaction on the reply,
        # and any future cache_store writes happen after the HTTP response
        # has been flushed — the user sees the answer sooner.
        background_tasks.add_task(
            _add_to_history, session_id, request.message, response_text
        )

        return {
            "text": response_text,
            "session_id": session_id,
            "type": "answer",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"chat endpoint failed: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/session/start", tags=["session"])
async def session_start():
    import uuid
    session_id = str(uuid.uuid4())
    session_token = secrets.token_urlsafe(32)
    with _session_active_lock:
        _session_tokens[session_id] = session_token
    _touch_session(session_id)
    logger.info(f"Session started: {session_id}")
    return {"session_id": session_id, "session_token": session_token}


@app.post("/session/{session_id}/end", tags=["session"])
async def session_end(session_id: str = Depends(verify_session_token)):
    with _session_active_lock:
        _session_last_active.pop(session_id, None)
        _session_histories.pop(session_id, None)
        _session_tokens.pop(session_id, None)
    logger.info(f"Session '{session_id}' destroyed")
    return {"status": "ok", "session_id": session_id}


class ResetRequest(BaseModel):
    session_id: str = Field(..., max_length=128, pattern=r"^[A-Za-z0-9_-]+$")


@app.post("/chat/reset", tags=["chat"])
async def chat_reset(
    request: ResetRequest,
    x_session_token: Optional[str] = Header(default=None, alias="X-Session-Token"),
):
    session_id = verify_session_token(request.session_id, x_session_token)
    with _session_active_lock:
        _session_histories.pop(session_id, None)
    logger.info(f"Session '{session_id}' history cleared")
    return {"status": "ok", "session_id": session_id}


if __name__ == "__main__":
    import uvicorn
    logger.info("Starting IMIQ API v6.0 (LangGraph single-agent pipeline on MCP tools)")
    # reload=False so WatchFiles does not restart the server mid-request.
    # Restarts kill the 4 MCP stdio subprocesses and wipe in-memory session
    # state, which manifested as agent timeouts and 404s on /session/end.
    uvicorn.run("api:app", host="127.0.0.1", port=8000, reload=False)
