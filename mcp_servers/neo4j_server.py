"""
Neo4j MCP Server for the Magdeburg Campus Mobility Assistant.
Exposes raw Cypher execution, schema introspection, and search tools
so that a ReAct agent can autonomously query the campus graph database.
"""

import json
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastmcp import FastMCP
from neo4j import GraphDatabase, Query, READ_ACCESS

from config import (
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE,
)
from mcp_servers._place_resolver import resolve_place_candidates, decide_place
from mcp_servers._geocode import geocode_fallback, short_label

_DEFAULT_QUERY_TIMEOUT = 8.0


def _q(cypher: str, timeout: float = _DEFAULT_QUERY_TIMEOUT) -> Query:
    """Wrap a Cypher string in a Query object with a per-query timeout."""
    return Query(cypher, timeout=timeout)

mcp = FastMCP("neo4j-campus", instructions=(
    "Static campus data: Buildings, Stops, POIs, Streets, Landmarks, Areas, "
    "Sensors (metadata only), and transit Lines. Sensor nodes store metadata "
    "(IDs, types); LIVE sensor values (weather, parking, traffic) live in "
    "FIWARE — do NOT ask this server for current readings."
))

# ---------------------------------------------------------------------------
# Driver (module-level singleton, read-only). Shared with neo4j_tools via
# `neo4j_tools.get_default_driver()`.
# ---------------------------------------------------------------------------
_driver = GraphDatabase.driver(
    NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD),
    connection_acquisition_timeout=5.0,
    connection_timeout=3.0,
    max_connection_pool_size=50,
    # Aura/cloud load balancers silently drop idle TCP connections; without
    # these, the first query after an idle gap fails with "defunct connection"
    # and stalls the agent until its 90s timeout.
    max_connection_lifetime=300,
    keep_alive=True,
    liveness_check_timeout=60,
)


def _run_read(cypher: str, params: dict = None, timeout: float = _DEFAULT_QUERY_TIMEOUT) -> list[dict]:
    """Execute a read-only Cypher query and return results as list of dicts.

    `timeout` is a per-query server-side timeout (seconds) enforced by Neo4j.

    The session runs in READ access mode so the server rejects any write
    (including write procedures like apoc.create/refactor.* that slip past the
    text-level allow-list) with "Writing in read access mode not allowed".
    """
    with _driver.session(
        database=NEO4J_DATABASE, default_access_mode=READ_ACCESS
    ) as session:
        result = session.run(_q(cypher, timeout=timeout), parameters=params or {})
        return [dict(record) for record in result]


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

# Campus building lookup — one reliable primitive that resolves a campus
# building AND traverses its location context. A building NUMBER matches the
# campus building by its zero-padded alias ONLY, so unrelated OSM "Gebäude N"
# address buildings are never returned. Replaces several brittle prompt rules.
_BUILDING_NUM_RE = re.compile(
    r'(?:geb(?:\.|aeude|äude)?|building|bldg)\s*0*(\d{1,2})\b|^\s*0*(\d{1,2})\s*$',
    re.IGNORECASE,
)


def _extract_building_number(q: str):
    m = _BUILDING_NUM_RE.search((q or "").strip())
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return None
    return n if 1 <= n <= 99 else None


_BUILDING_CTX_TAIL = """
    OPTIONAL MATCH (b)-[:ON_STREET]->(st:Street)
    OPTIONAL MATCH (b)-[adj:ADJACENT_TO]-(nb:Building)
    OPTIONAL MATCH (b)-[:NEAREST_STOP|ACCESSIBLE_STOP]->(stop:Stop)
    RETURN b.name AS name, b.function AS what, b.aliases AS aliases,
           b.departments AS departments, b.opening_hours AS opening_hours,
           b.latitude AS latitude, b.longitude AS longitude,
           collect(DISTINCT st.name) AS streets,
           collect(DISTINCT {n: nb.name, d: adj.distance_m}) AS adj_raw,
           collect(DISTINCT stop.name) AS stops
"""

_BUILDING_BY_NUMBER = (
    "MATCH (b:Building) WHERE ANY(a IN coalesce(b.aliases,[]) WHERE toLower(a) = $alias) "
    "WITH b LIMIT 1 " + _BUILDING_CTX_TAIL
)

_BUILDING_BY_KEYWORD = (
    "MATCH (b:Building) "
    "WHERE toLower(b.name) CONTAINS $q "
    "   OR ANY(a IN coalesce(b.aliases,[]) WHERE toLower(a) CONTAINS $q) "
    "   OR toLower(coalesce(b.function,'')) CONTAINS $q "
    "WITH b, (CASE WHEN b.source IS NULL THEN 0 ELSE 1 END) AS cr, "
    "        (CASE WHEN toLower(b.name) = $q THEN 0 "
    "              WHEN ANY(a IN coalesce(b.aliases,[]) WHERE toLower(a) = $q) THEN 1 "
    "              WHEN toLower(b.name) CONTAINS $q THEN 2 ELSE 3 END) AS mr "
    "ORDER BY cr, mr, size(b.name) LIMIT 1 " + _BUILDING_CTX_TAIL
)


@mcp.tool()
def get_building(query: str) -> str:
    """Look up an OVGU CAMPUS building and return WHAT it is plus WHERE it is.

    Use this for ANY "where is / what is <building>" question — "building 3",
    "gebäude 12", "the rectorate", "Faculty of Computer Science", "library".
    Resolves campus buildings reliably: a building NUMBER matches the campus
    building exactly via its alias and will NEVER return an unrelated OSM
    "Gebäude N" address building elsewhere in the city. Also traverses the graph
    for the building's street, neighbouring buildings, and nearest transit stop,
    so you can describe its location concretely. Coordinates are included for the
    map card — describe location in words, don't read coordinates aloud.

    Args:
        query: building number / name / alias, e.g. "building 3", "gebäude 12",
               "rektorat", "Faculty of Electrical Engineering", "library".

    Returns:
        JSON: {found, name, what, on_street, adjacent_buildings, nearest_stop,
        departments, opening_hours, latitude, longitude}, or {found: false}.
    """
    q = (query or "").strip()
    if not q:
        return json.dumps({"found": False, "error": "empty query"})
    num = _extract_building_number(q)
    if num is not None:
        rows = _run_read(_BUILDING_BY_NUMBER, {"alias": f"building {num:02d}"}, timeout=6.0)
    else:
        rows = _run_read(_BUILDING_BY_KEYWORD, {"q": q.lower()}, timeout=6.0)
    if not rows:
        return json.dumps({"found": False, "query": query})
    r = rows[0]
    adj = [x for x in (r.get("adj_raw") or []) if x and x.get("n") and x.get("n") != r.get("name")]
    adj.sort(key=lambda x: x["d"] if x.get("d") is not None else 9e9)
    streets = [s for s in (r.get("streets") or []) if s]
    stops = [s for s in (r.get("stops") or []) if s]
    campus_alias = next((a for a in (r.get("aliases") or []) if str(a).lower().startswith("building ")), None)
    return json.dumps({
        "found": True,
        "name": r.get("name"),
        "campus_alias": campus_alias,
        "what": r.get("what"),
        "departments": r.get("departments"),
        "opening_hours": r.get("opening_hours"),
        "on_street": streets[0] if streets else None,
        "adjacent_buildings": [x["n"] for x in adj[:3]],
        "nearest_stop": stops[0] if stops else None,
        "latitude": r.get("latitude"),
        "longitude": r.get("longitude"),
    }, default=str)

_NODES_QUERY = """
    CALL db.labels() YIELD label
    CALL {
        WITH label
        MATCH (n) WHERE label IN labels(n)
        WITH n LIMIT 1
        RETURN keys(n) AS props
    }
    CALL {
        WITH label
        MATCH (n) WHERE label IN labels(n)
        RETURN count(n) AS cnt
    }
    RETURN label, props, cnt
    ORDER BY cnt DESC
"""

_RELS_QUERY = """
    CALL db.relationshipTypes() YIELD relationshipType AS type
    CALL {
        WITH type
        MATCH (a)-[r]->(b) WHERE type(r) = type
        WITH a, r, b LIMIT 1
        RETURN keys(r) AS props, labels(a)[0] AS from_label, labels(b)[0] AS to_label
    }
    RETURN type, props, from_label, to_label
"""

_DOMAIN_NOTES = """## Important Notes

- Building names are like 'Building 03', 'Building 27' (zero-padded)
- Stop names are prefixed with 'Magdeburg ', e.g. 'Magdeburg Hauptbahnhof'
- POI types include: Restaurant, Cafe, Bar, Shop, etc.
- POIs have 'cuisine' property (e.g. 'turkish', 'italian')
- NEARBY relationships have distance_m, walk_time_min, tier, category properties
- NEXT_STOP relationships have 'line', 'direction', 'order' properties
- Use point.distance() for geographic distance calculations
- Coordinates are stored as latitude/longitude properties on nodes"""


# Enum-like properties sampled at startup so the agent sees real distinct values
# instead of guessing string literals (case, spelling, coarse vs. specific buckets).
# Each entry: (kind, label_or_reltype, property, is_list_property)
_VALUE_CATALOG_SPECS = [
    ("node", "POI",      "type",            False),
    ("node", "POI",      "cuisine",         False),
    ("node", "POI",      "price_range",     False),
    ("node", "POI",      "dietary_options", True),
    ("node", "Building", "fiware_type",     False),
    ("node", "Line",     "type",            False),
    ("node", "Sensor",   "type",            False),
    ("node", "Sensor",   "category",        False),
    ("node", "Street",   "highway_type",    False),
    ("rel",  "NEARBY",   "category",        False),
    ("rel",  "NEARBY",   "tier",            False),
    ("rel",  "NEXT_STOP","line",            False),
    ("rel",  "NEXT_STOP","direction",       False),
]

_CATALOG_LIMIT = 15
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _catalog_query(kind: str, label: str, prop: str, is_list: bool, limit: int) -> str:
    if kind == "node":
        if is_list:
            return (
                f"MATCH (n:{label}) WHERE n.{prop} IS NOT NULL "
                f"UNWIND n.{prop} AS v "
                f"RETURN DISTINCT v AS value, count(*) AS cnt "
                f"ORDER BY cnt DESC LIMIT {limit}"
            )
        return (
            f"MATCH (n:{label}) WHERE n.{prop} IS NOT NULL "
            f"RETURN DISTINCT n.{prop} AS value, count(*) AS cnt "
            f"ORDER BY cnt DESC LIMIT {limit}"
        )
    return (
        f"MATCH ()-[r:{label}]->() WHERE r.{prop} IS NOT NULL "
        f"RETURN DISTINCT r.{prop} AS value, count(*) AS cnt "
        f"ORDER BY cnt DESC LIMIT {limit}"
    )


def build_value_catalog() -> str:
    # Snapshot distinct values of enum-like properties at startup so the agent
    # filters on real strings (exact case) instead of guesses.
    lines = ["## Property Value Catalog (sampled from live DB at startup)",
             "", "Values are case-sensitive. Use these exact strings in filters.",
             "If you need values for a property not listed here, call "
             "`sample_values(kind, label, property)`.", ""]

    for kind, label, prop, is_list in _VALUE_CATALOG_SPECS:
        cypher = _catalog_query(kind, label, prop, is_list, _CATALOG_LIMIT)
        try:
            rows = _run_read(cypher, timeout=15.0)
        except Exception as e:
            lines.append(f"- **{label}.{prop}**: (error: {e})")
            continue

        if not rows:
            lines.append(f"- **{label}.{prop}**: (no values)")
            continue

        parts = [f"`{r['value']}` ({r['cnt']})" for r in rows]
        tag = "LIST" if is_list else ("rel" if kind == "rel" else "node")
        lines.append(f"- **{label}.{prop}** [{tag}]: {', '.join(parts)}")

    return "\n".join(lines)


@mcp.tool()
def sample_values(kind: str, label: str, property: str, limit: int = 15, is_list: bool = False) -> str:
    """Return the TOP-N distinct values of a property with their counts, ordered by frequency.

    This is a DISCOVERY tool — use it when a filter returns unexpectedly empty to reveal
    the exact case-sensitive enum strings that exist in the graph. The result is truncated
    to `limit` entries (default 15), so it is NOT a full enumeration. If you need complete
    counts (e.g. "how many X per Y"), use `execute_cypher` with `COUNT` + `GROUP BY`.

    Args:
        kind: 'node' for node labels, 'rel' for relationship types.
        label: node label (e.g. 'POI', 'Building') or relationship type (e.g. 'NEARBY').
        property: property name (e.g. 'type', 'cuisine', 'category', 'tier').
        limit: max distinct values to return (default 15, capped at 100).
        is_list: set True if the property is a LIST (e.g. POI.dietary_options).

    Examples:
        sample_values('node', 'POI', 'type')
        sample_values('rel',  'NEARBY', 'category')
        sample_values('node', 'POI', 'dietary_options', is_list=True)
    """
    if kind not in ("node", "rel"):
        return json.dumps({"error": "kind must be 'node' or 'rel'"})
    if not _IDENT_RE.match(label):
        return json.dumps({"error": f"invalid label/reltype: {label!r}"})
    if not _IDENT_RE.match(property):
        return json.dumps({"error": f"invalid property name: {property!r}"})
    limit = max(1, min(int(limit), 100))

    cypher = _catalog_query(kind, label, property, bool(is_list), limit)
    try:
        return json.dumps(_run_read(cypher), indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": f"sample_values failed: {e}"})


def build_structural_schema() -> str:
    # Single source of truth for node labels + relationship types.
    # Called at agent init to inject into the system prompt, and by get_schema() for external MCP clients.
    schema_parts = []

    try:
        node_results = _run_read(_NODES_QUERY, timeout=15.0)
        schema_parts.append("## Node Labels\n")
        for row in node_results:
            label = row.get("label", "?")
            props = row.get("props", [])
            cnt = row.get("cnt", 0)
            schema_parts.append(f"- **{label}** ({cnt} nodes): {', '.join(sorted(props))}")
    except Exception as e:
        schema_parts.append(f"Node labels error: {e}")

    try:
        rel_results = _run_read(_RELS_QUERY, timeout=15.0)
        schema_parts.append("\n## Relationship Types\n")
        for row in rel_results:
            rtype = row.get("type", "?")
            props = row.get("props", [])
            from_l = row.get("from_label", "?")
            to_l = row.get("to_label", "?")
            prop_str = f" (props: {', '.join(sorted(props))})" if props else ""
            schema_parts.append(f"- (:{from_l})-[:{rtype}]->(:{to_l}){prop_str}")
    except Exception as e:
        schema_parts.append(f"Relationship types error: {e}")

    return "\n".join(schema_parts)


# ---------------------------------------------------------------------------
# Schema + value catalog — module-level CACHED SINGLETONS.
# Computed ONCE at import time so every agent init reuses the same string
# (previously: ~13 catalog queries + 2 schema queries per agent instance).
# Agents import `CACHED_SCHEMA_STRING` and `CACHED_VALUE_CATALOG_STRING`
# directly instead of calling the builder functions.
# ---------------------------------------------------------------------------
try:
    CACHED_SCHEMA_STRING = build_structural_schema()
except Exception as _schema_err:  # pragma: no cover — startup-best-effort
    CACHED_SCHEMA_STRING = f"(schema unavailable at startup: {_schema_err})"

try:
    CACHED_VALUE_CATALOG_STRING = build_value_catalog()
except Exception as _cat_err:  # pragma: no cover — startup-best-effort
    CACHED_VALUE_CATALOG_STRING = f"(value catalog unavailable at startup: {_cat_err})"


def invalidate_schema_cache() -> dict:
    """Recompute `CACHED_SCHEMA_STRING` and `CACHED_VALUE_CATALOG_STRING`
    from the live database. Call this after a schema change or catalog
    refresh (e.g. admin endpoint). Returns the new sizes."""
    global CACHED_SCHEMA_STRING, CACHED_VALUE_CATALOG_STRING
    CACHED_SCHEMA_STRING = build_structural_schema()
    CACHED_VALUE_CATALOG_STRING = build_value_catalog()
    return {
        "schema_chars": len(CACHED_SCHEMA_STRING),
        "catalog_chars": len(CACHED_VALUE_CATALOG_STRING),
    }


# ---------------------------------------------------------------------------
# Schema allow-list for validating LLM-generated Cypher (H21).
# ---------------------------------------------------------------------------
_VALID_LABELS = frozenset({
    "Stop", "Line", "Street", "Landmark", "Area", "Building", "POI", "Sensor",
})
_VALID_REL_TYPES = frozenset({
    "SERVED_BY", "NEXT_STOP", "WALKING_DISTANCE", "BORDERED_BY", "SAME_STRUCTURE",
    "CONNECTED_INTERNALLY", "CONTIGUOUS_TO", "PROVIDES_COOLING_TO",
    "RECEIVES_COOLING_FROM", "SURROUNDS", "SURROUNDED_BY", "LOOKS_ALIKE",
    "HAS_LANDMARK", "FACES", "BEHIND_LANDMARK", "VIEWS", "CONTAINS", "NEAREST_STOP",
    "NEAR_BUILDING", "ON_STREET", "INTERSECTS", "NEARBY", "ADJACENT_TO",
    "NEAREST_BUILDING", "ACCESSIBLE_ROUTE", "ACCESSIBLE_STOP",
})

# Patterns to pull out label/reltype references from a Cypher source string.
# Labels appear as `:Label` — possibly followed by `|:Other` for alternatives.
# Rel types appear as `[:REL]`, `[r:REL]`, `[r:REL|OTHER]`, `[r:REL*1..3]`, etc.
_LABEL_RE = re.compile(r":([A-Z][A-Za-z0-9_]*)")
_REL_RE = re.compile(r"\[[^\]]*?:([A-Z_][A-Z0-9_]*(?:\s*\|\s*:?[A-Z_][A-Z0-9_]*)*)")

# Write/side-effect verbs disallowed for LLM queries (top-level; case-insensitive).
_WRITE_VERBS_RE = re.compile(
    r"\b(CREATE|DELETE|DETACH\s+DELETE|REMOVE|SET|MERGE|DROP)\b",
    re.IGNORECASE,
)

# `LOAD CSV` reads from an arbitrary URL/file path → SSRF + data exfiltration.
_LOAD_CSV_RE = re.compile(r"\bLOAD\s+CSV\b", re.IGNORECASE)

# Procedure CALLs are denied except this tiny read-only allow-list. Without it,
# read-mode procedures bypass every other guard: apoc.load.json/jdbc/csv reach
# arbitrary hosts (SSRF/exfiltration) and dbms.*/db.* leak config and internals.
# (Write procedures like apoc.create/refactor/periodic.* are also caught here
# AND rejected by the READ_ACCESS session — defence in depth.)
_ALLOWED_PROCEDURES = frozenset({
    "db.index.fulltext.querynodes",
    "db.index.fulltext.queryrelationships",
})

# Match `CALL proc.name(` or `CALL proc.name YIELD`, but NOT `CALL { subquery }`.
_PROC_CALL_RE = re.compile(r"\bCALL\s+(?!\{)([A-Za-z_][\w.]*)", re.IGNORECASE)


def _extract_labels_and_rels(cypher: str) -> tuple[set[str], set[str]]:
    """Return (labels, rel_types) referenced by the query. Rel patterns may
    contain alternatives (`[:A|B]`) or quantifiers (`[:A*1..3]`) — split on
    `|` and strip variable-length parts so we only classify bare type names."""
    # Strip line comments // ... and block comments /* ... */ cheaply.
    cleaned = re.sub(r"//.*?$", " ", cypher, flags=re.MULTILINE)
    cleaned = re.sub(r"/\*.*?\*/", " ", cleaned, flags=re.DOTALL)

    labels: set[str] = set()
    for m in _LABEL_RE.finditer(cleaned):
        labels.add(m.group(1))

    rels: set[str] = set()
    for m in _REL_RE.finditer(cleaned):
        # Match group may contain "A|B|:C"; split and clean each token.
        for alt in m.group(1).split("|"):
            alt = alt.strip().lstrip(":").strip()
            # Strip any *..N quantifier suffix
            alt = alt.split("*", 1)[0].strip()
            if alt:
                rels.add(alt)

    # Remove labels that were actually rel-type tokens we already captured.
    # (Conservative: if something appears both as a label-shaped `:X` and an
    # allowed rel type, prefer the rel-type classification.)
    labels -= rels

    return labels, rels


def _validate_cypher_allow_list(query: str) -> dict | None:
    """Reject queries that reference unknown labels/rel types, or that use
    write verbs. Returns an error dict when invalid, or None when clean."""
    # 1) forbid writes
    write_match = _WRITE_VERBS_RE.search(query)
    if write_match:
        verb = write_match.group(1).upper().strip()
        return {
            "error": "write_operation_not_allowed",
            "invalid_verb": verb,
            "hint": "This server is read-only. Remove CREATE/DELETE/REMOVE/SET/MERGE/DROP.",
        }

    # 2) forbid LOAD CSV (arbitrary URL/file access)
    if _LOAD_CSV_RE.search(query):
        return {
            "error": "load_csv_not_allowed",
            "hint": "This server is read-only and cannot load external data sources.",
        }

    # 3) forbid procedure CALLs outside the read-only allow-list
    for m in _PROC_CALL_RE.finditer(query):
        proc = m.group(1)
        if proc.lower() not in _ALLOWED_PROCEDURES:
            return {
                "error": "procedure_not_allowed",
                "invalid_procedure": proc,
                "hint": (
                    "Only plain read queries (MATCH/RETURN) and full-text search "
                    "are allowed. Procedure calls such as apoc.* and dbms.* are blocked."
                ),
                "allowed_procedures": sorted(_ALLOWED_PROCEDURES),
            }

    labels, rels = _extract_labels_and_rels(query)
    invalid_labels = sorted(l for l in labels if l not in _VALID_LABELS)
    invalid_rels = sorted(r for r in rels if r not in _VALID_REL_TYPES)

    if invalid_labels or invalid_rels:
        return {
            "error": "invalid_label_or_rel",
            "invalid": {
                "labels": invalid_labels,
                "relationship_types": invalid_rels,
            },
            "valid_labels": sorted(_VALID_LABELS),
            "valid_relationship_types": sorted(_VALID_REL_TYPES),
        }
    return None


@mcp.tool()
def get_schema() -> str:
    """Return the graph schema: node labels with their property keys,
    relationship types with their property keys, and example counts.
    Call this FIRST before writing any Cypher queries."""
    # Use the cached string — the schema does not change at runtime.
    return CACHED_SCHEMA_STRING + "\n\n" + _DOMAIN_NOTES


@mcp.tool()
def execute_cypher(query: str, params: str = "{}") -> str:
    """Execute a read-only Cypher query against the campus graph database.

    Args:
        query: A valid Cypher READ query (MATCH, RETURN, etc.). Write verbs
            (CREATE, DELETE, REMOVE, SET, MERGE, DROP), LOAD CSV, and procedure
            calls (apoc.*, dbms.*, db.* — except full-text search) are rejected.
            Only the valid labels/relationship types from the graph schema
            are accepted; unknown `:Label` or `[:REL]` references are rejected.
        params: JSON string of query parameters. Keys must match the
            `$placeholder` names inside the query text.

            Example — `params='{"name": "Building 03", "limit": 10}'`
            matches placeholders `$name` and `$limit` in the query.

    Returns:
        JSON array of result records, or an error message.

    Examples:
        execute_cypher("MATCH (b:Building) WHERE b.name = $name RETURN b", '{"name": "Building 03"}')
        execute_cypher("MATCH (s:Stop) RETURN s.name, s.lines LIMIT $limit", '{"limit": 10}')
        execute_cypher("MATCH (b:Building)-[r:NEARBY]->(p:POI) WHERE p.type = $t RETURN p.name, r.distance_m ORDER BY r.distance_m LIMIT 5", '{"t": "Restaurant"}')
    """
    # Safety: schema allow-list (labels/rels) + write-verb rejection.
    validation_error = _validate_cypher_allow_list(query)
    if validation_error is not None:
        return json.dumps(validation_error)

    try:
        parsed_params = json.loads(params) if isinstance(params, str) else params
    except json.JSONDecodeError as e:
        return json.dumps({"error": f"Invalid params JSON: {e}"})

    try:
        results = _run_read(query, parsed_params)

        # Convert Neo4j node/relationship objects to dicts
        serializable = []
        for record in results:
            row = {}
            for key, value in record.items():
                if hasattr(value, "items"):
                    row[key] = dict(value)
                elif isinstance(value, list):
                    row[key] = [dict(v) if hasattr(v, "items") else v for v in value]
                else:
                    row[key] = value
            serializable.append(row)

        return json.dumps(serializable, indent=2, default=str)
    except Exception as e:
        return json.dumps({"error": f"Cypher execution failed: {e}"})


def _get_line_direction(line: str, seg_from: str, seg_to: str) -> str | None:
    """Get the travel direction (terminal stop name) from NEXT_STOP edge direction property.

    A stop can have outgoing edges for the same line in BOTH directions (northbound
    and southbound). We must pick the edge that actually lies on a path to the
    destination — otherwise we may return the opposite terminal.
    """
    result = _run_read("""
        MATCH path = (a:Stop {name: $from})-[rels:NEXT_STOP*1..50]->(c:Stop {name: $to})
        WHERE ALL(rel IN rels WHERE rel.line = $line)
        RETURN rels[0].direction AS direction, size(rels) AS hops
        ORDER BY hops
        LIMIT 1
    """, {"from": seg_from, "to": seg_to, "line": line}, timeout=12.0)
    if result and result[0].get("direction"):
        # Format is "Origin → Terminal" — extract terminal
        parts = result[0]["direction"].split("→")
        if len(parts) == 2:
            return parts[1].strip()
    return None


# ---------------------------------------------------------------------------
# Path finding: SINGLE consolidated query covering 0- and 1-transfer routes
# plus a graph-wide shortestPath fallback.
#
# Previously `_find_best_path` made up to 15+ round-trips (shared-line probe,
# per-line path probe, transfer candidate fan-out, per-candidate segment
# probes...). The new implementation:
#
#   * Runs one UNION ALL Cypher that produces candidate paths with
#     `cost = total_stops + 10 * num_transfers`.
#   * Prefers APOC `apoc.algo.dijkstra` when available, otherwise uses the
#     pure-Cypher branches below. This keeps one round-trip either way.
# ---------------------------------------------------------------------------

_APOC_AVAILABLE: bool | None = None


def _apoc_available() -> bool:
    global _APOC_AVAILABLE
    if _APOC_AVAILABLE is not None:
        return _APOC_AVAILABLE
    try:
        rows = _run_read("CALL dbms.procedures() YIELD name WHERE name STARTS WITH 'apoc.' "
                          "RETURN count(*) AS n", timeout=15.0)
        _APOC_AVAILABLE = bool(rows and rows[0].get("n", 0) > 0)
    except Exception:
        _APOC_AVAILABLE = False
    return _APOC_AVAILABLE


# Transit pathfinding queries, run SEQUENTIALLY with early-exit (NOT bundled in
# one CALL{...UNION...} — that produced a pathological plan that timed out even
# though each strategy alone is sub-second). The line filter is INLINE
# ({line: line}) so Neo4j prunes per hop instead of expanding every *..50 path
# then filtering. Direct (0 transfers) is always preferred by the cost function
# (total_stops + 10*num_transfers), so trying it first and stopping is correct.
_TRANSIT_DIRECT_Q = """
    MATCH (a:Stop {name: $origin}), (b:Stop {name: $dest})
    UNWIND [l IN a.lines WHERE l IN b.lines] AS line
    MATCH path = (a)-[:NEXT_STOP*..50 {line: line}]->(b)
    WITH [s IN nodes(path) | s.name] AS stops,
         [rel IN relationships(path) | rel.line] AS lines,
         size(nodes(path)) AS total_stops
    RETURN stops, lines, total_stops AS len, 0 AS num_transfers
    ORDER BY total_stops LIMIT 1
"""

_TRANSIT_TRANSFER_Q = """
    MATCH (a:Stop {name: $origin}), (b:Stop {name: $dest})
    MATCH (t:Stop)
    WHERE t.name <> a.name AND t.name <> b.name
      AND ANY(ol IN a.lines WHERE ol IN t.lines)
      AND ANY(dl IN b.lines WHERE dl IN t.lines)
    WITH a, b, t,
         [ol IN a.lines WHERE ol IN t.lines] AS la_list,
         [dl IN b.lines WHERE dl IN t.lines] AS lb_list,
         point.distance(point({latitude: a.latitude, longitude: a.longitude}),
                        point({latitude: t.latitude, longitude: t.longitude}))
       + point.distance(point({latitude: t.latitude, longitude: t.longitude}),
                        point({latitude: b.latitude, longitude: b.longitude})) AS geo_detour
    ORDER BY geo_detour LIMIT 5
    UNWIND la_list AS la
    UNWIND lb_list AS lb
    OPTIONAL MATCH p1 = (a)-[:NEXT_STOP*..50 {line: la}]->(t)
    OPTIONAL MATCH p2 = (t)-[:NEXT_STOP*..50 {line: lb}]->(b)
    WITH p1, p2 WHERE p1 IS NOT NULL AND p2 IS NOT NULL
    WITH [s IN nodes(p1) | s.name] + [s IN nodes(p2)[1..] | s.name] AS stops,
         [rel IN relationships(p1) | rel.line] + [rel IN relationships(p2) | rel.line] AS lines
    RETURN stops, lines, size(stops) AS len, 1 AS num_transfers
    ORDER BY len LIMIT 1
"""

_TRANSIT_SHORTEST_Q = """
    MATCH (a:Stop {name: $origin}), (b:Stop {name: $dest})
    MATCH path = shortestPath((a)-[:NEXT_STOP*..50]->(b))
    WITH [s IN nodes(path) | s.name] AS stops,
         [r IN relationships(path) | r.line] AS lines,
         size(nodes(path)) AS total_stops
    RETURN stops, lines, total_stops AS len,
           size([i IN range(0, size(lines)-2) WHERE lines[i] <> lines[i+1]]) AS num_transfers
    LIMIT 1
"""


def _try_read(cypher: str, params: dict, timeout: float) -> list:
    """_run_read that treats a per-strategy failure (most commonly the Neo4j
    transaction timeout on a pathological transfer expansion) as 'no rows' so
    the caller falls through to the next, cheaper strategy instead of
    crashing the whole tool call."""
    try:
        return _run_read(cypher, params, timeout=timeout)
    except Exception as e:
        print(f"[TRANSIT] strategy query failed ({type(e).__name__}); "
              f"falling through to next strategy")
        return []


def _find_best_path(o: str, d: str) -> dict | None:
    """Find the best transit path from stop `o` to stop `d`.

    Tries direct (0 transfers) -> 1 transfer -> shortestPath fallback as
    SEPARATE queries with early-exit. Bundling them into a single
    CALL{...UNION...} query produced a plan that timed out at 12s. Direct is
    always preferred by cost (total_stops + 10*num_transfers), so early-exit
    on the first hit preserves the original ranking. A strategy that errors
    (e.g. the transfer expansion exceeding its transaction timeout for hub
    pairs with many shared lines) counts as 'no path' and the next strategy
    runs — shortestPath then still produces an answer, just possibly with
    one transfer more.
    """
    params = {"origin": o, "dest": d}
    rows = _try_read(_TRANSIT_DIRECT_Q, params, timeout=8.0)
    if not rows:
        rows = _try_read(_TRANSIT_TRANSFER_Q, params, timeout=12.0)
    if not rows:
        rows = _try_read(_TRANSIT_SHORTEST_Q, params, timeout=8.0)
    if not rows:
        return None
    r = rows[0]
    lines = _relabel_min_transfers(r["stops"]) or r["lines"]
    return {"stops": r["stops"], "lines": lines, "len": r["len"]}


# All lines serving each consecutive hop of a stop sequence, in order.
_HOP_LINES_Q = """
    UNWIND range(0, size($stops) - 2) AS i
    MATCH (a:Stop)-[r:NEXT_STOP]->(b:Stop)
    WHERE a.name = $stops[i] AND b.name = $stops[i + 1]
    RETURN i, collect(DISTINCT r.line) AS lines
    ORDER BY i
"""


def _relabel_min_transfers(stops: list) -> list | None:
    """Per-hop line labels with the MINIMUM number of line changes.

    Stops sharing a track have one parallel NEXT_STOP edge per line, and
    shortestPath picks an arbitrary edge per hop — so its label sequence can
    'switch' between Tram 3/4/5 on a stretch a single tram covers, reporting
    phantom transfers (a real case showed 8 where 2 suffice). This fetches
    ALL lines serving each hop and greedily extends the line that reaches
    furthest — the classic interval-cover greedy, which is optimal here.

    Returns None (caller keeps the original labels) when any hop's line set
    is unavailable.
    """
    if not stops or len(stops) < 2:
        return None
    try:
        rows = _run_read(_HOP_LINES_Q, {"stops": stops}, timeout=8.0)
    except Exception:
        return None
    by_i = {row["i"]: row["lines"] for row in rows}
    hop_lines = []
    for i in range(len(stops) - 1):
        lines = by_i.get(i)
        if not lines:
            return None
        hop_lines.append(lines)

    def run_length(line: str, start: int) -> int:
        n = 0
        for j in range(start, len(hop_lines)):
            if line not in hop_lines[j]:
                break
            n += 1
        return n

    labels: list = []
    current: str | None = None
    i = 0
    while i < len(hop_lines):
        if current is None or current not in hop_lines[i]:
            current = max(hop_lines[i], key=lambda ln: run_length(ln, i))
        labels.append(current)
        i += 1
    return labels


_NEAREST_STOP_Q = """
    MATCH (s:Stop)
    WITH s, point.distance(point({latitude: $lat, longitude: $lon}),
                           point({latitude: s.latitude, longitude: s.longitude})) AS d
    RETURN s.name AS stop_name, s.latitude AS stop_lat, s.longitude AS stop_lon,
           round(d) AS walk_m
    ORDER BY d LIMIT 1
"""


def _anchor(lat, lon):
    """Validated (lat, lon) inside Magdeburg for nearest-branch picking, else None."""
    if lat is None or lon is None:
        return None
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if 52.05 <= lat_f <= 52.20 and 11.55 <= lon_f <= 11.75:
        return (lat_f, lon_f)
    return None


def _candidate_payload(c: dict) -> dict:
    """Shape one disambiguation candidate for the agent + map pin (drops the
    resolver's internal fields). `latitude`/`longitude` let api.py pin it."""
    out = {"name": c.get("name"), "type": c.get("type"),
           "latitude": c.get("lat"), "longitude": c.get("lon")}
    if c.get("district"):
        out["district"] = c["district"]
    if c.get("distance_m") is not None:
        out["distance_m"] = c["distance_m"]
    ns = c.get("nearest_stop") or {}
    if ns.get("name"):
        out["nearest_stop"] = ns["name"]
    return out


def _resolve_transit_decision(place_name: str, anchor=None) -> tuple:
    """Resolve ONE transit endpoint to a decision, mirroring the road-routing
    disambiguation: the canonical candidate SET first (auto-pick the branch
    nearest `anchor`, or report candidates so the agent asks "which one?"), then
    the geocoder + nearest-stop fallback for off-graph places. Makes transit
    work for ANY Magdeburg address — the place boards at its nearest stop.

    Returns ``("resolved", {name, type, lat, lon, nearest_stop})`` |
    ``("ambiguous", [candidate dicts])`` | ``("none", None)``."""
    if not place_name or not place_name.strip():
        return ("none", None)
    try:
        cands = resolve_place_candidates(_run_read, place_name)
    except Exception:
        cands = []
    cands = [c for c in cands if (c.get("nearest_stop") or {}).get("name")]
    if cands:
        dec = decide_place(cands, anchor=anchor)
        if dec["status"] == "resolved":
            return ("resolved", dec["place"])
        if dec["status"] == "ambiguous":
            return ("ambiguous", dec["candidates"])

    geo = geocode_fallback(place_name)
    if not geo:
        return ("none", None)
    rows = _run_read(_NEAREST_STOP_Q, {"lat": geo["lat"], "lon": geo["lon"]}, timeout=8.0)
    if not rows:
        return ("none", None)
    r = rows[0]
    return ("resolved", {
        "name": short_label(geo.get("label", "")) or place_name,
        "type": "geocoded",
        "lat": geo["lat"],
        "lon": geo["lon"],
        "nearest_stop": {
            "name": r["stop_name"],
            "lat": r["stop_lat"],
            "lon": r["stop_lon"],
            "walk_m": r["walk_m"] or 0,
        },
    })


def _transit_endpoint(search_term: str, resolved: dict) -> dict:
    """Shape a resolve_place() result into the transit-route endpoint payload
    the UI card reads (search term + chosen stop + entity coords)."""
    ns = resolved.get("nearest_stop") or {}
    return {
        "search_term": search_term,
        "stop": ns.get("name"),
        "walk_meters": ns.get("walk_m", 0),
        "resolved_from": resolved.get("name"),
        "entity_type": resolved.get("type"),
        "entity_name": resolved.get("name"),
        "entity_lat": resolved.get("lat"),
        "entity_lon": resolved.get("lon"),
        "stop_name": ns.get("name"),
        "stop_lat": ns.get("lat"),
        "stop_lon": ns.get("lon"),
    }


@mcp.tool()
def find_transit_route(origin: str, destination: str,
                       near_lat: float | None = None,
                       near_lon: float | None = None) -> str:
    """Find the shortest transit route between two locations using Neo4j graph traversal.
    Accepts stop names, building names, or POI names — resolves them to the nearest stop automatically.
    Returns step-by-step directions with lines, transfer points, and walking segments.

    Use this tool for ANY transit routing question instead of manually writing NEXT_STOP path queries.

    Disambiguation: if a name matches several distinct places (a chain like
    "Lidl" / "World of Pizza"), returns ``ambiguous: true`` with the candidate
    list and a `which` field (origin/destination) so you ASK which one — it will
    NOT guess which branch the user starts from. Pass the user's location as
    `near_lat`/`near_lon` to auto-pick the nearest branch; an ambiguous
    DESTINATION is auto-picked as the branch nearest the origin.

    Args:
        origin: Starting location, e.g. 'Building 3', 'Hauptbahnhof', 'mensa', 'ENERCON'
        destination: Destination, e.g. 'Opernhaus', 'Alter Markt', 'IMIQ', 'Building 22'
        near_lat, near_lon: optional user location, to pick the nearest branch.

    Returns:
        JSON with route segments, transfer points, total stops, and walking
        distances, OR ``{"ambiguous": true, "which", "candidates": [...]}``.
    """
    _GERMAN_NAME_HINT = ("If this is an English description of a place, retry "
                         "with its German name (e.g. \"Foreigners' Office\" -> "
                         "'Ausländerbehörde').")
    user_anchor = _anchor(near_lat, near_lon)

    # Origin — anchored ONLY on the user; ambiguous with no user location → ask.
    o_status, origin_r = _resolve_transit_decision(origin, user_anchor)
    if o_status == "ambiguous":
        return json.dumps({"success": False, "ambiguous": True, "which": "origin",
                           "place": origin,
                           "candidates": [_candidate_payload(c) for c in origin_r],
                           "message": (f"Several distinct places match the origin '{origin}'. "
                                       "Ask the user which one — pins are on the map.")})
    if o_status == "none":
        return json.dumps({"error": f"Could not resolve origin '{origin}' to a transit stop.",
                           "hint": _GERMAN_NAME_HINT})

    # Destination — anchored on the user, else the resolved origin (nearest to start).
    dest_anchor = user_anchor or (origin_r["lat"], origin_r["lon"])
    d_status, dest_r = _resolve_transit_decision(destination, dest_anchor)
    if d_status == "ambiguous":
        return json.dumps({"success": False, "ambiguous": True, "which": "destination",
                           "place": destination,
                           "candidates": [_candidate_payload(c) for c in dest_r],
                           "message": (f"Several distinct places match the destination "
                                       f"'{destination}'. Ask the user which one — pins are on the map.")})
    if d_status == "none":
        return json.dumps({"error": f"Could not resolve destination '{destination}' to a transit stop.",
                           "hint": _GERMAN_NAME_HINT})

    o = origin_r["nearest_stop"]["name"]
    d = dest_r["nearest_stop"]["name"]

    if o == d:
        return json.dumps({
            "origin": _transit_endpoint(origin, origin_r),
            "destination": _transit_endpoint(destination, dest_r),
            "message": "Origin and destination resolve to the same stop.",
        })

    best_path = _find_best_path(o, d)

    if best_path is None:
        return json.dumps({
            "error": "No route found.",
            "origin_stop": o,
            "dest_stop": d,
        })

    stop_names = best_path["stops"]
    lines_used = best_path["lines"]

    segments = []
    transfers = []
    seg_start = 0

    for i in range(len(lines_used)):
        is_last = (i == len(lines_used) - 1)
        line_changes = not is_last and lines_used[i] != lines_used[i + 1]

        if is_last or line_changes:
            seg_line = lines_used[i]
            seg_to = stop_names[i + 1]

            # Look up the terminal stop in the travel direction
            direction = _get_line_direction(seg_line, stop_names[seg_start], seg_to)

            segments.append({
                "line": seg_line,
                "from": stop_names[seg_start],
                "to": seg_to,
                "direction": direction,
                "stops": stop_names[seg_start:i + 2],
                "num_stops": i + 2 - seg_start,
            })
            if line_changes:
                transfers.append(stop_names[i + 1])
            seg_start = i + 1

    result = {
        "origin": _transit_endpoint(origin, origin_r),
        "destination": _transit_endpoint(destination, dest_r),
        "total_stops": len(stop_names),
        "total_transfers": len(transfers),
        "transfer_points": transfers,
        "segments": segments,
        "all_stops": stop_names,
    }

    return json.dumps(result, indent=2, default=str)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    mcp.run(transport="stdio")
