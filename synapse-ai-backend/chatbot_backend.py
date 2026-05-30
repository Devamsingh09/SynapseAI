from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from typing import TypedDict, Annotated, Literal, Iterator, Tuple

from langchain_core.messages import SystemMessage, HumanMessage, RemoveMessage, AIMessage, AIMessageChunk
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.prebuilt import ToolNode
from langgraph.config import get_stream_writer
from dotenv import load_dotenv
import sqlite3
import threading
from tools import rag_tool, web_search, calculator, get_stock_price, current_datetime
import os
load_dotenv(override=True)

from langchain_groq import ChatGroq

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
CHAT_MODEL = os.getenv("GROQ_CHAT_MODEL", "llama-3.3-70b-versatile")
SUMMARY_MODEL = os.getenv("GROQ_SUMMARY_MODEL", "llama-3.1-8b-instant")

llm = ChatGroq(
    model=CHAT_MODEL,
    temperature=0,
    api_key=GROQ_API_KEY,
    streaming=True,
    request_timeout=120,
)

# Summary / title model — kept smaller for speed and cost
summary_llm = ChatGroq(
    model=SUMMARY_MODEL,
    temperature=0,
    api_key=GROQ_API_KEY,
    request_timeout=60,
)

# 2. DEFINE TOOLS 
tools = [rag_tool, web_search, calculator, get_stock_price, current_datetime]
llm_with_tools = llm.bind_tools(tools)

# 3. DEFINE STATE 
class ChatState(TypedDict):
    messages: Annotated[list, add_messages]
    summary: str

#  4. NODES

SYSTEM_PROMPT = """You are Synapse AI — a helpful, accurate assistant (a focused mini ChatGPT-style agent).

## How you respond
- Be clear, friendly, and direct. Match the user's tone and depth.
- Prefer short, useful answers. Use lists or steps only when they genuinely help.
- If you are unsure, say so. Do not invent live data (prices, news, dates, document quotes).
- After using tools, always give a final natural-language answer — never stop at raw tool output.
- For facts from web_search, base your answer on the search results, not on memory alone.

## Your tools (use when needed)
You have these tools: web_search, rag_tool, calculator, get_stock_price, current_datetime.

## MANDATORY tool use (training data may be outdated)
For the questions below, NEVER answer from memory alone. Always use tools:

1. **current_datetime** first when the user asks about "current", "now", "today", "incumbent", or who holds an office.
2. **web_search** immediately after, using the year from current_datetime in the query.

Always use this two-step flow for:
- Chief Minister, Prime Minister, President, Governor, Mayor, or any office holder ("who is the CM of…", "who runs…")
- Election results, who is in power, cabinet, government leadership
- News, sports scores, company CEOs, or any role that could have changed recently

Example — "Who is the CM of Tamil Nadu?":
→ current_datetime → web_search("Tamil Nadu Chief Minister 2026 current")

If search results conflict with your memory, **trust the search results**.

### web_search
USE when the user asks about:
- Current events, news, recent facts, or anything that changes over time
- Office holders, politicians, elections, governments (see MANDATORY section above)
- People, products, companies, or topics where up-to-date info matters
- "Latest", "today", "now", "recent", or "what happened" style questions
DO NOT use for: timeless definitions, pure math concepts, or coding syntax with no live data needed.

### get_stock_price
USE when the user asks for a stock/share price, ticker quote, or market close for a symbol (e.g. AAPL, TSLA, NVDA).
- Pass the ticker symbol (not the company name) when possible.
DO NOT use for: crypto unless a clear stock symbol is given, portfolio advice, or predictions.

### calculator
USE when the user needs explicit arithmetic or numeric computation (add, sub, mul, div).
- Supported operations: add, sub, mul, div.
DO NOT use for: pure conceptual math explanations with no numbers to compute.

### current_datetime
USE when the user asks what time or date it is, or before web_search for any "current / today / now" factual question.
- Default timezone is Asia/Kolkata unless they specify another (e.g. America/New_York, UTC).
- Include the year from this tool in web_search queries about office holders or recent events.

### rag_tool
USE when the user asks about content from your uploaded/stored documents (e.g. ethics chapter PDF, course material in the vector index).
- Good for: "what does the document say about…", quotes, definitions, scenarios from that material.
DO NOT use for: general web facts or stock prices — use web_search or get_stock_price instead.

## When to use multiple tools (in sequence)
You may call one tool, read the result, then call another if the task requires it. Examples:
- "Who is the CM of Tamil Nadu?" → current_datetime, then web_search.
- "What's Apple's stock price and today's headlines?" → get_stock_price, then web_search.
- "What time is it in New York and what's Tesla trading at?" → current_datetime, then get_stock_price.
- "Search for inflation news and add 5% to 1200" → web_search, then calculator.

Rules for multi-tool use:
1. For office holders and current events, always current_datetime + web_search (mandatory).
2. One tool at a time per turn when possible; after each result, decide if another tool is still required.
3. Combine all results into one coherent final reply for the user.

## When NOT to use tools
- Greetings, thanks, small talk, creative writing, or coding help with no live data.
- Static concepts: "what is a binary tree?", "explain photosynthesis" (no current facts needed).

## Tool loop behavior
If you call a tool, wait for its result, then either call another tool or write your final answer.
Never leave the user with only a tool call — always finish with a helpful message."""


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
        # Skip tool-call chunks (no user-visible text yet)
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


def iter_chat_stream(message: str, thread_id: str) -> Iterator[Tuple[str, object]]:
    """
    Sync generator of SSE-oriented events: ('token', str), ('status', str), ('done', None), ('error', str).
    Tokens are streamed live from Groq via LangGraph custom stream mode.
    """
    config = {"configurable": {"thread_id": thread_id}}
    lock = get_thread_lock(thread_id)
    with lock:
        try:
            for event in chatbot.stream(
                {"messages": [HumanMessage(content=message)]},
                config=config,
                stream_mode=["custom", "messages"],
            ):
                mode, payload = _unpack_stream_event(event)

                if mode == "custom":
                    if isinstance(payload, dict):
                        token = payload.get("token")
                        if token:
                            yield ("token", token)
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
            yield ("error", str(exc))


def chat_node(state: ChatState):
    """Main Chat Node — streams Groq tokens to the client while building the final AIMessage."""
    summary = state.get("summary", "")
    messages = state["messages"]

    system_parts = [SYSTEM_PROMPT]
    if summary:
        system_parts.append(f"Long-Term Memory (Summary of past events):\n{summary}")

    system_msg = SystemMessage(content="\n\n".join(system_parts))
    messages = [system_msg] + messages

    writer = get_stream_writer()
    gathered = None

    for chunk in llm_with_tools.stream(messages):
        gathered = chunk if gathered is None else gathered + chunk
        token = extract_chunk_text(chunk)
        if token:
            writer({"token": token})

    if gathered is None:
        gathered = AIMessage(content="")

    return {"messages": [gathered]}

def summarize_conversation(state: ChatState):
    """Compresses old messages into a summary"""
    summary = state.get("summary", "")
    messages = state["messages"]
    
    # --- THE 12/4 RULE ---
    # We keep the last 4 messages RAW (perfect memory).
    # We summarize everything before that.
    messages_to_summarize = messages[:-4] 
    
    if not messages_to_summarize:
        return {"summary": summary} 
    
    # Prompt Logic
    if summary:
        prompt = (
            f"Current Summary: {summary}\n\n"
            "New lines to add:\n"
            f"{messages_to_summarize}\n\n"
            "INSTRUCTION: Update the summary. Keep it concise but PRESERVE specific entities (names, dates, errors, code snippets). Do not lose technical details."
        )
    else:
        prompt = (
            "Create a summary of this conversation. "
            "PRESERVE specific entities (names, dates, errors, code snippets). "
            f"Lines: {messages_to_summarize}"
        )

    # Generate new summary using the Mini-Brain
    response = summary_llm.invoke([HumanMessage(content=prompt)])
    new_summary = response.content
    
    # Deleting the processed messages from DB to free up tokens
    delete_messages = [RemoveMessage(id=m.id) for m in messages_to_summarize]
    
    return {"summary": new_summary, "messages": delete_messages}

def should_summarize(state: ChatState) -> Literal["summarize_conversation", "tools", END]:
    """Decides if we need to summarize"""
    messages = state["messages"]
    
    # 1. If tools are called, GO TO TOOLS (Do not summarize yet)
    if hasattr(messages[-1], "tool_calls") and len(messages[-1].tool_calls) > 0:
        return "tools"
    
    # 2. TRIGGER: If we have more than 12 messages, clean up memory
    if len(messages) > 12:
        return "summarize_conversation"
    
    # 3. Otherwise, stop and wait for user
    return END

#  5. GRAPH CONSTRUCTION 

conn = sqlite3.connect("chatbot.db", check_same_thread=False, timeout=30)
configure_sqlite_connection(conn)
checkpointer = SqliteSaver(conn=conn)
checkpointer.setup()

graph = StateGraph(ChatState)

graph.add_node("chat_node", chat_node)
graph.add_node("tools", ToolNode(tools))
graph.add_node("summarize_conversation", summarize_conversation)

# Flow Logic
graph.add_edge(START, "chat_node")
graph.add_conditional_edges("chat_node", should_summarize)
graph.add_edge("tools", "chat_node")
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
