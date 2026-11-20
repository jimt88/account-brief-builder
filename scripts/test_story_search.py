"""Check Atlas Vector Search with 3 example queries (live: needs Atlas + Gemini).

For each query we check that:
  - k=2 returns exactly 2 stories, best first, each with company, url, summary, score,
  - with an industry filter, every result is from that industry group,
  - the expected company is among the results.
Pauses between queries to stay inside the Gemini free-tier rate limit.

Run from the project folder:  python scripts/test_story_search.py
"""

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brief.story_search import COLLECTION_NAME, DB_NAME, find_similar_stories  # noqa: E402

PAUSE_SECONDS = 4

# (query, industry filter or None, a company we expect in the top 2)
QUERIES = [
    ("German online retailer with a slow product catalogue and weak personalisation", None, "OTTO"),
    ("European parcel carrier tracking millions of parcels in real time",
     "Transport, Logistics & Travel", "Swiss Post"),
    ("Mobile network operator unifying customer data for a better app experience",
     "Telecommunications", "Deutsche Telekom"),
]


def main() -> int:
    load_dotenv()
    mongo = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=10_000, appname="account-brief")
    gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    stories = mongo[DB_NAME][COLLECTION_NAME]
    failures = 0

    for query, industry, expected in QUERIES:
        results = find_similar_stories(stories, gemini, query, k=2, industry=industry)
        problems = []
        if len(results) != 2:
            problems.append(f"expected 2 results, got {len(results)}")
        if any(not all(r.get(f) for f in ("company", "url", "summary", "score")) for r in results):
            problems.append("a result is missing company/url/summary/score")
        if [r["score"] for r in results] != sorted((r["score"] for r in results), reverse=True):
            problems.append("results are not sorted best first")
        if industry and any(r["industry_group"] != industry for r in results):
            problems.append(f"a result is outside the {industry} filter")
        if expected not in [r["company"] for r in results]:
            problems.append(f"{expected} not in the top 2")

        found = ", ".join(f"{r['company']} ({r['score']:.2f})" for r in results)
        print(f"{'PASS' if not problems else 'FAIL'}: {query!r} [{industry or 'any industry'}] -> {found}")
        for problem in problems:
            print(f"    - {problem}")
        failures += bool(problems)
        time.sleep(PAUSE_SECONDS)

    mongo.close()
    print(f"\n{len(QUERIES) - failures} of {len(QUERIES)} queries passed.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
