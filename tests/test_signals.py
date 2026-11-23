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
    assert verify_signals([real], [PAGE]) == [real.model_copy(update={"source_type": "website"})]


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
        doc = self.docs.get(query["url"])
        return doc if doc and doc["fetched_at"] >= query["fetched_at"]["$gte"] else None

    def update_one(self, query, update, upsert=False):
        doc = self.docs.setdefault(query["url"], {})
        doc.update(update["$set"])
        for field in update.get("$unset", {}):
            doc.pop(field, None)


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


def test_expired_pages_are_ignored_before_ttl_deletes_them():
    cache = FakeCache()
    with mock.patch.object(website.requests, "get", side_effect=fake_site):
        read_extra_pages(["https://example.com/about"], cache=cache)
    cache.docs["https://example.com/about"]["fetched_at"] -= website.timedelta(seconds=website.CACHE_SECONDS + 1)
    assert website.cached_page(cache, "https://example.com/about") is None


def test_home_cached_as_extra_link_still_finds_sub_pages():
    cache = FakeCache()
    with mock.patch.object(website.requests, "get", side_effect=fake_site):
        read_extra_pages(["https://example.com"], cache=cache)  # Cached without links.
        pages = read_website("example.com", cache=cache)
    assert [p.url for p in pages] == ["https://example.com", "https://example.com/about"]


def test_refreshing_home_as_extra_link_drops_its_old_links():
    cache = FakeCache()
    with mock.patch.object(website.requests, "get", side_effect=fake_site):
        read_website("example.com", cache=cache)
        cache.docs["https://example.com"]["fetched_at"] -= website.timedelta(seconds=website.CACHE_SECONDS + 1)
        read_extra_pages(["https://example.com"], cache=cache)
    assert "links" not in cache.docs["https://example.com"]


def test_mongodb_in_use_needs_mongodb_in_the_quote():
    job = Page(url="https://jobs.lever.co/acme/1", title="Engineer", source_type="job",
               text="You will run MongoDB Atlas clusters in production. You will mentor engineers on our team.")
    kept = verify_signals([
        Signal(type=SignalType.MONGODB_IN_USE, quote="You will run MongoDB Atlas clusters in production.",
               source_url=job.url, technology="MongoDB"),
        Signal(type=SignalType.MONGODB_IN_USE, quote="You will mentor engineers on our team.", source_url=job.url),
        Signal(type=SignalType.COMPETITOR_DATABASE, quote="You will mentor engineers on our team.",
               source_url=job.url, technology="Oracle"),
    ], [job])
    assert [(s.type, s.technology) for s in kept] == [(SignalType.MONGODB_IN_USE, "MongoDB")]


def test_competitor_database_quote_must_name_a_database():
    job = Page(url="https://jobs.lever.co/acme/1", title="Engineer", source_type="job",
               text="Our ledger runs on Oracle and PostgreSQL today. You will mentor engineers on our team.")
    kept = verify_signals([
        Signal(type=SignalType.COMPETITOR_DATABASE, quote="Our ledger runs on Oracle and PostgreSQL today.",
               source_url=job.url, technology="Oracle"),
        Signal(type=SignalType.COMPETITOR_DATABASE, quote="You will mentor engineers on our team.", source_url=job.url),
        Signal(type=SignalType.COMPETITOR_DATABASE, quote="You will mentor engineers on our team.", source_url=job.url,
               technology="Oracle"),
    ], [job])
    assert [s.technology for s in kept] == ["Oracle"]
    other = Page(url="https://acme.com", title="Acme", text="Orders are stored in Vitess across three regions.")
    assert verify_signals([Signal(type=SignalType.COMPETITOR_DATABASE, quote=other.text, source_url=other.url,
                                  technology="Vitess")], [other])[0].technology == "Vitess"
    stream = Page(url="https://acme.com/s", title="Acme", text="Every payment flows through Kafka and MongoDB Atlas.")
    assert verify_signals([Signal(type=SignalType.COMPETITOR_DATABASE, quote=stream.text, source_url=stream.url,
                                  technology=tech) for tech in ("Kafka", "MongoDB")], [stream]) == []
    cosmos = Page(url="https://acme.com/j", title="Job", text="You will run Azure Cosmos DB for MongoDB in production.")
    assert verify_signals([Signal(type=SignalType.COMPETITOR_DATABASE, quote=cosmos.text, source_url=cosmos.url,
                                  technology="Azure Cosmos DB for MongoDB")], [cosmos])


def test_pages_are_labelled_with_source_and_date():
    from datetime import datetime, timezone
    from brief.signals import format_pages
    job = Page(url="https://jobs.lever.co/acme/1", title="Engineer", text="x" * 100_000, source_type="job",
               published_at=datetime(2026, 11, 1, tzinfo=timezone.utc))
    text = format_pages([PAGE, job])
    assert "=== JOB (2026-11-01): https://jobs.lever.co/acme/1 ===" in text
    assert "=== WEBSITE: https://example.com/about ===" in text
    assert len(text) < 40_000  # Long pages are cut to a fair share.
