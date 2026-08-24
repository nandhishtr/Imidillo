"""
Composed Neo4jTransitGraph class.

The live surface is small: the shared `driver`/`database` handles (used by
mcp_servers/routing_server.py to run the canonical place resolver's Cypher),
`test_connection` (api.py /health, APP.py CLI), and `find_nearest_stop`
(api.py's transit origin hint). The former Search / Transit / Spatial /
Sensor mixins were unused dead code and were removed.
"""

from neo4j_tools._base import Neo4jBase


class Neo4jTransitGraph(Neo4jBase):
    """Neo4j graph interface: shared driver + nearest-stop lookup."""
    pass
