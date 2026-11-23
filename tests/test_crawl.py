"""Phase 9 wider crawl: sitemaps, URL scoring, subdomains, time budget. No network."""

from datetime import datetime, timezone
from unittest import mock

from brief import website
from brief.website import parse_sitemap, read_website, registrable_domain, same_site, score_url

NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
TEXT = "We build cloud data platforms for logistics companies across Europe. " * 10

SITEMAP_INDEX = """<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <sitemap><loc>https://example.com/sitemap-pages.xml</loc></sitemap>
  <sitemap><loc>https://evil.test/sitemap.xml</loc></sitemap>
</sitemapindex>"""

SITEMAP_PAGES = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://example.com/careers/engineering</loc><lastmod>2026-10-01</lastmod></url>
  <url><loc>https://example.com/news/ai-platform</loc><lastmod>2020-01-01</lastmod></url>
  <url><loc>https://example.com/privacy-policy</loc></url>
  <url><loc>https://example.com/report.pdf</loc></url>
  <url><loc>https://example.com/shop/shoes</loc></url>
  <url><loc>https://investors.example.com/results</loc></url>
  <url><loc>https://other.test/careers</loc></url>
</urlset>"""


class FakeResponse:
    def __init__(self, url, text="", status=200, content_type="text/html"):
        self.url, self.text, self.status_code = url, text, status
        self.headers = {"Content-Type": content_type}


def fake_site(url, **kwargs):
    pages = {
        "https://example.com/robots.txt": "User-agent: *\nAllow: /\nSitemap: https://example.com/sitemap_index.xml",
        "https://example.com/sitemap_index.xml": SITEMAP_INDEX,
        "https://example.com/sitemap-pages.xml": SITEMAP_PAGES,
        "https://example.com": f"<a href='/about'>About</a><p>{TEXT}</p>",
        "https://example.com/careers/engineering": f"<p>{TEXT}</p>",
        "https://example.com/news/ai-platform": f"<p>{TEXT}</p>",
        "https://investors.example.com/robots.txt": "User-agent: *\nAllow: /",
        "https://investors.example.com/results": f"<p>{TEXT}</p>",
    }
    url = url.rstrip("/")
    if url not in pages:
        return FakeResponse(url, status=404)
    return FakeResponse(url, pages[url], content_type="text/xml" if url.endswith((".xml", ".txt")) else "text/html")


def test_parse_sitemap_index_and_urlset():
    assert parse_sitemap(SITEMAP_INDEX) == ([], ["https://example.com/sitemap-pages.xml", "https://evil.test/sitemap.xml"])
    pages, children = parse_sitemap(SITEMAP_PAGES)
    assert children == [] and pages[0] == ("https://example.com/careers/engineering", "2026-10-01")
    assert parse_sitemap("not xml <") == ([], [])


def test_url_scoring_keywords_recency_and_skips():
    assert score_url("https://example.com/careers/engineering", "2026-10-01", NOW) == 2 + website.RECENT_BONUS
    assert score_url("https://example.com/careers/engineering", "2020-01-01", NOW) == 2
    assert score_url("https://example.com/investors/annual-report-2025", "", NOW) == 2
    assert score_url("https://example.com/privacy-policy") == 0
    assert score_url("https://example.com/careers/report.pdf") == 0
    assert score_url("https://example.com/shop/shoes") == 0
    assert score_url("https://example.com/detail/mail") == 0  # "ai" only as a whole word


def test_same_registrable_domain_only():
    assert registrable_domain("careers.tesco.co.uk") == "tesco.co.uk"
    assert registrable_domain("www.bankofireland.com") == "bankofireland.com"
    assert same_site("https://investors.example.com/x", "https://www.example.com/")
    assert not same_site("https://example.com.evil.test/x", "https://example.com/")
    assert not same_site("https://other.test/careers", "https://example.com/")


def test_sitemap_pages_best_first_with_subdomains_and_no_other_sites():
    with mock.patch.object(website.requests, "get", side_effect=fake_site) as get:
        pages = read_website("example.com")
    assert [p.url for p in pages] == [
        "https://example.com", "https://example.com/careers/engineering",
        "https://example.com/news/ai-platform", "https://investors.example.com/results"]
    asked = {c.args[0] for c in get.call_args_list}
    assert not any("evil.test" in u or "other.test" in u for u in asked)
    assert not any(u.endswith(("privacy-policy", ".pdf", "/about")) for u in asked)


def test_crawl_stops_at_the_time_budget_and_keeps_pages_read():
    def slow_site(url, **kwargs):
        if "news" in url or "investors" in url and "robots" not in url:
            import time
            time.sleep(0.5)
        return fake_site(url)

    notes = []
    with mock.patch.object(website, "CRAWL_TIME_BUDGET_S", 0.3), \
         mock.patch.object(website.requests, "get", side_effect=slow_site):
        pages = read_website("example.com", notes=notes)
    assert "https://example.com/careers/engineering" in [p.url for p in pages]
    assert "https://example.com/news/ai-platform" not in [p.url for p in pages]
    assert any("Stopped reading the site" in n for n in notes)


def test_max_pages_caps_the_sitemap_crawl():
    with mock.patch.object(website.requests, "get", side_effect=fake_site):
        assert len(read_website("example.com", max_pages=2)) == 2
