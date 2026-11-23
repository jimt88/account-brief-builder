"""Compare vector-only and hybrid story matches for the evaluation accounts (live).

Uses each account's saved profile and verified signals from adr_brief.accounts, so it
doesn't re-read websites or write briefs: only the story search runs, in both modes.
Run evaluate_accounts.py first so the accounts exist.

Run from the project folder:  python scripts/compare_story_modes.py
"""

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brief.build import match_story  # noqa: E402
from brief.signals import CompanyProfile  # noqa: E402
from brief.story_search import COLLECTION_NAME, DB_NAME  # noqa: E402

COMPANIES = ["Glanbia", "Bank of Ireland", "N26", "Allegro", "DSV"]
PAUSE_SECONDS = 21  # each search makes one Atlas query embedding: about 3 a minute on free M0


def main() -> int:
    load_dotenv()
    db = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=10_000)[DB_NAME]
    print("| Company | Vector-only story | Hybrid story | Hybrid ranks (meaning, keywords) |")
    print("|---|---|---|---|")
    for company in COMPANIES:
        account = db["accounts"].find_one({"company": company}, sort=[("updated_at", -1)])
        if account is None:
            print(f"| {company} | (not saved yet) | | |")
            continue
        profile = CompanyProfile(**account["profile"], signals=account["signals"])
        found = {}
        for mode in ("vector", "hybrid"):
            found[mode] = match_story(db[COLLECTION_NAME], profile, mode=mode)
            time.sleep(PAUSE_SECONDS)
        v, h = found["vector"], found["hybrid"]
        ranks = h.get("ranks") or {}
        print(f"| {company} | {v['company']} ({v['score']:.2f}) | {h['company']} ({h['score']:.2f}, "
              f"{h['match_mode']}) | {ranks.get('vector', '-')}, {ranks.get('text', '-')} |", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
