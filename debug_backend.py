# debug_backend.py
import time
from chatbot_backend import chatbot
from langchain_core.messages import HumanMessage

print("--- STARTING DEBUG TEST ---")
thread_id = "test_debug_123"

print("1. Sending message 'hi' to chatbot...")
start = time.time()

try:
    # Run synchronously to see the final result
    result = chatbot.invoke(
        {"messages": [HumanMessage(content="hi")]},
        config={"configurable": {"thread_id": thread_id}}
    )
    
    print(f"2. Finished in {time.time() - start:.2f} seconds.")
    
    messages = result.get("messages", [])
    print(f"3. Message Count: {len(messages)}")
    
    if messages:
        last_msg = messages[-1]
        print("--- LAST MESSAGE DETAILS ---")
        print(f"Type: {type(last_msg)}")
        print(f"Content: '{last_msg.content}'")
        print(f"Tool Calls: {last_msg.tool_calls if hasattr(last_msg, 'tool_calls') else 'None'}")
        print("----------------------------")
    else:
        print("ERROR: Chatbot returned NO messages.")

except Exception as e:
    print(f"\nCRITICAL ERROR: {e}")

print("--- END DEBUG TEST ---")