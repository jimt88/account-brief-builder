"""Score the account, match a customer story, write the brief and save it.

Everything here works only from verified evidence: the company profile and the
signals whose quotes were found word for word on a public page.

  score_account()  transparent 0-100 score with a stored breakdown
  match_story()    closest MongoDB story (same industry first; hybrid $rankFusion of
                   Vector Search + Atlas Search, or Vector Search only)
  write_brief()    Gemini writes the brief from the evidence only
  check_brief()    warns about anything that breaks the rules
  save_account()   one document per company in adr_brief.accounts, with history
"""

import logging
import os
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

from google import genai
from google.genai import types
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from brief.gemini_utils import TEXT_MODEL, call_with_retry, normalise, parsed_or_raise
from brief.industries import IndustryGroup
from brief.signals import CompanyProfile, SignalType, story_query_text
from brief.story_search import find_similar_stories, find_similar_stories_hybrid

ACCOUNTS_COLLECTION = "accounts"
MAX_HISTORY = 5  # earlier versions kept per account; older ones are dropped
# "hybrid" ($rankFusion of meaning + keywords) or "vector" (meaning only).
STORY_MATCH_MODE = os.getenv("STORY_MATCH_MODE", "hybrid")
log = logging.getLogger(__name__)

# ---- 1. Score ---------------------------------------------------------------

# Points for each signal type (counted once per type, however many quotes).
SIGNAL_POINTS = {
    SignalType.AI_INITIATIVE: 25,
    SignalType.HIRING_ENGINEERS: 20,
    SignalType.LEGACY_DATABASE: 20,
    SignalType.MODERNISATION: 15,
    SignalType.SCALE_OR_REAL_TIME: 10,
}
EMEA_POINTS = 10
MAX_SCORE = 100

# Countries in Europe, the Middle East and Africa (names as Gemini usually writes them).
EMEA_COUNTRIES = {
    "albania", "algeria", "andorra", "angola", "armenia", "austria", "azerbaijan", "bahrain",
    "belarus", "belgium", "benin", "bosnia and herzegovina", "botswana", "bulgaria", "burkina faso",
    "cameroon", "croatia", "cyprus", "czech republic", "czechia", "denmark", "egypt", "england",
    "estonia", "ethiopia", "finland", "france", "georgia", "germany", "ghana", "gibraltar", "greece",
    "guernsey", "hungary", "iceland", "iran", "iraq", "ireland", "isle of man", "israel", "italy",
    "ivory coast", "jersey", "jordan", "kenya", "kosovo", "kuwait", "latvia", "lebanon", "libya",
    "liechtenstein", "lithuania", "luxembourg", "madagascar", "malta", "mauritius", "moldova",
    "monaco", "montenegro", "morocco", "mozambique", "namibia", "netherlands", "the netherlands",
    "nigeria", "north macedonia", "northern ireland", "norway", "oman", "poland", "portugal", "qatar",
    "romania", "rwanda", "san marino", "saudi arabia", "scotland", "senegal", "serbia", "slovakia",
    "slovenia", "south africa", "spain", "sweden", "switzerland", "tanzania", "tunisia", "turkey",
    "türkiye", "uganda", "ukraine", "united arab emirates", "uae", "united kingdom", "uk", "wales",
    "zambia", "zimbabwe",
    # Remaining African and Middle Eastern countries, plus common alternative names.
    "benin", "burundi", "cabo verde", "cape verde", "central african republic", "chad", "comoros",
    "congo", "republic of the congo", "democratic republic of the congo", "dr congo", "drc",
    "côte d'ivoire", "cote d'ivoire", "djibouti", "equatorial guinea", "eritrea", "eswatini",
    "swaziland", "gabon", "gambia", "the gambia", "guinea", "guinea-bissau", "lesotho", "liberia",
    "malawi", "mali", "mauritania", "niger", "palestine", "sao tome and principe",
    "são tomé and príncipe", "seychelles", "sierra leone", "somalia", "south sudan", "sudan",
    "syria", "togo", "western sahara", "yemen", "vatican city", "holy see", "faroe islands",
    "great britain", "britain", "czech", "republic of ireland", "the uk", "the united kingdom",
}


def score_account(profile: CompanyProfile) -> tuple[int, list[dict]]:
    """Return (score, breakdown). Each breakdown line says why points were given."""
    breakdown = []
    for signal_type, points in SIGNAL_POINTS.items():
        quotes = [s for s in profile.signals if s.type == signal_type]
        if quotes:
            breakdown.append({
                "type": signal_type.value,
                "reason": f"{quotes[0].label} signal",
                "points": points,
                "evidence": quotes[0].source_url,
            })
    if profile.hq_country.strip().lower() in EMEA_COUNTRIES:
        breakdown.append({"type": "emea_hq", "reason": f"HQ in EMEA ({profile.hq_country})", "points": EMEA_POINTS, "evidence": ""})
    return min(MAX_SCORE, sum(line["points"] for line in breakdown)), breakdown


# ---- 2. Match a story -------------------------------------------------------

def search_stories(collection, gemini: genai.Client, query: str, industry: str | None, mode: str) -> list[dict]:
    """The best story for query. Hybrid falls back to vector-only if it fails (for
    example story_text_index is missing), so the demo never breaks."""
    if mode == "hybrid":
        try:
            stories = find_similar_stories_hybrid(collection, gemini, query, k=1, industry=industry)
            for story in stories:
                story["match_mode"] = "hybrid"
            return stories
        except Exception as exc:  # noqa: BLE001 - any search failure: use vector-only instead.
            log.warning("Hybrid story search failed, using vector-only: %s", exc)
    stories = find_similar_stories(collection, gemini, query, k=1, industry=industry)
    for story in stories:
        story["match_mode"] = "vector"
    return stories


def match_story(collection, gemini: genai.Client, profile: CompanyProfile, mode: str | None = None) -> dict | None:
    """Closest story in the same industry group; any industry if there's none.
    mode is "hybrid" or "vector" (default STORY_MATCH_MODE)."""
    mode = mode or STORY_MATCH_MODE
    query = story_query_text(profile)
    stories = []
    if profile.industry_group != IndustryGroup.OTHER:
        stories = search_stories(collection, gemini, query, profile.industry_group.value, mode)
    if stories:
        stories[0]["industry_filter"] = profile.industry_group.value
    else:
        stories = search_stories(collection, gemini, query, None, mode)
        if stories:
            stories[0]["industry_filter"] = None
    return stories[0] if stories else None


# ---- 3. Write the brief -----------------------------------------------------

MAX_HOOK_WORDS = 25
MAX_OPENER_WORDS = 85  # About 30 seconds when spoken.
MAX_EMAIL_WORDS = 80
NUMBER = r"\d+(?:[.,]\d+)?"  # e.g. 70, 1.5, 99.95


class Persona(BaseModel):
    side: str  # "IT" or "Business".
    role: str  # Job title to look for, e.g. "Head of Data Platforms".
    why: str  # One line: why they might care (a hypothesis).


class Email(BaseModel):
    subject: str
    body: str


class SalesBrief(BaseModel):
    why_now: list[str]  # Exactly 2 bullets, each citing a signal like "[1]".
    personas: list[Persona]  # One IT, one Business. Hypotheses, not facts.
    value_hook: str
    cold_call_opener: str
    email: Email
    discovery_questions: list[str]  # Exactly 2.
    matched_story_why: str  # One line on why this customer story fits.
    signals_used: list[int]  # Signal numbers (1-based) the brief relies on.


PROMPT = """You are an Account Development Representative at MongoDB writing a
short, specific brief for a first outreach to {company}.

Rules:
- Use ONLY the facts below. Do not invent numbers, names, products, projects or events.
- Never say {company} uses, or does not use, any vendor or technology (MongoDB,
  Oracle, AWS, etc.) unless one of the quotes says so.
- Refer to the customer story as an example of a similar company; do not claim
  {company} is a MongoDB customer.
- Plain, friendly British English. No hype, no buzzwords, no emojis. Write numbers
  as digits and copy them exactly as they appear in the facts.

Write:
- why_now: exactly 2 short bullets on why to reach out now, each citing the
  signal number it is based on, like "[2]".
- personas: exactly 2 people to approach, one with side "IT" and one with side
  "Business": a job title and one line on why they might care. These are guesses.
- value_hook: ONE plain-English sentence, under {max_hook} words, linking their
  strongest signal to what MongoDB Atlas helps with.
- cold_call_opener: the first 30 seconds of a cold call (max {max_opener} words).
  Introduce yourself as "[Your name] from MongoDB", give the reason for calling (a
  specific signal), a one-line proof point from the customer story, and end with a
  question asking for a short conversation.
- email: subject (max 8 words) and body (under {max_email} words). Open with the
  signal, add the customer-story proof point, ask for 20 minutes. Sign off "[Your name]".
- discovery_questions: exactly 2 open questions that test whether the signal is a
  real data/technology need (no yes/no questions).
- matched_story_why: one line on why the customer story below is relevant.
- signals_used: the numbers of the signals you relied on.

COMPANY: {company} ({industry}, {country})
WHAT THEY DO: {what_they_do}
SITUATION: {situation}

SIGNALS (verified quotes from public pages):
{signals}

SIMILAR MONGODB CUSTOMER STORY: {story_company} ({story_industry}, {story_country})
{story_summary}
Story outcomes (quoted from {story_url}):
{story_outcomes}
"""


def write_brief(gemini: genai.Client, profile: CompanyProfile, story: dict) -> SalesBrief:
    signals = "\n".join(
        f'{n}. [{s.label}] "{s.quote}" ({s.source_url})' for n, s in enumerate(profile.signals, start=1)
    ) or "(none found)"
    outcomes = "\n".join(f'- {o["claim"]}: "{o["quote"]}"' for o in story.get("outcomes", [])) or "(none)"
    prompt = PROMPT.format(
        company=profile.company, industry=profile.industry,
        country=profile.hq_country or "HQ not stated", what_they_do=profile.what_they_do,
        situation=profile.situation, signals=signals,
        story_company=story["company"], story_industry=story["industry"], story_country=story["hq_country"],
        story_summary=story["summary"], story_url=story["source_url"], story_outcomes=outcomes,
        max_hook=MAX_HOOK_WORDS, max_opener=MAX_OPENER_WORDS, max_email=MAX_EMAIL_WORDS,
    )
    response = call_with_retry(
        gemini.models.generate_content,
        model=TEXT_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=SalesBrief,
            temperature=0.4,  # A little variety in wording; the facts stay fixed.
        ),
    )
    return parsed_or_raise(response)


# ---- 4. Check the brief -----------------------------------------------------

# Vendor and technology names the brief must not attach to the company without a quote.
VENDORS = [
    "oracle", "sql server", "microsoft sql", "postgres", "postgresql", "mysql", "mariadb", "db2",
    "sap hana", "teradata", "snowflake", "databricks", "dynamodb", "cosmos db", "cassandra",
    "couchbase", "redis", "elasticsearch", "firebase", "aws", "amazon web services", "azure",
    "google cloud", "gcp", "salesforce", "mainframe",
]


def check_brief(brief: SalesBrief, profile: CompanyProfile, story: dict) -> list[str]:
    """Return warnings for anything that breaks the rules (empty list = all good)."""
    warnings = []

    def too_long(label, text, limit):
        words = len(text.split())
        if words > limit:
            warnings.append(f"{label} is {words} words (aim for {limit} or fewer).")

    too_long("Value hook", brief.value_hook, MAX_HOOK_WORDS)
    too_long("Cold-call opener", brief.cold_call_opener, MAX_OPENER_WORDS)
    too_long("Email body", brief.email.body, MAX_EMAIL_WORDS + 10)  # Small allowance for "Hi"/sign-off.
    if len(brief.discovery_questions) != 2:
        warnings.append(f"Expected 2 discovery questions, got {len(brief.discovery_questions)}.")
    if len(brief.why_now) != 2:
        warnings.append(f"Expected 2 why-now bullets, got {len(brief.why_now)}.")
    for bullet in brief.why_now:
        cited = [int(n) for n in re.findall(r"\[(\d+)\]", bullet)]
        if not cited or any(not 1 <= n <= len(profile.signals) for n in cited):
            warnings.append(f'Why-now bullet doesn\'t cite a real signal: "{bullet}"')
    if sorted(p.side for p in brief.personas) != ["Business", "IT"]:
        warnings.append("Expected one IT persona and one Business persona.")
    bad_refs = [n for n in brief.signals_used if not 1 <= n <= len(profile.signals)]
    if bad_refs:
        warnings.append(f"Brief refers to signal numbers that don't exist: {bad_refs}.")

    text = normalise(" ".join(
        brief.why_now + [brief.value_hook, brief.cold_call_opener, brief.email.subject, brief.email.body]
        + brief.discovery_questions
        + [part for persona in brief.personas for part in (persona.role, persona.why)]
        + [brief.matched_story_why]
    ))
    quotes = normalise(" ".join(s.quote for s in profile.signals))
    for vendor in VENDORS:
        if re.search(rf"\b{re.escape(vendor)}\b", text) and not re.search(rf"\b{re.escape(vendor)}\b", quotes):
            warnings.append(f'Mentions "{vendor}" but no quote from {profile.company} does; remove or check it.')

    # Every number in the brief must appear somewhere in the evidence we gave Gemini.
    evidence = normalise(" ".join(
        [profile.company, profile.industry, profile.hq_country, profile.what_they_do, profile.situation]
        + [s.quote for s in profile.signals]
        + [story["company"], story["industry"], story["hq_country"], story["summary"]]
        + [o["claim"] + " " + o["quote"] for o in story.get("outcomes", [])]
    ))
    evidence_numbers = set(re.findall(NUMBER, evidence)) | {"20", "30"}  # 20 min / 30 s come from our prompt.
    for number in sorted(set(re.findall(NUMBER, re.sub(r"\[\d+\]", "", text)))):
        if number not in evidence_numbers:
            warnings.append(f'The number "{number}" is not in the evidence; check it before using.')
    return warnings


# ---- 5. Save ----------------------------------------------------------------

STATUSES = ["New", "Researched", "Contacted", "Meeting set", "Not now"]


def domain_of(website: str) -> str:
    """'https://www.Example.com/about' -> 'example.com' (the account's key)."""
    website = website.strip().lower()
    host = urlparse(website if "://" in website else "https://" + website).netloc
    return host.removeprefix("www.")


def save_account(accounts, website: str, profile: CompanyProfile, score: int, breakdown: list[dict],
                 story: dict, brief: SalesBrief, warnings: list[str], pages_read: list[str]) -> dict:
    """Upsert one document per company, keyed by domain. On a re-run the previous
    brief and score are appended to history[] (only the last MAX_HISTORY are kept)
    and the status is kept.

    This is one atomic update (an aggregation-pipeline update): the history entry is
    built from the document being replaced, inside the same write, so two people
    rebuilding the same account at once can't lose or duplicate a brief."""
    domain = domain_of(website)
    now = datetime.now(timezone.utc)
    new_values = {
        "company": profile.company,
        "website": website,
        "profile": profile.model_dump(mode="json", exclude={"signals"}),
        "signals": [s.model_dump(mode="json") for s in profile.signals],
        "score": score,
        "score_breakdown": breakdown,
        "matched_story": {
            "company": story["company"], "url": story["source_url"], "summary": story["summary"],
            "score": story["score"], "industry_filter": story.get("industry_filter"),
            "match_mode": story.get("match_mode", "vector"), "ranks": story.get("ranks"),
            "why": brief.matched_story_why,
        },
        "brief": brief.model_dump(mode="json"),
        "warnings": warnings,
        "pages_read": pages_read,
        "updated_at": now,
    }
    previous_version = {"brief": "$brief", "score": "$score",
                        "score_breakdown": "$score_breakdown", "updated_at": "$updated_at"}
    update = [
        # Stage 1 reads the current document: archive its brief (if any), keep status/created_at.
        {"$set": {
            "domain": domain,
            "status": {"$ifNull": ["$status", "New"]},
            "created_at": {"$ifNull": ["$created_at", now]},
            # Append the old version, then keep only the newest MAX_HISTORY entries.
            "history": {"$slice": [
                {"$cond": [
                    {"$ifNull": ["$brief", False]},
                    {"$concatArrays": [{"$ifNull": ["$history", []]}, [previous_version]]},
                    {"$ifNull": ["$history", []]},
                ]},
                -MAX_HISTORY,
            ]},
        }},
        # Stage 2 writes the new brief. $literal stops text like "$5m" being read as a field.
        {"$set": {key: {"$literal": value} for key, value in new_values.items()}},
    ]
    try:
        accounts.update_one({"domain": domain}, update, upsert=True)
    except DuplicateKeyError:  # Two first-time saves raced; the other one inserted. Update it.
        accounts.update_one({"domain": domain}, update, upsert=True)
    return accounts.find_one({"domain": domain})
