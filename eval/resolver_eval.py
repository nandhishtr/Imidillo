"""
Resolver regression eval — guards the canonical place resolver
(mcp_servers/_place_resolver.py::resolve_place) against the "mensa" class of
bug, where a place name resolved to different nodes across tools, or an
OSM-imported decoy outranked the curated campus node.

Deterministic and LLM-free: it calls resolve_place() against the live Neo4j
graph and asserts the resolved node, its coordinates, and its nearest stop.
This is the layer find_transit_route and the routing tools now share, so
locking it down catches the regression at the source — no agent run, no API
key, no flakiness.

Run:
    python eval/resolver_eval.py            # all cases
    python eval/resolver_eval.py -v         # also print resolved payloads

Exit codes (CI-friendly):
    0  = all assertions passed, OR skipped because Neo4j was unreachable
         (Aura Free auto-pauses — infra down must not fail the build)
    1  = at least one assertion failed (a real regression)
"""

from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The graph still uses the older `CALL { ... }` subquery form, which Neo4j 5/6
# flags with a benign DEPRECATION notification per query. Silence the driver's
# notification logging so the eval output stays readable in CI.
logging.getLogger("neo4j").setLevel(logging.ERROR)

from neo4j import GraphDatabase, Query  # noqa: E402

from config import (  # noqa: E402
    NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE,
)
from mcp_servers._place_resolver import resolve_place  # noqa: E402


# ---------------------------------------------------------------------------
# Cases. Each asserts whatever fields it specifies (all optional):
#   name_contains : substring expected in the resolved node name (lowercased)
#   expect_stop   : exact nearest-stop name
#   expect_type   : 'Stop' | 'Building' | 'POI'
#   lat_range/lon_range : (min, max) box the resolved coords must fall in
#                         — the decoy guard (campus Mensa vs OSM "Mensa" 2 km off)
# ---------------------------------------------------------------------------
CASES = [
    {
        "query": "mensa",
        "name_contains": "mensa",
        "expect_stop": "Magdeburg Universitätsbibliothek",
        "lat_range": (52.138, 52.141),
        "lon_range": (11.645, 11.649),
        "why": "campus Mensa, NOT the OSM building named 'Mensa' ~2 km away",
    },
    {
        "query": "Mensa Uni",
        "name_contains": "mensa",
        "expect_stop": "Magdeburg Universitätsbibliothek",
        "why": "explicit campus-mensa name",
    },
    {
        "query": "Building 27",
        "name_contains": "mensa",
        "expect_stop": "Magdeburg Universitätsbibliothek",
        "why": "the campus Mensa building by number",
    },
    {
        "query": "Building 3",
        "name_contains": "electrical engineering",
        "expect_stop": "Magdeburg Universität",
        "why": "numbered campus building resolves exactly (not Building 30/32)",
    },
    {
        "query": "rektorat",
        "name_contains": "rectorate",
        "why": "German alias resolves to the curated Rectorate (Building 04)",
    },
    {
        "query": "hauptbahnhof",
        "name_contains": "hauptbahnhof",
        "expect_type": "Stop",
        "why": "a transit stop, not a building/POI",
    },
    {
        "query": "hauptbanhof",
        "name_contains": "hauptbahnhof",
        "expect_type": "Stop",
        "why": "1-char typo still clears the similarity gate",
    },
    {
        "query": "Wasserturm Salbke",
        "name_contains": "wasserturm",
        "expect_type": "Building",
        "why": "exact OSM name match beats a fuzzier curated stop (band rule)",
    },
]

# Off-graph names that MUST return None so callers fall through to the
# city-wide geocoder. Guards the confidence gate: Lucene fuzzy search always
# returns *something*, and before the gate these resolved to unrelated
# places (e.g. 'Dönerbude am Damm' -> the 'Magdeburg Am Stern' stop).
NEGATIVE_CASES = [
    {"query": "Halberstädter Straße 88",
     "why": "house-number address — stop on the same street must NOT match"},
    {"query": "Stadtfeld Ost", "why": "district name, not in the graph"},
    {"query": "Dönerbude am Damm", "why": "made-up POI; 'am' must not carry the score"},
    {"query": "Aldi Olvenstedter Graseweg",
     "why": "branch not in graph; must not match an arbitrary other Aldi"},
    {"query": "Hundeschule Magdeburg",
     "why": "'hundeschule' must not fuzzy-match the 'Hochschule' stop"},
    {"query": "Zimmer 305", "why": "room number — nothing to resolve"},
]

# Synonyms that MUST all resolve to the same stop (the heart of the #1 bug:
# "where is X" and "how do I get to X" must agree).
CONSISTENCY_GROUPS = [
    {
        "label": "campus Mensa synonyms agree on one stop",
        "queries": ["mensa", "Mensa Uni", "Building 27"],
        "expect_stop": "Magdeburg Universitätsbibliothek",
    },
]


def _make_run_read(driver):
    def run_read(cypher: str, params: dict | None = None, timeout: float = 8.0) -> list:
        with driver.session(database=NEO4J_DATABASE) as session:
            result = session.run(Query(cypher, timeout=timeout), parameters=params or {})
            return [dict(record) for record in result]
    return run_read


def _check_case(run_read, case: dict, verbose: bool) -> tuple[bool, list[str]]:
    query = case["query"]
    hit = resolve_place(run_read, query)
    if verbose:
        print(f"    resolved({query!r}) = {hit}")
    if not hit:
        return False, [f"resolve_place({query!r}) returned None"]

    errs: list[str] = []
    name = (hit.get("name") or "")
    if "name_contains" in case and case["name_contains"].lower() not in name.lower():
        errs.append(f"name {name!r} does not contain {case['name_contains']!r}")
    if "expect_type" in case and hit.get("type") != case["expect_type"]:
        errs.append(f"type {hit.get('type')!r} != {case['expect_type']!r}")
    if "expect_stop" in case:
        stop = (hit.get("nearest_stop") or {}).get("name")
        if stop != case["expect_stop"]:
            errs.append(f"nearest stop {stop!r} != {case['expect_stop']!r}")
    for axis, key in (("lat", "lat_range"), ("lon", "lon_range")):
        if key in case:
            lo, hi = case[key]
            val = hit.get(axis)
            if val is None or not (lo <= val <= hi):
                errs.append(f"{axis} {val} not in [{lo}, {hi}]")
    return (not errs), errs


def _check_consistency(run_read, group: dict, verbose: bool) -> tuple[bool, list[str]]:
    stops = {}
    for q in group["queries"]:
        hit = resolve_place(run_read, q)
        stops[q] = (hit.get("nearest_stop") or {}).get("name") if hit else None
        if verbose:
            print(f"    {q!r} -> stop {stops[q]!r}")
    distinct = set(stops.values())
    errs: list[str] = []
    if len(distinct) != 1:
        errs.append(f"queries disagree on stop: {stops}")
    elif group.get("expect_stop") and group["expect_stop"] not in distinct:
        errs.append(f"agreed stop {distinct} != expected {group['expect_stop']!r}")
    return (not errs), errs


def main() -> int:
    verbose = "-v" in sys.argv or "--verbose" in sys.argv

    driver = GraphDatabase.driver(
        NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD),
        connection_acquisition_timeout=5.0, connection_timeout=5.0,
    )
    # Connectivity precheck — skip (not fail) if the DB is unreachable, since
    # Aura Free auto-pauses after ~3 idle days.
    try:
        with driver.session(database=NEO4J_DATABASE) as s:
            s.run("RETURN 1").consume()
    except Exception as e:
        print(f"[SKIP] Neo4j unreachable ({type(e).__name__}: {e}); skipping resolver eval.")
        driver.close()
        return 0

    run_read = _make_run_read(driver)
    passed = failed = 0

    print("== Place-resolution cases ==")
    for case in CASES:
        ok, errs = _check_case(run_read, case, verbose)
        if ok:
            passed += 1
            print(f"  [PASS] {case['query']!r:>16}  ({case.get('why', '')})")
        else:
            failed += 1
            print(f"  [FAIL] {case['query']!r:>16}  ({case.get('why', '')})")
            for e in errs:
                print(f"         - {e}")

    print("\n== Negative cases (must fall through to the geocoder) ==")
    for case in NEGATIVE_CASES:
        hit = resolve_place(run_read, case["query"])
        if hit is None:
            passed += 1
            print(f"  [PASS] {case['query']!r:>28}  ({case.get('why', '')})")
        else:
            failed += 1
            print(f"  [FAIL] {case['query']!r:>28}  resolved to "
                  f"{hit.get('name')!r} ({hit.get('type')}, "
                  f"conf={hit.get('confidence')}) — expected None")

    print("\n== Consistency groups ==")
    for group in CONSISTENCY_GROUPS:
        ok, errs = _check_consistency(run_read, group, verbose)
        if ok:
            passed += 1
            print(f"  [PASS] {group['label']}")
        else:
            failed += 1
            print(f"  [FAIL] {group['label']}")
            for e in errs:
                print(f"         - {e}")

    driver.close()
    print(f"\n{passed} passed, {failed} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
