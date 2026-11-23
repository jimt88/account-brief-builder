"""The whole account-brief pipeline in one function, shared by the Streamlit app
and scripts/make_brief.py:

  public website (+ optional extra links) -> verified signals -> score
  -> closest MongoDB story -> brief + checks -> saved in adr_brief.accounts
"""

import logging
from dataclasses import dataclass, field
from typing import Callable

from google import genai

from brief.build import (ACCOUNTS_COLLECTION, SalesBrief, check_brief, match_story, save_account,
                         score_account, write_brief)
from brief.signals import CompanyProfile, extract_signals
from brief.story_search import COLLECTION_NAME
from concurrent.futures import ThreadPoolExecutor

from brief.github import read_github
from brief.github import summary_line as github_line
from brief.jobs import read_jobs, summary_line
from brief.news import read_news
from brief.website import PAGE_CACHE_COLLECTION, read_extra_pages, read_website


class BriefError(Exception):
    """A problem we can explain to the user in one sentence (e.g. no readable pages)."""


log = logging.getLogger(__name__)


@dataclass
class BriefResult:
    profile: CompanyProfile
    story: dict
    brief: SalesBrief
    warnings: list[str]
    pages_read: list[str]
    signals_dropped: int = 0
    jobs: dict = field(default_factory=dict)  # jobs_summary: platform, board_url, total_open, ...
    news: dict = field(default_factory=dict)  # news_summary: articles_found, articles_read
    github: dict = field(default_factory=dict)  # github_summary: org, repos_checked, files_read
    links_skipped: list[str] = field(default_factory=list)
    score: int = 0
    score_breakdown: list[dict] = field(default_factory=list)
    account: dict = field(default_factory=dict)  # The saved adr_brief.accounts document.


def build_brief(
    company: str,
    website: str,
    db,
    gemini: genai.Client,
    progress: Callable[[str], None] = lambda message: None,
    extra_urls: list[str] | None = None,
    match_mode: str | None = None,
) -> BriefResult:
    """Run every step for one company and save it. `db` is the adr_brief database.
    `progress` is called with a short message per step. `match_mode` is "hybrid" or
    "vector" (default: STORY_MATCH_MODE in brief/build.py)."""
    progress(f"1/5 Reading {website} ...")
    site_problem, notes = None, []
    cache = db[PAGE_CACHE_COLLECTION]
    # GDELT can take 10-20 s, so the news search runs while the website is read.
    background = ThreadPoolExecutor(max_workers=1)
    news_future = background.submit(read_news, company, website, cache)
    try:
        pages = read_website(website, notes=notes, cache=cache)
    except ValueError as exc:  # Site down, blocked or robots.txt says no.
        pages, site_problem = [], str(exc)

    # Extra links the user pasted (e.g. a press release) help when the site is thin.
    thin_links = []
    extra_pages, skipped = read_extra_pages(extra_urls or [], cache=cache, links_out=thin_links)
    seen = {p.url for p in pages}
    pages += [p for p in extra_pages if p.url not in seen]
    skipped = notes + [f"Skipped {reason}" for reason in skipped]
    for page in pages:
        progress(f"Read {page.url}" + (" (cached)" if page.cached else ""))

    # Job ads, if the careers page (or a pasted link) points to a public job board.
    links = [link for page in pages for link in page.found_links] + thin_links + list(extra_urls or [])
    job_pages, jobs, job_note = read_jobs(links, cache=cache)
    if jobs:
        progress(summary_line(jobs) + (" (cached)" if job_pages and job_pages[0].cached else ""))
    if job_note:
        skipped.append(job_note)
    job_urls = {p.url for p in job_pages}  # A feed posting beats the same posting read as a web page.
    pages = [p for p in pages if p.url not in job_urls] + job_pages

    # Dependency files from the company's public GitHub org, only if its pages link to one.
    github_signals, github_pages, github, github_note = read_github(links, preferred=list(extra_urls or []))
    if github:
        progress(github_line(github, github_signals))
    if github_note:
        skipped.append(github_note)

    try:
        news_pages, news, news_note = news_future.result()
    except Exception as exc:  # noqa: BLE001 - news is a bonus: never lose the brief over it.
        log.warning("News failed: %s", exc)
        news_pages, news, news_note = [], {}, "Skipped news: something went wrong while reading it."
    finally:
        background.shutdown(wait=False)
    if news:
        progress(f"News: {news['articles_found']} recent articles, {news['articles_read']} readable")
    if news_note:
        skipped.append(news_note)
    seen = {p.url for p in pages}
    pages += [p for p in news_pages if p.url not in seen]
    for note in skipped:
        progress(note)

    if not pages:
        raise BriefError(
            (site_problem or "No readable public pages found; the site may need JavaScript.")
            + " Try the company's corporate or investor site, or paste a press release "
            "or careers page in the extra links box."
        )

    progress(f"2/5 Finding signals in {len(pages)} pages ...")
    profile, dropped = extract_signals(gemini, company, pages)
    profile.signals += github_signals  # Found in code, already checked word for word.
    if not profile.signals:
        raise BriefError(
            "No signals with a word-for-word quote were found on the public pages. Try the "
            "company's newsroom or investor site, or paste a press release in Extra links."
        )

    score, breakdown = score_account(profile)

    progress("3/5 Finding the closest MongoDB customer story ...")
    story = match_story(db[COLLECTION_NAME], profile, mode=match_mode)
    if story is None:
        raise BriefError("No customer story found. Has scripts/create_autoembed_index.py been run?")

    progress("4/5 Writing the brief ...")
    brief = write_brief(gemini, profile, story)
    warnings = check_brief(brief, profile, story)

    progress("5/5 Saving the account ...")
    pages_read = [p.url for p in pages + github_pages]
    account = save_account(db[ACCOUNTS_COLLECTION], website, profile, score, breakdown,
                           story, brief, warnings, pages_read, jobs=jobs, news=news,
                           github=github)
    return BriefResult(
        profile=profile,
        story=story,
        brief=brief,
        warnings=warnings,
        pages_read=pages_read,
        signals_dropped=dropped,
        jobs=jobs,
        news=news,
        github=github,
        links_skipped=skipped,
        score=score,
        score_breakdown=breakdown,
        account=account,
    )
