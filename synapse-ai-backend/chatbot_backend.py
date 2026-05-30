from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from typing import TypedDict, Annotated, Literal

from langchain_core.messages import SystemMessage, HumanMessage, RemoveMessage
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.prebuilt import ToolNode, tools_condition
from dotenv import load_dotenv
import sqlite3
from tools import rag_tool, web_search, calculator, get_stock_price, current_datetime
import os
load_dotenv(override=True)

from langchain_groq import ChatGroq

llm = ChatGroq(
    model="llama-3.1-8b-instant",
    temperature=0,
    api_key=os.getenv("GROQ_API_KEY")  # paste directly
)

# Summary Model (Fast & Efficient for Memory)
summary_llm = ChatGroq(model="llama-3.1-8b-instant", temperature=0,api_key=os.getenv("GROQ_API_KEY"))

# 2. DEFINE TOOLS 
tools = [rag_tool, web_search, calculator, get_stock_price, current_datetime]
llm_with_tools = llm.bind_tools(tools)

# 3. DEFINE STATE 
class ChatState(TypedDict):
    messages: Annotated[list, add_messages]
    summary: str

#  4. NODES 

def chat_node(state: ChatState):
    """Main Chat Node with Long-Term Memory Injection"""
    summary = state.get("summary", "")
    messages = state["messages"]

    # Injecting Summary if it exists
    if summary:
        system_msg = SystemMessage(content=f"Long-Term Memory (Summary of past events): {summary}")
        # We insert the summary at the start of the context
        messages = [system_msg] + messages
    
    response = llm_with_tools.invoke(messages)
    return {"messages": [response]}

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

conn = sqlite3.connect('chatbot.db', check_same_thread=False)
checkpointer = SqliteSaver(conn=conn)

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
