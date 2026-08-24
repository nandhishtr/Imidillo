"""
Neo4j keepalive job — stops Aura Free from idle-pausing the instance.

Aura Free suspends an instance after ~72 hours without QUERY activity (an
open-but-idle driver connection does not count), and a paused instance can
only be resumed by hand in the Aura console. A daily `RETURN 1` from the
timer keeps the clock from ever reaching 72 h, with two missed runs of
slack before a pause could happen.

This prevents pausing — it cannot UN-pause. If the instance is already
paused (box was off for days), this job fails with exit 1 until someone
resumes the instance in the console.

Chatbot infrastructure: this protects the chatbot's Neo4j instance, so it
lives in the chatbot repo (the email/report jobs are a separate project,
imidillo-sleeping-agents). Needs the `neo4j` driver (the app venv has it).

Run manually:
    python scripts/neo4j_keepalive.py

Exit codes: 0 = ping ok, 2 = Neo4j not configured (skip, not an error),
1 = ping failed (systemd shows the unit as failed; reason in journalctl).
"""

from __future__ import annotations

import os
import sys
import time

# Bootstrap the repo root so `from config import ...` resolves — same idiom
# as mcp_servers/*.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import NEO4J_DATABASE, NEO4J_PASSWORD, NEO4J_URI, NEO4J_USERNAME  # noqa: E402

# Bounded everywhere: a hung network path must fail the unit, not wedge it.
_CONNECT_TIMEOUT_S = 10.0
_QUERY_TIMEOUT_S = 10.0


def run() -> int:
    if not NEO4J_PASSWORD:
        print("[KEEPALIVE] NEO4J_PASSWORD missing in .env - nothing pinged.")
        return 2

    # Import here so an unprovisioned box (no NEO4J_PASSWORD) exits 2 above
    # without needing the driver installed at all.
    try:
        from neo4j import GraphDatabase, Query
    except ImportError:
        print("[KEEPALIVE] the 'neo4j' driver is not installed in this venv "
              "(pip install neo4j) - Neo4j is configured, so this is a failure, "
              "not a skip: without the ping Aura Free pauses after ~3 idle days.")
        return 1

    started = time.monotonic()
    driver = None
    try:
        driver = GraphDatabase.driver(
            NEO4J_URI,
            auth=(NEO4J_USERNAME, NEO4J_PASSWORD),
            connection_timeout=_CONNECT_TIMEOUT_S,
            connection_acquisition_timeout=_CONNECT_TIMEOUT_S,
        )
        with driver.session(database=NEO4J_DATABASE) as session:
            ok = session.run(Query("RETURN 1 AS ok", timeout=_QUERY_TIMEOUT_S)).single()["ok"]
        elapsed = time.monotonic() - started
        print(f"[KEEPALIVE] ok - RETURN 1 -> {ok} on {NEO4J_URI} ({elapsed:.2f}s)")
        return 0
    except Exception as e:
        print(f"[KEEPALIVE] ping failed on {NEO4J_URI}: {type(e).__name__}: {e}")
        print("[KEEPALIVE] if this is Aura Free and the instance is already "
              "paused, resume it manually in the Aura console - this job only "
              "prevents pausing, it cannot resume.")
        return 1
    finally:
        if driver is not None:
            try:
                driver.close()
            except Exception:
                pass  # best-effort; the ping already succeeded or failed above


if __name__ == "__main__":
    sys.exit(run())
