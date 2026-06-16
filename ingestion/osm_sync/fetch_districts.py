"""
District / Stadtteil tagging for Magdeburg — Overpass + shapely -> Neo4j.

Fetches Magdeburg's Stadtteil boundaries (admin_level 10), rebuilds each polygon
from its OSM relation members with shapely, creates :District nodes, and tags
every POI / Building / Stop with the `district` it falls in (point-in-polygon via
a shapely STRtree). This is what lets disambiguation say "the one in Stadtfeld vs
Sudenburg" instead of bare coordinates.

Idempotent: re-running re-tags from scratch. Default target STAGING; --production
writes to Aura.

    python ingestion/osm_sync/fetch_districts.py --dry-run    # fetch + PIP self-test, no DB
    python ingestion/osm_sync/fetch_districts.py              # STAGING
    python ingestion/osm_sync/fetch_districts.py --production # Aura (asks)
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dotenv import load_dotenv
from shapely import STRtree
from shapely.geometry import LineString, Point
from shapely.ops import polygonize, unary_union
from _overpass import overpass  # noqa: E402

# A few known points to sanity-check the polygons in --dry-run (no DB needed).
_PROBE_POINTS = [
    ("World of Pizza (Maxim-Gorki-Str.)", 52.13097, 11.61341),
    ("World of Pizza (SW)", 52.10930, 11.59789),
    ("Hauptbahnhof", 52.13044, 11.62652),
    ("Faculty of Computer Science", 52.13882, 11.64568),
    ("Allee-Center", 52.13084, 11.63709),
]


def fetch_districts() -> list[dict]:
    """[{name, geom (shapely polygon), admin_level}] for Magdeburg Stadtteile.

    Rebuilds each boundary relation's polygon from its outer member ways with
    shapely.polygonize (handles ring assembly), so no multipolygon math by hand."""
    print("Querying Overpass for Stadtteil boundaries (admin_level=10)...")
    data = overpass('rel(area.a)["boundary"="administrative"]["admin_level"="10"];out geom;')
    out = []
    for rel in data.get("elements", []):
        if rel.get("type") != "relation":
            continue
        name = (rel.get("tags") or {}).get("name")
        if not name:
            continue
        lines = []
        for m in rel.get("members", []):
            if m.get("type") == "way" and m.get("role") in ("outer", ""):
                pts = [(p["lon"], p["lat"]) for p in (m.get("geometry") or [])
                       if "lat" in p and "lon" in p]
                if len(pts) >= 2:
                    lines.append(LineString(pts))
        if not lines:
            continue
        polys = list(polygonize(lines))
        if not polys:
            continue
        geom = unary_union(polys)
        out.append({"name": name, "geom": geom,
                    "admin_level": (rel.get("tags") or {}).get("admin_level")})
    print(f"  built {len(out)} district polygons")
    return out


def make_locator(districts: list[dict]):
    """Return district_for(lat, lon) -> name | None, backed by a shapely STRtree."""
    geoms = [d["geom"] for d in districts]
    tree = STRtree(geoms)

    def district_for(lat, lon):
        pt = Point(lon, lat)  # shapely is (x=lon, y=lat)
        for idx in tree.query(pt):
            if districts[int(idx)]["geom"].contains(pt):
                return districts[int(idx)]["name"]
        return None

    return district_for


_UPSERT_DISTRICT = """
UNWIND $batch AS row
MERGE (d:District {name: row.name})
SET d.admin_level = row.admin_level, d.geometry_wkt = row.wkt,
    d.centroid_lat = row.clat, d.centroid_lon = row.clon, d.source = 'osm'
"""

_TAG_NODES = """
UNWIND $batch AS row
MATCH (n) WHERE id(n) = row.nid
SET n.district = row.district
"""


def write_to_neo4j(districts: list[dict], production: bool) -> None:
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
        if input("Tags PRODUCTION Aura with districts. Type 'yes': ").strip().lower() != "yes":
            raise SystemExit("aborted")

    locator = make_locator(districts)
    driver = GraphDatabase.driver(uri, auth=(user, password),
                                  max_connection_lifetime=300, keep_alive=True,
                                  liveness_check_timeout=60)
    with driver, driver.session(database=database) as session:
        # 1. Upsert :District nodes.
        drows = [{"name": d["name"], "admin_level": d["admin_level"],
                  "wkt": d["geom"].wkt, "clat": d["geom"].centroid.y,
                  "clon": d["geom"].centroid.x} for d in districts]
        session.run(_UPSERT_DISTRICT, batch=drows)
        print(f"  upserted {len(drows)} :District nodes")

        # 2. Tag every located node with its district.
        nodes = session.run(
            "MATCH (n) WHERE (n:POI OR n:Building OR n:Stop) "
            "AND n.latitude IS NOT NULL AND n.longitude IS NOT NULL "
            "RETURN id(n) AS nid, n.latitude AS lat, n.longitude AS lon"
        ).data()
        tagged, batch = 0, []
        for r in nodes:
            name = locator(r["lat"], r["lon"])
            if name:
                batch.append({"nid": r["nid"], "district": name})
        for i in range(0, len(batch), 1000):
            session.run(_TAG_NODES, batch=batch[i:i + 1000])
        tagged = len(batch)
        print(f"  tagged {tagged:,}/{len(nodes):,} nodes with a district")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--production", action="store_true")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")

    districts = fetch_districts()
    if not districts:
        raise SystemExit("No district polygons built — check the Overpass query.")
    names = sorted(d["name"] for d in districts)
    print(f"  districts: {', '.join(names)}")

    if args.dry_run:
        locate = make_locator(districts)
        print("\n  PIP self-test:")
        for label, lat, lon in _PROBE_POINTS:
            print(f"    {label:38} -> {locate(lat, lon)}")
        print("\nDry run — no Neo4j write.")
        return
    print()
    write_to_neo4j(districts, production=args.production)


if __name__ == "__main__":
    main()
