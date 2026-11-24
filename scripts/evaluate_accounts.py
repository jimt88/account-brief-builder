"""Run the full flow on test companies and check every result (live: Atlas + Gemini).

For each company:
  - every signal quote is re-downloaded from its source page and must be there word for word,
  - the brief has no warnings (numbers, vendor claims, citations, lengths),
  - the score equals the breakdown total (capped at 100),
  - a customer story was matched (and whether it was in the same industry).

Run from the project folder:  python scripts/evaluate_accounts.py [--mode vector|hybrid] [--compare-v1] [company ...]
(--mode defaults to STORY_MATCH_MODE, i.e. hybrid.)
Pauses between companies to stay inside the Gemini free-tier rate limit.

--compare-v1 reads each saved account first (built by the v1 app: 4 pages, score v1),
rebuilds it with the current app, and prints a Markdown table for the README:
verified signals v1 -> v2, signals by source, score v1 -> v2 and the strongest new
signal. It pauses 60 s between companies (Gemini, Atlas embedding and GDELT limits).
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
from brief.build import ACCOUNTS_COLLECTION, domain_of  # noqa: E402
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
PAUSE_SECONDS = 21  # Gemini free tier, and Atlas query embeddings (about 3 a minute on M0).
COMPARE_PAUSE_SECONDS = 60  # A v2 brief makes up to 3 embedding queries and a GDELT call.
SOURCE_ORDER = ["job", "news", "github", "website"]  # Extra links count as website here.


def source_counts(signals: list) -> dict:
    counts = dict.fromkeys(SOURCE_ORDER, 0)
    for s in signals:
        counts[s.source_type if s.source_type in counts else "website"] += 1
    return counts


def strongest_new_signal(breakdown: list[dict]) -> str:
    """The highest-scoring line that came from a source v1 never read (jobs, news, GitHub)."""
    new = [line for line in breakdown if any(f"({name}," in line["reason"] for name in ("job ad", "news", "GitHub"))
           or line["type"] == "hiring_volume"]
    return f"{new[0]['reason']} (+{new[0]['points']})" if new else "None: nothing beyond the website"


def v1_baseline(before: dict | None) -> tuple:
    """(signals, score) of the saved v1 version: the account itself, or if it was already
    rebuilt with v2, its newest v1 history entry (history keeps the score, not the signals)."""
    if not before:
        return "-", "-"
    if before.get("score_version", 1) < 2:
        return len(before["signals"]), before["score"]
    old = [h for h in before.get("history") or [] if (h.get("score_version") or 1) < 2]
    return "-", old[-1]["score"] if old else "-"


def compare_row(company: str, before: dict | None, r) -> str:
    v1_signals, v1_score = v1_baseline(before)
    c = source_counts(r.profile.signals)
    return (f"| {company} | {v1_signals} → {len(r.profile.signals)} | "
            f"{c['job']}/{c['news']}/{c['github']}/{c['website']} | {v1_score} → {r.score} | "
            f"{strongest_new_signal(r.score_breakdown)} |")


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
    compare = "--compare-v1" in args
    args = [a for a in args if a != "--compare-v1"]
    only = [a.lower() for a in args]
    rows, table = [], []
    first = True
    for company, website in COMPANIES:
        if only and company.lower() not in only:
            continue
        if not first:
            time.sleep(COMPARE_PAUSE_SECONDS if compare else PAUSE_SECONDS)
        first = False
        print(f"\n=== {company} ({website}) ===", flush=True)
        before = db[ACCOUNTS_COLLECTION].find_one({"domain": domain_of(website)}) if compare else None
        try:
            r = build_brief(company, website, db, gemini, progress=lambda m: print("  " + m, flush=True),
                            match_mode=mode)
        except BriefError as exc:
            rows.append((company, "ERROR", str(exc)))
            if compare:
                v1_signals, v1_score = v1_baseline(before)
                table.append(f"| {company} | {v1_signals} → - | - | {v1_score} → - | Not rebuilt: {exc} |")
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
        if compare:
            table.append(compare_row(company, before, r))

    print("\nRESULTS")
    for company, verdict, detail in rows:
        print(f"{verdict:5} {company}: {detail}")
    if compare:
        print("\n| Company | Verified signals v1 → v2 | Signals by source (jobs/news/github/site) "
              "| Score v1 → v2 | Strongest new signal |\n|---|---|---|---|---|")
        print("\n".join(table))
    return 0 if all(v == "PASS" for _, v, _ in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
