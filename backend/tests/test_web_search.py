"""
Tests for web_search_service — the pipeline behind tools.web_search.

No network and no embedding model: Tavily is a fake client and semantic
similarity is a keyword-overlap stand-in, so these run in well under a second.
"""
from datetime import datetime

import numpy as np
import pytest

import web_search_service as ws
from adaptive_loop import _FAILURE_SIGNAL_RE

NOW = datetime(2026, 10, 7, 12, 0)


def _fake_similarity(query, texts):
    q = ws._keywords(query)
    return np.array([len(q & ws._keywords(t)) / max(1, len(q)) for t in texts], dtype=np.float32)


SCORER = ws.make_hybrid_scorer(_fake_similarity)


class FakeClient:
    """Returns canned Tavily responses in order and records each call's params."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def search(self, query, **params):
        self.calls.append(params)
        return self.responses.pop(0) if self.responses else {"results": []}


def _hit(url, title, content, score=0.9, **extra):
    return {"url": url, "title": title, "content": content, "score": score, **extra}


@pytest.fixture(autouse=True)
def _clear_cache():
    ws.cache_clear()
    yield
    ws.cache_clear()


# ─────────────────────────────────────────────────────────────────────────────
# Query intent
# ─────────────────────────────────────────────────────────────────────────────


def test_news_topic_only_for_explicit_news_queries():
    assert ws.search_params("latest news about OpenAI")["topic"] == "news"
    assert ws.search_params("breaking news today")["time_range"] == "day"
    assert ws.search_params("OpenAI news this month")["time_range"] == "week"
    # A price page is not a news article — keep the general index.
    assert "topic" not in ws.search_params("silver price today India")
    assert "topic" not in ws.search_params("latest Python version")


def test_search_params_stay_on_one_credit_depth_with_timeout():
    params = ws.search_params("anything")
    assert params["search_depth"] == "basic"
    assert params["timeout"] <= 10
    assert "youtube.com" in params["exclude_domains"]


# ─────────────────────────────────────────────────────────────────────────────
# Cleaning + chunking
# ─────────────────────────────────────────────────────────────────────────────


def test_chunking_drops_site_chrome_and_keeps_facts():
    content = (
        "## Silver Price in India\n\n"
        "The price of silver in India today is ₹234.90 per gram and ₹2,34,900 per kilogram.\n\n"
        "Join WhatsApp Channel\n"
        "We use cookies on our website to give you the best experience.\n"
        "Home › Economy › Silver Rate Today\n"
        "1. ### What is the current price of silver in India?\n"
        "[...] | Date | 10 gram | 1 Kg |\n| --- | --- | --- |\n| Oct 03, 2026 | ₹2,349 | ₹2,34,900 |"
    )
    passages = ws.chunk_content(content)
    joined = " || ".join(passages)
    assert "₹234.90 per gram" in joined
    assert "Oct 03, 2026 | ₹2,349 | ₹2,34,900" in joined
    for junk in ("WhatsApp", "cookies", "Home ›", "What is the current price", "##", "---"):
        assert junk not in joined


def test_chunking_strips_links_urls_and_citations():
    content = (
        "Spain won the [2026 FIFA World Cup](https://example.com/wc) final 1–0 after extra time, "
        "see https://example.com/report for details.\n\n"
        "8. ↑ \"Spain crowned champions\". The Guardian. Retrieved 20 July 2026.\n"
        "www.theverge.com/sports/123 2026-07-19T00:00:00.0000000 Spain celebrates in Madrid after the final"
    )
    passages = ws.chunk_content(content)
    joined = " || ".join(passages)
    assert "Spain won the 2026 FIFA World Cup final" in joined
    assert "http" not in joined and "theverge.com" not in joined and "T00:00" not in joined
    assert "Retrieved" not in joined


def test_markdown_escapes_removed():
    passages = ws.chunk_content("The current RBI repo rate is 5.25%\\. It has stayed at 5.25%\\ since December 2025\\.")
    assert passages == ["The current RBI repo rate is 5.25%. It has stayed at 5.25% since December 2025."]


def test_long_text_splits_on_sentence_boundaries():
    sentence = "Fadnavis took office as chief minister on 5 December 2024 after the state election. "
    passages = ws.chunk_content(sentence * 12)
    assert len(passages) > 1
    assert all(len(p) <= ws.PASSAGE_MAX_CHARS for p in passages)
    assert all(p.endswith(".") for p in passages)


# ─────────────────────────────────────────────────────────────────────────────
# Dates
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("hit, expected", [
    ({"title": "Silver Rate Today (3 October 2026)", "content": ""}, datetime(2026, 10, 3)),
    ({"title": "Silver price", "content": "As of 29 September 2026, silver is trading at ₹225323."}, datetime(2026, 9, 29)),
    ({"title": "x", "content": "", "published_date": "Thu, 02 Oct 2026 08:00:00 GMT"}, datetime(2026, 10, 2)),
    ({"title": "Nippon ETF", "content": "share price is ₹209.02 as on 07-Oct-2026, down 0.8%"}, datetime(2026, 10, 7)),
])
def test_source_date(hit, expected):
    assert ws.source_date(hit, NOW) == expected


def test_source_date_ignores_history_dates_in_body_and_future_dates():
    assert ws.source_date({"title": "FIFA World Cup", "content": "Argentina won on 18 December 2022."}, NOW) is None
    assert ws.source_date({"title": "Match on 12 March 2027", "content": ""}, NOW) is None


# ─────────────────────────────────────────────────────────────────────────────
# End to end
# ─────────────────────────────────────────────────────────────────────────────

CM_RESPONSE = {"results": [
    _hit("https://en.wikipedia.org/wiki/Devendra_Fadnavis", "Devendra Fadnavis - Wikipedia",
         "Devendra Fadnavis is serving as the Chief Minister of Maharashtra since 5 December 2024.\n\n"
         "He was born in Nagpur and studied law at Nagpur University before entering local politics in 1992."),
    # Same page through a translate proxy — must be collapsed into one source.
    _hit("https://translate.google.com/translate?u=https%3A%2F%2Fm.en.wikipedia.org%2Fwiki%2FDevendra_Fadnavis",
         "Devendra Fadnavis", "Devendra Fadnavis is serving as the Chief Minister of Maharashtra since 5 December 2024."),
    _hit("https://example.com/cricket", "Cricket scores",
         "India beat Australia by six wickets in the second test at Adelaide on Sunday afternoon.", score=0.7),
]}


def test_relevant_passages_kept_irrelevant_dropped():
    out = ws.run_web_search("current chief minister of Maharashtra", FakeClient(CM_RESPONSE), SCORER, NOW)
    assert "Chief Minister of Maharashtra since 5 December 2024" in out
    assert "Australia" not in out and "Nagpur University" not in out
    assert out.count("Chief Minister of Maharashtra since") == 1  # proxy duplicate removed
    assert "translate.google" not in out
    assert "Answer only from these excerpts" in out


def test_success_output_does_not_trip_adaptive_failure_trigger():
    out = ws.run_web_search("current chief minister of Maharashtra", FakeClient(CM_RESPONSE), SCORER, NOW)
    assert not _FAILURE_SIGNAL_RE.search(out)


def test_empty_results_trip_adaptive_failure_trigger():
    out = ws.run_web_search("anything at all", FakeClient({"results": []}), SCORER, NOW)
    assert out == ws.NO_RESULTS_MESSAGE
    assert _FAILURE_SIGNAL_RE.search(out)


def test_weak_matches_are_flagged_trip_the_trigger_and_are_not_cached():
    weak_response = {"results": [_hit(
        "https://example.com/a", "Moon",
        "The chief guest at the Pune moon festival spoke about lunar folklore and harvest songs.", score=0.6)]}
    client = FakeClient(weak_response, weak_response)
    out = ws.run_web_search("chief minister Maharashtra salary", client, SCORER, NOW)
    assert "loosely related" in out
    assert _FAILURE_SIGNAL_RE.search(out)
    ws.run_web_search("chief minister Maharashtra salary", client, SCORER, NOW)
    assert len(client.calls) == 2  # weak output was not served from cache


def test_weak_news_search_retries_on_general_index():
    weak_news = {"results": [_hit("https://example.com/n", "Sports", "High school football tonight at Edison.", 0.5)]}
    general = {"results": [_hit("https://isro.gov.in/x", "ISRO mission",
                                "ISRO launched the PSLV mission carrying an earth observation satellite this week.")]}
    client = FakeClient(weak_news, general)
    out = ws.run_web_search("ISRO mission news this week", client, SCORER, NOW)
    assert client.calls[0]["topic"] == "news"
    assert "topic" not in client.calls[1]
    assert "PSLV mission" in out and "football" not in out


def test_repeat_query_served_from_cache():
    client = FakeClient(CM_RESPONSE)
    first = ws.run_web_search("Current Chief Minister of Maharashtra?", client, SCORER, NOW)
    second = ws.run_web_search("current chief minister of maharashtra", client, SCORER, NOW)
    assert first == second
    assert len(client.calls) == 1


def test_output_respects_passage_budget():
    long = " ".join(f"Maharashtra chief minister fact number {i} is recorded in the state archive." for i in range(80))
    response = {"results": [_hit(f"https://site{i}.com/p", f"Page {i}", long) for i in range(6)]}
    out = ws.run_web_search("Maharashtra chief minister", FakeClient(response), SCORER, NOW)
    bullet_chars = sum(len(line) - 2 for line in out.splitlines() if line.startswith("- "))
    assert bullet_chars <= ws.OUTPUT_CHAR_BUDGET
    assert out.count("\n- ") <= ws.MAX_PASSAGES
    assert out.count("\nURL: ") <= ws.MAX_SOURCES
