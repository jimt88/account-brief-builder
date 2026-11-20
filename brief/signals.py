"""Pull evidence-backed sales signals out of a company's public pages with Gemini.

Every signal must come with an exact quote and the URL of the page it came
from. After Gemini answers, we check each quote really appears on that page
(ignoring case, spacing and curly quotes). Signals that fail the check are
dropped, so nothing in the brief is made up.
"""

from enum import Enum

from google import genai
from google.genai import types
from pydantic import BaseModel

from brief.industries import IndustryGroup
from brief.gemini_utils import TEXT_MODEL, call_with_retry, normalise, parsed_or_raise
from brief.website import Page

MAX_SIGNALS = 6


class SignalType(str, Enum):
    AI_INITIATIVE = "ai_initiative"
    HIRING_ENGINEERS = "hiring_engineers"
    LEGACY_DATABASE = "legacy_database"
    MODERNISATION = "modernisation"
    SCALE_OR_REAL_TIME = "scale_or_real_time"
    NEW_MARKET_OR_PRODUCT = "new_market_or_product"
    OTHER = "other"


# Plain-English labels for the app and the brief.
SIGNAL_LABELS = {
    SignalType.AI_INITIATIVE: "AI initiative",
    SignalType.HIRING_ENGINEERS: "Hiring engineers",
    SignalType.LEGACY_DATABASE: "Legacy database",
    SignalType.MODERNISATION: "Modernisation",
    SignalType.SCALE_OR_REAL_TIME: "Scale or real time",
    SignalType.NEW_MARKET_OR_PRODUCT: "New market or product",
    SignalType.OTHER: "Other",
}


class Signal(BaseModel):
    type: SignalType
    quote: str  # Copied word for word from the page.
    source_url: str  # The page the quote is on.

    @property
    def label(self) -> str:
        return SIGNAL_LABELS[self.type]


class CompanyProfile(BaseModel):
    company: str
    what_they_do: str  # 1-2 sentences.
    industry: str
    industry_group: IndustryGroup  # Fixed list, used to filter the story search.
    hq_country: str  # Empty unless the pages state it.
    situation: str  # 2-3 sentences on their current priorities; used to find a similar story.
    signals: list[Signal]


PROMPT = """You are helping a MongoDB sales rep research {company}.
Below are public pages about the company (its website plus any extra links). Using ONLY this text:

1. company, what_they_do (1-2 sentences), industry (a short label) and
   industry_group (the closest group from the allowed list).
2. hq_country: ONLY if the pages state where the company is headquartered;
   otherwise an empty string. Do not guess.
3. Up to {max_signals} signals that suggest a reason to talk about a modern
   data platform. Prefer specific and recent over generic. For each:
   - type: ai_initiative, hiring_engineers, legacy_database, modernisation,
     scale_or_real_time, new_market_or_product or other,
   - quote: ONE sentence copied WORD FOR WORD from the page (keep the original
     language; no edits, no ellipses, no joining sentences),
   - source_url: the URL of the page the quote is on, exactly as written below.
   Do not invent signals. If the pages have few signals, return fewer.
4. situation: 2-3 English sentences on the company's current priorities and
   data/technology challenges, based only on the quotes.

{pages}"""


def story_query_text(profile: CompanyProfile) -> str:
    """Text used to find the closest customer story. Same shape as the text the
    stories were embedded from (company, industry, country, then the situation)."""
    country = f", {profile.hq_country}" if profile.hq_country else ""
    return (
        f"{profile.company} ({profile.industry}{country}). "
        f"{profile.what_they_do} Situation: {profile.situation}"
    )


def format_pages(pages: list[Page]) -> str:
    return "\n\n".join(f"=== PAGE: {p.url} ===\nTITLE: {p.title}\n{p.text}" for p in pages)


def verify_signals(signals: list[Signal], pages: list[Page]) -> list[Signal]:
    """Keep only signals whose quote is really on a page. If Gemini gave the
    wrong URL but the quote is on another page we read, fix the URL."""
    page_norms = {p.url: normalise(p.text) for p in pages}
    verified = []
    for s in signals:
        quote = normalise(s.quote)
        if not 15 <= len(quote) <= 400:  # Too short to be evidence, or not one sentence.
            continue
        if quote in page_norms.get(s.source_url, ""):
            verified.append(s)
            continue
        for url, text in page_norms.items():
            if quote in text:
                verified.append(s.model_copy(update={"source_url": url}))
                break
    return verified[:MAX_SIGNALS]


def extract_signals(gemini: genai.Client, company: str, pages: list[Page]) -> tuple[CompanyProfile, int]:
    """Return the company profile with verified signals, and how many were dropped."""
    response = call_with_retry(
        gemini.models.generate_content,
        model=TEXT_MODEL,
        contents=PROMPT.format(company=company, max_signals=MAX_SIGNALS, pages=format_pages(pages)),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=CompanyProfile,
            temperature=0,
        ),
    )
    profile: CompanyProfile = parsed_or_raise(response)
    verified = verify_signals(profile.signals, pages)
    dropped = len(profile.signals) - len(verified)
    return profile.model_copy(update={"signals": verified}), dropped
