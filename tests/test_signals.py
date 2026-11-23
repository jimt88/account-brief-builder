"""Offline tests for the website reader and the quote check (no network, no Gemini).

Run from the project folder:  python -m pytest -q
"""

from unittest import mock

from brief.industries import IndustryGroup
from brief.signals import CompanyProfile, Signal, SignalType, story_query_text, verify_signals
from brief import website
from brief.website import Page, pick_target_links, read_extra_pages, read_website

PAGE = Page(
    url="https://example.com/about",
    title="About",
    text="Example Ltd is based in Dublin. We are migrating our legacy Oracle database to the cloud this year.",
)
CAREERS = Page(
    url="https://example.com/careers",
    title="Careers",
    text="We are hiring 20 backend engineers to build real-time payment services.",
)


def signal(quote, url=PAGE.url, kind=SignalType.LEGACY_DATABASE):
    return Signal(type=kind, quote=quote, source_url=url)


def test_real_quote_is_kept():
    real = signal("We are migrating our legacy Oracle database to the cloud this year.")
    assert verify_signals([real], [PAGE]) == [real]


def test_fake_quote_is_rejected():
    fake = signal("We are replacing our legacy database with MongoDB Atlas next quarter.")
    assert verify_signals([fake], [PAGE, CAREERS]) == []


def test_edited_quote_is_rejected():
    edited = signal("We are migrating our old Oracle database to the cloud this year.")
    assert verify_signals([edited], [PAGE]) == []


def test_case_spacing_and_curly_quotes_are_ignored():
    page = Page("https://example.com/news", "News", "We\u2019re launching an  AI assistant for customers.")
    assert len(verify_signals([signal("we're launching an AI assistant for customers.", page.url)], [page])) == 1


def test_wrong_url_is_corrected_to_the_page_with_the_quote():
    s = signal("We are hiring 20 backend engineers to build real-time payment services.", url=PAGE.url)
    [kept] = verify_signals([s], [PAGE, CAREERS])
    assert kept.source_url == CAREERS.url


def test_story_query_skips_missing_country():
    profile = CompanyProfile(
        company="Example", what_they_do="Payments.", industry="Fintech",
        industry_group=IndustryGroup.FINANCIAL, hq_country="", situation="Modernising.", signals=[],
    )
    assert "()" not in story_query_text(profile) and ", )" not in story_query_text(profile)


def test_target_pages_come_first():
    links = ["https://example.com/news/a", "https://example.com/careers", "https://example.com/about-us",
             "https://example.com/products"]
    assert pick_target_links(links)[:3] == [
        "https://example.com/about-us", "https://example.com/careers", "https://example.com/news/a"]


class FakeResponse:
    def __init__(self, url, status=200, html="", content_type="text/html"):
        self.url, self.status_code, self.text = url, status, html
        self.headers = {"Content-Type": content_type}


def fake_site(url, **kwargs):
    """A tiny pretend website: robots.txt blocks /careers, /about is fine, /blog is missing."""
    long_text = "We build software for logistics companies across Europe. " * 10
    pages = {
        "https://example.com/robots.txt": FakeResponse(url, html="User-agent: *\nDisallow: /careers"),
        "https://example.com": FakeResponse(url, html=f'<a href="/about">About</a><a href="/careers">Careers</a>'
                                                      f'<a href="/blog">Blog</a><p>{long_text}</p>'),
        "https://example.com/about": FakeResponse(url, html=f"<p>{long_text}</p>"),
    }
    return pages.get(url.rstrip("/"), FakeResponse(url, status=404))


def test_read_website_respects_robots_and_explains_skipped_pages():
    notes = []
    with mock.patch.object(website.requests, "get", side_effect=fake_site):
        pages = read_website("example.com", notes=notes)
    assert [p.url for p in pages] == ["https://example.com", "https://example.com/about"]
    assert any("robots.txt" in n and "/careers" in n for n in notes)
    assert any("/blog" in n for n in notes)


def test_at_most_four_pages_and_ten_second_timeout():
    assert website.MAX_PAGES == 4 and website.TIMEOUT_SECONDS == 10


def test_login_only_sites_are_refused():
    pages, skipped = read_extra_pages(["https://www.linkedin.com/company/example"])
    assert pages == [] and "login" in skipped[0]


def test_parent_company_folder_is_not_mistaken_for_about():
    links = ["https://example.com/company/news/cloud-data-ai", "https://example.com/company/about-us",
             "https://example.com/company/careers", "https://example.com/engineering"]
    assert pick_target_links(links)[:3] == [
        "https://example.com/company/about-us", "https://example.com/company/careers",
        "https://example.com/engineering"]


class FakeCache:
    """A dict-backed stand-in for the page_cache collection."""

    def __init__(self):
        self.docs = {}

    def find_one(self, query):
        return self.docs.get(query["url"])

    def update_one(self, query, update, upsert=False):
        self.docs.setdefault(query["url"], {}).update(update["$set"])


def test_uncached_pages_are_stored_in_the_cache():
    cache = FakeCache()
    with mock.patch.object(website.requests, "get", side_effect=fake_site):
        pages = read_website("example.com", cache=cache)
    assert not any(p.cached for p in pages)
    assert set(cache.docs) == {"https://example.com", "https://example.com/about"}
    assert cache.docs["https://example.com"]["links"] == ["https://example.com/about"]
    assert cache.docs["https://example.com/about"]["fetched_at"].tzinfo is not None


def test_cached_pages_skip_every_download():
    cache = FakeCache()
    with mock.patch.object(website.requests, "get", side_effect=fake_site):
        first = read_website("example.com", cache=cache)
    with mock.patch.object(website.requests, "get", side_effect=AssertionError("no downloads")) as get:
        again = read_website("example.com", cache=cache)
    assert get.call_count == 0  # not even robots.txt
    assert [(p.url, p.text) for p in again] == [(p.url, p.text) for p in first]
    assert all(p.cached for p in again)


def test_cached_extra_link_is_not_downloaded():
    cache = FakeCache()
    with mock.patch.object(website.requests, "get", side_effect=fake_site):
        read_extra_pages(["https://example.com/about"], cache=cache)
    with mock.patch.object(website.requests, "get", side_effect=AssertionError("no downloads")):
        pages, skipped = read_extra_pages(["https://example.com/about"], cache=cache)
    assert [p.cached for p in pages] == [True] and skipped == []
