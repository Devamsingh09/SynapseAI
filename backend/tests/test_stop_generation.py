"""
Tests for "Stop generating": the per-chunk stop check in the streaming LLM
loop, and the end-to-end behaviour of iter_chat_stream when a stop arrives
mid-answer or mid-tool-call. Uses the same fake-graph approach as
test_graph_integration.py — no network, no Groq.
"""
import threading
import time
import uuid

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

import chatbot_backend as cb

# The tool used by the end-to-end tests sets whatever event is registered here.
_tool_hook = {"event": None}


@tool
def slow_search(query: str) -> str:
    """Fake tool; simulates the user pressing Stop while it runs."""
    if _tool_hook["event"] is not None:
        _tool_hook["event"].set()
    return "tool result"


def _graph():
    g = StateGraph(cb.ChatState)
    g.add_node("chat_node", cb.chat_node)
    g.add_node("tools", ToolNode([slow_search]))
    g.add_node("adaptive_evaluator", cb.adaptive_evaluator_node)
    g.add_node("force_finalize", cb.force_finalize_node)
    g.add_node("summarize_conversation", cb.summarize_conversation)
    g.add_edge(START, "chat_node")
    g.add_conditional_edges("chat_node", cb.should_summarize)
    g.add_conditional_edges("tools", cb.route_after_tools)
    g.add_edge("adaptive_evaluator", "chat_node")
    g.add_conditional_edges("force_finalize", cb.should_summarize)
    g.add_edge("summarize_conversation", END)
    return g.compile(checkpointer=MemorySaver())


@pytest.fixture
def graph(monkeypatch):
    g = _graph()
    monkeypatch.setattr(cb, "chatbot", g)
    _tool_hook["event"] = None
    yield g
    _tool_hook["event"] = None


def _history(g, thread_id):
    return g.get_state({"configurable": {"thread_id": thread_id}}).values.get("messages", [])


# ─────────────────────────────────────────────────────────────────────────────
# _stream_bound_llm
# ─────────────────────────────────────────────────────────────────────────────


class FakeBound:
    """Stands in for a Groq-bound model's .stream(); records whether the
    stream was closed (i.e. the HTTP stream to Groq would be released)."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def stream(self, messages):
        try:
            for c in self.chunks:
                yield c
        finally:
            self.closed = True


def test_stop_mid_stream_keeps_text_so_far_and_closes_stream():
    bound = FakeBound([AIMessageChunk(content=t) for t in ("Hel", "lo ", "wor", "ld")])
    seen = []
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 2          # stop arrives before the 3rd chunk

    out = cb._stream_bound_llm(bound, [], lambda e: seen.append(e["token"]), should_stop=should_stop)
    assert out.content == "Hello "
    assert seen == ["Hel", "lo "]
    assert not out.tool_calls
    del bound  # generator finalised


def test_stop_while_tool_call_is_streaming_drops_the_tool_call():
    chunks = [
        AIMessageChunk(content="", tool_call_chunks=[{"name": "slow_search", "args": '{"que', "id": "c1", "index": 0}]),
        AIMessageChunk(content="", tool_call_chunks=[{"name": None, "args": 'ry": "x"}', "id": None, "index": 0}]),
    ]
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 1

    out = cb._stream_bound_llm(FakeBound(chunks), [], None, should_stop=should_stop)
    assert out.tool_calls == [] and out.content == ""


def test_no_stop_behaves_exactly_as_before():
    out = cb._stream_bound_llm(FakeBound([AIMessageChunk(content="a"), AIMessageChunk(content="b")]), [], None)
    assert out.content == "ab"


# ─────────────────────────────────────────────────────────────────────────────
# iter_chat_stream end to end
# ─────────────────────────────────────────────────────────────────────────────


def test_stop_mid_answer_streams_no_more_tokens_and_saves_partial_reply(graph, monkeypatch):
    stop = threading.Event()
    words = ["Once ", "upon ", "a ", "time ", "there ", "was ", "a ", "bug."]

    def fake_llm(messages, writer, should_stop=None):
        text = ""
        for w in words:
            if should_stop and should_stop():
                break
            text += w
            writer({"token": w})
            time.sleep(0.03)   # tokens take time to arrive, like a real model
        return AIMessage(content=text)

    monkeypatch.setattr(cb, "_run_chat_llm", fake_llm)
    tid = f"t-{uuid.uuid4()}"
    events = []
    for ev in cb.iter_chat_stream("tell me a story", tid, stop_event=stop):
        events.append(ev)
        if sum(1 for k, _ in events if k == "token") == 3:
            stop.set()         # user presses Stop after seeing 3 words

    tokens = [p for k, p in events if k == "token"]
    assert tokens == ["Once ", "upon ", "a "]
    assert events[-1] == ("done", None)
    last = _history(graph, tid)[-1]
    assert isinstance(last, AIMessage) and last.content == "Once upon a " and not last.tool_calls


def test_stop_during_tool_ends_turn_with_valid_history_and_next_turn_works(graph, monkeypatch):
    stop = threading.Event()
    _tool_hook["event"] = stop
    llm_calls = []

    def fake_llm(messages, writer, should_stop=None):
        llm_calls.append(1)
        if len(llm_calls) == 1:
            return AIMessage(content="", tool_calls=[{"name": "slow_search", "args": {"query": "q"}, "id": "c1", "type": "tool_call"}])
        writer({"token": "Fresh answer."})
        return AIMessage(content="Fresh answer.")

    monkeypatch.setattr(cb, "_run_chat_llm", fake_llm)
    tid = f"t-{uuid.uuid4()}"
    events = list(cb.iter_chat_stream("search something", tid, stop_event=stop))

    assert events[-1] == ("done", None)
    assert len(llm_calls) == 1                     # nothing ran after the tool
    hist = _history(graph, tid)
    assert isinstance(hist[-1], ToolMessage)       # tool call is answered → history valid
    assert hist[-2].tool_calls[0]["id"] == hist[-1].tool_call_id

    # The next turn on the same thread is unaffected by the earlier stop.
    _tool_hook["event"] = None
    events2 = list(cb.iter_chat_stream("hello again", tid))
    assert [p for k, p in events2 if k == "token"] == ["Fresh answer."]
    assert _history(graph, tid)[-1].content == "Fresh answer."


def test_stop_before_any_text_saves_no_empty_reply(graph, monkeypatch):
    stop = threading.Event()

    def fake_llm(messages, writer, should_stop=None):
        stop.set()                                  # stopped during "thinking"
        return AIMessage(content="")

    monkeypatch.setattr(cb, "_run_chat_llm", fake_llm)
    tid = f"t-{uuid.uuid4()}"
    events = list(cb.iter_chat_stream("think hard", tid, stop_event=stop))
    assert events == [("done", None)]
    hist = _history(graph, tid)
    assert isinstance(hist[-1], HumanMessage)      # no empty assistant message stored


def test_stop_registry_is_cleaned_up(graph, monkeypatch):
    monkeypatch.setattr(cb, "_run_chat_llm", lambda m, w, should_stop=None: AIMessage(content="ok"))
    tid = f"t-{uuid.uuid4()}"
    list(cb.iter_chat_stream("hi", tid))
    assert tid not in cb._active_stop_events
