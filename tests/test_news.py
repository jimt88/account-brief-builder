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


ARTICLE = {"url": "https://news.example.ie/boi-ai", "title": "Bank of Ireland rolls out AI",
           "published_at": datetime(2026, 11, 20, tzinfo=timezone.utc), "domain": "news.example.ie"}


def test_login_form_page_is_not_news():
    form = '<form><input type="password"></form><p>' + "Sign in to keep reading this story about AI. " * 20 + "</p>"
    with mock.patch.object(news.requests, "get",
                           side_effect=lambda url, **kw: FakeResponse(url, status=404 if "robots" in url else 200,
                                                                      text=form)):
        assert news.read_article(ARTICLE) is None


def test_news_cache_is_kept_apart_from_website_cache():
    cache = mock.Mock()
    cache.find_one.return_value = None
    with mock.patch.object(news.requests, "get", side_effect=fake_web):
        page = news.read_article(ARTICLE, cache)
    assert page.source_type == "news"
    assert cache.find_one.call_args[0][0]["url"] == "news:https://news.example.ie/boi-ai"
    assert cache.update_one.call_args[0][0] == {"url": "news:https://news.example.ie/boi-ai"}


def test_cache_write_failure_keeps_the_article():
    from pymongo.errors import WriteError
    cache = mock.Mock()
    cache.find_one.return_value = None
    cache.update_one.side_effect = WriteError("Document failed validation")
    with mock.patch.object(news.requests, "get", side_effect=fake_web):
        assert news.read_article(ARTICLE, cache).source_type == "news"


def test_redirect_to_a_private_address_is_refused():
    from brief import website

    class Redirect(FakeResponse):
        def __init__(self, url):
            super().__init__(url, status=302)
            self.headers["Location"] = "http://169.254.169.254/latest/meta-data/"
    calls = []

    def get(url, **kwargs):
        calls.append(url)
        assert kwargs.get("allow_redirects") is False
        return Redirect(url)
    with mock.patch.object(website.requests, "get", side_effect=get):
        assert website.fetch("https://news.example.ie/boi-ai") is None
    assert calls == ["https://news.example.ie/boi-ai"]
    assert website.private_host("http://127.0.0.1/") and website.private_host("http://localhost/")
    assert not website.private_host("http://8.8.8.8/")


def test_article_with_an_optional_sign_in_box_is_still_read():
    html = ('<form><input type="password"></form><p>'
            + "Bank of Ireland said its new AI assistant now helps 10,000 staff answer questions. " * 30 + "</p>")
    with mock.patch.object(news.requests, "get",
                           side_effect=lambda url, **kw: FakeResponse(url, status=404 if "robots" in url else 200,
                                                                      text=html)):
        assert news.read_article(ARTICLE).source_type == "news"


def test_redirect_keeps_cookies_and_decodes_utf8_location():
    from brief import website
    seen = []

    class Hop(FakeResponse):
        def __init__(self, url, location=None):
            super().__init__(url, text="<p>" + "Public article text here. " * 20 + "</p>",
                             status=302 if location else 200)
            if location:
                self.headers["Location"] = location
            self.cookies = {"consent": "yes"} if location else {}

    def get(url, **kwargs):
        seen.append((url, dict(kwargs["cookies"])))
        return Hop(url, "/caf\u00c3\u00a9" if len(seen) == 1 else None)  # UTF-8 bytes read as latin-1.
    with mock.patch.object(website.requests, "get", side_effect=get):
        final_url, _ = website.fetch("HTTPS://news.example.ie/start")
    assert final_url == "https://news.example.ie/caf\u00e9"
    assert seen[1] == ("https://news.example.ie/caf\u00e9", {"consent": "yes"})
