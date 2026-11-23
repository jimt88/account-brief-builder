"""Run the full flow on test companies and check every result (live: Atlas + Gemini).

For each company:
  - every signal quote is re-downloaded from its source page and must be there word for word,
  - the brief has no warnings (numbers, vendor claims, citations, lengths),
  - the score equals the breakdown total (capped at 100),
  - a customer story was matched (and whether it was in the same industry).

Run from the project folder:  python scripts/evaluate_accounts.py [--mode vector|hybrid] [company ...]
(--mode defaults to STORY_MATCH_MODE, i.e. hybrid.)
Pauses between companies to stay inside the Gemini free-tier rate limit.
"""

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brief.gemini_utils import normalise  # noqa: E402
from brief.pipeline import BriefError, build_brief  # noqa: E402
from brief.story_search import DB_NAME  # noqa: E402
from brief.website import fetch, page_text  # noqa: E402

# Real EMEA companies with readable websites that aren't on MongoDB's customer pages.
COMPANIES = [
    ("Glanbia", "glanbia.com"),                 # Ireland
    ("Bank of Ireland", "bankofireland.com"),   # Ireland
    ("N26", "n26.com"),                         # Germany
    ("Allegro", "allegro.eu"),                  # Poland
    ("DSV", "dsv.com"),                         # Denmark
]
PAUSE_SECONDS = 20


def quotes_on_pages(signals: list) -> tuple[int, list[str]]:
    """Re-download each source page and count quotes found word for word."""
    cache, missing = {}, []
    for s in signals:
        if s.source_url not in cache:
            result = fetch(s.source_url)
            cache[s.source_url] = normalise(page_text(result[1])) if result else ""
        if normalise(s.quote) not in cache[s.source_url]:
            missing.append(s.quote[:60])
    return len(signals) - len(missing), missing


def main() -> int:
    load_dotenv()
    db = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=10_000, appname="account-brief")[DB_NAME]
    gemini = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    args = sys.argv[1:]
    mode = None
    if "--mode" in args:
        i = args.index("--mode")
        mode, args = args[i + 1], args[:i] + args[i + 2:]
    only = [a.lower() for a in args]
    rows = []
    for company, website in COMPANIES:
        if only and company.lower() not in only:
            continue
        print(f"\n=== {company} ({website}) ===", flush=True)
        try:
            r = build_brief(company, website, db, gemini, progress=lambda m: print("  " + m, flush=True),
                            match_mode=mode)
        except BriefError as exc:
            rows.append((company, "ERROR", str(exc)))
            continue
        found, missing = quotes_on_pages(r.profile.signals)
        total = min(100, sum(line["points"] for line in r.score_breakdown))
        same_industry = bool(r.story.get("industry_filter"))
        ok = not missing and not r.warnings and total == r.score and r.story
        rows.append((company, "PASS" if ok else "FAIL",
                     f"signals {found}/{len(r.profile.signals)} quoted | score {r.score} "
                     f"({'+'.join(str(x['points']) for x in r.score_breakdown) or '0'}) | "
                     f"story {r.story['company']} [{r.story.get('match_mode')}] "
                     f"({'same industry' if same_industry else 'any industry'}, "
                     f"{r.story['score']:.2f}) | warnings {len(r.warnings)}"))
        for w in r.warnings:
            print(f"  WARNING: {w}")
        for q in missing:
            print(f"  QUOTE NOT FOUND: {q}")
        time.sleep(PAUSE_SECONDS)

    print("\nRESULTS")
    for company, verdict, detail in rows:
        print(f"{verdict:5} {company}: {detail}")
    return 0 if all(v == "PASS" for _, v, _ in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
