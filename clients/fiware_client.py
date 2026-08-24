"""
FIWARE Context Broker client for real-time IoT sensor data. Queries weather, parking, traffic, air quality, and room sensors using NGSIv2 API.

COORDINATE ORDER: All methods accept `lat, lon` (WGS84 decimal degrees).
No GeoJSON reshuffling happens inside this client — FIWARE v2 uses `geo:point`
with `lat,lon` string. The ORS client handles its own `[lon,lat]` ↔ `lat,lon`
conversion internally; callers ALWAYS pass `lat, lon` to *this* client.

The FIWARE `coords` parameter is formatted `lat,lon` (as produced here from
`lat` / `lon` arguments).
"""

import asyncio
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import httpx

try:  # services/thresholds.py is owned by a sibling agent — import defensively.
    from services.thresholds import CACHE_TTL_SECONDS as _DEFAULT_CACHE_TTL
except Exception:  # pragma: no cover - thresholds is optional at import time
    _DEFAULT_CACHE_TTL = 300

try:
    from tenacity import (
        AsyncRetrying,
        Retrying,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential_jitter,
    )
    _HAS_TENACITY = True
except Exception:  # pragma: no cover
    _HAS_TENACITY = False


_SHARED_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=10.0, pool=5.0)
_SHARED_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=50)
_DEFAULT_HEADERS = {"Accept-Encoding": "gzip"}

# 5 min TTL response cache for idempotent GETs.
_RESPONSE_TTL_SECONDS = 300

# L12 — module-level async httpx client. Lazily created by `get_async_client()`;
# one pool per process keeps keepalive connections warm across requests.
_ASYNC_CLIENT: Optional[httpx.AsyncClient] = None
_ASYNC_CLIENT_LOCK = threading.Lock()


def get_async_client() -> httpx.AsyncClient:
    """Return the module-level shared `httpx.AsyncClient`, creating if needed.

    Event-loop-aware: if the cached client was bound to a now-dead loop
    (e.g. created in a warmup thread's asyncio.run()), recreate it on the
    current loop so we don't hit "Event loop is closed" on reuse.

    Configured with:
      * `timeout=httpx.Timeout(connect=5.0, read=10.0)`
      * `limits=httpx.Limits(max_keepalive_connections=20, max_connections=50)`
      * `headers={"Accept-Encoding": "gzip"}` (httpx decompresses transparently)
    """
    global _ASYNC_CLIENT
    try:
        current_loop = asyncio.get_running_loop()
    except RuntimeError:
        current_loop = None
    with _ASYNC_CLIENT_LOCK:
        bound_loop = (
            getattr(_ASYNC_CLIENT, "_bound_loop", None)
            if _ASYNC_CLIENT is not None
            else None
        )
        if (_ASYNC_CLIENT is None
                or _ASYNC_CLIENT.is_closed
                or (current_loop is not None and bound_loop is not current_loop)):
            _ASYNC_CLIENT = httpx.AsyncClient(
                timeout=_SHARED_TIMEOUT,
                limits=_SHARED_LIMITS,
                headers=_DEFAULT_HEADERS,
            )
            _ASYNC_CLIENT._bound_loop = current_loop
        return _ASYNC_CLIENT


# ---------------------------------------------------------------------------
# Magdeburg bounds check (H26)
# ---------------------------------------------------------------------------
_MAG_LAT_MIN, _MAG_LAT_MAX = 52.05, 52.20
_MAG_LON_MIN, _MAG_LON_MAX = 11.55, 11.75


def _assert_magdeburg_bounds(lat: float, lon: float) -> None:
    """Raise ValueError if (lat, lon) falls outside the Magdeburg region.

    Guards against silent lat/lon swaps that would produce answers about a
    location hundreds of km away.
    """
    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"coordinates must be numeric, got lat={lat!r}, lon={lon!r}") from exc
    if not (_MAG_LAT_MIN <= lat_f <= _MAG_LAT_MAX and _MAG_LON_MIN <= lon_f <= _MAG_LON_MAX):
        raise ValueError(
            f"coordinates ({lat_f}, {lon_f}) outside Magdeburg bounds "
            f"lat[{_MAG_LAT_MIN}-{_MAG_LAT_MAX}], lon[{_MAG_LON_MIN}-{_MAG_LON_MAX}]"
        )


# ---------------------------------------------------------------------------
# TTL response cache
# ---------------------------------------------------------------------------
class _TTLCache:
    """Tiny thread-safe TTL cache keyed by (endpoint, sorted-args tuple)."""

    def __init__(self, ttl_seconds: float = _RESPONSE_TTL_SECONDS) -> None:
        self.ttl = ttl_seconds
        self._store: Dict[Tuple, Tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key: Tuple) -> Optional[Any]:
        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expiry, value = entry
            if expiry < now:
                self._store.pop(key, None)
                return None
            return value

    def put(self, key: Tuple, value: Any) -> None:
        with self._lock:
            self._store[key] = (time.monotonic() + self.ttl, value)


_fiware_cache = _TTLCache(_DEFAULT_CACHE_TTL if _DEFAULT_CACHE_TTL else _RESPONSE_TTL_SECONDS)


def _cache_key(endpoint: str, params: Dict[str, Any]) -> Tuple:
    # Sort for stable key regardless of insertion order; include endpoint.
    return (endpoint, tuple(sorted((k, str(v)) for k, v in params.items())))


# ---------------------------------------------------------------------------
# Schema safety (H) — validated JSON decode
# ---------------------------------------------------------------------------
def _safe_json(
    response: httpx.Response,
    *,
    endpoint: str,
    expected: str,
) -> Tuple[Optional[Any], Optional[Dict[str, Any]]]:
    """Decode `response.json()` defensively.

    Returns `(body, None)` on success, or `(None, error_dict)` on schema drift.
    `expected` is the type the caller is prepared to receive: one of
    "list", "dict", or "any". Callers should still validate the presence of
    specific top-level keys on top of this.
    """
    try:
        body = response.json()
    except (ValueError, TypeError) as exc:
        return None, {
            "found": False,
            "error": "schema_mismatch",
            "endpoint": endpoint,
            "details": f"response was not valid JSON: {exc}",
        }
    if expected == "list" and not isinstance(body, list):
        return None, {
            "found": False,
            "error": "schema_mismatch",
            "endpoint": endpoint,
            "details": f"expected JSON array, got {type(body).__name__}",
        }
    if expected == "dict" and not isinstance(body, dict):
        return None, {
            "found": False,
            "error": "schema_mismatch",
            "endpoint": endpoint,
            "details": f"expected JSON object, got {type(body).__name__}",
        }
    return body, None


# ---------------------------------------------------------------------------
# Retry helper (tenacity if present, inline otherwise)
# ---------------------------------------------------------------------------
_RETRY_ATTEMPTS = 3
_RETRY_WAITS = (0.5, 1.0, 2.0)


def _should_retry_response(status: int) -> bool:
    return 500 <= status < 600


async def _aretry_call(coro_factory: Callable[[], Any]):
    """Call the async zero-arg `coro_factory` with retry on TransportError/5xx.

    NEVER retries on 4xx — those are client errors and won't improve with a
    second attempt. Retries 5xx and `httpx.TransportError` only, up to
    `_RETRY_ATTEMPTS` total calls, with exponential-jittered backoff.
    """
    if _HAS_TENACITY:
        retrying = AsyncRetrying(
            stop=stop_after_attempt(_RETRY_ATTEMPTS),
            wait=wait_exponential_jitter(initial=0.5, max=2.0),
            retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                response = await coro_factory()
                if _should_retry_response(response.status_code):
                    # Signal tenacity to retry. Use HTTPStatusError so the
                    # retry predicate matches without masquerading the error.
                    raise httpx.HTTPStatusError(
                        f"server error {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                return response
        return None  # pragma: no cover
    # Inline fallback — exponential backoff with jitter (random.uniform(0, 0.25)).
    last_exc: Optional[BaseException] = None
    for i in range(_RETRY_ATTEMPTS):
        try:
            response = await coro_factory()
            if _should_retry_response(response.status_code) and i < _RETRY_ATTEMPTS - 1:
                await asyncio.sleep(_RETRY_WAITS[i] + random.uniform(0, 0.25))
                continue
            return response
        except httpx.TransportError as exc:
            last_exc = exc
            if i < _RETRY_ATTEMPTS - 1:
                await asyncio.sleep(_RETRY_WAITS[i] + random.uniform(0, 0.25))
                continue
            raise
    if last_exc:
        raise last_exc
    return None  # pragma: no cover


def _sretry_call(call: Callable[[], Any]):
    """Sync retry wrapper — same contract as `_aretry_call` (no 4xx retry)."""
    if _HAS_TENACITY:
        retrying = Retrying(
            stop=stop_after_attempt(_RETRY_ATTEMPTS),
            wait=wait_exponential_jitter(initial=0.5, max=2.0),
            retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
            reraise=True,
        )
        for attempt in retrying:
            with attempt:
                response = call()
                if _should_retry_response(response.status_code):
                    raise httpx.HTTPStatusError(
                        f"server error {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                return response
        return None  # pragma: no cover
    last_exc: Optional[BaseException] = None
    for i in range(_RETRY_ATTEMPTS):
        try:
            response = call()
            if _should_retry_response(response.status_code) and i < _RETRY_ATTEMPTS - 1:
                time.sleep(_RETRY_WAITS[i] + random.uniform(0, 0.25))
                continue
            return response
        except httpx.TransportError as exc:
            last_exc = exc
            if i < _RETRY_ATTEMPTS - 1:
                time.sleep(_RETRY_WAITS[i] + random.uniform(0, 0.25))
                continue
            raise
    if last_exc:
        raise last_exc
    return None  # pragma: no cover


# ---------------------------------------------------------------------------
# Sync-from-async bridge
# ---------------------------------------------------------------------------
def _run_sync(coro):
    """Run an awaitable from sync code, creating a loop if none is running."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop and loop.is_running():
        # Caller is inside an event loop but invoked the sync method —
        # run the coroutine in a worker thread with its own loop.
        result: Dict[str, Any] = {}

        def runner():
            result["value"] = asyncio.run(coro)

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join()
        return result.get("value")
    return asyncio.run(coro)


class FIWAREClient:
    """Async-first FIWARE NGSIv2 client with sync shims for legacy callers."""

    # Module-level shared AsyncClient (re-bound per instance so different
    # base_urls / api_keys coexist).
    _shared_async_client: Optional[httpx.AsyncClient] = None
    _shared_sync_client: Optional[httpx.Client] = None
    _shared_lock = threading.Lock()

    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip('/')
        self.api_key = api_key
        self._headers = {**_DEFAULT_HEADERS, "x-api-key": api_key}

    # ---- clients (module-level singletons) ------------------------------
    @classmethod
    def _async_client(cls) -> httpx.AsyncClient:
        # Delegate to module-level pool so there's exactly one AsyncClient
        # per process regardless of how many FIWAREClient instances exist.
        return get_async_client()

    @classmethod
    def _sync_client(cls) -> httpx.Client:
        with cls._shared_lock:
            if cls._shared_sync_client is None or cls._shared_sync_client.is_closed:
                cls._shared_sync_client = httpx.Client(
                    timeout=_SHARED_TIMEOUT,
                    limits=_SHARED_LIMITS,
                    headers=_DEFAULT_HEADERS,
                )
            return cls._shared_sync_client

    # ---- query_entities --------------------------------------------------
    def _build_query_params(
        self,
        entity_type: str,
        entity_id: Optional[str],
        id_pattern: Optional[str],
        q: Optional[str],
        mq: Optional[str],
        georel: Optional[str],
        geometry: Optional[str],
        coords: Optional[str],
        attrs: Optional[Union[str, List[str]]],
        metadata: Optional[Union[str, List[str]]],
        order_by: Optional[str],
        limit: Union[int, str],
        offset: Union[int, str],
        options: Optional[Union[str, List[str]]],
    ) -> Dict[str, str]:
        limit = int(limit) if isinstance(limit, str) else limit
        offset = int(offset) if isinstance(offset, str) else offset

        params: Dict[str, str] = {
            "type": entity_type,
            "limit": str(min(limit, 1000)),
        }
        if entity_id:
            params["id"] = entity_id
        elif id_pattern:
            params["idPattern"] = id_pattern
        if q:
            params["q"] = q
        if mq:
            params["mq"] = mq
        if georel and geometry and coords:
            params["georel"] = georel
            params["geometry"] = geometry
            params["coords"] = coords
        if attrs:
            params["attrs"] = ','.join(attrs) if isinstance(attrs, list) else attrs
        if metadata:
            params["metadata"] = ','.join(metadata) if isinstance(metadata, list) else metadata
        if order_by:
            params["orderBy"] = order_by
        if offset > 0:
            params["offset"] = str(offset)
        if options:
            params["options"] = ','.join(options) if isinstance(options, list) else options
        else:
            params["options"] = "count,keyValues"
        return params

    @staticmethod
    def _render_query_result(
        response: httpx.Response, entity_type: str, params: Dict[str, str]
    ) -> Dict[str, Any]:
        if response.status_code != 200:
            return {
                "success": False,
                "error": f"FIWARE returned status {response.status_code}",
                "details": response.text,
                "params": params,
            }
        # H — schema safety. Query endpoint returns a JSON array of entities.
        entities, err = _safe_json(response, endpoint=str(response.url), expected="list")
        if err is not None:
            err["params"] = params
            err["entity_type"] = entity_type
            return err
        total_header = response.headers.get("Fiware-Total-Count")
        # H25 — never report len(batch) as total. If the header is absent or
        # unparseable, set total_count=None and flag with warning="total_unknown".
        if total_header is not None:
            try:
                total_count: Optional[int] = int(total_header)
                warning: Optional[str] = None
            except ValueError:
                total_count = None
                warning = "total_unknown"
        else:
            total_count = None
            warning = "total_unknown"
        out: Dict[str, Any] = {
            "success": True,
            "entities": entities,
            "total_count": total_count,
            "returned": len(entities),
            "entity_type": entity_type,
            "params": params,
        }
        if warning:
            out["warning"] = warning
        return out

    async def aquery_entities(
        self,
        entity_type: str,
        entity_id: Optional[str] = None,
        id_pattern: Optional[str] = None,
        q: Optional[str] = None,
        mq: Optional[str] = None,
        georel: Optional[str] = None,
        geometry: Optional[str] = None,
        coords: Optional[str] = None,
        attrs: Optional[Union[str, List[str]]] = None,
        metadata: Optional[Union[str, List[str]]] = None,
        order_by: Optional[str] = None,
        limit: Union[int, str] = 20,
        offset: Union[int, str] = 0,
        options: Optional[Union[str, List[str]]] = None,
    ) -> Dict[str, Any]:
        params = self._build_query_params(
            entity_type, entity_id, id_pattern, q, mq, georel, geometry,
            coords, attrs, metadata, order_by, limit, offset, options,
        )
        url = f"{self.base_url}/entities"
        key = _cache_key(url, params)
        cached = _fiware_cache.get(key)
        if cached is not None:
            return cached
        client = self._async_client()
        try:
            response = await _aretry_call(
                lambda: client.get(url, params=params, headers=self._headers)
            )
        except httpx.TimeoutException:
            return {"success": False, "error": "FIWARE query timed out"}
        except httpx.TransportError as exc:
            return {"success": False, "error": f"Could not connect to FIWARE: {exc}"}
        except Exception as exc:  # pragma: no cover
            return {"success": False, "error": f"Unexpected error: {exc}"}
        rendered = self._render_query_result(response, entity_type, params)
        if rendered.get("success"):
            _fiware_cache.put(key, rendered)
        return rendered

    def query_entities(self, *args, **kwargs) -> Dict[str, Any]:
        return _run_sync(self.aquery_entities(*args, **kwargs))

    # ---- get_entity_by_id ------------------------------------------------
    async def aget_entity_by_id(self, entity_id: str, attrs: Optional[str] = None) -> Dict[str, Any]:
        url = f"{self.base_url}/entities/{entity_id}"
        params: Dict[str, str] = {"attrs": attrs} if attrs else {}
        client = self._async_client()
        try:
            response = await _aretry_call(
                lambda: client.get(url, params=params, headers=self._headers)
            )
        except httpx.TimeoutException:
            return {"success": False, "error": "FIWARE query timed out"}
        except httpx.TransportError as exc:
            return {"success": False, "error": f"Could not connect to FIWARE: {exc}"}
        except Exception as exc:
            return {"success": False, "error": f"Error: {exc}"}
        if response.status_code == 200:
            body, err = _safe_json(response, endpoint=str(response.url), expected="dict")
            if err is not None:
                err["entity_id"] = entity_id
                return err
            return {"success": True, "entity": body}
        if response.status_code == 404:
            return {"success": False, "error": "Entity not found", "entity_id": entity_id}
        return {
            "success": False,
            "error": f"FIWARE returned status {response.status_code}",
            "details": response.text,
        }

    def get_entity_by_id(self, entity_id: str, attrs: Optional[str] = None) -> Dict[str, Any]:
        return _run_sync(self.aget_entity_by_id(entity_id, attrs))

    # ---- query_sensor_by_coordinates ------------------------------------
    async def aquery_sensor_by_coordinates(
        self,
        latitude: float,
        longitude: float,
        sensor_type: str,
        radius: int = 500,
        attrs: Optional[str] = None,
    ) -> Dict[str, Any]:
        _assert_magdeburg_bounds(latitude, longitude)
        print(f"[FIWARE] Geo-query: type={sensor_type}, coords=({latitude}, {longitude}), radius={radius}m")

        type_mapping = {
            "Weather": "Weather", "Parking": "Parking", "Traffic": "Traffic",
            "AirQuality": "AirQuality", "Room": "Room", "Vehicle": "Vehicle", "POI": "POI",
        }
        fiware_type = type_mapping.get(sensor_type, sensor_type)
        params: Dict[str, str] = {
            "type": fiware_type,
            "georel": f"near;maxDistance:{radius}",
            "geometry": "point",
            "coords": f"{latitude},{longitude}",
            "limit": "1",
            "options": "keyValues",
        }
        if attrs:
            params["attrs"] = attrs
        url = f"{self.base_url}/entities"
        client = self._async_client()
        try:
            response = await _aretry_call(
                lambda: client.get(url, params=params, headers=self._headers)
            )
        except httpx.TimeoutException:
            return {"success": False, "error": "FIWARE query timed out"}
        except httpx.TransportError as exc:
            return {"success": False, "error": f"Could not connect to FIWARE: {exc}"}
        except Exception as exc:
            return {"success": False, "error": f"Unexpected error: {exc}"}

        if response.status_code != 200:
            return {
                "success": False,
                "error": f"FIWARE returned status {response.status_code}",
                "details": response.text,
            }
        entities, err = _safe_json(response, endpoint=str(response.url), expected="list")
        if err is not None:
            return err
        if not entities:
            print(f"[FIWARE] No sensor found within {radius}m")
            return {
                "success": False,
                "error": f"No {fiware_type} sensor found within {radius}m of ({latitude}, {longitude})",
            }
        entity = entities[0]
        print(f"[FIWARE] Found sensor: {entity.get('id', 'unknown')}")
        return {
            "success": True,
            "entity_type": fiware_type,
            "entity": entity,
            "query_location": {"latitude": latitude, "longitude": longitude},
        }

    def query_sensor_by_coordinates(
        self,
        latitude: float,
        longitude: float,
        sensor_type: str,
        radius: int = 500,
        attrs: Optional[str] = None,
    ) -> Dict[str, Any]:
        return _run_sync(self.aquery_sensor_by_coordinates(latitude, longitude, sensor_type, radius, attrs))

    # ---- convenience wrappers -------------------------------------------
    async def aget_weather(self, limit: int = 5) -> Dict[str, Any]:
        return await self.aquery_entities(entity_type="Weather", limit=limit, options="keyValues")

    def get_weather(self, limit: int = 5) -> Dict[str, Any]:
        return _run_sync(self.aget_weather(limit))

    async def aget_parking(self, limit: int = 10) -> Dict[str, Any]:
        return await self.aquery_entities(entity_type="Parking", limit=limit, options="keyValues")

    def get_parking(self, limit: int = 10) -> Dict[str, Any]:
        return _run_sync(self.aget_parking(limit))

    # ---- get_types (L28 collapsed fallback) ------------------------------
    async def aget_types(self) -> Dict[str, Any]:
        url = f"{self.base_url}/types"
        # L28 — ONE request only. If the primary endpoint 404s, skip the
        # retry (don't make a second call with the same body); return
        # {"found": false, "reason": "types_endpoint_missing"} instead.
        params = {"options": "values"}
        headers = {**self._headers, "Accept": "application/json"}
        client = self._async_client()
        try:
            response = await _aretry_call(
                lambda: client.get(url, params=params, headers=headers)
            )
        except httpx.TimeoutException:
            return {"success": False, "error": "FIWARE types query timed out"}
        except httpx.TransportError as exc:
            return {"success": False, "error": f"Could not connect to FIWARE: {exc}"}
        except Exception as exc:
            return {"success": False, "error": f"Unexpected error: {exc}"}
        if response.status_code == 200:
            body, err = _safe_json(response, endpoint=url, expected="any")
            if err is not None:
                return err
            return {"success": True, "types": body}
        if response.status_code == 404:
            # L28 — do NOT retry with a different query shape.
            return {
                "success": False,
                "found": False,
                "reason": "types_endpoint_missing",
                "endpoint": url,
            }
        return {
            "success": False,
            "error": f"FIWARE returned status {response.status_code}",
            "details": response.text,
        }

    def get_types(self) -> Dict[str, Any]:
        return _run_sync(self.aget_types())

    # ---- cleanup ---------------------------------------------------------
    async def aclose(self) -> None:
        # Best-effort: leave the module-level pool alone; tied to process lifetime.
        pass

    def close(self) -> None:
        # Compat shim — historical callers call this; pool lives for process life.
        pass
