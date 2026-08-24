"""
3-node LangGraph for the Magdeburg Campus Assistant.

Layout:

    cache_check ──hit──→ END
        │ miss
        ▼
    single_agent (GPT-5.4 with all 15 tools)
        │
        ▼
    cache_store ──→ END

The single agent owns the full tool surface (Neo4j + FIWARE + Routing +
Context). The model's native tool-calling loop handles routing, parallel
fan-out, and answer composition in one conversation — replacing what
used to be a supervisor + 4 specialist agents + synthesis pipeline.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from graph.agent import build_single_agent, create_single_agent_node
from graph.nodes.cache_node import create_cache_nodes
from graph.nodes.proactive_node import create_proactive_node
from graph.state import AgentState


def _route_after_cache(state: AgentState) -> str:
    """Cache hit → straight to END. Miss → proactive bridge → single agent."""
    if state.get("cache_hit"):
        return "end"
    return "proactive"


def build_graph(
    fiware_client: Any,
    semantic_cache: Any = None,
    checkpointer: Any = None,
    tools: Any = None,
    agent: Any = None,
):
    """Compile the 3-node graph and return the LangGraph app.

    The agent runs on MCP `tools` (or a prebuilt `agent`); `fiware_client`
    feeds the proactive bridge node.
    """
    if agent is None:
        if tools is None:
            raise ValueError(
                "build_graph requires MCP `tools` (or a prebuilt `agent`) — this "
                "build is MCP-only (no in-process fallback). Open tools via "
                "graph.mcp_client.open_mcp_tools."
            )
        agent, _tool_names = build_single_agent(tools)

    single_agent_node = create_single_agent_node(agent)
    proactive_node = create_proactive_node(fiware_client)
    cache_check, cache_store = create_cache_nodes(semantic_cache)

    g = StateGraph(AgentState)
    g.add_node("cache_check", cache_check)
    g.add_node("proactive", proactive_node)
    g.add_node("single_agent", single_agent_node)
    g.add_node("cache_store", cache_store)

    g.add_edge(START, "cache_check")
    g.add_conditional_edges(
        "cache_check",
        _route_after_cache,
        {"end": END, "proactive": "proactive"},
    )
    g.add_edge("proactive", "single_agent")
    g.add_edge("single_agent", "cache_store")
    g.add_edge("cache_store", END)

    app = g.compile(checkpointer=checkpointer)
    print("[GRAPH] Pipeline compiled (cache_check → proactive → single_agent → cache_store)")
    return app
