"""
Integration tests for the adaptive-loop graph wiring in chatbot_backend.py.

These tests build a SEPARATE compiled graph that reuses the exact same node
functions (chat_node, adaptive_evaluator_node, force_finalize_node,
route_after_tools, should_summarize) and edge topology as production, but:
  - swaps the real (network-calling) tools for a single fake in-memory tool
  - uses an in-memory MemorySaver checkpointer instead of the real chatbot.db
  - monkeypatches the chat LLM and the evaluator so nothing calls Groq

This gives high-fidelity coverage of the adaptive loop's control flow
(state reset across turns, tool-cycle budget enforcement, the
enabled/disabled switch) without any network access or live model calls.
"""
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

import chatbot_backend as cb
from adaptive_loop import LoopDecision


@tool
def fake_search(query: str) -> str:
    """A fake tool standing in for web_search etc. in tests."""
    return "placeholder"  # overwritten by FakeChatLLM/tool patching per-test where needed


def _build_test_graph():
    """Same node functions + edges as production, fake tools + memory checkpointer."""
    graph = StateGraph(cb.ChatState)
    graph.add_node("chat_node", cb.chat_node)
    graph.add_node("tools", ToolNode([fake_search]))
    graph.add_node("adaptive_evaluator", cb.adaptive_evaluator_node)
    graph.add_node("force_finalize", cb.force_finalize_node)
    graph.add_node("summarize_conversation", cb.summarize_conversation)

    graph.add_edge(START, "chat_node")
    graph.add_conditional_edges("chat_node", cb.should_summarize)
    graph.add_conditional_edges("tools", cb.route_after_tools)
    graph.add_edge("adaptive_evaluator", "chat_node")
    graph.add_conditional_edges("force_finalize", cb.should_summarize)
    graph.add_edge("summarize_conversation", END)
    return graph.compile(checkpointer=MemorySaver())


class FakeChatLLM:
    """Stand-in for cb._run_chat_llm — pops canned AIMessages, records inputs."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []  # each entry: the full `messages` list passed in

    def __call__(self, messages, writer, should_stop=None):
        self.calls.append(messages)
        if not self.responses:
            raise AssertionError("FakeChatLLM ran out of canned responses")
        return self.responses.pop(0)


class FakeEvaluator:
    """Stand-in for adaptive_loop.invoke_evaluator (patched as cb.invoke_evaluator)."""

    def __init__(self, decisions):
        self.decisions = list(decisions)
        self.call_count = 0

    def __call__(self, original_question, tool_context, previous_feedback, groq_api_key, **kwargs):
        self.call_count += 1
        if not self.decisions:
            raise AssertionError("FakeEvaluator ran out of canned decisions")
        return self.decisions.pop(0)


def _tool_call_message(args, call_id="c1"):
    return AIMessage(content="", tool_calls=[{"name": "fake_search", "args": args, "id": call_id, "type": "tool_call"}])


def _final_answer_message(text="Here is the final answer."):
    return AIMessage(content=text)


def _new_turn_input(message: str) -> dict:
    """Mirrors the reset iter_chat_stream performs on every new user message."""
    return {
        "messages": [HumanMessage(content=message)],
        "adaptive_feedback": "",
        "adaptive_iteration": 0,
        "adaptive_loop_enabled": True,
    }


@pytest.fixture
def test_chatbot():
    return _build_test_graph()


def _system_text(messages) -> str:
    return messages[0].content


# ─────────────────────────────────────────────────────────────────────────────
# Test 8 & 9 — adaptive iteration resets / does not leak across turns
# ─────────────────────────────────────────────────────────────────────────────


def test_adaptive_state_resets_and_does_not_leak_across_turns(test_chatbot, monkeypatch):
    thread_id = f"test-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}

    # Turn 1: multi-part query -> two evaluator passes -> finish -> final answer.
    fake_llm = FakeChatLLM(
        [
            _tool_call_message({"query": "NVDA price"}, call_id="c1"),
            _tool_call_message({"query": "NVDA news today"}, call_id="c2"),
            _final_answer_message("NVDA is at $180, up on strong earnings."),
        ]
    )
    fake_eval = FakeEvaluator(
        [
            LoopDecision(decision="continue", reason="missing reason", missing_information=["reason for move"], next_objective="find news", recommended_tool="web_search"),
            LoopDecision(decision="finish", reason="sufficient"),
        ]
    )
    monkeypatch.setattr(cb, "_run_chat_llm", fake_llm)
    monkeypatch.setattr(cb, "invoke_evaluator", fake_eval)

    turn1_input = _new_turn_input("What's Nvidia's latest price and why is it moving today?")
    test_chatbot.invoke(turn1_input, config=config)

    final_state = test_chatbot.get_state(config).values
    assert final_state["adaptive_iteration"] == 2
    assert fake_eval.call_count == 2
    # Turn 1 ends with "finish" feedback still sitting in state — this is
    # expected; iter_chat_stream (not this graph) is responsible for the
    # per-turn reset, exactly like the assertion below verifies.
    assert final_state["adaptive_feedback"] != ""

    # Turn 2: a fresh, simple, single-tool query. Reset the fake LLM/evaluator.
    fake_llm2 = FakeChatLLM(
        [
            _tool_call_message({"query": "TSLA price"}, call_id="c3"),
            _final_answer_message("Tesla is at $250."),
        ]
    )
    fake_eval2 = FakeEvaluator([])  # must NOT be called this turn
    monkeypatch.setattr(cb, "_run_chat_llm", fake_llm2)
    monkeypatch.setattr(cb, "invoke_evaluator", fake_eval2)

    turn2_input = _new_turn_input("And what about Tesla?")
    test_chatbot.invoke(turn2_input, config=config)

    # The critical leak check: chat_node's very first call this turn must NOT
    # carry turn 1's "finish" feedback content. Note: the STATIC system
    # prompt always contains the literal heading text "Adaptive feedback:"
    # as a formatting instruction — that's expected and turn-independent.
    # What must NOT appear is turn 1's actual per-turn feedback payload.
    first_call_system_text = _system_text(fake_llm2.calls[0])
    # This exact phrase only comes from format_finish_feedback()'s per-turn
    # payload (see adaptive_loop.py) — it does not appear anywhere in the
    # static SYSTEM_PROMPT, so its presence would prove turn 1 leaked.
    assert "generate the final answer now" not in first_call_system_text.lower()
    assert fake_eval2.call_count == 0  # simple single-tool turn — no trigger

    state_after_turn2 = test_chatbot.get_state(config).values
    assert state_after_turn2["adaptive_iteration"] == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 11 — tool-cycle limit enforced, force_finalize takes over
# ─────────────────────────────────────────────────────────────────────────────


def test_tool_cycle_limit_forces_finalize_without_extra_tool_call(test_chatbot, monkeypatch):
    thread_id = f"test-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}

    # A stubborn "model" that always wants another tool call — should be cut
    # off at ADAPTIVE_LOOP_MAX_TOOL_CYCLES regardless.
    responses = [_tool_call_message({"query": f"q{i}"}, call_id=f"c{i}") for i in range(cb.ADAPTIVE_LOOP_MAX_TOOL_CYCLES + 2)]
    fake_llm = FakeChatLLM(responses)
    fake_eval = FakeEvaluator([])  # evaluator must never fire — force_finalize check comes first
    finalize_calls = []

    def fake_stream_bound_llm(bound_llm, messages, writer, should_stop=None):
        finalize_calls.append(messages)
        return _final_answer_message("Best-effort answer from partial evidence.")

    monkeypatch.setattr(cb, "_run_chat_llm", fake_llm)
    monkeypatch.setattr(cb, "invoke_evaluator", fake_eval)
    monkeypatch.setattr(cb, "_stream_bound_llm", fake_stream_bound_llm)

    turn_input = _new_turn_input("Keep searching for more obscure trivia.")
    test_chatbot.invoke(turn_input, config=config)

    # chat_node should have been called exactly MAX_TOOL_CYCLES times (each
    # producing a tool call) before force_finalize took over.
    assert len(fake_llm.calls) == cb.ADAPTIVE_LOOP_MAX_TOOL_CYCLES
    assert len(finalize_calls) == 1
    # force_finalize's prompt must carry the budget-exhausted instruction.
    assert "tool execution budget" in _system_text(finalize_calls[0]).lower()
    assert fake_eval.call_count == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 12 — adaptive loop disabled preserves the original single-loop behavior
# ─────────────────────────────────────────────────────────────────────────────


def test_adaptive_loop_disabled_never_calls_evaluator_or_force_finalize(test_chatbot, monkeypatch):
    thread_id = f"test-{uuid.uuid4()}"
    config = {"configurable": {"thread_id": thread_id}}

    # Even a multi-part query that WOULD normally trigger the evaluator...
    fake_llm = FakeChatLLM(
        [
            _tool_call_message({"query": "NVDA price"}, call_id="c1"),
            _final_answer_message("NVDA is at $180."),
        ]
    )
    fake_eval = FakeEvaluator([])  # must never be invoked
    monkeypatch.setattr(cb, "_run_chat_llm", fake_llm)
    monkeypatch.setattr(cb, "invoke_evaluator", fake_eval)

    turn_input = {
        "messages": [HumanMessage(content="What's Nvidia's latest price and why is it moving today?")],
        "adaptive_feedback": "",
        "adaptive_iteration": 0,
        "adaptive_loop_enabled": False,  # <-- the emergency rollback switch
    }
    test_chatbot.invoke(turn_input, config=config)

    assert fake_eval.call_count == 0
    final_state = test_chatbot.get_state(config).values
    assert final_state["adaptive_iteration"] == 0
    assert final_state["adaptive_feedback"] == ""
