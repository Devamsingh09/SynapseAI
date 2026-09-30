# Adaptive Agent Loop

This document describes the adaptive evidence-sufficiency loop added to
Synapse AI's existing LangGraph chatbot, and how it fits into the
pre-existing architecture.

## 1. Existing Synapse architecture (unchanged)

Synapse AI is a FastAPI backend (`backend/main.py`) around a custom
LangGraph `StateGraph` (`backend/chatbot_backend.py`), backed by:

- **Groq** (`langchain-groq`) for the primary chat model
  (`GROQ_CHAT_MODEL`, default `openai/gpt-oss-120b`), with a non-Harmony
  fallback model (`GROQ_TOOL_FALLBACK_MODEL`) for malformed tool calls.
- **Tools** (`backend/tools.py`): `rag_tool`, `web_search`, `calculator`,
  `get_stock_price`, `current_datetime`, `get_weather`, `wikipedia_search`,
  `convert_currency`, `github_search`, `geo_lookup`.
- **SQLite checkpointing** (`SqliteSaver`, `backend/chatbot.db`) keyed by
  `thread_id`, with a per-thread lock for concurrent request safety.
- **Conversation summarization**, which only ever runs once a turn is fully
  complete (`should_summarize`), never mid-tool-loop.
- **Voice mode**, PDF/DOCX/image upload (`file_service.py`), and RAG over a
  FAISS index — all orthogonal to the loop described here and left
  untouched.

The original graph:

```
START → chat_node → should_summarize()
                        ├── tools → chat_node
                        ├── summarize_conversation → END
                        └── END
```

None of this was replaced. The adaptive loop is added as new nodes and
edges layered on top of the existing `tools` → `chat_node` cycle.

## 2. Why the loop is bounded

An unbounded "critic loop" (evaluate → replan → evaluate → replan …) would
make every request slower and more expensive, including trivial ones. The
design instead uses:

1. **A deterministic trigger** (`should_evaluate_after_tools` in
   `backend/adaptive_loop.py`) that decides, with no LLM call, whether the
   evaluator is even worth running. Most turns (greetings, single-tool
   lookups) never invoke it.
2. **A small, cheap evaluator model** (`GROQ_LOOP_EVALUATOR_MODEL`, default
   `openai/gpt-oss-20b`) — never the primary 120B model — used only when
   the trigger fires, with structured output and a 256-token cap.
3. **Two independent hard ceilings**:
   - `ADAPTIVE_LOOP_MAX_ITERATIONS` (default 2) — evaluator calls per turn.
   - `ADAPTIVE_LOOP_MAX_TOOL_CYCLES` (default 4) — tool executions per turn,
     enforced by a dedicated `force_finalize` node that synthesizes a
     best-effort answer with tools disabled once the budget is spent.
4. **A graph-level recursion limit** (`ADAPTIVE_LOOP_RECURSION_LIMIT = 30`,
   passed as a top-level `recursion_limit` in the run config, not inside
   `configurable`) as a last-resort safety net independent of the loop's
   own counters.
5. **A single rollback switch** — `ADAPTIVE_LOOP_ENABLED=false` fully
   disables the evaluator *and* the tool-cycle force-finalize behavior,
   restoring the original single tool-loop graph with no code changes.

## 3. State fields

`ChatState` (in `chatbot_backend.py`) gained three fields, all per-turn and
never merged into `messages`:

| Field                     | Type | Purpose                                              |
|---------------------------|------|-------------------------------------------------------|
| `adaptive_feedback`       | str  | Internal guidance injected into the next `chat_node` call. Empty when there's nothing to say. |
| `adaptive_iteration`      | int  | Number of evaluator calls so far this turn.           |
| `adaptive_loop_enabled`   | bool | Per-turn copy of the `ADAPTIVE_LOOP_ENABLED` setting. |

**Reset on every new user turn.** `iter_chat_stream()` passes
`adaptive_feedback=""`, `adaptive_iteration=0`, and
`adaptive_loop_enabled=<current config>` alongside the new `HumanMessage`
on every call. `summary` is deliberately *not* included in that reset —
long-term memory must persist across turns, while adaptive state must not
leak across turns (SQLite persistence is per-`thread_id`, so without this
reset a later turn would inherit a previous turn's feedback/iteration
count).

Adaptive feedback is never written into `messages`, so it is invisible to
the user, excluded from summarization, and excluded from
`/thread/{id}/history`.

## 4. Evaluator behavior

`adaptive_evaluator_node` (in `chatbot_backend.py`, logic in
`adaptive_loop.py`):

1. Emits an internal `{"status": "evaluating"}` stream event (never shown
   as answer text).
2. Extracts the **current turn only** — everything from the most recent
   `HumanMessage` onward (`get_current_turn_messages`) — to keep old
   conversation turns out of evaluator context (`build_tool_context`,
   capped at `MAX_EVALUATOR_CONTEXT_CHARS = 6000`).
3. Calls a structured evaluator (`LoopDecision` via
   `with_structured_output(method="json_schema", strict=True)`):
   ```python
   class LoopDecision(BaseModel):
       decision: Literal["continue", "finish"]
       reason: str
       missing_information: list[str]
       next_objective: str
       recommended_tool: str  # "" when nothing specific is recommended
   ```
4. Formats the decision into a short internal instruction
   (`feedback_for_decision`) and increments `adaptive_iteration`.
5. Always routes back to `chat_node` — the evaluator never answers the
   user directly; `chat_node` retains full responsibility for tool
   selection, generation, streaming, and voice/system-prompt handling.

**Evaluator failure is non-fatal.** Any exception (timeout, malformed
output, transient Groq error) is caught inside `invoke_evaluator`, logged,
and treated as `None` — the node clears `adaptive_feedback` and the graph
falls back to ordinary chat behavior. The adaptive loop is an enhancement,
never a single point of failure.

## 5. Trigger conditions

`should_evaluate_after_tools` only runs *after* a tool has already executed
this turn (so pure chat turns like "Hello" never reach it), and returns
`True` when any of:

- **A. Tool failure/no-result** — a `ToolMessage` in the current turn
  matches a word-boundary failure-signal regex (`failed`, `error`,
  `no results`, `could not`, `unable to`, `not found`, …). Word-boundary
  matching avoids naive substring false positives (e.g. "errorless").
- **B. Multi-part query** — the *original* user request matches a small,
  regex-based set of multi-step signals (`why`, `compare`, `versus`,
  `difference`, `also`, `based on`, …). This is intentionally a cheap
  heuristic, not a second classifier LLM.
- **C. Multiple distinct tools already used** this turn.

...and is always `False` once `adaptive_iteration >= ADAPTIVE_LOOP_MAX_ITERATIONS`.

Ordinary single-tool, single-step requests (weather, simple arithmetic,
one-shot searches) never satisfy A/B/C and therefore never invoke the
evaluator.

## 6. Tool-cycle limit

`route_after_tools` checks `should_force_finalize` (tool-result count in
the current turn `>= ADAPTIVE_LOOP_MAX_TOOL_CYCLES`) before the evaluator
trigger. Once the budget is hit, the graph routes to `force_finalize`
instead of back to `chat_node`:

- Uses the same primary model, **not tool-bound** (`llm`, not
  `llm_with_tools`), so it physically cannot request another tool call.
- Prompted explicitly: *"You have reached the tool execution budget for
  this turn. Do not call tools. Synthesize the best accurate answer from
  the evidence already collected. Clearly state what could not be
  established."*
- Emits `{"status": "finalizing"}`.
- Feeds into the same `should_summarize` router as `chat_node`, so
  summarization/END logic is unaffected.

This check runs *before* the evaluator trigger, so an exhausted tool
budget always wins over further evaluation.

## 7. Status events (SSE)

The existing SSE contract (`data: {"token"|"status"|"error": ...}`,
terminated by `data: [DONE]`) is unchanged. New internal statuses:

| Status       | Meaning                                              | Frontend label                  |
|--------------|-------------------------------------------------------|----------------------------------|
| `using_tools`| A tool is executing (pre-existing).                   | "Searching the web & using tools…" |
| `evaluating` | The adaptive evaluator is judging evidence sufficiency. | "Checking the results…"        |
| `refining`   | `chat_node` is running again with adaptive feedback.  | "Refining the answer…"          |
| `finalizing` | The tool-cycle budget was hit; synthesizing best-effort answer. | "Finalizing…"          |

Only final answer tokens are ever streamed as `{"token": ...}`. Evaluator
JSON, reasoning, tool arguments, and internal prompts are never streamed.

## 8. Voice behavior

The adaptive loop runs through the exact same graph and the same
`/chat/stream` endpoint in voice mode — there is no separate voice graph.
Only the final answer's tokens are appended to the TTS queue
(`extractNewSpeechChunks` in `voiceChat.js`, driven by `onToken`); internal
statuses only affect the visual "typing" label and are never spoken, aside
from the pre-existing "One moment." spoken cue on `using_tools`.

## 9. Example execution

**User:** "What is Nvidia's latest price and why did it move today?"

```
chat_node                     → chooses get_stock_price
tools (get_stock_price NVDA)  → latest daily close
adaptive_evaluator             → decision=continue
                                  missing=["reason for today's move"]
                                  next_objective="find current news"
                                  recommended_tool="web_search"
chat_node (with feedback)     → chooses web_search
tools (web_search)             → current news results
adaptive_evaluator             → decision=finish
chat_node (with feedback)     → writes the final natural-language answer
                                  (streamed token-by-token to the client)
summarize_conversation          → runs only now, after the full turn
END
```

Nothing in this flow is visible to the user except the tool-status labels
and the final streamed answer.

## 10. Configuration

| Variable                        | Default                | Purpose                                    |
|----------------------------------|-------------------------|---------------------------------------------|
| `ADAPTIVE_LOOP_ENABLED`         | `true`                  | Emergency rollback switch.                  |
| `ADAPTIVE_LOOP_MAX_ITERATIONS`  | `2`                     | Max evaluator calls per turn.                |
| `ADAPTIVE_LOOP_MAX_TOOL_CYCLES` | `4`                     | Max tool executions per turn.                |
| `GROQ_LOOP_EVALUATOR_MODEL`     | `openai/gpt-oss-20b`    | Evaluator model (separate from the 120B chat model). |

See `backend/.env.example` for a full annotated list including the
pre-existing model variables.
