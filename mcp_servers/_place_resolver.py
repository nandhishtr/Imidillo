"""
Canonical place resolver — the SINGLE source of truth for turning a free-text
place name ("mensa", "Building 03", "Hauptbahnhof", "ENERCON") into one graph
node plus its nearest transit stop.

Every tool that needs a location (get_building, find_transit_route,
get_routes_for_places, resolve_place_to_coordinates) resolves through this so a
question and its follow-up anchor to the SAME place. Previously each tool had
its own resolver and they disagreed — e.g. "where is mensa" found the curated
campus Mensa while "how do I get to mensa" matched an unrelated OSM building
literally named "Mensa" 2 km away.

Ranking rule (the fix): CURATED campus nodes (`source IS NULL`) outrank
OSM-imported nodes (`source = 'osm'`). Within a tier, full-text score decides.
This mirrors what get_building already did, generalised to every label.

Driver-agnostic: callers pass their own `run_read(cypher, params, timeout)`
(each MCP server process owns its own Neo4j driver).
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Callable, Optional

RunRead = Callable[..., list]

# Optional import of the shared threshold (pattern used across mcp_servers —
# tolerate absence so the resolver stays importable standalone).
try:
    from services.thresholds import RESOLVER_MIN_SIMILARITY as _MIN_SIMILARITY
except Exception:  # pragma: no cover
    _MIN_SIMILARITY = 0.60

_BUILDING_NUM_RE = re.compile(
    r"^\s*(?:building|geb\.?|geb[aä]ude)\s+0?(\d{1,2})\s*$", re.IGNORECASE
)

# Lucene specials we escape before building the full-text query string.
_LUCENE_SPECIALS = '+-&|!(){}[]^"~*?:\\/'


def _canonical_building_number(search: str) -> Optional[str]:
    """"building 3" / "Gebäude 27" -> canonical "Building 03" (zero-padded)."""
    m = _BUILDING_NUM_RE.match(search or "")
    return f"Building {int(m.group(1)):02d}" if m else None


def _lucene_escape(term: str) -> str:
    out = []
    for ch in term:
        if ch in _LUCENE_SPECIALS:
            out.append("\\")
        out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Name-similarity gate. Lucene full-text with fuzzy (~1) matching essentially
# ALWAYS returns a top hit, even for places that are not in the graph at all —
# the hit just has a weak score. Raw Lucene scores are not comparable across
# queries (they scale with term rarity and length), so instead of a score
# cutoff we compare the QUERY STRING against the candidate's name and aliases:
# deterministic, query-independent, and exactly targets the failure mode
# ("Pizzeria X" silently matching the unrelated "Pizzeria Y" across town).
# Candidates whose best similarity is below RESOLVER_MIN_SIMILARITY are
# rejected, and the caller falls through to the city-wide geocoder.
# ---------------------------------------------------------------------------

def _norm_text(s: str) -> str:
    """Lowercase, fold German umlauts/ß, strip punctuation, collapse spaces."""
    s = (s or "").lower()
    s = (s.replace("ß", "ss").replace("ä", "ae")
          .replace("ö", "oe").replace("ü", "ue"))
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


# The city name carries zero discriminating signal: every stop is prefixed
# "Magdeburg " and users append it to everything, so it inflates junk matches
# ("Hundeschule Magdeburg" vs "Magdeburg Hochschule ...").
_SIM_STOPWORDS = {"magdeburg"}


def name_similarity(query: str, name: str, aliases: list | None = None) -> float:
    """Best similarity in [0, 1] between a query and a node's name/aliases.

    Calibrated against the live graph (2026-06): real in-graph names match at
    >= 0.86 (exact / containment / 1-2 char typos), off-graph junk peaks ~0.6,
    so the 0.70 gate separates them. Signals per candidate string:
      * exact match -> 1.0
      * query contained in candidate -> 0.95 ("mensa" in "Mensa UniCampus");
        the REVERSE direction is deliberately NOT shortcut — "Aldi
        Olvenstedter Graseweg" containing the name "Aldi" must not match an
        arbitrary Aldi elsewhere, the unmatched street tokens count against it
      * whole-string fuzzy ratio
      * single-token queries: best per-token ratio x 0.9 — the discount lets
        true typos through ("hauptbanhof" 0.96 -> 0.86) while different words
        of the same shape stay below the gate ("hundeschule" vs "hochschule"
        0.76 -> 0.69)
      * multi-token queries: length-weighted recall of query tokens in the
        candidate ("computer science building" vs "Faculty of Computer
        Science" ~0.75), so short filler tokens can't carry the score
      * numeric query tokens are MANDATORY — "Halberstädter Straße 88" must
        not match a tram stop that merely shares the street name
    """
    q_tokens = [t for t in _norm_text(query).split() if t not in _SIM_STOPWORDS]
    if not q_tokens:
        return 0.0
    q = " ".join(q_tokens)
    q_nums = {t for t in q_tokens if t.isdigit()}
    best = 0.0
    for cand in [name] + list(aliases or []):
        c_tokens = [t for t in _norm_text(cand if isinstance(cand, str) else "").split()
                    if t not in _SIM_STOPWORDS]
        if not c_tokens:
            continue
        c = " ".join(c_tokens)
        if q == c:
            return 1.0
        if q_nums and not q_nums.issubset(set(c_tokens)):
            continue
        sim = 0.95 if (len(q) >= 4 and q in c) else 0.0
        sim = max(sim, _ratio(q, c))
        if len(q_tokens) == 1:
            sim = max(sim, 0.9 * max((_ratio(q, t) for t in c_tokens), default=0.0))
        else:
            weighted = sum(len(qt) * max((_ratio(qt, t) for t in c_tokens), default=0.0)
                           for qt in q_tokens)
            sim = max(sim, weighted / sum(len(qt) for qt in q_tokens))
        best = max(best, sim)
    return best


# Numbered campus buildings ("Building 27") resolve by exact alias only — the
# curated campus building, never an OSM "Gebäude N" address building. The stop
# is the NEAREST bound stop (ACCESSIBLE_STOP/NEAREST_STOP) or, if none, the
# geographic nearest — picked by real distance so it is deterministic.
_BY_NUMBER_Q = """
    MATCH (b:Building)
    WHERE b.name = $canonical OR $canonical IN COALESCE(b.aliases, [])
    WITH b LIMIT 1
    CALL {
        WITH b
        OPTIONAL MATCH (b)-[:ACCESSIBLE_STOP|NEAREST_STOP]->(sd:Stop)
        WITH b, collect(DISTINCT sd) AS direct
        MATCH (s:Stop)
        WITH s, direct, point.distance(
            point({latitude: b.latitude, longitude: b.longitude}),
            point({latitude: s.latitude, longitude: s.longitude})
        ) AS dist
        WHERE size(direct) = 0 OR s IN direct
        RETURN s AS chosen_stop, dist AS walk_m
        ORDER BY dist LIMIT 1
    }
    RETURN b.name AS entity_name, 'Building' AS entity_type,
           b.latitude AS entity_lat, b.longitude AS entity_lon, b.source AS entity_source,
           chosen_stop.name AS stop_name, chosen_stop.latitude AS stop_lat,
           chosen_stop.longitude AS stop_lon, round(walk_m) AS walk_meters,
           b.district AS district
"""

# Single-shot UNION across the 3 entry points (stop / building / POI). Each
# branch matches via its full-text index and projects a shared schema, then a
# nested CALL picks the bound transit stop (ACCESSIBLE_STOP / NEAREST_STOP) or
# the geographic nearest as fallback. `is_curated` (0 for curated, 1 for OSM)
# is the PRIMARY sort key so curated campus data wins ties against OSM decoys.
_UNION_Q = """
    CALL {
        // Branch 1: direct stop match
        CALL db.index.fulltext.queryNodes("stop_fts", $fts) YIELD node AS s, score
        RETURN s.name AS entity_name, 'Stop' AS entity_type,
               COALESCE(s.aliases, []) AS entity_aliases,
               s.latitude AS entity_lat, s.longitude AS entity_lon,
               (CASE WHEN s.source IS NULL THEN 0 ELSE 1 END) AS is_curated,
               s.name AS stop_name, s.latitude AS stop_lat, s.longitude AS stop_lon,
               0 AS walk_meters, score * 2 AS score, 1 AS priority, s.district AS district
        LIMIT 3

        UNION

        // Branch 2: building_fts -> nearest bound stop (ACCESSIBLE_STOP/
        // NEAREST_STOP) or geographic nearest, picked by real distance.
        CALL db.index.fulltext.queryNodes("building_fts", $fts) YIELD node AS b, score
        WITH b, score LIMIT 3
        CALL {
            WITH b
            OPTIONAL MATCH (b)-[:ACCESSIBLE_STOP|NEAREST_STOP]->(sd:Stop)
            WITH b, collect(DISTINCT sd) AS direct
            MATCH (s:Stop)
            WITH s, direct, point.distance(
                point({latitude: b.latitude, longitude: b.longitude}),
                point({latitude: s.latitude, longitude: s.longitude})
            ) AS dist
            WHERE size(direct) = 0 OR s IN direct
            RETURN s AS chosen_stop, dist AS walk_m
            ORDER BY dist LIMIT 1
        }
        RETURN b.name AS entity_name, 'Building' AS entity_type,
               COALESCE(b.aliases, []) AS entity_aliases,
               b.latitude AS entity_lat, b.longitude AS entity_lon,
               (CASE WHEN b.source IS NULL THEN 0 ELSE 1 END) AS is_curated,
               chosen_stop.name AS stop_name, chosen_stop.latitude AS stop_lat,
               chosen_stop.longitude AS stop_lon,
               round(walk_m) AS walk_meters, score AS score, 2 AS priority, b.district AS district

        UNION

        // Branch 3: poi_fts -> nearest bound stop (NEAREST_STOP/ACCESSIBLE_STOP)
        // or geographic nearest, picked by real distance.
        CALL db.index.fulltext.queryNodes("poi_fts", $fts) YIELD node AS p, score
        WITH p, score LIMIT 3
        CALL {
            WITH p
            OPTIONAL MATCH (p)-[:NEAREST_STOP|ACCESSIBLE_STOP]->(sd:Stop)
            WITH p, collect(DISTINCT sd) AS direct
            MATCH (s:Stop)
            WITH s, direct, point.distance(
                point({latitude: p.latitude, longitude: p.longitude}),
                point({latitude: s.latitude, longitude: s.longitude})
            ) AS dist
            WHERE size(direct) = 0 OR s IN direct
            RETURN s AS chosen_stop, dist AS walk_m
            ORDER BY dist LIMIT 1
        }
        RETURN p.name AS entity_name, 'POI' AS entity_type,
               COALESCE(p.aliases, []) AS entity_aliases,
               p.latitude AS entity_lat, p.longitude AS entity_lon,
               (CASE WHEN p.source IS NULL THEN 0 ELSE 1 END) AS is_curated,
               chosen_stop.name AS stop_name, chosen_stop.latitude AS stop_lat,
               chosen_stop.longitude AS stop_lon,
               round(walk_m) AS walk_meters, score AS score, 3 AS priority, p.district AS district
    }
    RETURN entity_name, entity_type, entity_aliases, entity_lat, entity_lon,
           is_curated, stop_name, stop_lat, stop_lon, walk_meters, score, priority, district
    ORDER BY is_curated ASC, score DESC, priority ASC, walk_meters ASC
    LIMIT 5
"""

# Last resort if the full-text indexes are unavailable: plain CONTAINS on stops.
_FALLBACK_Q = """
    MATCH (s:Stop)
    WHERE toLower(s.name) CONTAINS $search
    RETURN s.name AS entity_name, 'Stop' AS entity_type,
           s.latitude AS entity_lat, s.longitude AS entity_lon,
           s.name AS stop_name, s.latitude AS stop_lat, s.longitude AS stop_lon,
           0 AS walk_meters, s.district AS district
    ORDER BY size(s.name)
    LIMIT 1
"""


def _shape(row: dict, confidence: float | None = None) -> dict:
    """Normalise a result row into the canonical resolver shape."""
    shaped = {
        "name": row.get("entity_name"),
        "type": row.get("entity_type"),
        "lat": row.get("entity_lat"),
        "lon": row.get("entity_lon"),
        "district": row.get("district"),
        "nearest_stop": {
            "name": row.get("stop_name"),
            "lat": row.get("stop_lat"),
            "lon": row.get("stop_lon"),
            "walk_m": row.get("walk_meters") or 0,
        },
    }
    if confidence is not None:
        shaped["confidence"] = round(confidence, 3)
    # Curated campus node (source IS NULL → is_curated 0) vs OSM import. Internal
    # only (underscore) — tools build their own payloads and don't leak this.
    shaped["_curated"] = (row.get("is_curated") == 0)
    return shaped


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in metres; +inf on bad input (so it never picks)."""
    from math import radians, sin, cos, asin, sqrt
    try:
        rlat1, rlat2 = radians(float(lat1)), radians(float(lat2))
        dlat = rlat2 - rlat1
        dlon = radians(float(lon2) - float(lon1))
        a = sin(dlat / 2) ** 2 + cos(rlat1) * cos(rlat2) * sin(dlon / 2) ** 2
        return 2 * 6_371_000.0 * asin(sqrt(a))
    except (TypeError, ValueError):
        return float("inf")


def _same_place(a: dict, b: dict) -> bool:
    """True when two candidates are the SAME physical place, not two branches of
    a chain. Two hits matching the SAME query that sit on top of each other are
    one place — a POI and the building it's inside, or a curated node and its
    OSM duplicate (their names/types may differ: "Mensa Uni" vs "Mensa / Sports
    Center"). Genuine chain branches sit far apart and survive as distinct."""
    la, lo, lb, lob = a.get("lat"), a.get("lon"), b.get("lat"), b.get("lon")
    if None in (la, lo, lb, lob):
        return False
    d = _haversine_m(la, lo, lb, lob)
    if d > 45.0:           # > ~45 m apart → distinct places (chain branches)
        return False
    if d <= 30.0:          # basically the same spot → same place, even if names differ
        return True
    na, nb = _norm_text(a.get("name") or ""), _norm_text(b.get("name") or "")
    return bool(na) and (na == nb or na in nb or nb in na or _ratio(na, nb) >= 0.70)


def resolve_place_candidates(run_read: RunRead, query: str, limit: int = 6) -> list[dict]:
    """All DISTINCT in-graph matches for a query, ranked, deduplicated to
    physical places: a curated node and its co-located OSM duplicate collapse to
    one, while genuine chain branches (different locations) are kept separate.
    Empty list when nothing passes the similarity gate.

    Each item is the canonical `resolve_place` shape plus its `confidence`. This
    is the disambiguation-aware primitive; `resolve_place` returns just the top.
    """
    search = (query or "").strip()
    if not search:
        return []

    canonical = _canonical_building_number(search)
    if canonical:
        rows = run_read(_BY_NUMBER_Q, {"canonical": canonical}, 6.0)
        # Numbered campus buildings are unique — exact alias, no ambiguity.
        return [_shape(rows[0], confidence=1.0)] if rows else []

    escaped = _lucene_escape(search)
    fts_q = f"{escaped}~1 {escaped}^2" if len(search) >= 4 else escaped
    rows = run_read(_UNION_Q, {"fts": fts_q}, 6.0)
    passing: list[tuple] = []
    for idx, row in enumerate(rows or []):
        sim = name_similarity(search, row.get("entity_name") or "",
                              row.get("entity_aliases") or [])
        if sim >= _MIN_SIMILARITY:
            # Near-exact matches (>= 0.9) beat everything; within a band curated
            # first (the "mensa" decoy rule), then the query's original ranking.
            low_band = sim < 0.9
            passing.append((low_band, row.get("is_curated", 1),
                            -sim if low_band else 0.0, idx, row, sim))
    if not passing:
        # Plain substring on stop names (high precision; also the only path
        # when the full-text indexes are unavailable).
        rows = run_read(_FALLBACK_Q, {"search": search.lower()}, 6.0)
        return [_shape(rows[0])] if rows else []

    passing.sort(key=lambda t: t[:4])
    distinct: list[dict] = []
    for _, _, _, _, row, sim in passing:
        shaped = _shape(row, confidence=sim)
        if any(_same_place(shaped, kept) for kept in distinct):
            continue   # a duplicate of a place already kept (curated+OSM)
        distinct.append(shaped)
        if len(distinct) >= limit:
            break
    return distinct


def resolve_place(run_read: RunRead, query: str) -> Optional[dict]:
    """Resolve a place name to the single best canonical node + its nearest stop.

    Back-compat single-result entry point. Callers that want to disambiguate
    between chain branches should use `resolve_place_candidates` + `decide_place`.

    Returns ``{name, type, lat, lon, nearest_stop: {...}, confidence}`` or
    ``None`` when nothing matches confidently (caller may then geocode).
    """
    candidates = resolve_place_candidates(run_read, query)
    return candidates[0] if candidates else None


def decide_place(candidates: list[dict], anchor: Optional[tuple] = None,
                 ask_margin_m: float = 400.0, top_n: int = 4) -> dict:
    """Turn a distinct-candidate list into a resolution decision.

    - 0 candidates        -> ``{"status": "none"}``
    - 1 candidate         -> ``{"status": "resolved", "place": <it>}``
    - >1, anchor decides  -> ``{"status": "resolved", "place": <nearest>}`` when
                             the nearest beats the runner-up by `ask_margin_m`
    - >1, still ambiguous -> ``{"status": "ambiguous", "candidates": [...top_n]}``

    `anchor` is an ``(lat, lon)`` reference — the user's location, or the OTHER
    endpoint of a route — used to auto-pick the nearest branch when one clearly
    wins. Ambiguous candidates carry `distance_m` to the anchor (when given) so
    the caller can phrase "the one 300 m away vs the one across town".
    """
    if not candidates:
        return {"status": "none"}
    if len(candidates) == 1:
        return {"status": "resolved", "place": candidates[0]}

    # 1) A spatial anchor (the user, or the route's other endpoint) decides when
    #    one branch is clearly the nearest — beating the runner-up by the margin.
    if anchor and all(c.get("lat") is not None and c.get("lon") is not None
                      for c in candidates):
        ranked = sorted(candidates,
                        key=lambda c: _haversine_m(anchor[0], anchor[1], c["lat"], c["lon"]))
        d0 = _haversine_m(anchor[0], anchor[1], ranked[0]["lat"], ranked[0]["lon"])
        d1 = _haversine_m(anchor[0], anchor[1], ranked[1]["lat"], ranked[1]["lon"])
        if (d1 - d0) >= ask_margin_m:
            pick = dict(ranked[0])
            pick["distance_m"] = round(d0)
            pick["picked_by"] = "nearest"
            return {"status": "resolved", "place": pick}

    # 2) Genuinely ambiguous → ask. When several CURATED peers exist (the two
    #    Hauptbahnhof platforms, the two campus Mensas) offer those rather than
    #    padding the list with OSM also-rans that merely contain the query word.
    #    A lone curated branch does NOT win — being hand-added doesn't make one
    #    Lidl "the" Lidl; that's what the spatial anchor in (1) is for.
    curated = [c for c in candidates
               if c.get("_curated") and (c.get("confidence") or 0) >= 0.9]
    pool = curated if len(curated) >= 2 else candidates
    out = []
    for c in pool[:top_n]:
        cc = dict(c)
        if anchor and c.get("lat") is not None and c.get("lon") is not None:
            cc["distance_m"] = round(_haversine_m(anchor[0], anchor[1], c["lat"], c["lon"]))
        out.append(cc)
    return {"status": "ambiguous", "candidates": out}
