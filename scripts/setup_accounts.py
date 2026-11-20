"""Create the adr_brief.accounts collection: one document per company you research.

  - schema validation: domain, company, score (0-100) and status are required,
    and status must be one of the pipeline stages,
  - a unique index on domain, so each company is stored once (re-runs update it),
  - an index on score (highest first) for the ranked pipeline view.

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
    print(f"{DB_NAME}.{ACCOUNTS_COLLECTION} ready: validator, unique domain index, score index.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
