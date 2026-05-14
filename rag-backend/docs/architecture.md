# Architecture Overview — RAG Kemendikdasmen

> State after Phase 7 (Mei 2026). Update this doc when adding new major components.

## Request Lifecycle

```
POST /ask  or  POST /ask/stream
         │
         ▼
app/main.py → normalize_school_name(query)
         │         check_rate_limit(user_id)
         │
         ▼
app/services/core/ask_pipeline.py  run_ask_pipeline()
         │
         ├─ 1. Cache check  ──────────────────────────────────────────────────┐
         │      TieredCache.get(cache_key)                                    │
         │      L1 (in-memory LRU, TTL 5 min) → L2 (Redis, TTL 30 min)      │
         │      HIT → return cached response immediately ──────────────────────┘
         │
         ├─ 2. Classification (Intent + Route)
         │      IntentClassifier.classify(query)
         │        Tier 1: PatternClassifier (regex, <1ms, ~40% coverage)
         │        Tier 2: RuleClassifier (scoring, <5ms, +34% coverage)
         │        Tier 3: cognitive_router → DeepSeek LLM (3–8s, ~26% queries)
         │      → CognitiveIntent: module, scope, jenjang, wilayah, sub_queries
         │
         ├─ 3. OOT / reject check
         │      is_clearly_oot(query) → narrate_out_of_context()
         │
         ├─ 4. Handler dispatch (based on intent.module)
         │      "aggregation"  → aggregation_handler.handle_aggregation_query()
         │      "list"         → list_school_handler.ListSchoolHandler.handle()
         │      "profile"      → profile_handler.ProfileHandler.handle()
         │      "comparison"   → comparison_handler.handle_comparison_query()
         │      "ranking"      → list_handler with sort
         │
         ├─ 5. Location Resolution (inside handlers / entity_service)
         │      LocationResolver (Phase 3) — 5-step chain:
         │        a. Exact match in LOCATION_INDEX
         │        b. Prefix-normalized match (kab./kota/prov./kec.)
         │        c. Fuzzy match (rapidfuzz, threshold 87)
         │        d. Special alias (JAKARTA_ALIAS, AREA_GROUP)
         │        e. Disambiguation → AMBIGUOUS result with candidates
         │      entity_service.resolve_entities() — full resolver for handlers
         │
         ├─ 6. Elasticsearch query
         │      Hybrid search: BM25 + semantic (all-MiniLM-L6-v2 embeddings)
         │      Aggregation queries via aggregation_core.py
         │
         ├─ 7. Narration
         │      LocalNarrator (simple queries, no LLM, <50ms)
         │      OR narrator_service.narrate_*() → DeepSeek prompt chain
         │        narrator_prompts.py builds the prompt
         │        context_optimizer.py manages token budget
         │
         └─ 8. Response build + background jobs
                _bg_tasks   → FastAPI BackgroundTasks (save_chat_history — critical)
                _bg_queue   → SimpleTaskQueue (set_cache, log_rag_evaluation)
                _bg_trigger → trigger_background_jobs (analytics, suggestions)
```

## Classification Tiers

| Tier | Module | Latency | Coverage | Mechanism |
|------|--------|---------|----------|-----------|
| 1 | `tier1_pattern.py` | <1ms | ~40% | Regex patterns for clear intents |
| 2 | `tier2_rule.py` | <5ms | +34% = 74% total | Keyword scoring matrix |
| 3 | `cognitive_router.py` | 3–8s | ~26% | DeepSeek LLM with structured prompt |

All tiers return the same `CognitiveIntent` dataclass. Tier 3 is only called when Tier 1+2 both return `None`.

## Cache Strategy

```
Query
  ↓
TieredCache.get(key)
  ├── L1: in-memory dict (LRU, maxsize=512, TTL=5min)  ← hit: ~0ms
  └── L2: Redis  (TTL=30min)                            ← hit: ~2ms
       └── Miss → Elasticsearch query                   ← ~50–500ms

Intent cache (cognitive_router): TTL_L1=5min, TTL_L2=30min, maxsize=512
Response cache (ask_pipeline):    TTL_L1=5min, TTL_L2=30min
```

TieredCache uses `asyncio.Lock` for thread-safety. LRU eviction removes the oldest entry when `maxsize` is reached.

## SSE Streaming (/ask/stream)

```
POST /ask/stream
  │
  ▼
ask_stream_generator()
  │   ┌─────────────────────────────────────────────────────┐
  │   │  asyncio.Queue(maxsize=20)  ← producer-consumer     │
  │   │                                                      │
  │   │  asyncio.create_task(run_pipeline())                │
  │   │    ├─ on_step("classifying", "...")  → queue.put() │
  │   │    ├─ on_step("searching", "...")    → queue.put() │
  │   │    └─ {"_result": result}            → queue.put() │
  │   │                                                      │
  │   │  Generator reads queue, yields SSE strings:         │
  │   │    event: status  data: {"step": "classifying", …}  │
  │   │    event: status  data: {"step": "searching", …}    │
  │   │    event: result  data: {<full AskResponse>}        │
  │   │    event: done    data: {}                          │
  │   └─────────────────────────────────────────────────────┘
  │
  ▼
StreamingResponse(media_type="text/event-stream")
```

On disconnect or timeout (90s): `pipeline_task.cancel()`.

## Background Jobs

Two mechanisms, different reliability guarantees:

| Mechanism | Used for | Guarantee |
|-----------|----------|-----------|
| `FastAPI BackgroundTasks` | `save_chat_history` | Runs after response sent, same process |
| `SimpleTaskQueue` (asyncio) | `set_cache`, `log_rag_evaluation` | Best-effort, drops if queue full (maxsize=100) |
| `trigger_background_jobs` | analytics, suggestion, low-score alert | Fire-and-forget via `asyncio.create_task` |

`trigger_background_jobs(payload, background_tasks=None)` — when `background_tasks=None` (SSE path), uses `asyncio.create_task()` instead of `background_tasks.add_task()`.

## Logging

All application code uses structured JSON logging:

```python
from app.utils.structured_log import get_logger
logger = get_logger(__name__)
logger.info("[MODULE] event", extra={"query": "...", "duration_ms": 42})
```

Output (LOG_FORMAT=json, default):
```json
{"ts": 1716890400.123, "level": "INFO", "module": "app.services.core.ask_pipeline", "event": "[PIPELINE] start", "query": "berapa SD di bandung"}
```

Set `LOG_FORMAT=plain` for human-readable output (development/test).
`configure_logging()` is called once at startup in `app/main.py`.

## Deployment (single process)

```bash
# Development
uvicorn app.main:app --reload --port 3000

# Production
uvicorn app.main:app --host 0.0.0.0 --port 3000 --workers 1
```

Single worker is intentional: `LOCATION_INDEX` is a module-level dict loaded once at startup via `initialize_location_index()`. Multi-worker would require each worker to reload it (acceptable but wasteful). Redis handles shared state between restarts.

## Key Invariants

1. `LOCATION_INDEX` — never modified at runtime, only read. Loaded at startup.
2. `TieredCache` — L1 is process-local. Redis (L2) is shared across restarts.
3. `on_step` callback — called only from inside `run_ask_pipeline()`. `None` on non-SSE path (zero overhead).
4. Handler return shape — always `{"status", "provider", "data", "confidence", "metadata"}`. Pipeline builds `AskResponse` from this.
5. `ask_pipeline.py` — returns raw `dict`, never `AskResponse`. Caller (main.py or stream.py) builds the response model.
