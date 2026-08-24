"""
Main application module for the Magdeburg Campus Assistant.
Provides a dependency-injection factory (create_app) and a lazy singleton (get_app)
for wiring all external clients and the LangGraph pipeline.
No side effects at import time — all initialization happens inside create_app().
"""

import sys
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

import asyncio
from dataclasses import dataclass
from typing import Optional, Any

from clients import FIWAREClient
from services import SemanticCache
from neo4j_tools import Neo4jTransitGraph
from config import (
    FIWARE_BASE_URL,
    FIWARE_API_KEY,
    NEO4J_URI,
    NEO4J_USERNAME,
    NEO4J_PASSWORD,
    NEO4J_DATABASE,
    SEMANTIC_CACHE_ENABLED,
    SEMANTIC_CACHE_THRESHOLD,
    SEMANTIC_CACHE_TTL,
    SEMANTIC_CACHE_MAX_SIZE,
    get_encoder,
)

__all__ = ['AppContext', 'create_app', 'get_app']


# ---------------------------------------------------------------------------
# Application context
# ---------------------------------------------------------------------------
@dataclass
class AppContext:
    """Holds all initialized application dependencies."""
    neo4j_graph: Neo4jTransitGraph
    fiware_client: FIWAREClient
    graph_app: Any  # Compiled LangGraph app
    semantic_cache: Any = None
    checkpointer: Any = None
    single_agent: Any = None  # the gpt-5.4 ReAct agent (for the token-streaming path)


def create_app(
    neo4j_graph=None,
    fiware_client=None,
    semantic_encoder=None,
) -> AppContext:
    """
    Factory that wires all dependencies. Pass pre-built objects for testing;
    omit for production defaults that connect to real services.
    """

    if fiware_client is None:
        fiware_client = FIWAREClient(FIWARE_BASE_URL, FIWARE_API_KEY)

    # Only load the ~150 MB BGE embedding model if something actually uses
    # it. Semantic cache is the only real customer; skipping the load saves
    # ~35-40s of startup time and ~150 MB RAM when SEMANTIC_CACHE_ENABLED=false.
    if semantic_encoder is None and SEMANTIC_CACHE_ENABLED:
        semantic_encoder = get_encoder()

    if neo4j_graph is None:
        neo4j_graph = Neo4jTransitGraph(
            NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE,
        )

    # Semantic cache
    cache = None
    if SEMANTIC_CACHE_ENABLED and semantic_encoder is not None:
        cache = SemanticCache(
            encoder=semantic_encoder,
            threshold=SEMANTIC_CACHE_THRESHOLD,
            ttl_seconds=SEMANTIC_CACHE_TTL,
            max_size=SEMANTIC_CACHE_MAX_SIZE,
        )
        print("Semantic cache initialized")

    # Checkpointer (in-memory; swap to SqliteSaver for cross-restart persistence)
    checkpointer = None
    try:
        from langgraph.checkpoint.memory import InMemorySaver
        checkpointer = InMemorySaver()
        print("  Checkpointer: InMemorySaver (in-memory)")
    except ImportError:
        try:
            from langgraph.checkpoint.memory import MemorySaver
            checkpointer = MemorySaver()
            print("  Checkpointer: MemorySaver (in-memory)")
        except Exception as e:
            print(f"  Checkpointer disabled: {e}")

    # MCP-only: the LangGraph pipeline is built later, in the API lifespan
    # (api.py) or the CLI below, AFTER the MCP tool sessions are open — it needs
    # an async context and the MCP tools. create_app wires only the synchronous
    # clients + cache + checkpointer; `graph_app` stays None until then.
    return AppContext(
        neo4j_graph=neo4j_graph,
        fiware_client=fiware_client,
        graph_app=None,
        semantic_cache=cache,
        checkpointer=checkpointer,
    )


# ---------------------------------------------------------------------------
# Lazy singleton for production use
# ---------------------------------------------------------------------------
_app: Optional[AppContext] = None


def get_app() -> AppContext:
    """Get the singleton app context. Creates real dependencies on first call."""
    global _app
    if _app is None:
        _app = create_app()
    return _app


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
async def _cli_main() -> None:
    """Interactive CLI — opens the MCP tool sessions, builds the gpt-5.4 agent
    on them, then runs a REPL. MCP is the only tool path (no fallback)."""
    from contextlib import AsyncExitStack
    from graph.mcp_client import open_mcp_tools
    from graph.graph import build_graph

    print("=" * 60)
    print("MAGDEBURG ASSISTANT (single gpt-5.4 / MCP)")
    print("=" * 60)

    print("\nInitializing...")
    ctx = get_app()
    print("Testing Neo4j...")
    print("Neo4j connected" if ctx.neo4j_graph.test_connection() else "Neo4j failed")

    cli_conversation_history: list[dict[str, str]] = []
    async with AsyncExitStack() as stack:
        tools, per_server = await open_mcp_tools(stack)
        ctx.graph_app = build_graph(
            fiware_client=ctx.fiware_client,
            semantic_cache=ctx.semantic_cache,
            checkpointer=ctx.checkpointer,
            tools=tools,
        )
        print(f"\nReady on {len(tools)} MCP tools. Type 'quit' to exit.\n")
        try:
            while True:
                try:
                    user_input = (await asyncio.to_thread(input, "You: ")).strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nGoodbye!")
                    break
                if user_input.lower() in ("quit", "exit", "bye"):
                    print("Goodbye!")
                    break
                if not user_input:
                    continue
                try:
                    result = await ctx.graph_app.ainvoke(
                        {
                            "query": user_input,
                            "session_id": "cli_session",
                            "messages": [],
                            "user_location": None,
                            "conversation_history": list(cli_conversation_history),
                            "response": None,
                            "cache_hit": False,
                        },
                        config={"configurable": {"thread_id": "cli_session"}},
                    )
                    response = (result.get("final_response") or result.get("response")
                                or "I'm sorry, I couldn't generate a response.")
                    cli_conversation_history.append({"role": "user", "content": user_input})
                    cli_conversation_history.append({"role": "assistant", "content": response})
                    if len(cli_conversation_history) > 80:
                        cli_conversation_history = cli_conversation_history[-80:]
                    print(f"\nAssistant: {response}\n")
                except Exception as e:
                    print(f"Error: {e}")
                    import traceback
                    traceback.print_exc()
        finally:
            ctx.neo4j_graph.close()
            ctx.fiware_client.close()
            print("Connections closed.")


if __name__ == "__main__":
    asyncio.run(_cli_main())
