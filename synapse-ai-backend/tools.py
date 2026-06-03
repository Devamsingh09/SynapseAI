import os
from datetime import datetime
from typing import Optional

import pytz
import requests
from langchain_community.embeddings import HuggingFaceEmbeddings
from langchain_community.vectorstores import FAISS
from langchain_core.tools import tool
from tavily import TavilyClient

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
FAISS_DIR = os.path.join(BASE_DIR, "faiss_ethics_ch10")

_embeddings = HuggingFaceEmbeddings(model_name="sentence-transformers/all-MiniLM-L6-v2")
_vector_store = FAISS.load_local(
    FAISS_DIR,
    _embeddings,
    allow_dangerous_deserialization=True,
)
_retriever = _vector_store.as_retriever(search_kwargs={"k": 4})


def _tavily_client() -> TavilyClient:
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        raise ValueError("TAVILY_API_KEY is not configured")
    return TavilyClient(api_key=api_key)


def _http_get(
    url: str,
    timeout: int = 15,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
) -> requests.Response:
    return requests.get(url, timeout=timeout, headers=headers or {}, params=params or {})


@tool
def rag_tool(query: str) -> str:
    """Retrieve relevant passages from the stored PDF document index."""
    docs = _retriever.invoke(query)
    if not docs:
        return "No matching document passages found."
    parts = []
    for i, d in enumerate(docs, 1):
        parts.append(f"[{i}] {d.page_content}")
    return "\n\n".join(parts)


@tool
def web_search(query: str) -> str:
    """Search the web via Tavily for up-to-date information. Use for current office holders, news, sports, commodities (silver/gold), and any fact that may have changed. Include the current year in the query."""
    try:
        client = _tavily_client()
        response = client.search(
            query=query,
            search_depth="advanced",
            max_results=5,
            include_answer=True,
        )

        parts = []
        answer = response.get("answer")
        if answer:
            parts.append(f"Summary: {answer}")

        results = response.get("results") or []
        for i, hit in enumerate(results, 1):
            title = hit.get("title", "")
            content = hit.get("content", "")
            url = hit.get("url", "")
            parts.append(f"{i}. {title}\n{content}\nSource: {url}")

        if not parts:
            return "No web results found."
        return "\n\n".join(parts)
    except Exception as e:
        return f"Web search failed: {type(e).__name__}: {e}"


@tool
def calculator(first_num: float, second_num: float, operation: str) -> dict:
    """Perform basic arithmetic operation on two numbers.
    Supported operation: add, sub, mul, div"""
    try:
        if operation == "add":
            result = first_num + second_num
        elif operation == "sub":
            result = first_num - second_num
        elif operation == "mul":
            result = first_num * second_num
        elif operation == "div":
            if second_num == 0:
                return {"error": "Division by zero not allowed!"}
            result = first_num / second_num
        else:
            return {"error": f"Unsupported operation '{operation}'"}
        return {
            "first_num": first_num,
            "second_num": second_num,
            "operation": operation,
            "result": result,
        }
    except Exception as e:
        return {"error": str(e)}


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


@tool
def get_weather(location: str) -> str:
    """Get current weather and short forecast for a city or place name (e.g. Chennai, London, New York). Free Open-Meteo data — no API key."""
    location = location.strip()
    if not location:
        return "Please provide a location name."
    try:
        geo = _http_get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": location, "count": 1, "language": "en", "format": "json"},
        )
        geo.raise_for_status()
        results = geo.json().get("results") or []
        if not results:
            return f"No location found for '{location}'."

        place = results[0]
        lat, lon = place["latitude"], place["longitude"]
        name = place.get("name", location)
        country = place.get("country", "")
        admin = place.get("admin1", "")
        label = ", ".join(p for p in (name, admin, country) if p)

        forecast = _http_get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m",
                "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum",
                "timezone": "auto",
                "forecast_days": 3,
            },
        )
        forecast.raise_for_status()
        data = forecast.json()
        cur = data.get("current") or {}
        daily = data.get("daily") or {}

        code = cur.get("weather_code")
        desc = _weather_code_label(code)

        lines = [
            f"Weather for {label}:",
            f"Now: {cur.get('temperature_2m')}°C (feels like {cur.get('apparent_temperature')}°C), {desc}",
            f"Humidity: {cur.get('relative_humidity_2m')}%, Wind: {cur.get('wind_speed_10m')} km/h",
        ]
        if daily.get("time"):
            lines.append("Next days:")
            for i, day in enumerate(daily["time"][:3]):
                d_code = (daily.get("weather_code") or [None])[i]
                tmax = (daily.get("temperature_2m_max") or [None])[i]
                tmin = (daily.get("temperature_2m_min") or [None])[i]
                lines.append(f"  {day}: {tmin}–{tmax}°C, {_weather_code_label(d_code)}")
        return "\n".join(lines)
    except Exception as e:
        return f"Weather lookup failed: {type(e).__name__}: {e}"


def _weather_code_label(code: Optional[int]) -> str:
    if code is None:
        return "unknown"
    labels = {
        0: "clear sky",
        1: "mainly clear",
        2: "partly cloudy",
        3: "overcast",
        45: "fog",
        48: "depositing rime fog",
        51: "light drizzle",
        53: "moderate drizzle",
        55: "dense drizzle",
        61: "slight rain",
        63: "moderate rain",
        65: "heavy rain",
        71: "slight snow",
        73: "moderate snow",
        75: "heavy snow",
        80: "rain showers",
        81: "moderate rain showers",
        82: "violent rain showers",
        95: "thunderstorm",
    }
    return labels.get(code, f"weather code {code}")


@tool
def wikipedia_search(query: str) -> str:
    """Search Wikipedia for encyclopedia summaries. Use for historical facts, definitions, biographies, science concepts — not for live news or prices."""
    query = query.strip()
    if not query:
        return "Please provide a search query."
    try:
        search = _http_get(
            "https://en.wikipedia.org/w/api.php",
            params={
                "action": "query",
                "list": "search",
                "srsearch": query,
                "format": "json",
                "srlimit": 3,
            },
            headers={"User-Agent": "SynapseAI/1.0 (education chatbot)"},
        )
        search.raise_for_status()
        hits = search.json().get("query", {}).get("search") or []
        if not hits:
            return f"No Wikipedia articles found for '{query}'."

        parts = []
        for hit in hits[:3]:
            title = hit.get("title", "")
            summary = _http_get(
                f"https://en.wikipedia.org/api/rest_v1/page/summary/{requests.utils.quote(title, safe='')}",
                headers={"User-Agent": "SynapseAI/1.0 (education chatbot)"},
            )
            if summary.status_code != 200:
                continue
            s = summary.json()
            extract = (s.get("extract") or "")[:1200]
            url = s.get("content_urls", {}).get("desktop", {}).get("page", "")
            parts.append(f"**{title}**\n{extract}\nSource: {url}")

        if not parts:
            return f"Could not load Wikipedia summaries for '{query}'."
        return "\n\n".join(parts)
    except Exception as e:
        return f"Wikipedia search failed: {type(e).__name__}: {e}"


@tool
def convert_currency(amount: float, from_currency: str, to_currency: str) -> str:
    """Convert money between currencies using live exchange rates (Frankfurter/ECB). Use ISO codes like USD, EUR, INR, GBP."""
    from_currency = from_currency.strip().upper()
    to_currency = to_currency.strip().upper()
    try:
        r = _http_get(
            "https://api.frankfurter.app/latest",
            params={"amount": amount, "from": from_currency, "to": to_currency},
        )
        r.raise_for_status()
        data = r.json()
        rate_date = data.get("date", "")
        converted = (data.get("rates") or {}).get(to_currency)
        if converted is None:
            return f"Could not convert {from_currency} to {to_currency}."
        return (
            f"{amount} {from_currency} = {converted} {to_currency} "
            f"(rate date: {rate_date}, source: ECB via Frankfurter)"
        )
    except Exception as e:
        return f"Currency conversion failed: {type(e).__name__}: {e}"


@tool
def lookup_pincode(pincode: str) -> str:
    """Look up Indian postal pincode details (state, district, post offices). Use for 6-digit Indian pincodes only."""
    pincode = pincode.strip()
    if not pincode.isdigit() or len(pincode) != 6:
        return "Please provide a valid 6-digit Indian pincode."
    try:
        r = _http_get(f"https://api.postalpincode.in/pincode/{pincode}")
        r.raise_for_status()
        payload = r.json()
        if not payload or payload[0].get("Status") != "Success":
            msg = payload[0].get("Message", "Pincode not found") if payload else "Pincode not found"
            return msg

        post_offices = payload[0].get("PostOffice") or []
        if not post_offices:
            return f"No post offices found for pincode {pincode}."

        sample = post_offices[0]
        header = (
            f"Pincode {pincode}: {sample.get('District', '')}, "
            f"{sample.get('State', '')}, {sample.get('Country', 'India')}"
        )
        offices = []
        for po in post_offices[:12]:
            offices.append(
                f"- {po.get('Name', '')} ({po.get('BranchType', '')}, {po.get('Block', '')})"
            )
        extra = ""
        if len(post_offices) > 12:
            extra = f"\n... and {len(post_offices) - 12} more post offices."
        return header + "\nPost offices:\n" + "\n".join(offices) + extra
    except Exception as e:
        return f"Pincode lookup failed: {type(e).__name__}: {e}"


@tool
def fetch_url(url: str) -> str:
    """Read and summarize the main text content of a web page URL. Use when the user shares a link or asks what's on a specific page."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        r = _http_get(
            f"https://r.jina.ai/{url}",
            timeout=30,
            headers={"Accept": "text/plain"},
        )
        r.raise_for_status()
        text = r.text.strip()
        if len(text) > 8000:
            text = text[:8000] + "\n\n[Content truncated.]"
        return text or "No readable content returned from that URL."
    except Exception as e:
        return f"URL fetch failed: {type(e).__name__}: {e}"


@tool
def github_search(query: str, search_type: str = "repositories") -> str:
    """Search GitHub for repositories or users. search_type: 'repositories' or 'users'. Use for open-source projects, repos, GitHub profiles."""
    query = query.strip()
    if not query:
        return "Please provide a search query."
    search_type = search_type.strip().lower()
    if search_type not in ("repositories", "users"):
        search_type = "repositories"

    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "SynapseAI/1.0",
    }
    token = os.getenv("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        if search_type == "users":
            r = _http_get(
                "https://api.github.com/search/users",
                params={"q": query, "per_page": 5},
                headers=headers,
            )
        else:
            r = _http_get(
                "https://api.github.com/search/repositories",
                params={"q": query, "sort": "stars", "order": "desc", "per_page": 5},
                headers=headers,
            )
        r.raise_for_status()
        items = r.json().get("items") or []
        if not items:
            return f"No GitHub {search_type} found for '{query}'."

        parts = []
        for i, item in enumerate(items, 1):
            if search_type == "users":
                parts.append(
                    f"{i}. {item.get('login')} — {item.get('html_url')}\n"
                    f"   Type: {item.get('type', 'User')}"
                )
            else:
                desc = (item.get("description") or "No description")[:200]
                parts.append(
                    f"{i}. {item.get('full_name')} (★ {item.get('stargazers_count', 0)})\n"
                    f"   {desc}\n"
                    f"   {item.get('html_url')}"
                )
        return "\n\n".join(parts)
    except Exception as e:
        return f"GitHub search failed: {type(e).__name__}: {e}"


@tool
def geo_lookup(ip: str = "") -> str:
    """Look up geographic location for an IP address. Leave ip empty to look up the server's public IP. Use for 'where is this IP' questions — not for street-level user location."""
    ip = ip.strip()
    try:
        if not ip:
            ip_resp = _http_get("https://api.ipify.org?format=json")
            ip_resp.raise_for_status()
            ip = ip_resp.json().get("ip", "")
            if not ip:
                return "Could not determine public IP."

        r = _http_get(
            f"http://ip-api.com/json/{ip}",
            params={
                "fields": "status,message,country,countryCode,regionName,city,lat,lon,timezone,isp,query",
            },
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "success":
            return data.get("message", f"Lookup failed for IP {ip}")

        return (
            f"IP: {data.get('query')}\n"
            f"Location: {data.get('city')}, {data.get('regionName')}, {data.get('country')} ({data.get('countryCode')})\n"
            f"Coordinates: {data.get('lat')}, {data.get('lon')}\n"
            f"Timezone: {data.get('timezone')}\n"
            f"ISP: {data.get('isp')}"
        )
    except Exception as e:
        return f"Geo lookup failed: {type(e).__name__}: {e}"


@tool
def current_datetime(tz_name: str = "Asia/Kolkata") -> str:
    """Return current date & time for a given timezone (default: Asia/Kolkata)."""
    tz = pytz.timezone(tz_name)
    return datetime.now(tz).strftime("%A, %d %B %Y, %I:%M:%S %p %Z")
