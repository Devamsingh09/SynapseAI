from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from typing import TypedDict, Annotated, Literal

from langchain_core.messages import SystemMessage, HumanMessage, RemoveMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.prebuilt import ToolNode
from dotenv import load_dotenv
import sqlite3
from tools import rag_tool, web_search, calculator, get_stock_price, current_datetime, wikipedia_search 
import os
load_dotenv(override=True)

from langchain_groq import ChatGroq

llm = ChatGroq(
    model="llama-3.1-8b-instant",
    temperature=0,
    api_key=os.getenv("GROQ_API_KEY"),
    request_timeout=30  # ✅ FIX: prevent silent hang on slow responses
)

# Summary Model
summary_llm = ChatGroq(
    model="llama-3.1-8b-instant",
    temperature=0,
    api_key=os.getenv("GROQ_API_KEY"),
    request_timeout=30  # ✅ FIX: prevent summarizer from freezing the chat
)

# 2. DEFINE TOOLS
tools = [rag_tool, web_search, calculator, get_stock_price, current_datetime,wikipedia_search]
llm_with_tools = llm.bind_tools(tools)

# 3. DEFINE STATE
class ChatState(TypedDict):
    messages: Annotated[list, add_messages]
    summary: str

# 4. NODES

def chat_node(state: ChatState):
    """Main Chat Node with Long-Term Memory Injection"""
    summary = state.get("summary", "")
    messages = state["messages"]

    if summary:
        system_msg = SystemMessage(content=f"Long-Term Memory (Summary of past events): {summary}")
        messages = [system_msg] + messages

    response = llm_with_tools.invoke(messages)
    return {"messages": [response]}


def summarize_conversation(state: ChatState):
    summary = state.get("summary", "")
    messages = state["messages"]

    messages_to_summarize = messages[:-4]

    if not messages_to_summarize:
        return {"summary": summary}

    if summary:
        prompt = f"""You are a memory manager for an AI assistant. Your job is to maintain a rich, detailed running summary of a conversation.

EXISTING SUMMARY:
{summary}

NEW MESSAGES TO INCORPORATE:
{messages_to_summarize}

INSTRUCTIONS:
- Merge the new messages into the existing summary
- KEEP all previously stored facts, they are important
- PRESERVE: names, dates, numbers, error messages, code snippets, file names, decisions made
- PRESERVE: what the user was trying to build or solve
- PRESERVE: any preferences or constraints the user mentioned
- PRESERVE: results of tool calls (stock prices, search results, calculations)
- ADD new information from the new messages
- If the user corrected something or changed direction, reflect that update
- Write in third person (e.g. "The user asked...", "The assistant explained...")
- Keep it structured and scannable — use short labeled sections if helpful
- Maximum length: 400 words. Be dense but complete.

OUTPUT: Updated summary only. No preamble, no explanation."""

    else:
        prompt = f"""You are a memory manager for an AI assistant. Your job is to create a rich, detailed summary of a conversation so the assistant can remember it later.

CONVERSATION TO SUMMARIZE:
{messages_to_summarize}

INSTRUCTIONS:
- PRESERVE: names, dates, numbers, error messages, code snippets, file names, decisions made
- PRESERVE: what the user was trying to build or solve and why
- PRESERVE: any preferences, constraints, or requirements the user mentioned
- PRESERVE: results of tool calls (stock prices fetched, search results, calculations done)
- PRESERVE: the flow of the conversation — what was asked, what was answered, what worked
- Note any unresolved questions or things the user said they would do next
- Write in third person (e.g. "The user asked...", "The assistant explained...")
- Keep it structured and scannable — use short labeled sections if helpful
- Maximum length: 400 words. Be dense but complete.

OUTPUT: Summary only. No preamble, no explanation."""

    try:
        # ✅ FIX: Use .invoke() instead of raw .client.create()
        # Ensures LangGraph correctly tags this call with node metadata
        # so the summary never leaks into the frontend stream.
        response = summary_llm.invoke([HumanMessage(content=prompt)])
        new_summary = response.content
    except Exception:
        # ✅ FIX: If summarization fails or times out, keep existing summary
        # instead of crashing the whole conversation
        new_summary = summary

    delete_messages = [RemoveMessage(id=m.id) for m in messages_to_summarize]
    return {"summary": new_summary, "messages": delete_messages}


def should_summarize(state: ChatState) -> Literal["summarize_conversation", "tools", END]:
    """Decides if we need to summarize"""
    messages = state["messages"]

    # 1. If tools are called, GO TO TOOLS (Do not summarize yet)
    if hasattr(messages[-1], "tool_calls") and len(messages[-1].tool_calls) > 0:
        return "tools"

    # 2. ✅ FIX: Raised from 12 to 20 — 12 was too aggressive
    # (only 6 exchanges before summarization kicked in and froze chat)
    if len(messages) > 20:
        return "summarize_conversation"

    # 3. Otherwise, stop and wait for user
    return END


# 5. GRAPH CONSTRUCTION

conn = sqlite3.connect('chatbot.db', check_same_thread=False)
checkpointer = SqliteSaver(conn=conn)

# ✅ Creates checkpoints + writes tables on startup
# Without this, /threads crashes on a fresh DB before any chat is saved
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
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT DISTINCT thread_id FROM checkpoints")
        return [row[0] for row in cursor.fetchall()]
    except Exception:
        return []

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