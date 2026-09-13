import streamlit as st
import uuid
from chatbot_backend import chatbot, retrieve_all_threads, delete_thread_data, generate_summary
from langchain_core.messages import HumanMessage, AIMessage
from streamlit_mic_recorder import speech_to_text

# page config
st.set_page_config(
    page_title="Synapse AI", 
    page_icon="🧠", 
    layout="wide",
    initial_sidebar_state="expanded"
)

# css
st.markdown("""
<style>
    /* 1. GLOBAL THEME & TYPOGRAPHY */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;600;700&display=swap');
    
    html, body, [class*="css"] {
        font-family: 'Inter', sans-serif;
    }

    .stApp {
        background-color: #0b0f14;
        background-image: radial-gradient(circle at 50% 0%, #171e2e 0%, #0b0f14 70%);
        color: #e6edf3;
    }
    
    /* 2. CUSTOM SCROLLBAR (The Pro Touch) */
    ::-webkit-scrollbar {
        width: 10px;
        background: #0b0f14;
    }
    ::-webkit-scrollbar-thumb {
        background: #1f2a37;
        border-radius: 5px;
    }
    ::-webkit-scrollbar-thumb:hover {
        background: #3b82f6;
    }

    /* 3. SIDEBAR STYLING */
    [data-testid="stSidebar"] {
        background-color: #06090f;
        border-right: 1px solid #1f2a37;
    }

    .sidebar-title {
        font-size: 26px;
        font-weight: 800;
        background: linear-gradient(90deg, #3b82f6, #8b5cf6);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        margin-bottom: 20px;
        text-align: center;
        letter-spacing: -0.5px;
    }

    /* 4. VOICE WIDGET */
    div.st-key-STT {
        display: flex;
        justify-content: center;
        margin-bottom: 15px;
    }
    div.st-key-STT button {
        width: 100%;
        background: linear-gradient(135deg, rgba(59, 130, 246, 0.1), rgba(139, 92, 246, 0.1)) !important;
        border: 1px solid rgba(59, 130, 246, 0.4) !important;
        color: #ffffff !important;
        border-radius: 12px !important;
        height: 55px !important;
        font-weight: 600 !important;
        box-shadow: 0 4px 15px rgba(0, 0, 0, 0.2);
        transition: all 0.3s ease;
    }
    div.st-key-STT button:hover {
        border-color: #3b82f6 !important;
        background: linear-gradient(135deg, rgba(59, 130, 246, 0.2), rgba(139, 92, 246, 0.2)) !important;
        box-shadow: 0 0 20px rgba(59, 130, 246, 0.4);
        transform: translateY(-2px);
    }
    
    /* 5. HERO SECTION CARDS (Welcome Screen) */
    .hero-card {
        background-color: #0f1623;
        border: 1px solid #1f2a37;
        padding: 20px;
        border-radius: 12px;
        text-align: center;
        transition: transform 0.2s, border-color 0.2s;
        height: 100%;
    }
    .hero-card:hover {
        border-color: #3b82f6;
        transform: translateY(-5px);
        background-color: #161b22;
        cursor: pointer;
    }
    .hero-icon { font-size: 30px; margin-bottom: 10px; }
    .hero-title { font-weight: 600; font-size: 16px; color: #e6edf3; margin-bottom: 5px; }
    .hero-desc { font-size: 12px; color: #94a3b8; }

    /* 6. CHAT INPUT */
    .stChatInputContainer textarea {
        background-color: #0d1117 !important;
        color: #e6edf3 !important;
        border: 1px solid #30363d !important;
        border-radius: 12px !important;
        padding: 15px !important;
    }
    .stChatInputContainer textarea:focus {
        border-color: #3b82f6 !important;
        box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.2) !important;
    }

    #MainMenu {visibility: hidden;}
    footer {visibility: hidden;}
    header {background: transparent !important;}

</style>
""", unsafe_allow_html=True)

# SESSION STATE-
if "thread_id" not in st.session_state:
    st.session_state["thread_id"] = str(uuid.uuid4())
if "titles" not in st.session_state:
    st.session_state["titles"] = {}
if "hero_prompt" not in st.session_state:
    st.session_state["hero_prompt"] = None

# HELPER: Load Chat
def load_chat(thread_id):
    st.session_state["thread_id"] = thread_id
    st.session_state["messages"] = []
    
    state = chatbot.get_state(config={"configurable": {"thread_id": thread_id}})
    history = state.values.get("messages", [])
    
    for msg in history:
        if isinstance(msg, HumanMessage):
            st.session_state["messages"].append({"role": "user", "content": msg.content})
        elif isinstance(msg, AIMessage) and msg.content:
            st.session_state["messages"].append({"role": "assistant", "content": msg.content})
            
    if thread_id not in st.session_state["titles"]:
        user_msgs = [m for m in history if isinstance(m, HumanMessage)]
        if user_msgs:
            if len(user_msgs[0].content) < 15:
                st.session_state["titles"][thread_id] = "New Conversation"
            else:
                title = generate_summary(user_msgs[0].content)
                st.session_state["titles"][thread_id] = title

# SIDEBAR UI 
with st.sidebar:
    st.markdown('<div class="sidebar-title">🧠 Synapse AI</div>', unsafe_allow_html=True)
    
    st.caption("🎙️ Voice Command")
    voice_text = speech_to_text(
        language='en',
        start_prompt="Start Recording",
        stop_prompt="Stop & Send",
        just_once=True,
        key='STT'
    )
    
    st.markdown("---")
    
    if st.button("➕ Start New Chat", use_container_width=True):
        st.session_state["thread_id"] = str(uuid.uuid4())
        st.session_state["messages"] = []
        st.session_state["hero_prompt"] = None # Reset hero selection
        st.rerun()
    
    st.markdown("### 🗂️ History")
    
    db_threads = retrieve_all_threads()
    recent_threads = db_threads[-15:]
    
    for tid in reversed(recent_threads):
        title = st.session_state["titles"].get(tid, "New Conversation")
        if title == "New Conversation": 
             load_chat(tid) 
             title = st.session_state["titles"].get(tid, tid[:10])

        col1, col2 = st.columns([0.85, 0.15])
        
        with col1:
            if tid == st.session_state["thread_id"]:
                st.button(f"🔷 {title}", key=f"btn_{tid}", use_container_width=True)
            else:
                if st.button(f"{title}", key=f"btn_{tid}", use_container_width=True):
                    load_chat(tid)
                    st.rerun()
        
        with col2:
            if st.button("✖", key=f"del_{tid}", help="Delete"):
                delete_thread_data(tid)
                if tid == st.session_state["thread_id"]:
                    st.session_state["thread_id"] = str(uuid.uuid4())
                    st.session_state["messages"] = []
                st.rerun()

#  MAIN CHAT AREA 

# Initializing Messages
if "messages" not in st.session_state:
    st.session_state["messages"] = []

#  (Welcome Screen) 
# Only showing this if there are no messages yet
if not st.session_state["messages"]:
    st.markdown("<br><br>", unsafe_allow_html=True) 
    st.markdown("<h1 style='text-align: center; color: #e6edf3;'>How can I help you today?</h1>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: #94a3b8; margin-bottom: 50px;'>I can help you write code, analyze data, or just chat.</p>", unsafe_allow_html=True)
    
    # 3 Suggestion cards
    col1, col2, col3 = st.columns(3)
    
    with col1:
        if st.button("🐍 Write a Python Script", use_container_width=True, help="Generate Python Code"):
            st.session_state["hero_prompt"] = "Write a Python script to automate a daily task."
            st.rerun()
            
    with col2:
        if st.button("📈 Analyze My Data", use_container_width=True, help="Data Analysis Help"):
            st.session_state["hero_prompt"] = "How do I use Pandas to analyze a CSV file?"
            st.rerun()
            
    with col3:
        if st.button("🧠 Explain a Concept", use_container_width=True, help="Learn something new"):
            st.session_state["hero_prompt"] = "Explain Quantum Computing in simple terms."
            st.rerun()

# CHAT HISTORY--
# If messages exist, show them
for msg in st.session_state["messages"]:
    if msg["role"] == "user":
        with st.chat_message("user", avatar="🧑‍💻"):
            st.markdown(msg["content"])
    else:
        with st.chat_message("assistant", avatar="🧠"):
            st.markdown(msg["content"])

#  INPUT HANDLING 
chat_input_text = st.chat_input("Ask anything...")

final_prompt = None

# Priority Logic: 1. Voice -> 2. Hero Button -> 3. Typed Text
if voice_text:
    final_prompt = voice_text
elif st.session_state["hero_prompt"]:
    final_prompt = st.session_state["hero_prompt"]
    st.session_state["hero_prompt"] = None # Reset after using
elif chat_input_text:
    final_prompt = chat_input_text

# Process Logic
if final_prompt:
    # 1. Adding User Message
    st.session_state["messages"].append({"role": "user", "content": final_prompt})
    
    # Forcing redraw to show user message immediately if it came from Hero/Voice
    if voice_text or st.session_state.get("hero_prompt_triggered"):
         st.rerun() 
         
    with st.chat_message("user", avatar="🧑‍💻"):
        st.markdown(final_prompt)

    # 2. Smart Title Generation
    user_msgs_count = len([m for m in st.session_state["messages"] if m["role"] == "user"])
    current_thread_id = st.session_state["thread_id"]
    
    if user_msgs_count == 1:
        if len(final_prompt) < 15:
             st.session_state["titles"][current_thread_id] = "New Conversation"
        else:
             title = generate_summary(final_prompt)
             st.session_state["titles"][current_thread_id] = title
             
    elif user_msgs_count >= 2:
        current_title = st.session_state["titles"].get(current_thread_id, "")
        if current_title == "New Conversation":
            title = generate_summary(final_prompt)
            st.session_state["titles"][current_thread_id] = title

    # 3. Stream Assistant Response
    with st.chat_message("assistant", avatar="🧠"):
        message_placeholder = st.empty()
        full_response = ""
        
        try:
            for chunk, _ in chatbot.stream(
                {"messages": [HumanMessage(content=final_prompt)]},
                config={"configurable": {"thread_id": st.session_state["thread_id"]}},
                stream_mode="messages",
            ):
                if isinstance(chunk, AIMessage) and chunk.content:
                    full_response += chunk.content
                    message_placeholder.markdown(full_response + "▌")
            
            message_placeholder.markdown(full_response)
            st.session_state["messages"].append({"role": "assistant", "content": full_response})
            
        except Exception as e:
            st.error(f"Error: {str(e)}")
    
    # 4. Cleanup
    if voice_text:
        st.rerun()
