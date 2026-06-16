"""
LangGraph state for the single-agent Magdeburg Campus Assistant.

Flow: cache_check → proactive → single_agent → cache_store.
"""

from typing import Annotated, Optional, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    """Shared state passed through every graph node.

    `total=False` so callers can pass partial state — graph nodes only
    populate the fields they care about.
    """

    # The user's original query text
    query: str

    # Session identifier for conversation threading
    session_id: str

    # LangChain message list, reduced via `add_messages` so each ReAct
    # turn appends rather than overwrites
    messages: Annotated[list[BaseMessage], add_messages]

    # User's GPS location if provided ({"lat": float, "lon": float})
    user_location: Optional[dict[str, float]]

    # Location-sharing state from the client: "on" | "off" | "denied" |
    # "unavailable" | "timeout" | "unsupported". Lets the agent ask the user to
    # enable location when a question needs their position but none was shared.
    location_status: Optional[str]

    # Conversation history from prior turns: [{"role": ..., "content": ...}]
    conversation_history: list[dict[str, str]]

    # Final response text. The API reads `final_response` for the user-
    # visible output; `response` is the same value, kept for cache_store.
    response: Optional[str]
    final_response: Optional[str]

    # Whether the query was answered from the semantic cache
    cache_hit: Optional[bool]

    # Proactive nearby context injected by the proactive bridge node when the
    # user's location is known (weather / parking / traffic near them).
    proactive_context: Optional[str]
