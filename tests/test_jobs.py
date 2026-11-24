"""Offline tests for the job-board reader (no network: feeds come from tests/fixtures)."""

from pathlib import Path
from unittest import mock

from brief import jobs
from brief.jobs import JobBoard, find_job_board, is_technical, read_jobs, remove_personal_data
from brief.website import Page, outside_links
from bs4 import BeautifulSoup

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, url, status=200, text=""):
        self.url, self.status_code, self.text = url, status, text

    def iter_content(self, size):
        body = self.text.encode()
        yield from (body[i:i + size] for i in range(0, len(body), size))

    def close(self):
        pass


def serve(files: dict):
    """A fake requests.get that answers URLs containing a key with that fixture file."""
    def get(url, **kwargs):
        for part, name in files.items():
            if part in url:
                return FakeResponse(url, text=(FIXTURES / name).read_text())
        return FakeResponse(url, status=404)
    return get


def test_platforms_are_detected_from_links():
    cases = {
        "https://boards.greenhouse.io/acme": ("Greenhouse", "acme"),
        "https://job-boards.greenhouse.io/acme/jobs/123": ("Greenhouse", "acme"),
        "https://boards.greenhouse.io/embed/job_board/js?for=acme": ("Greenhouse", "acme"),
        "https://boards-api.greenhouse.io/v1/boards/acme/jobs": ("Greenhouse", "acme"),
        "https://jobs.lever.co/acme/abc-123": ("Lever", "acme"),
        "https://jobs.eu.lever.co/acme": ("Lever EU", "acme"),
        "https://jobs.ashbyhq.com/acme": ("Ashby", "acme"),
        "https://careers.smartrecruiters.com/Acme1": ("SmartRecruiters", "Acme1"),
        "https://acme.jobs.personio.de/": ("Personio", "acme"),
        "https://apply.workable.com/acme/": ("Workable", "acme"),
    }
    for link, (platform, slug) in cases.items():
        board, note = find_job_board([link])
        assert (board.platform, board.slug) == (platform, slug), link
        assert note is None


def test_workday_gets_a_note_and_no_download():
    with mock.patch.object(jobs.requests, "get") as get:
        pages, summary, note = read_jobs(["https://acme.wd3.myworkdayjobs.com/en-US/careers"])
    assert pages == [] and summary == {} and "Workday" in note and "Extra links" in note
    get.assert_not_called()


def test_no_board_link_means_no_calls():
    with mock.patch.object(jobs.requests, "get") as get:
        assert read_jobs(["https://example.com/careers", "https://apply.workable.com/j/ABC123"]) == ([], {}, None)
    get.assert_not_called()


def test_links_are_found_in_anchors_iframes_and_scripts():
    soup = BeautifulSoup('<a href="https://jobs.lever.co/acme">Jobs</a><a href="/about">About</a>'
                         '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>'
                         '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>'
                         '<a href="https://github.com/acme">GitHub</a><a href="https://clever.com/x">x</a>',
                         "html.parser")
    assert outside_links(soup, "https://acme.com") == [
        "https://jobs.lever.co/acme", "https://github.com/acme",
        "https://boards.greenhouse.io/embed/job_board?for=acme",
        "https://boards.greenhouse.io/embed/job_board/js?for=acme"]


def test_technical_filter():
    for title in ["Senior Backend Engineer", "Data Analyst", "Head of Engineering", "Site Reliability (SRE)",
                  "ML Researcher", "IT Support Specialist", "Database Administrator", "AI Product Lead"]:
        assert is_technical(title), title
    for title in ["Account Executive", "Business Development Representative", "Office Manager",
                  "Paid Social Lead", "Maintenance Technician"]:
        assert not is_technical(title), title


def test_personal_data_is_removed():
    text = remove_personal_data("Write to jane.doe@acme.ie or call +353 1 234 5678. We grew 40% in 2025 to €12,000,000.")
    assert "jane" not in text and "234 5678" not in text
    assert "2025" in text and "40%" in text


def test_greenhouse_feed():
    with mock.patch.object(jobs.requests, "get", side_effect=serve({"greenhouse": "greenhouse.json"})):
        pages, summary, note = read_jobs(["https://boards.greenhouse.io/mongodb"])
    assert note is None
    assert summary["platform"] == "Greenhouse" and summary["total_open"] == 3 and summary["technical_open"] == 2
    assert len(pages) == 2 and all(p.source_type == "job" and p.published_at for p in pages)
    assert all("greenhouse.io" in p.url or "mongodb.com" in p.url for p in pages)
    assert "boards-api" not in pages[0].url  # The public posting page is cited, not the API.
    assert "<p>" not in pages[0].text and "&lt;" not in pages[0].text
    assert pages[0].published_at >= pages[1].published_at  # Newest first.


def test_lever_feed():
    with mock.patch.object(jobs.requests, "get", side_effect=serve({"api.lever.co": "lever.json"})):
        pages, summary, _ = read_jobs(["https://jobs.lever.co/palantir"])
    assert summary["platform"] == "Lever" and summary["total_open"] == 3
    assert pages and all(p.url.startswith("https://jobs.lever.co/") for p in pages)
    assert all(p.published_at.year > 2000 for p in pages)  # createdAt is in milliseconds.


def test_personio_feed_keeps_tech_roles_and_drops_personal_data():
    with mock.patch.object(jobs.requests, "get", side_effect=serve({"personio": "personio.xml"})):
        pages, summary, _ = read_jobs(["https://acme.jobs.personio.de/"])
    assert summary["total_open"] == 2 and summary["technical_open"] == 1
    [page] = pages
    assert page.url == "https://acme.jobs.personio.de/job/2884401"
    assert "PostgreSQL" in page.text and "Kafka" in page.text
    assert "jane.doe" not in page.text and "1250 1004" not in page.text


def test_ashby_workable_and_smartrecruiters_feeds():
    files = {"ashbyhq": "ashby.json", "workable": "workable.json",
             "postings/7440": "smartrecruiters_detail.json", "smartrecruiters": "smartrecruiters.json"}
    with mock.patch.object(jobs.requests, "get", side_effect=serve(files)):
        ashby, _, _ = read_jobs(["https://jobs.ashbyhq.com/ramp"])
        workable, _, _ = read_jobs(["https://apply.workable.com/huggingface/"])
        smart, smart_summary, _ = read_jobs(["https://careers.smartrecruiters.com/ServiceNow"])
    assert ashby and all(p.url.startswith("https://jobs.ashbyhq.com/") for p in ashby)
    assert len(workable) == 2 and workable[0].text.startswith("Open-Source Machine Learning Engineer")
    assert smart_summary["total_open"] == 2 and len(smart) == 1
    assert smart[0].url.startswith("https://jobs.smartrecruiters.com/ServiceNow/744000163299524")
    assert "ServiceNow" in smart[0].text  # The description came from the detail call.


def test_blocked_feed_is_skipped_with_a_note():
    with mock.patch.object(jobs.requests, "get", return_value=FakeResponse("x", status=403)):
        pages, summary, note = read_jobs(["https://jobs.lever.co/acme"])
    assert pages == [] and summary == {} and "403" in note


def test_at_most_25_postings_kept():
    many = [jobs.Posting(f"Engineer {n}", "", f"https://jobs.lever.co/acme/{n}", None, "") for n in range(40)]
    assert len(jobs.pick_postings(many)) == jobs.MAX_JOB_POSTINGS == 25


class FakeCache:
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


def test_board_is_cached_for_a_day():
    cache = FakeCache()
    with mock.patch.object(jobs.requests, "get", side_effect=serve({"greenhouse": "greenhouse.json"})):
        first, summary, _ = read_jobs(["https://boards.greenhouse.io/mongodb"], cache=cache)
    with mock.patch.object(jobs.requests, "get") as get:
        again, cached_summary, _ = read_jobs(["https://boards.greenhouse.io/mongodb"], cache=cache)
    get.assert_not_called()
    assert [p.url for p in again] == [p.url for p in first] and all(p.cached for p in again)
    assert cached_summary["technical_open"] == summary["technical_open"]
    assert isinstance(JobBoard("Greenhouse", "x").feed_url, str) and isinstance(Page("u", "t", "x"), Page)


def test_smartrecruiters_reads_every_list_page():
    import json
    page = lambda n, start: json.dumps({"totalFound": 250, "content": [
        {"id": str(start + i), "name": f"Data Engineer {start + i}"} for i in range(n)]})
    def get(url, **kwargs):
        offset = int(url.split("offset=")[1]) if "offset=" in url else 0
        if "/postings/" in url:
            return FakeResponse(url, text="{}")
        return FakeResponse(url, text=page(100 if offset < 200 else 50, offset))
    with mock.patch.object(jobs.requests, "get", side_effect=get):
        pages, summary, _ = read_jobs(["https://careers.smartrecruiters.com/Acme"])
    assert summary["total_open"] == summary["technical_open"] == 250 and len(pages) == 25


def test_board_embedded_on_a_thin_careers_page_is_kept():
    from brief import website
    long_text = "We build software for logistics companies across Europe. " * 10

    def site(url, **kwargs):
        pages = {
            "https://acme.com/robots.txt": "",
            "https://acme.com": f'<a href="/careers">Careers</a><p>{long_text}</p>',
            "https://acme.com/careers": '<h1>Join us</h1><script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>',
        }
        html = pages.get(url.rstrip("/"))
        response = FakeResponse(url, status=200 if html is not None else 404, text=html or "")
        response.headers = {"Content-Type": "text/html"}
        return response

    with mock.patch.object(website.requests, "get", side_effect=site):
        pages = website.read_website("acme.com")
        thin = []
        website.read_extra_pages(["https://acme.com/careers"], links_out=thin)
    assert [p.url for p in pages] == ["https://acme.com"]
    assert find_job_board(pages[0].found_links)[0].slug == "acme"
    assert find_job_board(thin)[0].slug == "acme"
