"""
Chain / brand grouping for Magdeburg POIs — Neo4j only (no Overpass).

Groups POIs that share a brand into first-class :Brand nodes, so the system
KNOWS a name is multi-branch ("Lidl" -> 10 branches) instead of rediscovering it
by dedup each time. Grouping key = brand tag, else operator, else normalized
name (folds umlauts/case/punctuation), so 'World of Pizza' groups by name even
without a brand tag.

Writes :Brand {key, name, branch_count}, (:POI)-[:BRANCH_OF]->(:Brand), and sets
`brand_key` + `is_chain=true` on members. Idempotent. Reads/writes the chosen
target — run AFTER fetch_pois on the same DB. Default STAGING; --production -> Aura.

    python ingestion/osm_sync/group_brands.py --dry-run     # report chains, no write
    python ingestion/osm_sync/group_brands.py               # STAGING
    python ingestion/osm_sync/group_brands.py --production  # Aura (asks)
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv


def _norm(s: str) -> str:
    s = (s or "").lower()
    s = s.replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return " ".join(s.split())


def _display_name(members: list[dict]) -> str:
    brands = [m["brand"] for m in members if m.get("brand")]
    if brands:
        return Counter(brands).most_common(1)[0][0]
    return Counter(m["name"] for m in members).most_common(1)[0][0]


_UPSERT_BRAND = """
UNWIND $batch AS row
MERGE (b:Brand {key: row.key})
SET b.name = row.name, b.branch_count = row.count, b.source = 'osm'
WITH b, row
UNWIND row.nids AS nid
MATCH (p:POI) WHERE id(p) = nid
SET p.brand_key = row.key, p.is_chain = true
MERGE (p)-[:BRANCH_OF]->(b)
"""


def _driver(production: bool):
    from neo4j import GraphDatabase
    prefix = "NEO4J" if production else "NEO4J_STAGING"
    uri = os.getenv(f"{prefix}_URI", "")
    user = os.getenv(f"{prefix}_USERNAME", "neo4j")
    password = os.getenv(f"{prefix}_PASSWORD", "")
    database = os.getenv(f"{prefix}_DATABASE", "neo4j")
    if not uri or not password:
        raise SystemExit(f"{prefix}_URI / {prefix}_PASSWORD missing in .env")
    drv = GraphDatabase.driver(uri, auth=(user, password), max_connection_lifetime=300,
                               keep_alive=True, liveness_check_timeout=60)
    return drv, database, ("PRODUCTION (Aura)" if production else "STAGING"), uri


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--production", action="store_true")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")

    driver, database, label, uri = _driver(args.production)
    with driver, driver.session(database=database) as session:
        rows = session.run(
            "MATCH (p:POI) WHERE p.name IS NOT NULL "
            "RETURN id(p) AS nid, p.name AS name, p.brand AS brand, p.operator AS operator"
        ).data()
        print(f"target: {label} ({uri}) — {len(rows):,} POIs")

        groups = defaultdict(list)
        for r in rows:
            key = _norm(r.get("brand") or r.get("operator") or r.get("name"))
            if key:
                groups[key].append(r)
        chains = {k: v for k, v in groups.items() if len(v) >= 2}
        print(f"  chains (>=2 branches): {len(chains)}; "
              f"branches covered: {sum(len(v) for v in chains.values()):,}")
        print("  top 15 chains:")
        for k, v in sorted(chains.items(), key=lambda kv: -len(kv[1]))[:15]:
            print(f"    {len(v):>3}x  {_display_name(v)}")

        if args.dry_run:
            print("\nDry run — no Neo4j write.")
            return
        if args.production and input("Writes Brand nodes to PRODUCTION Aura. Type 'yes': ").strip().lower() != "yes":
            raise SystemExit("aborted")

        batch = [{"key": k, "name": _display_name(v), "count": len(v),
                  "nids": [m["nid"] for m in v]} for k, v in chains.items()]
        for i in range(0, len(batch), 200):
            session.run(_UPSERT_BRAND, batch=batch[i:i + 200])
        print(f"  wrote {len(batch)} :Brand nodes and BRANCH_OF edges")


if __name__ == "__main__":
    main()
