"""Live check that saving accounts is safe when several saves run at once (needs Atlas).

Runs 6 saves of the same account in parallel on a temporary collection and checks
the result: one document, 5 history entries (every replaced brief exactly once),
status still "New", and text containing "$" stored as-is. Drops the collection after.

Run from the project folder:  python scripts/test_save_account.py
"""

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brief.build import Email, Persona, SalesBrief, save_account  # noqa: E402
from brief.industries import IndustryGroup  # noqa: E402
from brief.signals import CompanyProfile  # noqa: E402
from brief.story_search import DB_NAME  # noqa: E402

SAVES = 6


def main() -> int:
    load_dotenv()
    db = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=10_000, appname="account-brief")[DB_NAME]
    test = db["accounts_save_test"]
    test.drop()
    test.create_index("domain", unique=True)
    profile = CompanyProfile(company="Example", what_they_do="Payments.", industry="Fintech",
                             industry_group=IndustryGroup.FINANCIAL, hq_country="", situation="x", signals=[])
    story = {"company": "Acme", "source_url": "https://x", "summary": "s", "score": 0.8}

    def save(n):
        brief = SalesBrief(why_now=[], personas=[Persona(side="IT", role="CTO", why="w")],
                           value_hook=f"Brief {n} costs $5m", cold_call_opener="o",
                           email=Email(subject="s", body="b"), discovery_questions=[],
                           matched_story_why="w", signals_used=[])
        save_account(test, "example.com", profile, n, [], story, brief, [], [])

    with ThreadPoolExecutor(SAVES) as pool:
        list(pool.map(save, range(SAVES)))

    docs = list(test.find())
    doc = docs[0]
    briefs = [h["brief"]["value_hook"] for h in doc["history"]] + [doc["brief"]["value_hook"]]
    checks = {
        "one document": len(docs) == 1,
        f"{SAVES - 1} history entries": len(doc["history"]) == SAVES - 1,
        "every brief kept exactly once": sorted(briefs) == sorted(f"Brief {n} costs $5m" for n in range(SAVES)),
        'status still "New"': doc["status"] == "New",
    }
    test.drop()
    for name, ok in checks.items():
        print(f"{'PASS' if ok else 'FAIL'}: {name}")
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
