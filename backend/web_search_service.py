"""
Web search pipeline behind tools.web_search.

One Tavily call, then everything else happens locally before the chat model
sees a single character:

  1. clean     — strip markdown/nav/cookie boilerplate from each result
  2. chunk     — split into sentence-bounded passages (tables row by row)
  3. score     — hybrid relevance: semantic (MiniLM) + keyword overlap + Tavily's own score
  4. filter    — drop passages below MIN_RELEVANCE, near-duplicates, and
                 anything past the per-source / total budget
  5. format    — group the surviving passages under their source, with a
                 date label when the page states one

Everything here is free: Tavily "basic" depth is 1 credit per call (verified
via include_usage), and scoring reuses the MiniLM model tools.py already
loads for RAG — no new services or dependencies.

`score_passages` is the single relevance step; a different scorer (e.g. a
dedicated relevance model) can be passed to `run_web_search` without
touching the rest of the pipeline.

Output wording deliberately avoids the adaptive loop's failure words
(see adaptive_loop._FAILURE_SIGNAL_RE) on success, and keeps them on
failure, so the evaluator's retry trigger still behaves correctly.
"""
from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Sequence
from urllib.parse import parse_qs, urlparse

import numpy as np

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────

# "fast"/"ultra-fast" cost the same credit and answer ~2x quicker, but in
# testing returned stale pages ("Argentina won the most recent World Cup")
# where "basic" found the current answer. Correctness wins.
SEARCH_DEPTH = "basic"
MAX_RESULTS = 6                # Tavily bills per request, not per result
SEARCH_TIMEOUT_SEC = 8         # client default is 60s — fail fast and let the loop recover

OUTPUT_CHAR_BUDGET = 1700      # passage text only; headers/URLs bring the total near the old 1800-2400
MAX_PASSAGES = 8
MAX_PASSAGES_PER_SOURCE = 3
MAX_SOURCES = 4                # each source also costs a title + URL line
PASSAGE_TARGET_CHARS = 320
PASSAGE_MAX_CHARS = 480
PASSAGE_MIN_CHARS = 40
TABLE_ROW_MIN_CHARS = 20
MAX_PASSAGES_TO_SCORE = 48     # bounds embedding cost (~0.4s on CPU for 48 passages)

MIN_RELEVANCE = 0.45           # hybrid score a passage needs to be kept (answers score ~0.6-0.9)
WEAK_RELEVANCE_FLOOR = 0.25    # below this nothing is returned at all
DUPLICATE_JACCARD = 0.75
TITLE_ECHO_JACCARD = 0.8       # passage that just repeats the page title
SHORT_PASSAGE_CHARS = 80
SHORT_PASSAGE_PENALTY = 0.08

CACHE_TTL_FRESH_SEC = 300      # prices, scores, "today", news
CACHE_TTL_STABLE_SEC = 1800
CACHE_MAX_ENTRIES = 256

# Pages whose extracted text is mostly captions, reels or comments, plus
# search-engine result pages (a list of other people's snippets, not a source).
EXCLUDED_DOMAINS = [
    "youtube.com", "instagram.com", "facebook.com", "tiktok.com", "pinterest.com",
    "duckduckgo.com", "bing.com",
]

# ─────────────────────────────────────────────────────────────────────────────
# QUERY INTENT
# ─────────────────────────────────────────────────────────────────────────────

_NEWS_RE = re.compile(r"\b(news|headlines?|breaking|announce[sd]?|announcement)\b", re.I)
_VERY_RECENT_RE = re.compile(r"\b(today|tonight|yesterday|breaking|this morning)\b", re.I)
_FRESH_RE = re.compile(
    r"\b(today|tonight|yesterday|now|current(ly)?|latest|live|recent|this (week|month)|"
    r"price|prices|rate|rates|score|scores|news|headlines?|breaking|weather|stock)\b",
    re.I,
)


def search_params(query: str) -> dict:
    """Tavily parameters for this query. `topic="news"` only for explicit news
    asks — "silver price today" is a price page, not a news article."""
    params = {
        "search_depth": SEARCH_DEPTH,
        "max_results": MAX_RESULTS,
        "exclude_domains": EXCLUDED_DOMAINS,
        "timeout": SEARCH_TIMEOUT_SEC,
    }
    if _NEWS_RE.search(query):
        params["topic"] = "news"
        params["time_range"] = "day" if _VERY_RECENT_RE.search(query) else "week"
    return params


def is_time_sensitive(query: str) -> bool:
    return bool(_FRESH_RE.search(query))


# ─────────────────────────────────────────────────────────────────────────────
# CLEANING + CHUNKING
# ─────────────────────────────────────────────────────────────────────────────

_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)")
_MD_TITLE_LINK_RE = re.compile(r"\]\(\s*\"[^\"]*\"\s*\)")
_MD_ESCAPE_RE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!%| ])")
_URL_RE = re.compile(r"https?://\S+")
_IMAGE_PLACEHOLDER_RE = re.compile(r"_?Image \d+_?", re.I)
_HEADING_RE = re.compile(r"(^|\s)#{1,6}\s+")
_ENUMERATOR_RE = re.compile(r"^\s*\d{1,2}[.)]\s+")
_BARE_PATH_RE = re.compile(r"\b(?:[\w-]+\.)+[a-z]{2,}/\S*", re.I)   # "openai.com/news/"
_ISO_STAMP_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}T[\d:.]+Z?\b")
_CITATION_RE = re.compile(r"↑|↩|\bRetrieved \d|\bArchived from the original\b")
_EMPHASIS_RE = re.compile(r"(\*\*|__|\*|_(?=\S))")
_TABLE_RULE_RE = re.compile(r"^[\s|:\-]+$")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9₹$€£\"'(])")
_WS_RE = re.compile(r"\s+")

_BOILERPLATE_RE = re.compile(
    r"\b(cookies?|subscribe|newsletter|sign (up|in)|log ?in|advertisement|sponsored|"
    r"join (our )?whatsapp|whatsapp channel|all rights reserved|copyright|privacy policy|"
    r"terms of (use|service)|loading\.\.\.|load more|read more|click here|share this|"
    r"follow us|download (the|our) app|watch reels|skip to (main )?content|related news|"
    r"more stories|view all)\b",
    re.I,
)


def _clean_line(line: str) -> str:
    line = _MD_IMAGE_RE.sub(" ", line)
    line = _MD_LINK_RE.sub(r"\1", line)
    line = _MD_TITLE_LINK_RE.sub(" ", line)  # leftovers like ]( "Headline")
    line = line.replace("[[", "[").replace("]]", "]")
    line = _URL_RE.sub(" ", line)
    line = _IMAGE_PLACEHOLDER_RE.sub(" ", line)
    line = _BARE_PATH_RE.sub(" ", line)
    line = _ISO_STAMP_RE.sub(" ", line)
    line = _HEADING_RE.sub(" ", line)
    line = _ENUMERATOR_RE.sub("", line)
    line = _EMPHASIS_RE.sub("", line)
    line = _MD_ESCAPE_RE.sub(r"\1", line)  # "5.25%\." -> "5.25%."
    return _WS_RE.sub(" ", line).strip()


def _is_junk(text: str, table_row: bool = False) -> bool:
    # Table rows are compact by nature ("Oct 03, 2026 | ₹2,349 | ₹2,34,900")
    # and are often the exact answer, so they get a lower bar.
    if len(text) < (TABLE_ROW_MIN_CHARS if table_row else PASSAGE_MIN_CHARS):
        return True
    alnum = sum(ch.isalnum() for ch in text)
    if alnum / max(1, len(text)) < (0.4 if table_row else 0.55):
        return True
    if _CITATION_RE.search(text):  # Wikipedia reference lists
        return True
    if text.count("›") + text.count("»") >= 2:  # breadcrumbs: Home › Economy › ...
        return True
    if text.endswith("?") and len(text) < 150:  # FAQ headings: on-topic, but no facts
        return True
    # Short lines that are mostly site chrome. Long passages that merely
    # mention "login" etc. are kept and left to relevance scoring.
    return len(text) < 200 and bool(_BOILERPLATE_RE.search(text))


def _table_row(line: str) -> Optional[str]:
    if not line.startswith("|"):
        return None
    if _TABLE_RULE_RE.match(line):
        return ""
    cells = [c.strip() for c in line.strip("|").split("|")]
    return " | ".join(c for c in cells if c)


def _split_long(text: str) -> List[str]:
    """Sentence-bounded windows of ~PASSAGE_TARGET_CHARS, never cutting mid-word."""
    out, buf = [], ""
    for sent in _SENTENCE_SPLIT_RE.split(text):
        sent = sent.strip()
        if not sent:
            continue
        while len(sent) > PASSAGE_MAX_CHARS:  # one giant "sentence" (lists, tables flattened)
            cut = sent.rfind(" ", 0, PASSAGE_MAX_CHARS)
            cut = cut if cut > PASSAGE_MIN_CHARS else PASSAGE_MAX_CHARS
            if buf:
                out.append(buf)
                buf = ""
            out.append(sent[:cut].strip())
            sent = sent[cut:].strip()
        if buf and len(buf) + 1 + len(sent) > PASSAGE_TARGET_CHARS:
            out.append(buf)
            buf = sent
        else:
            buf = f"{buf} {sent}".strip()
    if buf:
        out.append(buf)
    return out


def chunk_content(content: str) -> List[str]:
    """Clean a Tavily `content` field and split it into candidate passages.
    Tavily joins separate page snippets with "[...]" — those are hard breaks."""
    passages: List[tuple] = []   # (text, is_table_row)
    for segment in (content or "").split("[...]"):
        prose: List[str] = []

        def flush():
            if prose:
                passages.extend((p, False) for p in _split_long(" ".join(prose)))
                prose.clear()

        for raw in segment.splitlines():
            line = raw.strip()
            if not line:
                flush()  # blank line = paragraph break
                continue
            row = _table_row(line)
            if row is not None:
                flush()
                row = _clean_line(row)
                if row:
                    passages.append((row[:PASSAGE_MAX_CHARS], True))
                continue
            line = _clean_line(line)
            if line:
                prose.append(line)
        flush()
    return [p for p, is_row in passages if not _is_junk(p, table_row=is_row)]


# ─────────────────────────────────────────────────────────────────────────────
# DATES (so the model can tell a 9 Sep price from a 3 Oct one)
# ─────────────────────────────────────────────────────────────────────────────

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE_DMY_RE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?[\s\-]+{_MON},?[\s\-]+(\d{{4}})\b", re.I)
_DATE_MDY_RE = re.compile(rf"\b{_MON}\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I)
_DATE_ISO_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DATE_CUE_RE = re.compile(r"\b(as of|as on|updated(?: on)?|last updated|published(?: on)?|posted(?: on)?)\b[:\s\-]*", re.I)


def _parse_dates(text: str) -> List[datetime]:
    found = []
    for m in _DATE_DMY_RE.finditer(text):
        found.append((int(m.group(3)), _MONTHS[m.group(2)[:3].lower()], int(m.group(1))))
    for m in _DATE_MDY_RE.finditer(text):
        found.append((int(m.group(3)), _MONTHS[m.group(1)[:3].lower()], int(m.group(2))))
    for m in _DATE_ISO_RE.finditer(text):
        found.append((int(m.group(1)), int(m.group(2)), int(m.group(3))))
    out = []
    for y, mo, d in found:
        try:
            out.append(datetime(y, mo, d))
        except ValueError:
            pass
    return out


def source_date(hit: dict, now: Optional[datetime] = None) -> Optional[datetime]:
    """Best-effort page date: Tavily's published_date (news), else a date in
    the title, else a date right after "as of/updated/published" in the text.
    Bare dates elsewhere in the body are ignored — they are usually history."""
    now = now or datetime.now()
    limit = now + timedelta(days=1)
    candidates: List[datetime] = []
    published = hit.get("published_date") or ""
    candidates += _parse_dates(published) or _parse_dates(published.replace(",", " "))
    if not candidates:
        candidates += _parse_dates(hit.get("title", ""))
    if not candidates:
        content = hit.get("content", "") or ""
        for cue in _DATE_CUE_RE.finditer(content):
            candidates += _parse_dates(content[cue.end(): cue.end() + 40])
    valid = [d for d in candidates if d <= limit]
    return max(valid) if valid else None


# ─────────────────────────────────────────────────────────────────────────────
# RELEVANCE SCORING
# ─────────────────────────────────────────────────────────────────────────────

_STOPWORDS = frozenset(
    "a an the of in on at to for from by with and or is are was were be been what who whom which "
    "when where why how does do did can could should would will me my i you your it its this that "
    "these those there their about tell give show find please current currently latest today now "
    "2024 2025 2026 2027".split()
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _keywords(text: str) -> set:
    toks = set()
    for t in _TOKEN_RE.findall(text.lower()):
        if t in _STOPWORDS or (len(t) < 3 and not t.isdigit()):
            continue
        toks.add(t[:-1] if len(t) > 4 and t.endswith("s") else t)
    return toks


def _lexical_overlap(query_kw: set, passage: str) -> float:
    if not query_kw:
        return 0.0
    return len(query_kw & _keywords(passage)) / len(query_kw)


Embedder = Callable[[str, Sequence[str]], np.ndarray]   # -> cosine similarity per passage
Scorer = Callable[[str, Sequence["Passage"]], List[float]]


def make_minilm_similarity(embeddings) -> Embedder:
    """Cosine similarity via a LangChain Embeddings object (embed_query/embed_documents)."""
    def similarity(query: str, texts: Sequence[str]) -> np.ndarray:
        q = np.asarray(embeddings.embed_query(query), dtype=np.float32)
        d = np.asarray(embeddings.embed_documents(list(texts)), dtype=np.float32)
        q /= (np.linalg.norm(q) or 1.0)
        d /= np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-8)
        return d @ q
    return similarity


@dataclass
class Passage:
    text: str
    source_idx: int
    order: int
    source_score: float = 0.0
    score: float = 0.0


def make_hybrid_scorer(similarity: Embedder) -> Scorer:
    """0.65 semantic + 0.25 keyword overlap + 0.10 Tavily source score,
    minus a small penalty for fragments under SHORT_PASSAGE_CHARS (labels,
    infobox cells) that match the topic without stating anything.
    Keyword overlap rescues exact facts (numbers, names, table rows) that a
    small embedding model under-rates; the source score breaks ties."""
    def score(query: str, passages: Sequence[Passage]) -> List[float]:
        if not passages:
            return []
        sem = similarity(query, [p.text for p in passages])
        qkw = _keywords(query)
        return [
            float(0.65 * max(0.0, s) + 0.25 * _lexical_overlap(qkw, p.text) + 0.10 * p.source_score
                  - (SHORT_PASSAGE_PENALTY if len(p.text) < SHORT_PASSAGE_CHARS else 0.0))
            for s, p in zip(sem, passages)
        ]
    return score


# ─────────────────────────────────────────────────────────────────────────────
# SELECTION
# ─────────────────────────────────────────────────────────────────────────────


def _unproxied_url(url: str) -> str:
    """The real page behind a translate.google proxy link, else the URL itself."""
    try:
        parsed = urlparse(url)
        if "translate.google" in parsed.netloc:
            return parse_qs(parsed.query).get("u", [""])[0] or url
    except Exception:
        pass
    return url


def _canonical_url(url: str) -> str:
    """Collapse proxies/mirrors (translate.google, m./www./amp.) to one key."""
    try:
        parsed = urlparse(_unproxied_url(url))
        host = re.sub(r"^(www|m|amp|mobile)\.", "", parsed.netloc.lower())
        return host + parsed.path.rstrip("/").lower()
    except Exception:
        return url


def _domain(url: str) -> str:
    try:
        return re.sub(r"^(www|m|amp|mobile)\.", "", urlparse(url).netloc.lower())
    except Exception:
        return ""


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def select_passages(passages: List[Passage]) -> tuple[List[Passage], bool]:
    """Pick the best passages under the budgets. Returns (picked, weak) where
    weak=True means nothing cleared MIN_RELEVANCE and only near-misses remain."""
    ranked = sorted(passages, key=lambda p: p.score, reverse=True)
    strong = [p for p in ranked if p.score >= MIN_RELEVANCE]
    weak = not strong
    pool = strong or [p for p in ranked if p.score >= WEAK_RELEVANCE_FLOOR][:2]

    picked: List[Passage] = []
    picked_tokens: List[set] = []
    per_source: dict = {}
    used = 0
    for p in pool:
        if len(picked) >= MAX_PASSAGES:
            break
        if per_source.get(p.source_idx, 0) >= MAX_PASSAGES_PER_SOURCE:
            continue
        if p.source_idx not in per_source and len(per_source) >= MAX_SOURCES:
            continue
        toks = set(_TOKEN_RE.findall(p.text.lower()))
        if any(_jaccard(toks, t) >= DUPLICATE_JACCARD for t in picked_tokens):
            continue  # same fact syndicated across sites
        if used + len(p.text) > OUTPUT_CHAR_BUDGET and picked:
            continue  # a shorter, lower-ranked passage may still fit
        picked.append(p)
        picked_tokens.append(toks)
        per_source[p.source_idx] = per_source.get(p.source_idx, 0) + 1
        used += len(p.text)
    return picked, weak


def format_results(hits: List[dict], picked: List[Passage], weak: bool, now: datetime) -> str:
    # Sources in order of their best passage; passages in page order within a source.
    best: dict = {}
    for p in picked:
        best[p.source_idx] = max(best.get(p.source_idx, 0.0), p.score)
    blocks = []
    for n, src in enumerate(sorted(best, key=best.get, reverse=True), 1):
        hit = hits[src]
        url = _unproxied_url(hit.get("url", ""))
        dated = source_date(hit, now)
        meta = _domain(url)
        if dated:
            meta += f", {dated.day} {dated.strftime('%b %Y')}"
        lines = [f"[{n}] {(hit.get('title') or '').strip()[:110]} ({meta})"]
        lines += [f"- {p.text}" for p in sorted((p for p in picked if p.source_idx == src), key=lambda p: p.order)]
        lines.append(f"URL: {url}")
        blocks.append("\n".join(lines))

    if weak:
        # "no matching" is one of the adaptive loop's failure signals on
        # purpose: a weak search should let the evaluator try a better query.
        note = ("Only loosely related excerpts — no matching passage clearly answers the question. "
                "Use them only if they actually answer it; otherwise say the web search did not settle it.")
    else:
        note = ("Answer only from these excerpts. If sources disagree, prefer the most recent "
                "dated one and mention the difference. Do not add citation markers like 【1†L1-L4】 "
                "or [1]; name a source in plain words if needed.")
    return "\n\n".join(blocks) + "\n\n" + note


# ─────────────────────────────────────────────────────────────────────────────
# CACHE + CLIENT
# ─────────────────────────────────────────────────────────────────────────────

_cache: "OrderedDict[str, tuple[float, str]]" = OrderedDict()
_cache_lock = threading.Lock()


def _cache_key(query: str) -> str:
    return _WS_RE.sub(" ", query.lower()).strip(" ?.!")


def cache_get(query: str) -> Optional[str]:
    key = _cache_key(query)
    with _cache_lock:
        item = _cache.get(key)
        if not item:
            return None
        if item[0] < time.time():
            _cache.pop(key, None)
            return None
        _cache.move_to_end(key)
        return item[1]


def cache_put(query: str, text: str) -> None:
    ttl = CACHE_TTL_FRESH_SEC if is_time_sensitive(query) else CACHE_TTL_STABLE_SEC
    with _cache_lock:
        _cache[_cache_key(query)] = (time.time() + ttl, text)
        _cache.move_to_end(_cache_key(query))
        while len(_cache) > CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)


def cache_clear() -> None:
    with _cache_lock:
        _cache.clear()


# One client per worker thread: requests.Session keeps the TLS connection to
# Tavily alive between calls, and per-thread avoids sharing a Session across
# the chat thread pool.
_local = threading.local()


def thread_client(factory: Callable[[], object]):
    client = getattr(_local, "client", None)
    if client is None:
        client = _local.client = factory()
    return client


# ─────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────────────────────────────────────

NO_RESULTS_MESSAGE = "No web results found for this query. Try rephrasing it with more specific terms."
NO_MATCH_MESSAGE = ("No web results matched the question closely enough to rely on. "
                    "Try a more specific query.")


@dataclass
class _Outcome:
    hits: List[dict]
    picked: List[Passage]
    weak: bool

    @property
    def usable(self) -> bool:
        return bool(self.picked) and not self.weak


def _search_once(query: str, client, scorer: Scorer, params: dict) -> _Outcome:
    hits = client.search(query=query, **params).get("results") or []

    # Drop mirror/proxy duplicates of the same page, keeping the first (higher-ranked).
    seen, unique_hits = set(), []
    for h in hits:
        key = _canonical_url(h.get("url", ""))
        if key not in seen:
            seen.add(key)
            unique_hits.append(h)

    passages: List[Passage] = []
    for src, hit in enumerate(unique_hits):
        title_tokens = set(_TOKEN_RE.findall((hit.get("title") or "").lower()))
        for order, text in enumerate(chunk_content(hit.get("content", ""))):
            if _jaccard(set(_TOKEN_RE.findall(text.lower())), title_tokens) >= TITLE_ECHO_JACCARD:
                continue
            passages.append(Passage(text, src, order, float(hit.get("score") or 0.0)))
    if len(passages) > MAX_PASSAGES_TO_SCORE:
        # Keep the leading passages of every source rather than all of one long page.
        passages.sort(key=lambda p: (p.order, p.source_idx))
        passages = passages[:MAX_PASSAGES_TO_SCORE]

    if passages:
        for p, s in zip(passages, scorer(query, passages)):
            p.score = s
    picked, weak = select_passages(passages)
    return _Outcome(unique_hits, picked, weak)


def run_web_search(query: str, client, scorer: Scorer, now: Optional[datetime] = None) -> str:
    """Search, filter and format. Raises on transport errors — the tool
    wrapper turns those into a "Web search failed: ..." message."""
    query = (query or "").strip()
    if not query:
        return "Please provide a search query."
    cached = cache_get(query)
    if cached is not None:
        return cached

    now = now or datetime.now()
    params = search_params(query)
    outcome = _search_once(query, client, scorer, params)
    if not outcome.usable and params.get("topic") == "news":
        # The news index is narrow (recent articles only); if it had nothing
        # solid, one general search usually does. Costs 1 extra credit, only here.
        general = {k: v for k, v in params.items() if k not in ("topic", "time_range")}
        retry = _search_once(query, client, scorer, general)
        if retry.usable or (retry.picked and not outcome.picked):
            outcome = retry

    if not outcome.hits:
        return NO_RESULTS_MESSAGE
    if not outcome.picked:
        return NO_MATCH_MESSAGE

    text = format_results(outcome.hits, outcome.picked, outcome.weak, now)
    if not outcome.weak:
        cache_put(query, text)
    return text
