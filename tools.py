from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_core.tools import tool
from langchain_community.tools import DuckDuckGoSearchRun
import os
import requests
from langchain_community.vectorstores import FAISS
from langchain_community.embeddings import HuggingFaceEmbeddings

BASE_DIR = os.path.dirname(os.path.abspath(__file__))   # folder where tools.py lives
FAISS_DIR = os.path.join(BASE_DIR, "faiss_ethics_ch10") # put folder here

_embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
_vector_store = FAISS.load_local(
    FAISS_DIR,
    _embeddings,
    allow_dangerous_deserialization=True
)
_retriever = _vector_store.as_retriever(search_kwargs={"k": 4})

@tool
def rag_tool(query: str) -> dict:
    """Retrieve relevant information from the stored PDF vector index."""
    docs = _retriever.invoke(query)
    return {
        "query": query,
        "context": [d.page_content for d in docs],
        "metadata": [d.metadata for d in docs],
    }
_search = DuckDuckGoSearchRun()
@tool
def web_search(query: str) -> str:
    """Search the web and return results as text."""
    try:
        result = _search.run(query)
        # DuckDuckGoSearchRun usually returns a string, but ensure anyway
        return str(result)
    except Exception as e:
        return f"Web search failed: {type(e).__name__}: {e}"
@tool
def calculator(first_num:float,second_num:float, operation:str) -> dict:
    """Perform basic arithmetic operation on two numbers.
    Supported operation: add, sub, mul, div"""
    try:
        if operation=='add':
            result = first_num+second_num
        elif operation =='sub':
            result = first_num-second_num
        elif operation=='mul':
            result = first_num * second_num
        elif operation=='div':
            if second_num==0:
                return{'error':"Division by zero not allowed!"}
            result = first_num/second_num
        else:
            return {'error':f"Unsupported operation '{operation}'"}
        return {'first_num':first_num,'second_num':second_num,'operation':operation,'result':result}
    except Exception as e:
        return {'error':str(e)}

@tool
def get_stock_price(symbol: str) -> dict:
    """Return the latest daily close price for a stock symbol (e.g., AAPL, TSLA)."""
    symbol = symbol.strip().upper()
    url = f"https://www.alphavantage.co/query?function=TIME_SERIES_DAILY&symbol={symbol}&apikey=MU5WKN30VAC2LCDG"

    try:
        r = requests.get(url, timeout=15)
        data = r.json()

        ts = data.get("Time Series (Daily)")
        if not ts:
            # AlphaVantage often rate-limits; return the message safely
            return {"symbol": symbol, "error": "No data returned", "raw": data}

        latest_date = max(ts.keys())
        latest = ts[latest_date]

        return {
            "symbol": symbol,
            "date": latest_date,
            "open": latest.get("1. open"),
            "high": latest.get("2. high"),
            "low": latest.get("3. low"),
            "close": latest.get("4. close"),
            "volume": latest.get("5. volume"),
        }

    except Exception as e:
        return {"symbol": symbol, "error": str(e)}

from langchain_core.tools import tool
from datetime import datetime
import pytz

@tool
def current_datetime(tz_name: str = "Asia/Kolkata") -> str:
    """Return current date & time for a given timezone (default: Asia/Kolkata)."""
    tz = pytz.timezone(tz_name)
    return datetime.now(tz).strftime("%A, %d %B %Y, %I:%M:%S %p %Z")
