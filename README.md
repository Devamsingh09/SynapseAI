# Synapse AI — Merged Project

This package merges the real, currently-deployed backend (from the Hugging Face
Space `ThakurDev09/synapse-ai-backend`) with the frontend from GitHub
(`Devamsingh09/SynapseAI`). No code was changed — this is a clean, as-is copy
of both, just combined into one folder so they're easy to run together locally.

```
synapse-ai-final/
├── backend/            FastAPI + LangGraph agent (Groq-based, unmodified)
├── frontend/            React app (the active frontend, talks to backend on :8000)
├── streamlit_legacy/    Older Streamlit UI (imports chatbot_backend directly — not wired to the API)
└── Ethics of Data Science- Chapter10.pdf   (source doc for the RAG tool)
```

## 1. Backend setup

```powershell
cd backend
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and fill in your real keys:
```powershell
copy .env.example .env
```
Then edit `.env`:
```
GROQ_API_KEY=gsk_your_real_key
TAVILY_API_KEY=your_real_key
```

Run it:
```powershell
uvicorn main:app --reload
```
Confirm it's healthy: open `http://127.0.0.1:8000/health` — should show `"ok": true`.

## 2. Frontend setup (React — this is the active UI)

Open a **second terminal**:
```powershell
cd frontend
npm install
npm start
```
This starts the React dev server on `http://localhost:3000`, proxying API calls to
`http://localhost:8000` (see `package.json` → `"proxy"` and `src/apiConfig.js`) —
so the backend must already be running from step 1.

Open `http://localhost:3000` in your browser and send a test message.

## 3. (Optional) Legacy Streamlit UI

This older UI imports `chatbot_backend` directly rather than calling the API —
it's a separate, standalone entry point, not currently wired to `main.py`.
Only use this if you specifically want to test the LangGraph agent without the
FastAPI/React stack:
```powershell
cd streamlit_legacy
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy ..\backend\.env .env
streamlit run streamlit_app.py
```
(It needs the same `.env` as the backend, plus access to `faiss_ethics_ch10/` —
copy that folder in if you go this route, since it currently only exists under `backend/`.)

## Testing the backend directly (no frontend needed)

With the backend running, from a **second** terminal:
```powershell
cd backend
.\venv\Scripts\Activate.ps1
curl.exe http://127.0.0.1:8000/health
```
Should return `{"ok":true,"groq_configured":true,...}`.

For a full chat test, create `test.json`:
```powershell
'{"thread_id":"test1","message":"hello, who are you"}' | Out-File -Encoding utf8 test.json
curl.exe -N -X POST http://127.0.0.1:8000/chat/stream -H "Content-Type: application/json" --data "@test.json"
```
You should see a stream of `data: {"token": "..."}` lines ending in `data: [DONE]`.
(Swagger's `/docs` "Try it out" won't render this stream properly — use curl or the actual frontend to test `/chat/stream`.)
