"""Job postings from public hiring-platform feeds (Greenhouse, Lever, Ashby,
SmartRecruiters, Personio, Workable).

Job ads are the strongest buying signal: they name the stack a company really uses
("migrating from Oracle", "Kafka, PostgreSQL"), and they have dates.

  1. find the company's job board from links on the pages we already read (or a
     link the user pasted): the platform and the board name (slug),
  2. read the board's public JSON/XML feed (made for syndication; no login, no key),
  3. keep the MAX_JOB_POSTINGS most recent technical roles, as Pages with
     source_type="job", the public posting URL and the posting date.

Workday, SuccessFactors and Taleo have no simple public feed, so we only add a
note. Email addresses and phone numbers are removed before anything is stored, and
recruiter names are never kept as fields.
"""

import html
import json
import re
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

from brief.website import (MAX_CHARS_PER_PAGE, TIMEOUT_SECONDS, USER_AGENT, Page,
                           cached_page, store_page)

MAX_JOB_POSTINGS = 25
MAX_WORKERS = 4  # At most 4 downloads at once.
# A role is technical if its title or department contains one of these words.
TECH_ROLE_WORDS = [
    "engineer", "engineering", "developer", "data", "platform", "devops", "sre", "architect",
    "cloud", "database", "dba", "ml", "ai", "machine learning", "analytics", "cto",
    "head of engineering", "it",
]
# Board URL patterns: (platform, regex with the slug as group 1).
BOARD_PATTERNS = [
    ("Greenhouse", r"boards-api\.greenhouse\.io/v1/boards/([\w-]+)"),
    ("Greenhouse", r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/embed/job_board(?:/js)?\?for=([\w-]+)"),
    ("Greenhouse", r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/([\w-]+)"),
    ("Lever EU", r"jobs\.eu\.lever\.co/([\w.-]+)"),
    ("Lever", r"jobs\.lever\.co/([\w.-]+)"),
    ("Ashby", r"jobs\.ashbyhq\.com/([\w.%-]+)"),
    ("SmartRecruiters", r"(?:careers|jobs)\.smartrecruiters\.com/([\w-]+)"),
    ("Personio", r"([\w-]+)\.jobs\.personio\.(?:de|com)"),
    ("Workable", r"apply\.workable\.com/([\w-]+)"),
]
NOT_SLUGS = {"embed", "api", "j", "v1", "js", "www", "jobs", "careers", "oauth"}
# Platforms with no simple public feed: we only explain how to add a job ad by hand.
NO_FEED_PLATFORMS = {"Workday": "myworkdayjobs.com", "SuccessFactors": "successfactors.",
                     "Taleo": "taleo.net"}

EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
PHONE = re.compile(r"(?<![\w+])(?:\+|00)?\d[\d ().-]{7,}\d(?!\w)")


@dataclass
class JobBoard:
    platform: str
    slug: str

    @property
    def board_url(self) -> str:
        return {
            "Greenhouse": f"https://job-boards.greenhouse.io/{self.slug}",
            "Lever": f"https://jobs.lever.co/{self.slug}",
            "Lever EU": f"https://jobs.eu.lever.co/{self.slug}",
            "Ashby": f"https://jobs.ashbyhq.com/{self.slug}",
            "SmartRecruiters": f"https://careers.smartrecruiters.com/{self.slug}",
            "Personio": f"https://{self.slug}.jobs.personio.de",
            "Workable": f"https://apply.workable.com/{self.slug}",
        }[self.platform]

    @property
    def feed_url(self) -> str:
        return {
            "Greenhouse": f"https://boards-api.greenhouse.io/v1/boards/{self.slug}/jobs?content=true",
            "Lever": f"https://api.lever.co/v0/postings/{self.slug}?mode=json",
            "Lever EU": f"https://api.eu.lever.co/v0/postings/{self.slug}?mode=json",
            "Ashby": f"https://api.ashbyhq.com/posting-api/job-board/{self.slug}",
            "SmartRecruiters": f"https://api.smartrecruiters.com/v1/companies/{self.slug}/postings?limit=100",
            "Personio": f"https://{self.slug}.jobs.personio.de/xml?language=en",
            "Workable": f"https://apply.workable.com/api/v1/widget/accounts/{self.slug}?details=true",
        }[self.platform]


@dataclass
class Posting:
    title: str
    department: str
    url: str  # The public posting page: this is what gets cited.
    published_at: datetime | None
    description: str  # Plain text.
    detail_id: str = ""  # SmartRecruiters needs one more call for the description.


def find_job_board(links: list[str]) -> tuple[JobBoard | None, str | None]:
    """The first supported job board found in links, and a note if the company uses
    a platform with no public feed (Workday etc.). Never guesses from the name."""
    note = None
    for link in links:
        lower = link.lower()
        for platform, pattern in BOARD_PATTERNS:
            match = re.search(pattern, link, flags=re.IGNORECASE)
            if match and match.group(1).lower() not in NOT_SLUGS:
                return JobBoard(platform, match.group(1)), None
        for platform, marker in NO_FEED_PLATFORMS.items():
            if marker in lower and note is None:
                note = (f"Jobs are on {platform}, which has no public feed; paste a job ad link "
                        "in Extra links.")
    return None, note


def remove_personal_data(text: str) -> str:
    """Drop email addresses and phone numbers (9+ digits, so years and amounts stay)."""
    text = EMAIL.sub("[email removed]", text)
    return PHONE.sub(lambda m: "[phone removed]" if sum(c.isdigit() for c in m.group()) >= 9 else m.group(), text)


def html_to_text(markup: str) -> str:
    return " ".join(BeautifulSoup(markup or "", "html.parser").get_text(" ").split())


def is_technical(title: str, department: str = "") -> bool:
    haystack = f"{title} {department}"
    for word in TECH_ROLE_WORDS:
        if word in ("it", "ai", "ml"):  # Short words: whole word, any case except "it".
            flags = 0 if word == "it" else re.IGNORECASE
            if re.search(rf"\b{word.upper()}\b", haystack, flags):
                return True
        elif re.search(rf"\b{re.escape(word)}", haystack, re.IGNORECASE):
            return True
    return False


def parse_date(value) -> datetime | None:
    """ISO strings, dates (YYYY-MM-DD) or Unix milliseconds -> an aware datetime."""
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None


# ---- One parser per platform: feed body -> (postings, total open roles) --------

def parse_greenhouse(body: str) -> list[Posting]:
    return [Posting(j.get("title", ""), ", ".join(d.get("name", "") for d in j.get("departments") or []),
                    j.get("absolute_url", ""), parse_date(j.get("first_published") or j.get("updated_at")),
                    html_to_text(html.unescape(j.get("content") or "")))
            for j in json.loads(body).get("jobs", [])]


def parse_lever(body: str) -> list[Posting]:
    postings = []
    for j in json.loads(body):
        categories = j.get("categories") or {}
        lists = " ".join(f"{item.get('text', '')}: {html_to_text(item.get('content', ''))}"
                         for item in j.get("lists") or [])
        text = " ".join(filter(None, [j.get("descriptionPlain"), lists, j.get("additionalPlain")]))
        postings.append(Posting(j.get("text", ""), categories.get("team") or categories.get("department") or "",
                                j.get("hostedUrl", ""), parse_date(j.get("createdAt")), " ".join(text.split())))
    return postings


def parse_ashby(body: str) -> list[Posting]:
    return [Posting(j.get("title", ""), j.get("department") or j.get("team") or "", j.get("jobUrl", ""),
                    parse_date(j.get("publishedAt")),
                    j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")))
            for j in json.loads(body).get("jobs", []) if j.get("isListed", True)]


def parse_smartrecruiters(body: str, slug: str) -> list[Posting]:
    return [Posting(j.get("name", ""), (j.get("department") or {}).get("label") or (j.get("function") or {}).get("label", ""),
                    f"https://jobs.smartrecruiters.com/{slug}/{j['id']}", parse_date(j.get("releasedDate")), "",
                    detail_id=str(j["id"]))
            for j in json.loads(body).get("content", [])]


def parse_smartrecruiters_detail(body: str) -> tuple[str, str]:
    """(public posting URL, plain-text description) from one posting's detail call."""
    detail = json.loads(body)
    sections = (detail.get("jobAd") or {}).get("sections") or {}
    text = " ".join(html_to_text(s.get("text", "")) for s in sections.values() if isinstance(s, dict))
    return detail.get("postingUrl", ""), text


def parse_personio(body: str, slug: str) -> list[Posting]:
    postings = []
    for position in ET.fromstring(body.encode()).iter("position"):
        parts = [html_to_text(d.findtext("value", "")) for d in position.iter("jobDescription")]
        postings.append(Posting(position.findtext("name", ""), position.findtext("department", ""),
                                f"https://{slug}.jobs.personio.de/job/{position.findtext('id', '')}",
                                parse_date(position.findtext("createdAt")), " ".join(parts)))
    return postings


def parse_workable(body: str) -> list[Posting]:
    return [Posting(j.get("title", ""), j.get("department") or j.get("function") or "",
                    j.get("url") or j.get("shortlink", ""), parse_date(j.get("published_on") or j.get("created_at")),
                    html_to_text(j.get("description", "")))
            for j in json.loads(body).get("jobs", [])]


def parse_feed(board: JobBoard, body: str) -> list[Posting]:
    if board.platform == "Greenhouse":
        return parse_greenhouse(body)
    if board.platform.startswith("Lever"):
        return parse_lever(body)
    if board.platform == "Ashby":
        return parse_ashby(body)
    if board.platform == "SmartRecruiters":
        return parse_smartrecruiters(body, board.slug)
    if board.platform == "Personio":
        return parse_personio(body, board.slug)
    return parse_workable(body)


def pick_postings(postings: list[Posting]) -> list[Posting]:
    """The MAX_JOB_POSTINGS most recent technical roles (undated ones last)."""
    technical = [p for p in postings if p.url and is_technical(p.title, p.department)]
    oldest = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(technical, key=lambda p: p.published_at or oldest, reverse=True)[:MAX_JOB_POSTINGS]


def posting_page(board: JobBoard, posting: Posting) -> Page:
    text = remove_personal_data(" ".join(" ".join(filter(None, [
        f"{posting.title}.", f"Team: {posting.department}." if posting.department else "",
        posting.description])).split()))
    return Page(posting.url, posting.title, text[:MAX_CHARS_PER_PAGE], source_type="job",
                published_at=posting.published_at, source_label=f"{board.platform} job: {posting.title}")


def get(url: str) -> requests.Response | None:
    try:
        return requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT_SECONDS)
    except requests.RequestException:
        return None


def read_job_board(board: JobBoard, cache=None) -> tuple[list[Page], dict, str | None]:
    """(job pages, jobs_summary, note). The board and its postings are cached for a day."""
    hit = cached_page(cache, board.feed_url)
    if hit and hit[1] is not None and hit[0].jobs_summary:
        pages = [p for p in (cached_page(cache, url) for url in hit[1]) if p]
        if len(pages) == len(hit[1]):
            return [p[0] for p in pages], hit[0].jobs_summary, None

    response = get(board.feed_url)
    if response is None or response.status_code != 200:  # Includes 401, 403 and 429: stop.
        status = response.status_code if response is not None else "no answer"
        return [], {}, f"Couldn't read the {board.platform} job board ({status}); carried on without job ads."
    try:
        postings = parse_feed(board, response.text)
    except (ValueError, KeyError, ET.ParseError):
        return [], {}, f"The {board.platform} job feed wasn't in the expected format; carried on without job ads."

    picked = pick_postings(postings)
    if board.platform == "SmartRecruiters":  # The list has no descriptions: one detail call each.
        def detail(posting: Posting) -> Posting:
            reply = get(f"https://api.smartrecruiters.com/v1/companies/{board.slug}/postings/{posting.detail_id}")
            if reply is not None and reply.status_code == 200:
                url, posting.description = parse_smartrecruiters_detail(reply.text)
                posting.url = url or posting.url
            return posting
        with ThreadPoolExecutor(MAX_WORKERS) as pool:
            picked = list(pool.map(detail, picked))

    pages = [posting_page(board, p) for p in picked]
    summary = {"platform": board.platform, "board_url": board.board_url, "total_open": len(postings),
               "technical_open": sum(is_technical(p.title, p.department) for p in postings),
               "fetched_at": datetime.now(timezone.utc)}
    for page in pages:
        store_page(cache, page.url, page)
    store_page(cache, board.feed_url, Page(board.feed_url, f"{board.platform} board: {board.slug}", "",
                                           source_type="job_board", jobs_summary=summary),
               links=[p.url for p in pages])
    return pages, summary, None


def read_jobs(links: list[str], cache=None) -> tuple[list[Page], dict, str | None]:
    """Find the job board in links and read it. (pages, jobs_summary, note); the
    summary is {} when no supported board was found."""
    board, note = find_job_board(links)
    if board is None:
        return [], {}, note
    return read_job_board(board, cache)


def summary_line(summary: dict) -> str:
    """E.g. "Found Greenhouse board: 37 open roles, 12 technical"."""
    return (f"Found {summary['platform']} board: {summary['total_open']} open roles, "
            f"{summary['technical_open']} technical")


__all__ = ["JobBoard", "Posting", "find_job_board", "read_jobs", "summary_line"]
