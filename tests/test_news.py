"""Offline tests for the GDELT news reader (no network)."""

import json
from datetime import datetime, timezone
from unittest import mock

from brief import news
from brief.news import gdelt_query, parse_seendate, pick_articles, read_news

GDELT = {"articles": [
    {"url": "https://news.example.ie/boi-ai", "title": "Bank of Ireland rolls out AI assistant to staff",
     "seendate": "20261120T101500Z", "domain": "news.example.ie", "language": "English"},
    {"url": "https://other.example.com/boi-ai", "title": "Bank of Ireland rolls out AI assistant to staff!",
     "seendate": "20261120T111500Z", "domain": "other.example.com", "language": "English"},
    {"url": "https://zeitung.example.de/boi", "title": "Bank of Ireland startet KI-Assistenten",
     "seendate": "20261119T080000Z", "domain": "zeitung.example.de", "language": "German"},
    {"url": "https://sport.example.com/x", "title": "Ireland beat England at Lansdowne Road",
     "seendate": "20261118T080000Z", "domain": "sport.example.com", "language": "English"},
    {"url": "https://fin.example.com/y", "title": "Bank of Ireland results beat forecasts",
     "seendate": "20261101T080000Z", "domain": "fin.example.com", "language": "English"},
]}


class FakeResponse:
    def __init__(self, url, status=200, text="", content_type="text/html"):
        self.url, self.status_code, self.text = url, status, text
        self.headers = {"Content-Type": content_type}

    def json(self):
        return json.loads(self.text)


def test_gdelt_json_is_parsed_filtered_and_deduplicated():
    picked = pick_articles(GDELT, "Bank of Ireland")
    assert [a["url"] for a in picked] == [  # English first, newest first; German kept at the end.
        "https://news.example.ie/boi-ai", "https://fin.example.com/y", "https://zeitung.example.de/boi"]
    assert picked[0]["published_at"] == datetime(2026, 11, 20, 10, 15, tzinfo=timezone.utc)


def test_unrelated_titles_are_rejected():
    assert pick_articles({"articles": [{"url": "u", "title": "Allegrou wins award"}]}, "Allegro") == []
    assert pick_articles({"articles": [{"url": "u", "title": "Allegro launches ads"}]}, "Allegro")


def test_at_most_eight_articles():
    many = {"articles": [{"url": f"https://x.com/{n}", "title": f"DSV story number {n}"} for n in range(20)]}
    assert len(pick_articles(many, "DSV")) == news.MAX_NEWS_ARTICLES == 8


def test_query_quotes_long_names_and_adds_domain_for_short_ones():
    assert gdelt_query("Bank of Ireland", "bankofireland.com") == '"Bank of Ireland"'
    assert gdelt_query("N26", "https://www.n26.com") == "N26"
    assert gdelt_query("DSV", "dsv.com") == "DSV"
    assert gdelt_query("ABB", "global.abb") == "ABB global"
    assert parse_seendate("bad") is None


def fake_web(url, **kwargs):
    body = "<p>" + "Bank of Ireland said its new AI assistant now helps 10,000 staff answer questions. " * 5 + "</p>"
    if "gdeltproject" in url:
        return FakeResponse(url, text=json.dumps(GDELT))
    if url.endswith("/robots.txt"):
        return FakeResponse(url, status=404)
    if "fin.example.com" in url:
        return FakeResponse(url, status=403)  # Paywall.
    return FakeResponse(url, text=body)


def test_articles_become_dated_news_pages_and_paywalls_are_skipped():
    with mock.patch.object(news.requests, "get", side_effect=fake_web), \
            mock.patch("brief.website.requests.get", side_effect=fake_web):
        pages, summary, note = read_news("Bank of Ireland", "bankofireland.com")
    assert summary == {"articles_found": 3, "articles_read": 2} and note is None
    assert all(p.source_type == "news" and p.published_at for p in pages)
    assert "fin.example.com" not in " ".join(p.url for p in pages)


def test_gdelt_failure_is_a_note_not_an_error():
    with mock.patch.object(news.requests, "get", return_value=FakeResponse("x", status=429, text="slow down")):
        assert read_news("Acme Ltd", "acme.com") == ([], {}, "Skipped news: GDELT news search is busy "
                                                           "(one request every 5 seconds).")
    with mock.patch.object(news.requests, "get", side_effect=news.requests.Timeout):
        pages, summary, note = read_news("Acme Ltd", "acme.com")
    assert pages == [] and "didn't answer" in note
