"""
Single-agent factory for the Magdeburg Campus Assistant.

One GPT-5.4 ReAct agent owns the full tool surface (Neo4j + FIWARE +
Routing + Context). The model's native tool-calling loop handles routing,
parallel fan-out, and answer composition in a single conversation —
replacing what used to be a supervisor + 4 specialist agents + synthesis.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

try:
    from zoneinfo import ZoneInfo
    _MAGDEBURG_TZ = ZoneInfo("Europe/Berlin")
except Exception:  # no IANA tz data (bare Windows without tzdata) — fall back to server-local time
    _MAGDEBURG_TZ = None

from langchain_core.messages import AIMessage, HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent

from config import (
    AGENT_TIMEOUT,
    OPENAI_API_KEY,
    OPENAI_BASE_URL,
    SINGLE_AGENT_MODEL,
)
from graph.system_prompt import get_system_prompt

logger = logging.getLogger(__name__)


def build_single_agent(tools: Any):
    """Build the single gpt-5.4 ReAct agent on the given MCP tools.

    Tools come from the MCP servers (graph/mcp_client.py). This is the ONLY
    tool path — there is no in-process fallback.

    Returns (agent, tool_names). tool_names is for startup diagnostics.
    """
    all_tools = list(tools)
    tool_names = [getattr(t, "name", "?") for t in all_tools]
    print(f"[SINGLE AGENT] Using {len(all_tools)} MCP tools: {tool_names}")

    llm = ChatOpenAI(
        base_url=OPENAI_BASE_URL,
        api_key=OPENAI_API_KEY,
        model=SINGLE_AGENT_MODEL,
        temperature=0.0,
        streaming=True,
    )

    prompt = get_system_prompt()
    print(f"[SINGLE AGENT] System prompt ready ({len(prompt)} chars)")

    agent = create_react_agent(llm, all_tools, prompt=prompt)
    return agent, tool_names


def _format_history(history: list[dict]) -> str:
    if not history:
        return ""
    lines: list[str] = []
    for turn in history[-6:]:
        role = (turn.get("role") or "user").upper()
        content = turn.get("content") or ""
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _format_now() -> str:
    """Current date + time in Magdeburg, injected into every agent turn so the
    model can answer time-dependent questions (opening hours, day-of-week,
    "is the Mensa serving lunch right now?")."""
    now = datetime.now(_MAGDEBURG_TZ) if _MAGDEBURG_TZ else datetime.now()
    return f"Current date and time in Magdeburg: {now.strftime('%A, %d %B %Y, %H:%M')}"


def _format_location_status(user_location: Any, location_status: Any = None) -> str:
    """One line telling the agent the user's location-sharing state.

    When coordinates are present, the agent anchors "near me / nearest / from
    here" answers on them. When they're absent, the line says WHY (off / denied
    / unavailable) so the agent can ask the user to enable location (the 📍
    button by the message box) or state where they are, instead of silently
    guessing a position. See the LOCATION AWARENESS prompt section.
    """
    lat = lon = None
    if isinstance(user_location, dict):
        lat = user_location.get("lat") or user_location.get("latitude")
        lon = user_location.get("lon") or user_location.get("longitude")
    if lat is not None and lon is not None:
        return f"User's current location: latitude={lat}, longitude={lon} (location sharing is ON)."
    reason = {
        "denied": "permission denied",
        "unavailable": "position unavailable",
        "timeout": "request timed out",
        "unsupported": "not supported by their browser",
        "error": "could not be determined",
        "off": "not shared",
    }.get(str(location_status or "off").lower(), "not shared")
    return (
        f"User's location is unavailable ({reason}). If the question needs their current "
        'position ("near me", "from here", "nearest", "how do I get home"), ask them to tap '
        "the location (📍) button next to the message box, or to tell you where they are — "
        "don't guess a location. If it doesn't need their position, just answer."
    )


def _count_tool_calls(messages: list) -> int:
    n = 0
    for m in messages:
        tc = getattr(m, "tool_calls", None)
        if tc:
            n += len(tc)
    return n


def create_single_agent_node(agent):
    """Create a LangGraph node that invokes the single agent.

    Reads query / conversation_history / user_location from state; runs
    the agent with a wall-clock timeout; writes response and
    final_response back. The legacy `agent_results` field is left empty
    — downstream code (api.py card extractor) degrades gracefully.
    """

    async def single_agent_node(state: dict) -> dict:
        query = (state.get("query") or "").strip()
        conversation_history = state.get("conversation_history") or []
        user_location = state.get("user_location")

        parts: list[str] = []
        history_text = _format_history(conversation_history)
        if history_text:
            parts.append(f"Recent conversation:\n{history_text}")
        parts.append(_format_now())
        location_text = _format_location_status(user_location, state.get("location_status"))
        if location_text:
            parts.append(location_text)
        proactive_context = (state.get("proactive_context") or "").strip()
        if proactive_context:
            parts.append(proactive_context)
        parts.append(f"Question: {query}")
        user_msg = "\n\n".join(parts)

        print(f"[SINGLE AGENT] Processing: {query!r}")

        try:
            result = await asyncio.wait_for(
                agent.ainvoke({"messages": [HumanMessage(content=user_msg)]}),
                timeout=AGENT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            print(f"[SINGLE AGENT] Timeout after {AGENT_TIMEOUT}s")
            msg = "Sorry, that took longer than expected to look up. Please try again."
            return {"response": msg, "final_response": msg}
        except Exception as e:
            logger.error(f"Single agent failed: {e}", exc_info=True)
            print(f"[SINGLE AGENT] Error: {e}")
            msg = "Sorry, I ran into an internal error answering that. Please try again."
            return {"response": msg, "final_response": msg}

        msgs = result.get("messages") or []

        # Diagnostic: print every tool call (with args) and every tool
        # result (truncated). Helps catch cases where the LLM misreads a
        # successful tool result as a failure, or calls the wrong tool.
        for i, m in enumerate(msgs):
            cls = type(m).__name__
            tcs = getattr(m, "tool_calls", None)
            if tcs:
                for tc in tcs:
                    args_preview = str(tc.get("args", {}))[:300]
                    print(f"[SINGLE AGENT]   step {i} {cls} -> {tc.get('name')}({args_preview})")
            elif cls == "ToolMessage":
                content = getattr(m, "content", "") or ""
                preview = (content[:400] + "...") if len(content) > 400 else content
                print(f"[SINGLE AGENT]   step {i} ToolMessage <- {preview}")

        final = ""
        for m in reversed(msgs):
            if isinstance(m, AIMessage):
                content = (m.content or "").strip()
                if content:
                    final = content
                    break

        if not final:
            final = "Sorry, I couldn't put together an answer for that one."

        n_tool_calls = _count_tool_calls(msgs)
        print(
            f"[SINGLE AGENT] Done — {n_tool_calls} tool call(s), "
            f"{len(final)} char answer"
        )

        return {
            "response": final,
            "final_response": final,
            "messages": msgs,
        }

    return single_agent_node
