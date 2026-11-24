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
# A competitor_database quote must name its database: Gemini's `technology` or one of these.
OTHER_DATABASES = ["oracle", "sql server", "mssql", "postgres", "mysql", "mariadb", "dynamodb", "cosmos",
                   "cassandra", "db2", "sybase", "couchbase", "redis", "elasticsearch", "firestore", "firebase",
                   "aurora", "sqlite", "teradata", "spanner", "bigtable", "hbase", "neo4j", "scylla",
                   "cockroach", "supabase", "couchdb", "azure sql", "documentdb", "sap hana", "informix", "ingres"]

# Products Gemini sometimes tags as a competitor database that aren't one (unless the
# name also names one, e.g. "Cosmos DB for MongoDB").
NOT_COMPETITOR_DATABASES = ["mongo", "kafka", "rabbitmq", "spark", "flink", "kubernetes", "docker", "terraform"]


class SignalType(str, Enum):
    AI_INITIATIVE = "ai_initiative"
    HIRING_ENGINEERS = "hiring_engineers"
    LEGACY_DATABASE = "legacy_database"
    MODERNISATION = "modernisation"
    SCALE_OR_REAL_TIME = "scale_or_real_time"
    NEW_MARKET_OR_PRODUCT = "new_market_or_product"
    COMPETITOR_DATABASE = "competitor_database"
    MONGODB_IN_USE = "mongodb_in_use"
    OTHER = "other"


# Plain-English labels for the app and the brief.
SIGNAL_LABELS = {
    SignalType.AI_INITIATIVE: "AI initiative",
    SignalType.HIRING_ENGINEERS: "Hiring engineers",
    SignalType.LEGACY_DATABASE: "Legacy database",
    SignalType.MODERNISATION: "Modernisation",
    SignalType.SCALE_OR_REAL_TIME: "Scale or real time",
    SignalType.NEW_MARKET_OR_PRODUCT: "New market or product",
    SignalType.COMPETITOR_DATABASE: "Competitor database",
    SignalType.MONGODB_IN_USE: "Existing MongoDB usage: expansion",
    SignalType.OTHER: "Other",
}


class Signal(BaseModel):
    type: SignalType
    quote: str  # Copied word for word from the page.
    source_url: str  # The page the quote is on.
    technology: str = ""  # The named product, e.g. "Oracle" or "Kafka", if the quote names one.
    source_type: str = ""  # Set in code from the page: website, extra_link, job, news or github.
    published_at: str = ""  # Set in code: the page's date (YYYY-MM-DD), if known.

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
Below are public pages about the company: its website, any extra links, its
current job ads and recent news articles. Job ads are the strongest evidence of the
technology a company really uses today; prefer them for database and stack signals. NEWS pages are
recent events (launches, deals, results): quote the article text, not the headline,
unless the headline also appears in the page text. Using ONLY this text:

1. company, what_they_do (1-2 sentences), industry (a short label) and
   industry_group (the closest group from the allowed list).
2. hq_country: ONLY if the pages state where the company is headquartered;
   otherwise an empty string. Do not guess.
3. Up to {max_signals} signals that suggest a reason to talk about a modern
   data platform. Prefer specific and recent over generic. For each:
   - type: ai_initiative, hiring_engineers, legacy_database, modernisation,
     scale_or_real_time, new_market_or_product, competitor_database (they use
     another operational database such as Oracle, SQL Server, PostgreSQL, MySQL,
     DynamoDB, Cosmos DB or Cassandra), mongodb_in_use (they already use MongoDB)
     or other,
   - technology: the product the quote names (e.g. "Oracle", "Kafka"), or empty,
   - quote: ONE sentence copied WORD FOR WORD from the page (keep the original
     language; no edits, no ellipses, no joining sentences),
   - source_url: the URL of the page the quote is on, exactly as written below.
   Do not invent signals. If the pages have few signals, return fewer.
4. situation: 2-3 English sentences on the company's current priorities and
   data/technology challenges, based only on the quotes.

{pages}"""


def story_query_text(profile: CompanyProfile) -> str:
    """Text used to find the closest customer story: Atlas embeds it and compares it
    with each story's search_text (company, industry, then the situation)."""
    country = f", {profile.hq_country}" if profile.hq_country else ""
    return (
        f"{profile.company} ({profile.industry}{country}). "
        f"{profile.what_they_do} Situation: {profile.situation}"
    )


MAX_EVIDENCE_CHARS = 60_000  # Text sent to Gemini; each page gets a fair share.
MIN_CHARS_PER_PAGE = 1_500


def page_header(page: Page) -> str:
    date = f" ({page.published_at:%Y-%m-%d})" if page.published_at else ""
    return f"=== {page.source_type.upper()}{date}: {page.url} ===\nTITLE: {page.title}"


def format_pages(pages: list[Page]) -> str:
    """Pages for the prompt, labelled with their source and date. Long pages are cut so
    the total stays near MAX_EVIDENCE_CHARS; quotes are still checked on the full text."""
    share = max(MIN_CHARS_PER_PAGE, MAX_EVIDENCE_CHARS // max(len(pages), 1))
    return "\n\n".join(f"{page_header(p)}\n{p.text[:share]}" for p in pages)


def verify_signals(signals: list[Signal], pages: list[Page]) -> list[Signal]:
    """Keep only signals whose quote is really on a page. If Gemini gave the
    wrong URL but the quote is on another page we read, fix the URL."""
    page_norms = {p.url: normalise(p.text) for p in pages}
    pages_by_url = {p.url: p for p in pages}
    verified = []
    for s in signals:
        quote = normalise(s.quote)
        if not 15 <= len(quote) <= 400:  # Too short to be evidence, or not one sentence.
            continue
        if s.type == SignalType.MONGODB_IN_USE and "mongo" not in quote:
            continue  # "Already uses MongoDB" must be in the quote itself.
        if s.technology and normalise(s.technology) not in quote:
            s = s.model_copy(update={"technology": ""})  # Only keep a product the quote names.
        if s.type == SignalType.COMPETITOR_DATABASE and (
                (any(word in normalise(s.technology) for word in NOT_COMPETITOR_DATABASES)
                 and not any(db in normalise(s.technology) for db in OTHER_DATABASES))
                or not (s.technology or any(db in quote for db in OTHER_DATABASES))):
            continue  # The quote must name the other database, and it can't be MongoDB or Kafka.
        url = s.source_url if quote in page_norms.get(s.source_url, "") else next(
            (url for url, text in page_norms.items() if quote in text), None)
        if url is not None:
            page = pages_by_url[url]
            verified.append(s.model_copy(update={
                "source_url": url, "source_type": page.source_type,
                "published_at": page.published_at.date().isoformat() if page.published_at else ""}))
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
