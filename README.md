![Imidillo](Imidillo.png)

# Imidillo — The Magdeburg Campus Assistant

> *Ask a city anything.*

Imidillo is an AI assistant that knows Magdeburg: the buildings on the OVGU campus, the tram stops, the parking garages, and what the Mensa is serving today. It answers in plain language and draws routes on a live map.

Under the hood it is a single LLM agent wired to a knowledge graph and a live IoT sensor network through MCP. The result is part chatbot, part digital twin.

## What it can do

* **"Where is Building 5?"** Resolves campus buildings, lecture halls, POIs, and streets from a Neo4j knowledge graph built from OpenStreetMap data, and drops a pin on the map.
* **"How do I get from Hauptbahnhof to the university?"** Plans transit routes over the tram and bus network graph, plus walking, cycling, and driving routes via the IMIQ city routing service (GraphHopper, whole-Magdeburg coverage), rendered as route cards on the map.
* **"What's the temperature right now?"** Live weather, parking availability, air quality, traffic flow, and Elbe water levels straight from the city's FIWARE IoT context broker.
* **"What's in the Mensa today?"** Yes, including the daily menu.
* **It knows where you are.** Share your position and it quietly finds your nearest stop, so "how do I get home from here" just works.

## How it thinks

One ReAct agent (a LLM, `gpt-5.4`) owns the entire tool surface. No supervisor, no agent hand offs. The model's native tool calling loop does the routing, fans out calls in parallel, and composes the final answer.

The tools live in four MCP servers, each a separate stdio subprocess with a single responsibility:

```
                        ┌──────────────────────────────┐
  Browser widget  ───►  │   FastAPI  (api.py)          │
  (SSE token stream)    │   sessions · rate limiting   │
                        │   PII redaction · caching    │
                        └──────────────┬───────────────┘
                                       │
                        ┌──────────────▼───────────────┐
                        │   LangGraph ReAct agent      │
                        │   (one LLM, all the tools)   │
                        └──┬─────────┬─────────┬───────┘
                           │   MCP   │  (stdio)│
            ┌──────────────▼──┐ ┌────▼─────┐ ┌─▼──────────────┐ ┌────────────────┐
            │  neo4j-campus   │ │ fiware-  │ │    routing     │ │ context-bridge │
            │  buildings,     │ │ sensors  │ │ IMIQ walking / │ │ "what's near   │
            │  stops, POIs,   │ │ weather, │ │  cycling /     │ │  X?": graph +  │
            │  transit lines  │ │ parking, │ │  driving       │ │  sensors +     │
            │                 │ │ traffic  │ │                │ │ router, 1 call │
            └───────┬─────────┘ └────┬─────┘ └───────┬────────┘ └───────┬────────┘
                    │                │               │                  │
               Neo4j graph      FIWARE Orion     IMIQ routing      (all three)
              (knowledge)      (live city IoT)  (GraphHopper)
```

The division of labor is strict and deliberate. The graph server answers *"what exists and where"* (static knowledge), FIWARE answers *"what's happening right now"* (live readings), routing answers *"how do I get there"*, and the context bridge fuses all three for *"what's around X?"* questions in a single call. Place names resolve through one canonical resolver — Neo4j full-text with a name-similarity confidence gate, falling back to a city-wide geocoder chain (OSM Nominatim first, ORS/Pelias as a similarity-gated backup) for off-graph places — so every tool agrees on where a place is.

## The supporting cast

| Piece | What it does |
|---|---|
| **Token streaming** | Answers stream word by word over SSE. The model's `<think>…</think>` reasoning spans are stripped on the fly, and heartbeats keep the connection warm during tool calls. |
| **Semantic cache** | Repeated questions skip the LLM entirely, even when paraphrased (cosine similarity ≥ 0.88). Cache keys combine query, user, and location bucket, so one user's "parking near me" never leaks to another. |
| **Embeddings** | One shared `BAAI/bge-base-en-v1.5` sentence transformer per process, behind an LRU cache so hot queries never get encoded twice. |
| **Place resolver** | Fuzzy matching plus embeddings, so "hauptbanhof" (typo and all) still finds the Hauptbahnhof. |
| **Session security** | Each session gets its own bearer token, compared in constant time. Sessions expire after 30 minutes of inactivity, and requests are rate limited per IP (backed by Redis when available). |
| **PII hygiene** | Emails, phone numbers, and street addresses are redacted before anything is logged or replayed into LLM context. |
| **Map widget** | An embeddable JavaScript widget with zero dependencies (`static/imidillo-widget.js`): chat bubble, streaming text, map pins, route polylines. Drop it into any page. |

## Where the knowledge comes from

The `ingestion/` pipeline builds the Neo4j graph from raw city data:

1. **`downloaders/`** pull buildings, streets, and POIs from OpenStreetMap.
2. **`loaders/`** load them into Neo4j in batches, embeddings included.
3. **`linkers/`** do the clever part: they spatially link buildings ↔ streets ↔ stops ↔ POIs, detect duplicates, and resolve ambiguous matches so the graph is *connected*, not just populated.

There is also a proper eval harness (`eval/`) with an LLM factuality judge and adversarial test sets: typos, ambiguous place names ("Hauptbahnhof" the stop vs. the building), questions outside its domain, and cases where the data is simply missing. Threshold sweeps picked the cache and resolver cutoffs from data instead of vibes.

---

*Built at OVGU Magdeburg as part of the IMIQ project: a chatbot that doesn't just talk about the city, it's plugged into it.*
