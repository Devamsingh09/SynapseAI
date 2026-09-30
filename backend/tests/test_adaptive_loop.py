"""
Unit tests for backend/adaptive_loop.py — pure logic, no network access and
no dependency on chatbot_backend.py (which pulls in FAISS/embeddings at
import time). The evaluator LLM call is mocked wherever it's exercised.
"""
from unittest.mock import patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import adaptive_loop as al


def _tool_call(name, args, call_id="call_1"):
    return {"name": name, "args": args, "id": call_id, "type": "tool_call"}


def _ai_with_tool_call(name, args, call_id="call_1"):
    return AIMessage(content="", tool_calls=[_tool_call(name, args, call_id)])


def _tool_msg(name, content, call_id="call_1"):
    return ToolMessage(name=name, content=content, tool_call_id=call_id)


# ─────────────────────────────────────────────────────────────────────────────
# Test 1 & 2 — simple / single-tool turns should NOT trigger the evaluator
# ─────────────────────────────────────────────────────────────────────────────


def test_simple_greeting_has_no_tool_activity_to_trigger_on():
    # "Hello" never reaches should_evaluate_after_tools at all in the real
    # graph (no tool call happens), but as a sanity check: no tool activity
    # + a simple query must never look like a trigger condition.
    messages = [HumanMessage(content="Hello")]
    assert al.should_evaluate_after_tools("Hello", messages, iteration=0) is False


def test_single_tool_weather_query_does_not_trigger_evaluator():
    query = "What's the weather in Mumbai?"
    current_turn = [
        HumanMessage(content=query),
        _ai_with_tool_call("get_weather", {"location": "Mumbai"}),
        _tool_msg("get_weather", "Weather for Mumbai: Now: 29°C, clear sky"),
    ]
    assert al.should_evaluate_after_tools(query, current_turn, iteration=0) is False


def test_simple_calculator_query_does_not_trigger_evaluator():
    query = "What is 25 + 17?"
    current_turn = [
        HumanMessage(content=query),
        _ai_with_tool_call("calculator", {"first_num": 25, "second_num": 17, "operation": "add"}),
        _tool_msg("calculator", '{"result": 42}'),
    ]
    assert al.should_evaluate_after_tools(query, current_turn, iteration=0) is False


# ─────────────────────────────────────────────────────────────────────────────
# Test 3 — multi-part requests DO trigger the evaluator
# ─────────────────────────────────────────────────────────────────────────────


def test_multi_part_stock_query_triggers_evaluator():
    query = "What's Nvidia's latest price and why is it moving today?"
    current_turn = [
        HumanMessage(content=query),
        _ai_with_tool_call("get_stock_price", {"symbol": "NVDA"}),
        _tool_msg("get_stock_price", '{"symbol": "NVDA", "close": "180.00"}'),
    ]
    assert al.should_evaluate_after_tools(query, current_turn, iteration=0) is True


def test_is_multi_part_query_detects_common_patterns():
    assert al.is_multi_part_query("What's Tesla's stock price and why did it move?")
    assert al.is_multi_part_query("Compare Python and Rust for backend development")
    assert not al.is_multi_part_query("What's the weather in Chennai?")
    assert not al.is_multi_part_query("Hello")


# ─────────────────────────────────────────────────────────────────────────────
# Test 4 — tool failure triggers the evaluator
# ─────────────────────────────────────────────────────────────────────────────


def test_tool_failure_triggers_evaluator():
    query = "What's the weather in Zzzznotarealplace?"
    current_turn = [
        HumanMessage(content=query),
        _ai_with_tool_call("get_weather", {"location": "Zzzznotarealplace"}),
        _tool_msg("get_weather", "No location found for 'Zzzznotarealplace'."),
    ]
    assert al.should_evaluate_after_tools(query, current_turn, iteration=0) is True


def test_failure_signal_is_word_boundary_based_not_naive_substring():
    # "errorless" should not spuriously match "error" as a naive substring
    # check would; the word-boundary regex must not fire here.
    assert al.tool_results_indicate_failure(
        [_tool_msg("web_search", "The design is errorless and highly reliable.")]
    ) is False
    assert al.tool_results_indicate_failure(
        [_tool_msg("web_search", "Web search failed: ConnectionError")]
    ) is True


# ─────────────────────────────────────────────────────────────────────────────
# Test: evaluator iteration budget caps triggering
# ─────────────────────────────────────────────────────────────────────────────


def test_evaluator_does_not_trigger_past_max_iterations():
    query = "What's Nvidia's latest price and why is it moving today?"
    current_turn = [
        HumanMessage(content=query),
        _ai_with_tool_call("get_stock_price", {"symbol": "NVDA"}),
        _tool_msg("get_stock_price", '{"symbol": "NVDA"}'),
    ]
    assert al.should_evaluate_after_tools(query, current_turn, iteration=al.ADAPTIVE_LOOP_MAX_ITERATIONS) is False


# ─────────────────────────────────────────────────────────────────────────────
# Test 5 — evaluator schema parses correctly
# ─────────────────────────────────────────────────────────────────────────────


def test_loop_decision_schema_parses_continue():
    decision = al.LoopDecision(
        decision="continue",
        reason="price given, movement reason missing",
        missing_information=["reason for today's price movement"],
        next_objective="Find current news explaining the move.",
        recommended_tool="web_search",
    )
    assert decision.decision == "continue"
    assert decision.missing_information == ["reason for today's price movement"]


def test_loop_decision_schema_defaults_recommended_tool_to_empty_string():
    decision = al.LoopDecision(decision="finish", reason="fully answered")
    assert decision.recommended_tool == ""
    assert decision.missing_information == []


def test_loop_decision_rejects_invalid_decision_literal():
    with pytest.raises(Exception):
        al.LoopDecision(decision="maybe", reason="invalid")


# ─────────────────────────────────────────────────────────────────────────────
# Test 6 & 7 — feedback formatting for finish / continue
# ─────────────────────────────────────────────────────────────────────────────


def test_finish_decision_generates_final_answer_feedback():
    decision = al.LoopDecision(decision="finish", reason="sufficient evidence")
    feedback = al.feedback_for_decision(decision)
    assert "sufficient" in feedback.lower()
    assert "final answer" in feedback.lower()
    # Never leak evaluator internals like "reason=" or raw JSON.
    assert "reason=" not in feedback


def test_continue_decision_creates_actionable_feedback():
    decision = al.LoopDecision(
        decision="continue",
        reason="missing movement reason",
        missing_information=["reason for today's price movement"],
        next_objective="Find current news explaining the move.",
        recommended_tool="web_search",
    )
    feedback = al.feedback_for_decision(decision)
    assert "reason for today's price movement" in feedback
    assert "web_search" in feedback
    assert "do not repeat" in feedback.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Test 10 — identical tool-call signatures are detected
# ─────────────────────────────────────────────────────────────────────────────


def test_identical_tool_call_signatures_are_detected():
    sig1 = al.tool_call_signature("web_search", {"query": "NVDA news"})
    sig2 = al.tool_call_signature("web_search", {"query": "NVDA news"})
    sig3 = al.tool_call_signature("web_search", {"query": "different query"})
    assert sig1 == sig2
    assert sig1 != sig3


def test_tool_call_signature_ignores_arg_order():
    sig1 = al.tool_call_signature("convert_currency", {"amount": 10, "from_currency": "USD", "to_currency": "INR"})
    sig2 = al.tool_call_signature("convert_currency", {"to_currency": "INR", "from_currency": "USD", "amount": 10})
    assert sig1 == sig2


def test_extract_tool_signatures_from_messages():
    messages = [
        _ai_with_tool_call("web_search", {"query": "NVDA news"}, call_id="c1"),
        _tool_msg("web_search", "some results", call_id="c1"),
        _ai_with_tool_call("web_search", {"query": "NVDA news"}, call_id="c2"),
    ]
    sigs = al.extract_tool_signatures(messages)
    assert len(sigs) == 2
    assert sigs[0] == sigs[1]  # identical repeated call detected


# ─────────────────────────────────────────────────────────────────────────────
# Test 11 — tool-cycle limit enforcement helper
# ─────────────────────────────────────────────────────────────────────────────


def test_should_force_finalize_at_budget():
    current_turn = [HumanMessage(content="q")]
    for i in range(al.ADAPTIVE_LOOP_MAX_TOOL_CYCLES):
        current_turn.append(_tool_msg("web_search", "result", call_id=f"c{i}"))
    assert al.should_force_finalize(current_turn) is True


def test_should_force_finalize_below_budget():
    current_turn = [HumanMessage(content="q"), _tool_msg("web_search", "result")]
    assert al.should_force_finalize(current_turn) is False


# ─────────────────────────────────────────────────────────────────────────────
# Current-turn message extraction
# ─────────────────────────────────────────────────────────────────────────────


def test_get_current_turn_messages_starts_at_last_human_message():
    messages = [
        HumanMessage(content="turn 1"),
        AIMessage(content="answer 1"),
        HumanMessage(content="turn 2"),
        AIMessage(content="answer 2"),
    ]
    current = al.get_current_turn_messages(messages)
    assert len(current) == 2
    assert current[0].content == "turn 2"


def test_get_original_user_query_returns_most_recent_human_message():
    messages = [
        HumanMessage(content="old question"),
        AIMessage(content="old answer"),
        HumanMessage(content="What's Nvidia's latest price and why is it moving?"),
    ]
    assert al.get_original_user_query(messages) == "What's Nvidia's latest price and why is it moving?"


def test_evaluator_context_is_capped():
    huge_result = "x" * (al.MAX_EVALUATOR_CONTEXT_CHARS * 2)
    current_turn = [_tool_msg("web_search", huge_result)]
    context = al.build_tool_context(current_turn)
    assert len(context) <= al.MAX_EVALUATOR_CONTEXT_CHARS + len("\n[truncated]")


# ─────────────────────────────────────────────────────────────────────────────
# Evaluator invocation — mocked, never a live Groq call
# ─────────────────────────────────────────────────────────────────────────────


def test_invoke_evaluator_returns_decision_on_success():
    fake_decision = al.LoopDecision(decision="finish", reason="ok")
    with patch.object(al, "get_evaluator_runnable") as mock_get_runnable:
        mock_get_runnable.return_value.invoke.return_value = fake_decision
        result = al.invoke_evaluator("q", "context", "", "fake_key")
    assert result is fake_decision


def test_invoke_evaluator_returns_none_on_failure_never_raises():
    with patch.object(al, "get_evaluator_runnable") as mock_get_runnable:
        mock_get_runnable.return_value.invoke.side_effect = RuntimeError("groq timeout")
        result = al.invoke_evaluator("q", "context", "", "fake_key")
    assert result is None
