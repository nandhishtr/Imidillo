"""Neo4jBase: core infrastructure for the Neo4j Transit Graph.

Provides connection management, the connection health check, and the
nearest-stop lookup used by api.py's origin hint.

The driver is injected (shared singleton from mcp_servers/neo4j_server.py)
rather than created per instance — see `neo4j_tools.get_default_driver()`.
"""

from neo4j import GraphDatabase, Query
from typing import Dict, Optional
from models import Coordinates


_DEFAULT_QUERY_TIMEOUT = 8.0


def _q(cypher: str, timeout: float = _DEFAULT_QUERY_TIMEOUT) -> Query:
    """Wrap a Cypher string in a Query object with a per-query timeout."""
    return Query(cypher, timeout=timeout)


class Neo4jBase:
    def __init__(self, uri: str = None, username: str = None, password: str = None,
                 database: str = "neo4j", verbose: bool = False,
                 driver=None):
        """Accepts either an injected `driver` (preferred — shared singleton) or
        (uri, username, password) kwargs as a legacy fallback. When a driver is
        injected we do NOT own it and must not close it in __del__ / close()."""
        if driver is not None:
            self.driver = driver
            self._owns_driver = False
        else:
            # Legacy path: callers still creating their own driver.
            # Prefer `get_default_driver()` from the `neo4j_tools` package.
            if uri is None:
                from neo4j_tools import get_default_driver
                self.driver = get_default_driver()
                self._owns_driver = False
            else:
                self.driver = GraphDatabase.driver(
                    uri, auth=(username, password),
                    connection_acquisition_timeout=5.0,
                    connection_timeout=3.0,
                    max_connection_pool_size=50,
                    # Guard against cloud LBs dropping idle connections
                    # (see mcp_servers/neo4j_server.py).
                    max_connection_lifetime=300,
                    keep_alive=True,
                    liveness_check_timeout=60,
                )
                self._owns_driver = True

        self.database = database
        self._closed = False
        self.verbose = verbose
        import atexit
        atexit.register(self.close)

    def _log(self, message: str) -> None:
        if self.verbose:
            print(message)

    def close(self):
        # Only close if we own the driver — a shared singleton must outlive us.
        if not self._closed and self.driver is not None and getattr(self, "_owns_driver", False):
            try:
                self.driver.close()
                self._closed = True
            except Exception:
                pass

    def __del__(self):
        self.close()

    def test_connection(self) -> bool:
        try:
            with self.driver.session(database=self.database) as session:
                result = session.run(_q("RETURN 1 as test"))
                return result.single()["test"] == 1
        except Exception as e:
            print(f"Neo4j connection test failed: {e}")
            return False

    def find_nearest_stop(self, coords: Coordinates) -> Optional[Dict]:
        """Find the nearest transit stop to the given coordinates.
        Returns dict with name, lines, latitude, longitude, distance_meters or None.
        """
        with self.driver.session(database=self.database) as session:
            return self._find_nearest_stop(session, coords)

    def _find_nearest_stop(self, session, coords: Coordinates) -> Optional[Dict]:
        self._log(f"[NEO4J]     _find_nearest_stop: lat={coords.lat}, lon={coords.lon}")
        query = """
            MATCH (s:Stop)
            WITH s, point.distance(
                point({latitude: $lat, longitude: $lon}),
                point({latitude: s.latitude, longitude: s.longitude})
            ) as distance
            ORDER BY distance
            LIMIT 1
            RETURN s.name as name, s.lines as lines, s.latitude as latitude, s.longitude as longitude,
                   round(distance) as distance_meters
        """
        try:
            result = session.run(_q(query), lat=coords.lat, lon=coords.lon)
            record = result.single()
            if record:
                self._log(f"[NEO4J]     ✅ Nearest stop: {record['name']} ({record['distance_meters']}m)")
                return {
                    "name": record["name"],
                    "lines": record["lines"] or [],
                    "latitude": record["latitude"],
                    "longitude": record["longitude"],
                    "distance_meters": record["distance_meters"]
                }
            self._log(f"[NEO4J]     ❌ No stops found")
        except Exception as e:
            self._log(f"[NEO4J]     ⚠️ Error finding nearest stop: {e}")
        return None
