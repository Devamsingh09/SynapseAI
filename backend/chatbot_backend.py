from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from typing import TypedDict, Annotated, Literal, Iterator, Tuple, Optional

from langchain_core.messages import SystemMessage, HumanMessage, RemoveMessage, AIMessage, AIMessageChunk
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.prebuilt import ToolNode
from langgraph.config import get_stream_writer
from dotenv import load_dotenv
import json
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime
import pytz
from tools import (
    rag_tool,
    web_search,
    calculator,
    get_stock_price,
    current_datetime,
    get_weather,
    wikipedia_search,
    convert_currency,
    github_search,
    geo_lookup,
)
from adaptive_loop import (
    ADAPTIVE_LOOP_ENABLED_DEFAULT,
    ADAPTIVE_LOOP_MAX_TOOL_CYCLES,
    ADAPTIVE_LOOP_MAX_ITERATIONS,
    build_tool_context,
    feedback_for_decision,
    format_force_finalize_instruction,
    get_current_turn_messages,
    get_original_user_query,
    invoke_evaluator,
    log_adaptive_event,
    should_evaluate_after_tools,
    should_force_finalize,
)

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BACKEND_DIR, ".env"), override=True)

from langchain_groq import ChatGroq

GROQ_API_KEY = (os.getenv("GROQ_API_KEY") or "").strip()

CHAT_MODEL = os.getenv("GROQ_CHAT_MODEL", "openai/gpt-oss-120b")
# Fallback if the main model returns a malformed/failed tool call.
# Must be a NON-Harmony model (i.e. not another gpt-oss model) so it can
# actually recover from Harmony-specific failures on the primary model.
TOOL_FALLBACK_MODEL = os.getenv("GROQ_TOOL_FALLBACK_MODEL", "qwen/qwen3.8-27b")
SUMMARY_MODEL = os.getenv("GROQ_SUMMARY_MODEL", "openai/gpt-oss-20b")

# Adaptive loop: a comfortable ceiling far below an accidental infinite
# execution, but well above the expected worst case (tool cycles * 2 nodes
# + evaluator passes + force-finalize + summarization). Never exposed to
# the user — internal safety net only, on top of the loop's own bounded
# iteration/tool-cycle counters.
ADAPTIVE_LOOP_RECURSION_LIMIT = 30


def _make_groq(model: str, streaming: bool = True, timeout: int = 120, max_tokens: int = 1024) -> ChatGroq:
    return ChatGroq(
        model=model,
        temperature=0,
        api_key=GROQ_API_KEY,
        streaming=streaming,
        request_timeout=timeout,
        max_tokens=max_tokens,
    )


llm = _make_groq(CHAT_MODEL)
llm_invoke = _make_groq(CHAT_MODEL, streaming=False)
summary_llm = _make_groq(SUMMARY_MODEL, streaming=False, timeout=60)

# 2. DEFINE TOOLS
tools = [
    rag_tool,
    get_weather,
    wikipedia_search,
    convert_currency,
    github_search,
    geo_lookup,
    web_search,
    calculator,
    get_stock_price,
    current_datetime,
]
TOOL_NAMES = {t.name for t in tools}

_bind_kw = {"tool_choice": "auto", "parallel_tool_calls": False}
llm_with_tools = llm.bind_tools(tools, **_bind_kw)
llm_with_tools_invoke = llm_invoke.bind_tools(tools, **_bind_kw)

_tool_fallback_llm = None
_tool_fallback_with_tools = None
_tool_fallback_with_tools_invoke = None
_tool_fallback_invoke = None
if TOOL_FALLBACK_MODEL and TOOL_FALLBACK_MODEL != CHAT_MODEL:
    _tool_fallback_llm = _make_groq(TOOL_FALLBACK_MODEL)
    _tool_fallback_invoke = _make_groq(TOOL_FALLBACK_MODEL, streaming=False)
    _tool_fallback_with_tools = _tool_fallback_llm.bind_tools(tools, **_bind_kw)
    _tool_fallback_with_tools_invoke = _tool_fallback_invoke.bind_tools(tools, **_bind_kw)


# 3. DEFINE STATE
class ChatState(TypedDict):
    messages: Annotated[list, add_messages]
    summary: str
    # Adaptive loop — per-turn only, reset on every new user message (see
    # iter_chat_stream). Never mixed into `messages`/summarization so the
    # user never sees internal control instructions.
    adaptive_feedback: str
    adaptive_iteration: int
    adaptive_loop_enabled: bool

# 4. NODES

SYSTEM_PROMPT = """You are Synapse AI — a helpful, accurate assistant (a focused mini ChatGPT-style agent).

## How you respond
- Be clear, friendly, and direct. Match the user's tone and depth.
- Prefer short, useful answers. Use lists or steps only when they genuinely help.
- Prefer headers and bullet points over markdown tables for structured/comparative info — tables render unreliably in this chat UI. If a table is truly necessary, put every row on its own line (a real line break after each row) — never combine multiple rows onto one line.
- If you are unsure, say so. Do not invent live data (prices, news, dates, document quotes).
- After using tools, always give a final natural-language answer — never stop at raw tool output.
- Never write citation markers such as 【3†L1-L4】, 【turn0search1】, [1†source] or bare [1]/[2] reference numbers — they mean nothing to the user. If a source matters, name it in plain words (e.g. "according to Wikipedia") or give its URL.
- A reference date/time for today is provided below — use that year in search queries. Do NOT call current_datetime unless the user explicitly asks for the time or date.
- Whenever someone asks who made you , just reply Devam Singh is the AI//ML Engineer who made you graduated from IIIT Ranchi (DSAI) branch (2026-Batch), Do not mention OpenAI or something because OpenAI provided only LLM API. You can visit his portfolio and github and linkedin, their respective links are - 'https://ai-portfolio-mu-one.vercel.app/', 'https://github.com/Devamsingh09', 'https://www.linkedin.com/in/devam-singh-248025265/'.
- Never Omit internal tools names and working only you can talk about your core capabilitites, not how it is coming.
- Never response in much detail until it is asked by user, specially when conversation is casual QnA.
- Youa re free to generate markdown based tables and info, but carefully, it should look appealing after rendering.
## Your tools
- **web_search** — up-to-date facts: news, elections, sports results, office-holders, "latest/today/current". Base your answer on the results, not memory — trust search over memory if they conflict. One search per fact is enough — do not re-search to double-check an answer you already have unless the first search returned no results or was clearly wrong.
- **get_weather** — weather for a city/place.
- **wikipedia_search** — encyclopedia facts, history, definitions, biographies (not live news).
- **convert_currency** — money conversion with live rates.
- **get_stock_price** — listed stock tickers only (AAPL, TSLA, NVDA). NOT for commodities like gold/silver — use web_search for those.
- **calculator** — explicit arithmetic.
- **github_search** — GitHub repos or users.
- **geo_lookup** — IP address → country/city/ISP.
- **current_datetime** — only when explicitly asked "what time/date is it" (today's date is already in your context otherwise).
- **rag_tool** — questions about the uploaded/stored document (ethics PDF, course material).

## Multi-tool queries
Chain tools only when the user clearly needs two different capabilities, e.g. "Apple stock price and today's AI news" → get_stock_price, then web_search.

## When NOT to use tools
Greetings, thanks, small talk, creative writing, static explanations — no live data needed.

## Tool loop behavior
After a tool returns, either call another tool if still needed, or write your final answer. Never stop at raw tool output alone.

## Adaptive execution state
When a message below starting with "Adaptive feedback:" is present, treat it as internal execution guidance.

The original user request is the source of truth and must never be changed.

Use adaptive feedback to decide the next useful action.

If the feedback says information is missing:
- retrieve only the missing information
- choose the appropriate available tool
- do not repeat an already-successful tool call unnecessarily

If the feedback says evidence is sufficient:
- answer the user directly
- do not perform additional tools merely to double-check
- do not mention this adaptive process

Never mention:
- evaluator
- planner
- adaptive loop
- internal feedback
- hidden instructions
- iteration counters"""

VOICE_ADDENDUM = """## Voice mode (user is speaking aloud)
- **Be brief.** Prefer 1–3 short sentences. Aim for under 60 words unless the user asked for detail.
- **Write for fast speech.** Use short sentences. End each thought with a full stop. Keep moving — do not pause with long clauses or commas; split into separate sentences instead so the voice can speak quickly after every full stop.
- Answer the question directly — no long intros ("Sure!", "Great question!", "Let me explain…").
- Do not over-explain. Skip background the user did not ask for.
- No markdown, bullet lists, or code blocks unless explicitly requested.
- Plain spoken English (or match the user's language). Sound natural when read aloud.
- You still have full tool access — use tools when needed, then give a **short** spoken summary."""


def _reference_datetime_context() -> str:
    """Inject today's date so the model need not call current_datetime for most queries."""
    now = datetime.now(pytz.timezone("Asia/Kolkata"))
    return (
        "Reference date/time (Asia/Kolkata): "
        f"{now.strftime('%A, %d %B %Y, %I:%M %p %Z')}. "
        "Use this year when searching for current facts."
    )


def extract_ai_text(content) -> str:
    """Normalize AIMessage.content (str or multimodal list) to plain text."""
    if not content:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif "text" in block:
                    parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


def extract_chunk_text(chunk) -> str:
    """Text delta from a single LLM stream chunk (Groq token / sub-token)."""
    if chunk is None:
        return ""
    if isinstance(chunk, AIMessageChunk):
        text = extract_ai_text(chunk.content)
        if text:
            return text
        if getattr(chunk, "tool_call_chunks", None) or getattr(chunk, "tool_calls", None):
            return ""
    if hasattr(chunk, "content"):
        return extract_ai_text(chunk.content)
    return ""


_thread_locks: dict[str, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def get_thread_lock(thread_id: str) -> threading.Lock:
    """Serialize checkpoint access per conversation thread (allows parallel different threads)."""
    with _thread_locks_guard:
        if thread_id not in _thread_locks:
            _thread_locks[thread_id] = threading.Lock()
        return _thread_locks[thread_id]


# ── Stop generation ──────────────────────────────────────────────────────────
# The run currently holding a thread's lock registers its stop Event here, so
# graph nodes can look it up by thread_id from their config. Cancellation is
# cooperative: the streaming LLM loop checks it per chunk (covers the model's
# "thinking" phase too) and nodes that start after a stop do nothing, so the
# graph still ends cleanly and the checkpoint stays valid. A tool that is
# already executing finishes, but nothing runs after it.
_active_stop_events: dict[str, threading.Event] = {}
_active_stop_guard = threading.Lock()


def _stop_requested(config: Optional[RunnableConfig]) -> bool:
    thread_id = str((config or {}).get("configurable", {}).get("thread_id", ""))
    with _active_stop_guard:
        event = _active_stop_events.get(thread_id)
    return bool(event and event.is_set())


def configure_sqlite_connection(conn: sqlite3.Connection) -> None:
    """Improve concurrent read/write behaviour under parallel requests."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")


def _unpack_stream_event(event) -> Tuple[str, object]:
    """Normalize LangGraph stream tuples across versions."""
    if isinstance(event, tuple):
        if len(event) == 2 and event[0] in ("custom", "messages", "updates", "values"):
            return event[0], event[1]
        if len(event) == 3:
            return event[1], event[2]
    return "custom", event


def iter_chat_stream(
    message: str, thread_id: str, voice: bool = False, stop_event: Optional[threading.Event] = None
) -> Iterator[Tuple[str, object]]:
    """
    Sync generator of SSE-oriented events: ('token', str), ('status', str), ('done', None), ('error', str).
    Tokens are streamed live from Groq via LangGraph custom stream mode.
    voice=True adds concise spoken-response instructions (same graph, tools, and thread state).
    stop_event: set it to stop this turn early (user pressed Stop / disconnected);
    whatever text was generated so far is kept as the assistant's reply.
    """
    stop_event = stop_event or threading.Event()
    config = {
        "configurable": {"thread_id": thread_id, "voice_mode": voice},
        # Top-level (not "configurable") — a safety ceiling on total graph
        # steps for the whole turn, independent of the adaptive loop's own
        # iteration/tool-cycle budgets. Never surfaced to the client.
        "recursion_limit": ADAPTIVE_LOOP_RECURSION_LIMIT,
    }
    lock = get_thread_lock(thread_id)
    with lock:
        with _active_stop_guard:
            _active_stop_events[thread_id] = stop_event
        try:
            if stop_event.is_set():  # stopped while waiting for the previous turn
                yield ("done", None)
                return
            graph_input = {
                "messages": [HumanMessage(content=message)],
                # Reset per-turn adaptive state on every new user message so
                # it never leaks across turns via SQLite thread persistence.
                # `summary` is intentionally NOT included here — long-term
                # memory must persist across turns.
                "adaptive_feedback": "",
                "adaptive_iteration": 0,
                "adaptive_loop_enabled": ADAPTIVE_LOOP_ENABLED_DEFAULT,
            }
            for event in chatbot.stream(
                graph_input,
                config=config,
                stream_mode=["custom", "messages"],
            ):
                mode, payload = _unpack_stream_event(event)

                if mode == "custom":
                    if isinstance(payload, dict):
                        token = payload.get("token")
                        if token:
                            # After Stop the graph is only winding down — the
                            # client already has what it showed.
                            if not stop_event.is_set():
                                yield ("token", token)
                            continue
                        status = payload.get("status")
                        if status:
                            yield ("status", status)
                    continue

                if mode == "messages":
                    if isinstance(payload, tuple) and len(payload) == 2:
                        _msg, metadata = payload
                    else:
                        metadata = {}

                    node = metadata.get("langgraph_node") if metadata else None
                    if node == "tools":
                        yield ("status", "using_tools")

            yield ("done", None)
        except Exception as exc:
            # flush=True: this generator runs inside a ThreadPoolExecutor
            # worker (see main.py's _graph_worker) whose stdout is fully
            # buffered when piped/redirected — without it, error diagnostics
            # for the whole turn (adaptive loop included) can sit unwritten.
            print(f"[chat_stream error] thread={thread_id}: {type(exc).__name__}: {exc}", flush=True)
            yield ("error", "Something went wrong on my end. Please try again in a moment.")
        finally:
            with _active_stop_guard:
                if _active_stop_events.get(thread_id) is stop_event:
                    del _active_stop_events[thread_id]


def _is_groq_tool_failure(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "tool_use_failed" in text
        or "failed to call a function" in text
        or "failed_generation" in text
        or "tool call validation failed" in text
        or "which was not in request.tools" in text
        or ("invalid_request_error" in text and "tool" in text)
    )


def _parse_malformed_tool_name(raw_name: str):
    """Split Groq/Llama glitches like web_search{\"query\": \"...\"} into name + args."""
    raw = (raw_name or "").strip()
    if "{" not in raw:
        return raw, None
    tool_name = raw.split("{", 1)[0].strip()
    args_str = "{" + raw.split("{", 1)[1]
    try:
        return tool_name, json.loads(args_str)
    except json.JSONDecodeError:
        return tool_name, None


def _tool_name_pattern() -> str:
    return "|".join(re.escape(n) for n in sorted(TOOL_NAMES))


def _tool_calls_from_text(text: str) -> list:
    """
    Recover tool calls when the model writes them as plain text / XML instead of
    structured tool_calls — e.g. <web_search>{\"query\": \"...\"}</web_search>.
    """
    text = (text or "").strip()
    if not text:
        return []

    pattern = _tool_name_pattern()

    xml_match = re.search(
        rf"<({pattern})>\s*(\{{.*?\}})\s*</\1>",
        text,
        re.DOTALL | re.IGNORECASE,
    )
    if xml_match:
        name, args_str = xml_match.group(1), xml_match.group(2)
        try:
            return [{"name": name, "args": json.loads(args_str)}]
        except json.JSONDecodeError:
            pass

    inline_match = re.match(rf"^({pattern})\s*(\{{.*\}})\s*$", text, re.DOTALL)
    if inline_match:
        name, args_str = inline_match.group(1), inline_match.group(2)
        try:
            return [{"name": name, "args": json.loads(args_str)}]
        except json.JSONDecodeError:
            pass

    return []


def _aimessage_with_tool_calls(name: str, args: dict, msg: Optional[AIMessage] = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": args,
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "tool_call",
            }
        ],
        id=getattr(msg, "id", None) if msg else None,
    )


def _sanitize_ai_response(msg: AIMessage) -> AIMessage:
    """
    Repair malformed tool calls from Groq/Llama before LangGraph runs ToolNode.
    Fixes names like web_search{\"query\": \"...\"} merged into one string.
    Drops tool calls with an empty/unrecognized name instead of forwarding them —
    a malformed tool_call/ToolMessage pair persisted into history breaks Groq's
    Harmony renderer on the NEXT request ("Tools should have a name!").
    """
    tool_calls = list(getattr(msg, "tool_calls", None) or [])
    if tool_calls:
        fixed_calls = []
        dropped_any = False
        for tc in tool_calls:
            name = (tc.get("name") or "").strip()
            args = tc.get("args") or {}
            parsed_name, parsed_args = _parse_malformed_tool_name(name)
            if parsed_name in TOOL_NAMES:
                new_args = parsed_args if parsed_args else args
                fixed_calls.append(
                    {**tc, "name": parsed_name, "args": new_args, "type": tc.get("type") or "tool_call"}
                )
            elif name in TOOL_NAMES:
                fixed_calls.append(tc)
            else:
                dropped_any = True
        if fixed_calls:
            return AIMessage(content=msg.content or "", tool_calls=fixed_calls, id=getattr(msg, "id", None))
        if dropped_any:
            fallback = extract_ai_text(msg.content) or "I couldn't complete that tool call — could you rephrase?"
            return AIMessage(content=fallback, id=getattr(msg, "id", None))
        return msg

    text = extract_ai_text(msg.content).strip()
    if not text:
        return msg

    recovered = _tool_calls_from_text(text)
    if recovered:
        tc = recovered[0]
        if tc["name"] in TOOL_NAMES:
            return _aimessage_with_tool_calls(tc["name"], tc["args"], msg)

    inline = re.match(
        rf"^({_tool_name_pattern()})\s*(\{{.*\}})\s*$",
        text,
        re.DOTALL,
    )
    if inline:
        name, args = inline.group(1), json.loads(inline.group(2))
        if name in TOOL_NAMES:
            return _aimessage_with_tool_calls(name, args, msg)
    return msg


def _emit_text_to_writer(text: str, writer) -> None:
    if text and writer:
        writer({"token": text})


def _stopped_reply(gathered) -> AIMessage:
    """What a stopped generation leaves in history: the text produced so far,
    never a half-streamed tool call (an unanswered tool_call breaks the next
    Groq request)."""
    text = extract_ai_text(getattr(gathered, "content", "")) if gathered is not None else ""
    return AIMessage(content=text, id=getattr(gathered, "id", None))


def _stream_bound_llm(bound_llm, messages, writer, should_stop=None) -> AIMessage:
    """Stream tokens from Groq; return the assembled AIMessage.
    should_stop() is checked per chunk — breaking out closes the Groq stream,
    so a stopped answer stops costing tokens immediately."""
    gathered = None
    for chunk in bound_llm.stream(messages):
        if should_stop and should_stop():
            return _stopped_reply(gathered)
        gathered = chunk if gathered is None else gathered + chunk
        if getattr(chunk, "tool_call_chunks", None) or getattr(chunk, "tool_calls", None):
            continue
        token = extract_chunk_text(chunk)
        if not token or not writer:
            continue
        if re.search(rf"</?({_tool_name_pattern()})>", token, re.IGNORECASE):
            continue
        writer({"token": token})
    result = gathered if gathered is not None else AIMessage(content="")
    return _sanitize_ai_response(result)


def _invoke_bound_llm(bound_llm, messages, writer, should_stop=None) -> AIMessage:
    """Non-stream invoke — more reliable for tool calls on Groq Llama.
    Can't be interrupted mid-call; a Stop during it discards any tool calls."""
    response = _sanitize_ai_response(bound_llm.invoke(messages))
    if should_stop and should_stop():
        return _stopped_reply(response)
    if not (getattr(response, "tool_calls", None) or []):
        _emit_text_to_writer(extract_ai_text(response.content), writer)
    return response


def _run_chat_llm(messages, writer, should_stop=None) -> AIMessage:
    """
    Run chat with tools. Invoke-first for reliable tool JSON on Groq Llama.
    Streaming is fallback only — avoids narrating XML tool calls as the answer.
    """
    candidates = [
        (llm_with_tools_invoke, llm_with_tools),
        (_tool_fallback_with_tools_invoke, _tool_fallback_with_tools),
    ]

    # Always invoke-first: stream-first caused models to print <tool>{json}</tool> as text
    last_error = None
    for invoke_llm, stream_llm in candidates:
        if invoke_llm is None or stream_llm is None:
            continue
        for runner, bound in ((_stream_bound_llm, stream_llm), (_invoke_bound_llm, invoke_llm)):
            if should_stop and should_stop():  # e.g. stopped during a failed first attempt
                return AIMessage(content="")
            try:
                return runner(bound, messages, writer, should_stop=should_stop)
            except Exception as exc:
                last_error = exc
                if not _is_groq_tool_failure(exc):
                    raise

    raise last_error or RuntimeError("Chat model failed without a specific error.")


def chat_node(state: ChatState, config: RunnableConfig):
    """Main Chat Node — streams Groq tokens to the client while building the final AIMessage."""
    if _stop_requested(config):
        return {"messages": []}
    summary = state.get("summary", "")
    messages = state["messages"]
    adaptive_feedback = state.get("adaptive_feedback", "")

    voice_mode = bool((config or {}).get("configurable", {}).get("voice_mode", False))
    system_parts = [SYSTEM_PROMPT, _reference_datetime_context()]
    if voice_mode:
        system_parts.append(VOICE_ADDENDUM)
    if summary:
        system_parts.append(f"Long-Term Memory (Summary of past events):\n{summary}")
    if adaptive_feedback:
        system_parts.append(f"Adaptive feedback:\n{adaptive_feedback}")

    system_msg = SystemMessage(content="\n\n".join(system_parts))
    messages = [system_msg] + messages

    writer = get_stream_writer()
    if adaptive_feedback and writer:
        # A second (or later) pass informed by the evaluator — let the UI
        # show "refining" instead of the generic typing indicator.
        writer({"status": "refining"})
    gathered = _run_chat_llm(messages, writer, should_stop=lambda: _stop_requested(config))
    if _stop_requested(config) and not extract_ai_text(gathered.content).strip():
        return {"messages": []}  # stopped before any answer text: save nothing
    return {"messages": [gathered]}


def adaptive_evaluator_node(state: ChatState, config: RunnableConfig):
    """Judge whether this turn's tool evidence satisfies the ORIGINAL user
    request. Never streams to the user; failures fall back to normal chat."""
    if _stop_requested(config):
        return {"adaptive_feedback": ""}
    writer = get_stream_writer()
    if writer:
        writer({"status": "evaluating"})

    messages = state["messages"]
    current_turn = get_current_turn_messages(messages)
    original_question = get_original_user_query(messages)
    tool_context = build_tool_context(current_turn)
    previous_feedback = state.get("adaptive_feedback", "")
    iteration = state.get("adaptive_iteration", 0)
    thread_id = str((config or {}).get("configurable", {}).get("thread_id", ""))

    decision = invoke_evaluator(
        original_question,
        tool_context,
        previous_feedback,
        GROQ_API_KEY,
        thread_id=thread_id,
        iteration=iteration + 1,
    )
    log_adaptive_event(iteration + 1, decision)

    if decision is None:
        # Evaluator failure is non-fatal — clear adaptive state and let the
        # existing tool loop / chat_node finish naturally.
        return {"adaptive_feedback": "", "adaptive_iteration": iteration + 1}

    return {"adaptive_feedback": feedback_for_decision(decision), "adaptive_iteration": iteration + 1}


def force_finalize_node(state: ChatState, config: RunnableConfig):
    """Hard tool-cycle budget reached — synthesize a final answer WITHOUT
    calling more tools, using the same primary model but unbound from tools."""
    if _stop_requested(config):
        return {"adaptive_feedback": ""}
    writer = get_stream_writer()
    if writer:
        writer({"status": "finalizing"})

    summary = state.get("summary", "")
    messages = state["messages"]

    voice_mode = bool((config or {}).get("configurable", {}).get("voice_mode", False))
    system_parts = [SYSTEM_PROMPT, _reference_datetime_context()]
    if voice_mode:
        system_parts.append(VOICE_ADDENDUM)
    if summary:
        system_parts.append(f"Long-Term Memory (Summary of past events):\n{summary}")
    system_parts.append(f"Adaptive feedback:\n{format_force_finalize_instruction()}")

    system_msg = SystemMessage(content="\n\n".join(system_parts))
    full_messages = [system_msg] + messages

    # `llm` (unlike `llm_with_tools`) has no tools bound — it physically
    # cannot emit a tool call, guaranteeing this node terminates the loop.
    gathered = _stream_bound_llm(llm, full_messages, writer, should_stop=lambda: _stop_requested(config))
    if _stop_requested(config) and not extract_ai_text(gathered.content).strip():
        return {"adaptive_feedback": ""}
    return {"messages": [gathered], "adaptive_feedback": ""}


def route_after_tools(state: ChatState) -> Literal["adaptive_evaluator", "force_finalize", "chat_node"]:
    """Router for the 'tools' node — the adaptive-loop trigger point.

    Order matters: the hard tool-cycle budget is checked first (a safety
    net that applies even when the loop is disabled would be surprising,
    so it's gated behind adaptive_loop_enabled too), then the deterministic
    evaluator trigger.
    """
    messages = state["messages"]
    current_turn = get_current_turn_messages(messages)
    loop_enabled = state.get("adaptive_loop_enabled", ADAPTIVE_LOOP_ENABLED_DEFAULT)

    if not loop_enabled:
        return "chat_node"

    if should_force_finalize(current_turn):
        return "force_finalize"

    user_query = get_original_user_query(messages)
    iteration = state.get("adaptive_iteration", 0)
    if should_evaluate_after_tools(user_query, current_turn, iteration):
        return "adaptive_evaluator"
    return "chat_node"


def _messages_to_plain_text(messages) -> str:
    """Convert LangChain messages to plain 'role: content' text — no metadata,
    no tool_call payloads, no repr() junk — before sending to the summarizer."""
    lines = []
    for m in messages:
        role = getattr(m, "type", None) or m.__class__.__name__
        text = extract_ai_text(getattr(m, "content", ""))
        if not text and getattr(m, "tool_calls", None):
            names = ", ".join(tc.get("name", "?") for tc in m.tool_calls)
            text = f"[called tool(s): {names}]"
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


_RAW_WINDOW_CHAR_BUDGET = 4000


def _split_by_char_budget(messages, budget: int):
    """Walk backwards from the most recent message, keeping messages raw
    until the char budget runs out. Always keeps at least the last message,
    even if it alone exceeds the budget.

    Guarantee: the kept/raw window must include at least the most recent
    HumanMessage, even if that means exceeding the budget slightly. Some
    model chat templates (e.g. Qwen) hard-require a user-role message to be
    present and error out otherwise ("No user query found in messages")."""
    kept = []
    total = 0
    for m in reversed(messages):
        content = getattr(m, "content", "")
        size = len(content) if isinstance(content, str) else len(str(content))
        if kept and total + size > budget:
            break
        kept.append(m)
        total += size
    kept.reverse()
    cutoff = len(messages) - len(kept)

    if not any(isinstance(m, HumanMessage) for m in kept):
        for i in range(cutoff - 1, -1, -1):
            if isinstance(messages[i], HumanMessage):
                cutoff = i
                kept = messages[cutoff:]
                break

    return messages[:cutoff], kept


def summarize_conversation(state: ChatState, config: RunnableConfig):
    """Compresses old messages into a summary. Only runs once a turn has fully
    completed (see should_summarize) — never mid-tool-loop."""
    summary = state.get("summary", "")
    if _stop_requested(config):  # a stopped turn ends now; summarize after the next one
        return {"summary": summary}
    messages = state["messages"]

    messages_to_summarize, _kept_raw = _split_by_char_budget(messages, _RAW_WINDOW_CHAR_BUDGET)

    if not messages_to_summarize:
        return {"summary": summary}

    plain_text = _messages_to_plain_text(messages_to_summarize)

    if summary:
        prompt = (
            f"Current Summary: {summary}\n\n"
            "New lines to add:\n"
            f"{plain_text}\n\n"
            "INSTRUCTION: Rewrite the summary (do not just append). Keep it under ~250 words. "
            "PRESERVE specific entities (names, dates, errors, code snippets). Do not lose technical details."
        )
    else:
        prompt = (
            "Create a summary of this conversation in under ~250 words. "
            "PRESERVE specific entities (names, dates, errors, code snippets).\n\n"
            f"Lines:\n{plain_text}"
        )

    response = summary_llm.invoke(
        [HumanMessage(content=prompt)],
        max_tokens=400,
    )
    new_summary = response.content

    delete_messages = [RemoveMessage(id=m.id) for m in messages_to_summarize]

    return {"summary": new_summary, "messages": delete_messages}


def should_summarize(state: ChatState) -> Literal["tools", "summarize_conversation", END]:
    """Routes to tool execution if the model requested it. Once the turn is fully
    complete (no more tool calls), checks context size and routes to summarization
    if needed — this only prepares history for the NEXT turn, it never interrupts
    the current one mid-flight."""
    messages = state["messages"]
    if hasattr(messages[-1], "tool_calls") and len(messages[-1].tool_calls) > 0:
        return "tools"
    to_summarize, _kept_raw = _split_by_char_budget(messages, _RAW_WINDOW_CHAR_BUDGET)
    if to_summarize:
        return "summarize_conversation"
    return END


# 5. GRAPH CONSTRUCTION

# DATA_DIR lets deployments with an ephemeral container filesystem (e.g. a
# free Hugging Face Space, which wipes non-repo files on every rebuild)
# point chatbot.db at an attached persistent volume instead. Defaults to
# the backend directory — unchanged behavior for local dev.
_DATA_DIR = os.getenv("DATA_DIR", _BACKEND_DIR)
os.makedirs(_DATA_DIR, exist_ok=True)
_CHATBOT_DB_PATH = os.path.join(_DATA_DIR, "chatbot.db")

conn = sqlite3.connect(_CHATBOT_DB_PATH, check_same_thread=False, timeout=30)
configure_sqlite_connection(conn)
checkpointer = SqliteSaver(conn=conn)
checkpointer.setup()

graph = StateGraph(ChatState)

graph.add_node("chat_node", chat_node)
graph.add_node("tools", ToolNode(tools))
graph.add_node("adaptive_evaluator", adaptive_evaluator_node)
graph.add_node("force_finalize", force_finalize_node)
graph.add_node("summarize_conversation", summarize_conversation)

graph.add_edge(START, "chat_node")
graph.add_conditional_edges("chat_node", should_summarize)
# Adaptive-loop trigger point: after tools run, either go straight back to
# chat_node (ordinary single-step case — no unnecessary evaluator), into the
# adaptive_evaluator when the deterministic trigger fires, or into
# force_finalize once the hard tool-cycle budget is spent.
graph.add_conditional_edges("tools", route_after_tools)
# The evaluator never answers the user directly — it always routes back to
# chat_node, which owns tool selection, generation, and streaming.
graph.add_edge("adaptive_evaluator", "chat_node")
# force_finalize's output never contains tool calls (unbound model), so the
# same should_summarize router is safe to reuse here.
graph.add_conditional_edges("force_finalize", should_summarize)
graph.add_edge("summarize_conversation", END)

chatbot = graph.compile(checkpointer=checkpointer)

# HELPER FUNCTIONS

def retrieve_all_threads():
    cursor = conn.cursor()
    cursor.execute("SELECT DISTINCT thread_id FROM checkpoints")
    return [row[0] for row in cursor.fetchall()]

def delete_thread_data(thread_id):
    cursor = conn.cursor()
    cursor.execute("DELETE FROM checkpoints WHERE thread_id = ?", (thread_id,))
    cursor.execute("DELETE FROM writes WHERE thread_id = ?", (thread_id,))
    conn.commit()

def generate_summary(text):
    """Title Generator (Separate from Memory Summary)"""
    try:
        msg = f"Summarize this into a 3-5 word title. No quotes: {text}"
        response = summary_llm.invoke([HumanMessage(content=msg)])
        title = response.content.strip().replace('"', '').replace("'", "").replace("Title:", "").strip()
        return title if len(title) < 30 else title[:27] + "..."
    except:
        return "New Conversation" 