"""Build the MongoDB customer story library in adr_brief.stories.

For each URL in data/story_urls.txt this script:
  1. downloads the public story page and keeps only its visible text,
  2. asks Gemini to pull out structured facts (company, industry, challenge,
     solution, outcomes). Every outcome must come with an exact quote, and we
     drop any outcome whose quote is not really on the page,
  3. creates an embedding of the story with gemini-embedding-001,
  4. saves (upserts) one document per story, keyed by source_url.

A copy of the stories (without embeddings) is written to data/stories.json so
they can be reviewed in GitHub.

Run from the project folder:  python scripts/build_story_library.py
Needs MONGODB_URI and GEMINI_API_KEY in the environment or in .env.
Safe to re-run: stories already in Atlas are skipped, so adding a URL to
data/story_urls.txt and re-running only processes the new ones.
Add --refresh to re-read and update every story.
"""

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from pydantic import BaseModel
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brief.industries import INDUSTRY_GROUPS, IndustryGroup  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
URLS_FILE = ROOT / "data" / "story_urls.txt"
OUTPUT_FILE = ROOT / "data" / "stories.json"

DB_NAME = "adr_brief"
COLLECTION_NAME = "stories"
SCHEMA_VERSION = 1

TEXT_MODEL = "gemini-3.5-flash-lite"
EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSIONS = 768  # Must match the Vector Search index (next step).

MAX_PAGE_CHARS = 20_000  # Story pages are short; this keeps prompts small.
PAUSE_SECONDS = 4  # Stay under the Gemini free-tier requests-per-minute limit.


# ---- What we ask Gemini to return (structured output) ----------------------

class Outcome(BaseModel):
    claim: str  # Short plain-English result, e.g. "Cut query times by 80%".
    quote: str  # Exact sentence copied from the page that supports the claim.


class StoryFacts(BaseModel):
    company: str
    hq_country: str
    industry: str
    industry_group: IndustryGroup
    mongodb_products: list[str]
    use_case: str
    challenge: str
    solution: str
    outcomes: list[Outcome]
    summary: str


EXTRACTION_PROMPT = """You are reading a public MongoDB customer story.
Extract the facts below using ONLY the page text. Do not invent anything.
- company: the customer's name.
- hq_country: the country where the customer is headquartered (as stated or widely known).
- industry: one short label, e.g. "Retail", "Financial Services", "Energy".
- industry_group: the closest group from the allowed list.
- mongodb_products: MongoDB products named on the page, e.g. "MongoDB Atlas", "Atlas Vector Search".
- use_case: one short phrase, e.g. "IoT data platform", "Customer 360".
- challenge: 1-2 sentences on the problem before MongoDB.
- solution: 1-2 sentences on what they built with MongoDB.
- outcomes: up to 4 results. For each, a short claim and a quote that is copied
  WORD FOR WORD from the page text (one sentence, no edits, no ellipses).
- summary: 2-3 sentences a salesperson could reuse.

PAGE TEXT:
"""


# ---- Helpers ----------------------------------------------------------------

def read_urls() -> list[str]:
    lines = URLS_FILE.read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith("#")]


def fetch_page_text(url: str) -> str:
    """Download a public page and return its visible text on one line."""
    response = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (account-brief-builder)"}, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "nav", "header", "footer", "noscript", "svg"]):
        tag.decompose()
    return " ".join(soup.get_text(" ").split())


def normalise(text: str) -> str:
    """Make quote matching ignore curly quotes, dashes, case and extra spaces."""
    replacements = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "\u00a0": " "}
    for old, new in replacements.items():
        text = text.replace(old, new)
    return re.sub(r"\s+", " ", text).strip().lower()


def call_with_retry(func, *args, **kwargs):
    """Retry Gemini calls that hit the free-tier rate limit (HTTP 429) or a 5xx."""
    for attempt in range(5):
        try:
            return func(*args, **kwargs)
        except errors.APIError as exc:
            if exc.code in (429, 500, 503) and attempt < 4:
                wait = 15 * (attempt + 1)
                print(f"    Gemini busy ({exc.code}); waiting {wait}s and retrying...")
                time.sleep(wait)
            else:
                raise


def extract_facts(client: genai.Client, page_text: str) -> StoryFacts:
    response = call_with_retry(
        client.models.generate_content,
        model=TEXT_MODEL,
        contents=EXTRACTION_PROMPT + page_text[:MAX_PAGE_CHARS],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=StoryFacts,
            temperature=0,
        ),
    )
    return response.parsed


def embed_story(client: genai.Client, story: dict) -> list[float]:
    """One embedding per story, built from the fields that describe the situation."""
    text = (
        f"{story['company']} ({story['industry']}, {story['hq_country']}). "
        f"Use case: {story['use_case']}. Challenge: {story['challenge']} "
        f"Solution: {story['solution']} Summary: {story['summary']}"
    )
    response = call_with_retry(
        client.models.embed_content,
        model=EMBEDDING_MODEL,
        contents=[text],
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_DOCUMENT",
            output_dimensionality=EMBEDDING_DIMENSIONS,
        ),
    )
    return list(response.embeddings[0].values)


# MongoDB-side validation: rejects malformed story documents (see
# docs/mongodb-skills/mongodb-schema-design/references/fundamental-schema-validation.md).
STORY_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["source_url", "company", "industry", "summary", "outcomes", "embedding", "schema_version"],
        "properties": {
            "source_url": {"bsonType": "string"},
            "company": {"bsonType": "string", "minLength": 1},
            "industry": {"bsonType": "string"},
            "industry_group": {"enum": INDUSTRY_GROUPS},
            "summary": {"bsonType": "string", "minLength": 1},
            "outcomes": {
                "bsonType": "array",
                "maxItems": 4,
                "items": {"bsonType": "object", "required": ["claim", "quote"]},
            },
            "embedding": {
                "bsonType": "array",
                "minItems": EMBEDDING_DIMENSIONS,
                "maxItems": EMBEDDING_DIMENSIONS,
                "items": {"bsonType": "double"},
            },
            "schema_version": {"bsonType": "int"},
        },
    }
}


def get_stories_collection(mongo: MongoClient):
    db = mongo[DB_NAME]
    if COLLECTION_NAME in db.list_collection_names():
        db.command("collMod", COLLECTION_NAME, validator=STORY_VALIDATOR)
    else:
        db.create_collection(COLLECTION_NAME, validator=STORY_VALIDATOR)
    return db[COLLECTION_NAME]


# ---- Main -------------------------------------------------------------------

def main() -> int:
    load_dotenv()
    mongo_uri = os.getenv("MONGODB_URI")
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not mongo_uri or not gemini_key:
        print("ERROR: MONGODB_URI and GEMINI_API_KEY must be set (see .env.example).")
        return 1

    gemini = genai.Client(api_key=gemini_key)
    mongo = MongoClient(mongo_uri, serverSelectionTimeoutMS=10_000, appname="account-brief-stories")
    collection = get_stories_collection(mongo)

    urls = read_urls()
    refresh = "--refresh" in sys.argv
    already_saved = set() if refresh else set(collection.distinct("source_url"))
    saved, failed, reviewed = 0, [], []
    for number, url in enumerate(urls, start=1):
        print(f"[{number}/{len(urls)}] {url}")
        if url in already_saved:
            print("    already saved; skipping (use --refresh to update)")
            continue
        try:
            page_text = fetch_page_text(url)
            facts = extract_facts(gemini, page_text)
            time.sleep(PAUSE_SECONDS)

            # Keep only outcomes whose quote really appears on the page.
            page_norm = normalise(page_text)
            outcomes = [o.model_dump() for o in facts.outcomes if normalise(o.quote) in page_norm][:4]
            dropped = len(facts.outcomes) - len(outcomes)
            if not outcomes:
                # A story with no verified result is weak social proof; leave it out.
                raise ValueError("no outcome quote was found word for word on the page")

            story = facts.model_dump(mode="json")
            story.update(
                outcomes=outcomes,
                region="EMEA",
                source_url=url,
                fetched_at=datetime.now(timezone.utc),
                schema_version=SCHEMA_VERSION,
                embedding_model=EMBEDDING_MODEL,
            )
            story["embedding"] = embed_story(gemini, story)
            time.sleep(PAUSE_SECONDS)

            collection.update_one({"source_url": url}, {"$set": story}, upsert=True)
            saved += 1
            print(f"    saved: {story['company']} | {story['industry']} | {len(outcomes)} quoted outcomes"
                  + (f" ({dropped} dropped: quote not found on page)" if dropped else ""))

            review_copy = {k: v for k, v in story.items() if k != "embedding"}
            review_copy["fetched_at"] = review_copy["fetched_at"].isoformat()
            reviewed.append(review_copy)
        except Exception as exc:  # Keep going; report failures at the end.
            print(f"    FAILED: {type(exc).__name__}: {exc}")
            failed.append(url)

    # Unique index so the same story can never be stored twice.
    collection.create_index("source_url", unique=True)

    # Merge this run into the review copy, keeping the order of story_urls.txt.
    old = json.loads(OUTPUT_FILE.read_text()) if OUTPUT_FILE.exists() else []
    by_url = {s["source_url"]: s for s in old + reviewed}
    review = [by_url[u] for u in urls if u in by_url]
    OUTPUT_FILE.write_text(json.dumps(review, indent=2, ensure_ascii=False) + "\n")
    skipped = len(already_saved & set(urls))
    print(f"\nSaved {saved} new/updated stories ({skipped} already saved) to {DB_NAME}.{COLLECTION_NAME}. Review copy: data/stories.json")
    if failed:
        print("Failed URLs:\n  " + "\n  ".join(failed))
    mongo.close()
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
