"""Create the adr_brief.accounts collection: one document per company you research.

  - schema validation: domain, company, score (0-100) and status are required,
    and status must be one of the pipeline stages,
  - a unique index on domain, so each company is stored once (re-runs update it),
  - an index on score (highest first) for the ranked pipeline view,
  - a one-off fix for accounts saved before score_breakdown lines had a "type":
    the type is filled in from the line's reason, so the Pipeline shows the right quote.

Run once from the project folder:  python scripts/setup_accounts.py
Needs MONGODB_URI in the environment or in .env. Safe to re-run.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brief.build import ACCOUNTS_COLLECTION, STATUSES  # noqa: E402
from brief.signals import SIGNAL_LABELS  # noqa: E402
from brief.story_search import DB_NAME  # noqa: E402

VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["domain", "company", "score", "status", "signals", "updated_at"],
        "properties": {
            "domain": {"bsonType": "string", "minLength": 3},
            "company": {"bsonType": "string", "minLength": 1},
            "score": {"bsonType": "int", "minimum": 0, "maximum": 100},
            "status": {"enum": STATUSES},
            "signals": {"bsonType": "array"},
            "history": {"bsonType": "array"},
            "updated_at": {"bsonType": "date"},
        },
    }
}


def add_breakdown_types(accounts) -> int:
    """Give old score_breakdown lines a "type" (matched by reason). Returns how many
    accounts changed; 0 on a re-run."""
    changed = 0
    for doc in accounts.find({"score_breakdown": {"$elemMatch": {"type": {"$exists": False}}}}):
        lines = [dict(line) for line in doc["score_breakdown"]]
        for line in lines:
            if "type" not in line:
                line["type"] = next((t.value for t, label in SIGNAL_LABELS.items()
                                     if line["reason"] == f"{label} signal"), "emea_hq")
        # Only write if the breakdown is unchanged, so a rebuild saved meanwhile wins.
        result = accounts.update_one({"_id": doc["_id"], "score_breakdown": doc["score_breakdown"]},
                                     {"$set": {"score_breakdown": lines}})
        changed += result.modified_count
    return changed


def main() -> int:
    load_dotenv()
    db = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=10_000, appname="account-brief")[DB_NAME]
    if ACCOUNTS_COLLECTION in db.list_collection_names():
        db.command("collMod", ACCOUNTS_COLLECTION, validator=VALIDATOR)
    else:
        db.create_collection(ACCOUNTS_COLLECTION, validator=VALIDATOR)
    accounts = db[ACCOUNTS_COLLECTION]
    accounts.create_index("domain", unique=True)
    accounts.create_index([("score", -1), ("updated_at", -1)])
    fixed = add_breakdown_types(accounts)
    print(f"{DB_NAME}.{ACCOUNTS_COLLECTION} ready: validator, unique domain index, score index "
          f"({fixed} older accounts given breakdown types).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
