"""
Adaptive Agent Loop — evidence-sufficiency evaluation for Synapse AI.

This module contains ONLY the logic for the adaptive loop: configuration,
the structured evaluator schema/prompt, deterministic trigger detection,
current-turn message extraction, tool-call signature tracking, and feedback
formatting. The LangGraph wiring (nodes, edges) lives in chatbot_backend.py.

Design goals (see docs/ADAPTIVE_LOOP.md for the full write-up):
- Bounded: at most ADAPTIVE_LOOP_MAX_ITERATIONS evaluator calls and
  ADAPTIVE_LOOP_MAX_TOOL_CYCLES tool executions per user turn.
- Cheap: the evaluator only runs when a deterministic trigger fires — never
  on every tool call, and never as a second LLM classifying every query.
- Safe: evaluator failure never breaks the main chat path — callers should
  treat a None LoopDecision as "skip adaptive feedback, continue normally".
- Isolated: nothing here imports from chatbot_backend.py (avoids circular
  imports) and none of the evaluator's own text is user-visible.
"""
from __future__ import annotations

import json
import os
import re
from typing import Iterable, List, Literal, Optional

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────────────────────
# 1. CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


ADAPTIVE_LOOP_ENABLED_DEFAULT: bool = _env_bool("ADAPTIVE_LOOP_ENABLED", True)
ADAPTIVE_LOOP_MAX_ITERATIONS: int = _env_int("ADAPTIVE_LOOP_MAX_ITERATIONS", 2)
ADAPTIVE_LOOP_MAX_TOOL_CYCLES: int = _env_int("ADAPTIVE_LOOP_MAX_TOOL_CYCLES", 4)
GROQ_LOOP_EVALUATOR_MODEL: str = os.getenv("GROQ_LOOP_EVALUATOR_MODEL", "openai/gpt-oss-20b")

MAX_EVALUATOR_CONTEXT_CHARS = 6000
EVALUATOR_MAX_TOKENS = 256


# ─────────────────────────────────────────────────────────────────────────────
# 2. EVALUATOR SCHEMA
# ─────────────────────────────────────────────────────────────────────────────


class LoopDecision(BaseModel):
    """Structured output of the adaptive evaluator. Never shown to the user."""

    decision: Literal["continue", "finish"]
    reason: str = ""
    missing_information: List[str] = Field(default_factory=list)
    next_objective: str = ""
    recommended_tool: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# 3. SMALL LOCAL TEXT HELPERS (no dependency on chatbot_backend)
# ─────────────────────────────────────────────────────────────────────────────


def _content_to_text(content) -> str:
    """Normalize BaseMessage.content (str or multimodal list) to plain text."""
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


# ─────────────────────────────────────────────────────────────────────────────
# 4. CURRENT-TURN MESSAGE EXTRACTION
# ─────────────────────────────────────────────────────────────────────────────


def get_current_turn_messages(messages: List[BaseMessage]) -> List[BaseMessage]:
    """Everything from (and including) the most recent HumanMessage onward.

    Prevents old conversation turns from bloating evaluator context — the
    evaluator should only judge the CURRENT request against evidence
    gathered this turn.
    """
    last_human_idx: Optional[int] = None
    for i in range(len(messages) - 1, -1, -1):
        if isinstance(messages[i], HumanMessage):
            last_human_idx = i
            break
    if last_human_idx is None:
        return list(messages)
    return list(messages[last_human_idx:])


def get_original_user_query(messages: List[BaseMessage]) -> str:
    """The most recent HumanMessage's text — the immutable original request."""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return _content_to_text(m.content)
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# 5. TOOL-CALL SIGNATURE TRACKING (prevents repetitive identical calls)
# ─────────────────────────────────────────────────────────────────────────────


def tool_call_signature(name: str, args: dict) -> str:
    """Normalized 'name|{sorted json args}' signature for de-duplication."""
    try:
        normalized_args = json.dumps(args or {}, sort_keys=True, separators=(",", ":"))
    except TypeError:
        normalized_args = str(args)
    return f"{name}|{normalized_args}"


def extract_tool_signatures(messages: Iterable[BaseMessage]) -> List[str]:
    """All tool-call signatures issued so far in the given message window."""
    signatures: List[str] = []
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            signatures.append(tool_call_signature(tc.get("name", ""), tc.get("args", {}) or {}))
    return signatures


def distinct_tool_names(messages: Iterable[BaseMessage]) -> set:
    names = set()
    for m in messages:
        for tc in getattr(m, "tool_calls", None) or []:
            name = tc.get("name")
            if name:
                names.add(name)
    return names


def count_tool_results(messages: Iterable[BaseMessage]) -> int:
    """Number of ToolMessage results (~ one per executed tool call) in the window."""
    return sum(1 for m in messages if isinstance(m, ToolMessage))


# ─────────────────────────────────────────────────────────────────────────────
# 6. DETERMINISTIC TRIGGER DETECTION
# ─────────────────────────────────────────────────────────────────────────────

# Tool-failure / no-result signals. Word-boundary based to avoid false
# positives on ordinary content that merely mentions e.g. "error" in passing
# (still not perfect, but far safer than a bare substring check).
_FAILURE_SIGNAL_RE = re.compile(
    r"\b("
    r"failed|error|errors|"
    r"no data|no results?|no matching|no web results|"
    r"no articles? found|no location found|"
    r"unavailable|could not|couldn't|"
    r"unable to|not found|lookup failed"
    r")\b",
    re.IGNORECASE,
)

# Multi-part / multi-step query signals. Kept intentionally small and
# regex-based rather than a learned classifier — see module docstring.
_MULTI_PART_PATTERNS = [
    r"\bwhy\b",
    r"\bhow did\b",
    r"\bhow does\b",
    r"\bcompare\b",
    r"\bversus\b",
    r"\bvs\.?\b",
    r"\bdifference\b",
    r"\bthen\b",
    r"\balso\b",
    r"\bbased on\b",
    r"\band explain\b",
    r"\bexplain.*\band\b",
    r"\band why\b",
    r"\bwhat happened and why\b",
]
_MULTI_PART_RE = re.compile("|".join(_MULTI_PART_PATTERNS), re.IGNORECASE)


def tool_results_indicate_failure(current_turn_messages: Iterable[BaseMessage]) -> bool:
    """True if any ToolMessage in the current turn looks like a failed/empty result."""
    for m in current_turn_messages:
        if isinstance(m, ToolMessage):
            text = _content_to_text(m.content)
            if _FAILURE_SIGNAL_RE.search(text):
                return True
    return False


def is_multi_part_query(user_query: str) -> bool:
    """Heuristic: does the ORIGINAL request look like it has multiple sub-asks?"""
    if not user_query:
        return False
    return bool(_MULTI_PART_RE.search(user_query))


def should_evaluate_after_tools(
    user_query: str,
    current_turn_messages: List[BaseMessage],
    iteration: int,
) -> bool:
    """Deterministic gate for whether the (costly-ish) evaluator should run.

    Only called after a tool has already executed this turn, so trivial
    no-tool turns ("Hello", small talk) never reach this function at all.

    Returns True when:
      A. a tool result looks like a failure/no-result, OR
      B. the original query reads as multi-part/multi-step, OR
      C. more than one distinct tool has been used this turn.
    Always False once the per-turn evaluator iteration budget is spent.
    """
    if iteration >= ADAPTIVE_LOOP_MAX_ITERATIONS:
        return False
    if tool_results_indicate_failure(current_turn_messages):
        return True
    if is_multi_part_query(user_query):
        return True
    if len(distinct_tool_names(current_turn_messages)) > 1:
        return True
    return False


# ─────────────────────────────────────────────────────────────────────────────
# 7. EVALUATOR PROMPT CONSTRUCTION
# ─────────────────────────────────────────────────────────────────────────────

_EVALUATOR_SYSTEM_PROMPT = """You are the evidence-sufficiency evaluator for Synapse AI.

Your job is NOT to answer the user.

Your job is to decide whether the information collected so far is sufficient
to answer the user's original request accurately and completely.

Rules:
1. Compare the actual user request against the evidence collected.
2. Identify explicit sub-questions that remain unanswered.
3. Do not ask for more information merely because more information could exist.
4. Do not request a second search simply to confirm a result that already directly answers the question.
5. Continue only when there is a concrete unresolved requirement.
6. If a tool failed, returned no data, returned an error, or clearly produced insufficient evidence, continue when another action could reasonably recover.
7. If the request is fully answerable from the available evidence, finish.
8. Never invent missing facts.
9. Keep missing_information concise.
10. next_objective must be an actionable description of what information is still needed.
11. recommended_tool should contain the most appropriate existing Synapse tool name when obvious; otherwise use an empty string.

Return the structured decision."""


def build_tool_context(current_turn_messages: Iterable[BaseMessage]) -> str:
    """Render this turn's tool calls/results as compact text for the evaluator."""
    parts: List[str] = []
    for m in current_turn_messages:
        if isinstance(m, AIMessage) and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                try:
                    args_str = json.dumps(tc.get("args", {}) or {})
                except TypeError:
                    args_str = str(tc.get("args"))
                parts.append(f"Tool call: {tc.get('name')}({args_str})")
        elif isinstance(m, ToolMessage):
            text = _content_to_text(m.content)
            tool_name = getattr(m, "name", None) or "tool"
            parts.append(f"Tool result ({tool_name}): {text}")

    context = "\n".join(parts)
    if len(context) > MAX_EVALUATOR_CONTEXT_CHARS:
        context = context[:MAX_EVALUATOR_CONTEXT_CHARS] + "\n[truncated]"
    return context or "(no tool activity recorded)"


def build_evaluator_messages(
    original_question: str,
    tool_context: str,
    previous_feedback: str,
) -> list:
    """Build the (system, human) message pair sent to the evaluator LLM."""
    human = (
        f"Original user request:\n{original_question}\n\n"
        f"Tool calls/results from the current turn:\n{tool_context}\n\n"
        f"Previous adaptive feedback:\n{previous_feedback or '(none — first pass)'}"
    )
    return [
        ("system", _EVALUATOR_SYSTEM_PROMPT),
        ("human", human),
    ]


# ─────────────────────────────────────────────────────────────────────────────
# 8. EVALUATOR INVOCATION
# ─────────────────────────────────────────────────────────────────────────────

_evaluator_runnable = None


def _build_evaluator_runnable(groq_api_key: str):
    """Lazily construct the structured-output evaluator runnable.

    Isolated behind a function (rather than built at import time) so unit
    tests can monkeypatch `invoke_evaluator` / this factory without needing
    a live GROQ_API_KEY or network access.
    """
    from langchain_groq import ChatGroq

    kwargs = dict(
        model=GROQ_LOOP_EVALUATOR_MODEL,
        temperature=0,
        api_key=groq_api_key,
        streaming=False,
        request_timeout=30,
        max_tokens=EVALUATOR_MAX_TOKENS,
    )
    try:
        llm = ChatGroq(reasoning_effort="low", **kwargs)
    except Exception:
        # Installed langchain-groq/model combo may not support reasoning_effort.
        llm = ChatGroq(**kwargs)
    return llm.with_structured_output(LoopDecision, method="json_schema", strict=True)


def get_evaluator_runnable(groq_api_key: str):
    global _evaluator_runnable
    if _evaluator_runnable is None:
        _evaluator_runnable = _build_evaluator_runnable(groq_api_key)
    return _evaluator_runnable


def invoke_evaluator(
    original_question: str,
    tool_context: str,
    previous_feedback: str,
    groq_api_key: str,
    thread_id: str = "",
    iteration: int = 0,
) -> Optional[LoopDecision]:
    """Run the structured evaluator. Returns None on ANY failure (never raises) —
    callers must treat None as "skip adaptive feedback, fall back to normal chat".

    `thread_id`/`iteration` are passed through as LangSmith run tags/metadata
    only (never logged) — when LANGCHAIN_TRACING_V2 is on, this makes the
    evaluator's own LLM call show up in a trace as its own named, filterable
    run ("adaptive_loop:evaluator") instead of just being an unlabeled child
    call nested under the "adaptive_evaluator" graph node.
    """
    try:
        runnable = get_evaluator_runnable(groq_api_key)
        messages = build_evaluator_messages(original_question, tool_context, previous_feedback)
        run_config = {
            "run_name": "adaptive_loop:evaluator",
            "tags": ["adaptive_loop", "evaluator"],
            "metadata": {"adaptive_iteration": iteration, "thread_id": thread_id},
        }
        result = runnable.invoke(messages, config=run_config)
        if isinstance(result, LoopDecision):
            return result
        if isinstance(result, dict):
            return LoopDecision(**result)
        return None
    except Exception as exc:  # evaluator is an enhancement, never a hard dependency
        # flush=True: this runs inside a ThreadPoolExecutor worker (see
        # main.py's _graph_worker) whose stdout is fully buffered by default
        # when piped/redirected — without an explicit flush these lines can
        # sit unwritten for the life of the process, unlike uvicorn's own
        # request logs which flush per line.
        print(f"[adaptive_loop] evaluator failed: {type(exc).__name__}: {exc}", flush=True)
        return None


# ─────────────────────────────────────────────────────────────────────────────
# 9. FEEDBACK FORMATTING + LOOP DECISION HELPERS
# ─────────────────────────────────────────────────────────────────────────────


def format_continue_feedback(decision: LoopDecision) -> str:
    missing = "; ".join(decision.missing_information) if decision.missing_information else "unspecified"
    lines = [
        "The previous evidence is incomplete.",
        f"Missing: {missing}.",
    ]
    if decision.next_objective:
        lines.append(f"Next objective: {decision.next_objective}.")
    if decision.recommended_tool:
        lines.append(f"Recommended tool: {decision.recommended_tool}.")
    lines.append(
        "Do not repeat the previous tool call unless the previous result failed "
        "or there is a specific missing parameter. Use another appropriate tool "
        "if necessary. Do not mention this process to the user."
    )
    return " ".join(lines)


def format_finish_feedback() -> str:
    return (
        "The available evidence is sufficient. Generate the final answer now. "
        "Do not call another tool unless an explicit user requirement remains "
        "unresolved. Do not mention this process to the user."
    )


def feedback_for_decision(decision: LoopDecision) -> str:
    if decision.decision == "finish":
        return format_finish_feedback()
    return format_continue_feedback(decision)


def format_force_finalize_instruction() -> str:
    return (
        "You have reached the tool execution budget for this turn. "
        "Do not call tools. Synthesize the best accurate answer from the "
        "evidence already collected. Clearly state what could not be "
        "established."
    )


def should_force_finalize(current_turn_messages: List[BaseMessage]) -> bool:
    """True once the per-turn tool-cycle budget has been used up."""
    return count_tool_results(current_turn_messages) >= ADAPTIVE_LOOP_MAX_TOOL_CYCLES


def log_adaptive_event(iteration: int, decision: Optional[LoopDecision]) -> None:
    """Concise, secret-free log line — never logs prompts or raw credentials.
    flush=True — see the comment in invoke_evaluator's except block; this
    also runs off the main thread, so it needs the explicit flush to show
    up promptly in server logs instead of sitting in a stdout buffer."""
    if decision is None:
        print(f"adaptive_loop iteration={iteration} decision=evaluator_failed", flush=True)
        return
    missing = ",".join(decision.missing_information) if decision.missing_information else "-"
    print(
        f"adaptive_loop iteration={iteration} decision={decision.decision} "
        f"missing={missing} next_tool={decision.recommended_tool or '-'}",
        flush=True,
    )
