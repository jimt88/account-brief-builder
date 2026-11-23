"""The whole account-brief pipeline in one function, shared by the Streamlit app
and scripts/make_brief.py:

  public website (+ optional extra links) -> verified signals -> score
  -> closest MongoDB story -> brief + checks -> saved in adr_brief.accounts
"""

from dataclasses import dataclass, field
from typing import Callable

from google import genai

from brief.build import (ACCOUNTS_COLLECTION, SalesBrief, check_brief, match_story, save_account,
                         score_account, write_brief)
from brief.signals import CompanyProfile, extract_signals
from brief.story_search import COLLECTION_NAME
from brief.website import PAGE_CACHE_COLLECTION, read_extra_pages, read_website


class BriefError(Exception):
    """A problem we can explain to the user in one sentence (e.g. no readable pages)."""


@dataclass
class BriefResult:
    profile: CompanyProfile
    story: dict
    brief: SalesBrief
    warnings: list[str]
    pages_read: list[str]
    signals_dropped: int = 0
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
    try:
        pages = read_website(website, notes=notes, cache=cache)
    except ValueError as exc:  # Site down, blocked or robots.txt says no.
        pages, site_problem = [], str(exc)

    # Extra links the user pasted (e.g. a press release) help when the site is thin.
    extra_pages, skipped = read_extra_pages(extra_urls or [], cache=cache)
    seen = {p.url for p in pages}
    pages += [p for p in extra_pages if p.url not in seen]
    skipped = notes + [f"Skipped {reason}" for reason in skipped]
    for page in pages:
        progress(f"Read {page.url}" + (" (cached)" if page.cached else ""))
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
    if not profile.signals:
        raise BriefError(
            "No signals with a word-for-word quote were found on the public pages. Try the "
            "company's newsroom or investor site, or paste a press release in Extra links."
        )

    score, breakdown = score_account(profile)

    progress("3/5 Finding the closest MongoDB customer story ...")
    story = match_story(db[COLLECTION_NAME], gemini, profile, mode=match_mode)
    if story is None:
        raise BriefError("No customer story found. Has scripts/create_vector_index.py been run?")

    progress("4/5 Writing the brief ...")
    brief = write_brief(gemini, profile, story)
    warnings = check_brief(brief, profile, story)

    progress("5/5 Saving the account ...")
    pages_read = [p.url for p in pages]
    account = save_account(db[ACCOUNTS_COLLECTION], website, profile, score, breakdown,
                           story, brief, warnings, pages_read)
    return BriefResult(
        profile=profile,
        story=story,
        brief=brief,
        warnings=warnings,
        pages_read=pages_read,
        signals_dropped=dropped,
        links_skipped=skipped,
        score=score,
        score_breakdown=breakdown,
        account=account,
    )
