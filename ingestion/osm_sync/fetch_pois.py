"""
Broadened POI ingestion for Magdeburg — Overpass -> Neo4j (no osmnx/geopandas).

Widens coverage far beyond the original food+grocery slice: all shops, all
tourism/sights, historic sites, leisure, and civic amenities. Keeps the rich OSM
tags the assistant already uses (brand, cuisine, opening_hours, addr_*, wikidata,
website, phone, wheelchair) and adds a coarse `category`
(food/shopping/sights/leisure/services) so discovery can filter at the right
altitude.

MERGE on osm_id — hand-curated POIs (no osm_id) are NEVER touched; re-running is
idempotent. Every node it writes is tagged `ingest_batch` for easy rollback.
Default target is STAGING; pass --production to write to Aura.

    python ingestion/osm_sync/fetch_pois.py --dry-run     # counts only, no DB
    python ingestion/osm_sync/fetch_pois.py               # write to STAGING
    python ingestion/osm_sync/fetch_pois.py --production  # write to Aura (asks)
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
from _overpass import element_point, osm_id, overpass  # noqa: E402

# (category, Overpass selector). Wildcards ([shop]/[tourism]/[historic]) grab
# every value; amenity/leisure are value-filtered to skip noise (benches, ...).
_SELECTORS = [
    ("food",     '[amenity~"^(cafe|restaurant|fast_food|bar|pub|biergarten|ice_cream|food_court)$"]'),
    ("shopping", '[shop]'),
    ("sights",   '[tourism]'),
    ("sights",   '[historic]'),
    ("sights",   '[amenity~"^(theatre|cinema|arts_centre|place_of_worship|library|townhall|fountain)$"]'),
    ("leisure",  '[leisure~"^(park|garden|sports_centre|fitness_centre|stadium|swimming_pool|playground|nature_reserve)$"]'),
    ("services", '[amenity~"^(pharmacy|hospital|clinic|doctors|dentist|veterinary|fuel|charging_station|bank|atm|post_office|police|fire_station|marketplace|community_centre|courthouse)$"]'),
]

_FOOD_AMENITY = {"cafe", "restaurant", "fast_food", "bar", "pub", "biergarten", "ice_cream", "food_court"}
_SIGHT_AMENITY = {"theatre", "cinema", "arts_centre", "place_of_worship", "library", "townhall", "fountain"}
_SERVICE_AMENITY = {"pharmacy", "hospital", "clinic", "doctors", "dentist", "veterinary", "fuel",
                    "charging_station", "bank", "atm", "post_office", "police", "fire_station",
                    "marketplace", "community_centre", "courthouse"}
_LEISURE_VALUES = {"park", "garden", "sports_centre", "fitness_centre", "stadium",
                   "swimming_pool", "playground", "nature_reserve"}

# OSM tags worth keeping on the node (':' flattened to '_'). amenity/shop/tourism
# are handled separately as osm_* provenance fields.
_KEEP_TAGS = [
    "cuisine", "opening_hours", "phone", "website", "email", "operator", "brand",
    "wheelchair", "outdoor_seating", "takeaway", "delivery",
    "addr:street", "addr:housenumber", "addr:postcode", "addr:city",
    "historic", "leisure", "religion", "denomination", "stars", "description",
    "wikidata", "wikipedia",
]


def _pretty(v: str) -> str:
    return "".join(w.capitalize() for w in str(v).split("_"))


def _classify(tags: dict) -> tuple:
    """(category, type) for an element, by tag priority. `type` is a clean label
    derived from the OSM value (e.g. shop=bakery -> 'Bakery')."""
    am, shop = tags.get("amenity"), tags.get("shop")
    tour, hist, leis = tags.get("tourism"), tags.get("historic"), tags.get("leisure")
    if tour:
        return ("sights", _pretty(tour))
    if hist:
        return ("sights", _pretty(hist))
    if am in _FOOD_AMENITY:
        return ("food", _pretty(am))
    if am in _SIGHT_AMENITY:
        return ("sights", _pretty(am))
    if shop:
        return ("shopping", "Shop" if shop in ("yes", "no") else _pretty(shop))
    if leis in _LEISURE_VALUES:
        return ("leisure", _pretty(leis))
    if am in _SERVICE_AMENITY:
        return ("services", _pretty(am))
    if am:
        return ("services", _pretty(am))
    return ("other", "Other")


def _record(el: dict) -> dict | None:
    tags = el.get("tags") or {}
    name = tags.get("name")
    if not name:
        return None
    pt = element_point(el)
    if pt is None:
        return None
    lat, lon = pt
    category, ptype = _classify(tags)
    rec = {
        "osm_id": osm_id(el),
        "name": str(name),
        "type": ptype,
        "category": category,
        "latitude": float(lat),
        "longitude": float(lon),
        "osm_amenity": tags.get("amenity"),
        "osm_shop": tags.get("shop"),
        "osm_tourism": tags.get("tourism"),
    }
    for t in _KEEP_TAGS:
        v = tags.get(t)
        if v not in (None, ""):
            rec[t.replace(":", "_")] = v
    return rec


def fetch_records() -> list[dict]:
    union = "".join(f"nwr(area.a){sel};" for _, sel in _SELECTORS)
    print("Querying Overpass (all shops/tourism/historic/leisure/civic in Magdeburg)...")
    data = overpass(f"({union});out center tags;")
    elements = data.get("elements", [])
    print(f"  raw elements: {len(elements):,}")
    seen, records = set(), []
    skipped = Counter()
    for el in elements:
        oid = osm_id(el)
        if oid in seen:
            continue
        rec = _record(el)
        if rec is None:
            skipped["no_name_or_point"] += 1
            continue
        seen.add(oid)
        records.append(rec)
    if skipped:
        print(f"  skipped: {dict(skipped)}")
    return records


# ON MATCH only refreshes PURE OSM nodes (source='osm'). Hand-curated POIs (no
# osm_id, never matched here) and merged nodes (source IS NULL, kept manual
# identity) are protected — they only get the sync timestamp + rollback marker,
# never an OSM name/type overwrite.
_MERGE = """
UNWIND $batch AS row
MERGE (p:POI {osm_id: row.osm_id})
ON CREATE SET p = row, p.source = 'osm', p.last_osm_sync = $ts, p.ingest_batch = $batch_id
ON MATCH SET p += CASE WHEN p.source = 'osm' THEN row ELSE {} END,
    p.last_osm_sync = $ts, p.ingest_batch = $batch_id
"""


def push_to_neo4j(records: list[dict], production: bool) -> None:
    from neo4j import GraphDatabase

    prefix = "NEO4J" if production else "NEO4J_STAGING"
    label = "PRODUCTION (Aura)" if production else "STAGING"
    uri = os.getenv(f"{prefix}_URI", "")
    user = os.getenv(f"{prefix}_USERNAME", "neo4j")
    password = os.getenv(f"{prefix}_PASSWORD", "")
    database = os.getenv(f"{prefix}_DATABASE", "neo4j")
    if not uri or not password:
        raise SystemExit(f"{prefix}_URI / {prefix}_PASSWORD missing in .env")

    print(f"  target: {label} ({uri})")
    if production:
        confirm = input("Writes POIs to PRODUCTION Aura. Type 'yes' to proceed: ").strip().lower()
        if confirm != "yes":
            raise SystemExit("aborted")

    ts = datetime.now(timezone.utc).isoformat()
    batch_id = f"coverage-{ts[:19]}"
    driver = GraphDatabase.driver(uri, auth=(user, password),
                                  max_connection_lifetime=300, keep_alive=True,
                                  liveness_check_timeout=60)
    BATCH = 500
    with driver:
        with driver.session(database=database) as session:
            for i in range(0, len(records), BATCH):
                session.run(_MERGE, batch=records[i:i + BATCH], ts=ts, batch_id=batch_id)
                print(f"  wrote {min(i + BATCH, len(records)):,}/{len(records):,}")
    print(f"  done, ingest_batch={batch_id}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="fetch + report counts, no DB write")
    ap.add_argument("--production", action="store_true", help="write to Aura instead of staging")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")

    records = fetch_records()
    print(f"\n  named POIs: {len(records):,}")
    cats = Counter(r["category"] for r in records)
    print(f"  by category: {dict(cats.most_common())}")
    types = Counter(r["type"] for r in records)
    print("  top 20 types:")
    for t, n in types.most_common(20):
        print(f"    {n:>5} {t}")
    enriched = {k: sum(1 for r in records if r.get(k)) for k in ("brand", "cuisine", "opening_hours", "wikidata", "addr_street")}
    print(f"  enrichment present: {enriched}")

    if args.dry_run:
        print("\nDry run — no Neo4j write.")
        return
    print()
    push_to_neo4j(records, production=args.production)


if __name__ == "__main__":
    main()
