"""Create the adr_brief.page_cache collection: public pages read in the last 24 hours.

  - schema validation: url and text are strings, fetched_at is a date,
  - a unique index on url, so each page is cached once,
  - a TTL index on fetched_at: MongoDB deletes a cached page about a day after it
    was downloaded (the TTL monitor runs about once a minute, so not to the second).

Run once from the project folder:  python scripts/setup_page_cache.py
Needs MONGODB_URI in the environment or in .env. Safe to re-run.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brief.story_search import DB_NAME  # noqa: E402
from brief.website import CACHE_SECONDS, PAGE_CACHE_COLLECTION  # noqa: E402

VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["url", "text", "fetched_at"],
        "properties": {
            "url": {"bsonType": "string"},
            "text": {"bsonType": "string"},
            "fetched_at": {"bsonType": "date"},
            "source_type": {"enum": ["website", "extra_link", "job", "job_board", "news"]},
            "published_at": {"bsonType": ["date", "null"]},
        },
    }
}
TTL_INDEX = "fetched_at_ttl"


def main() -> int:
    load_dotenv()
    db = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=10_000, appname="account-brief")[DB_NAME]
    if PAGE_CACHE_COLLECTION in db.list_collection_names():
        db.command("collMod", PAGE_CACHE_COLLECTION, validator=VALIDATOR)
    else:
        db.create_collection(PAGE_CACHE_COLLECTION, validator=VALIDATOR)
    cache = db[PAGE_CACHE_COLLECTION]
    cache.create_index("url", unique=True)
    ttl = cache.index_information().get(TTL_INDEX)
    if ttl is None:
        cache.create_index("fetched_at", name=TTL_INDEX, expireAfterSeconds=CACHE_SECONDS)
    elif ttl.get("expireAfterSeconds") != CACHE_SECONDS:  # Change the expiry in place.
        db.command("collMod", PAGE_CACHE_COLLECTION,
                   index={"name": TTL_INDEX, "expireAfterSeconds": CACHE_SECONDS})
    print(f"{DB_NAME}.{PAGE_CACHE_COLLECTION} ready: validator, unique url index, "
          f"TTL index ({CACHE_SECONDS // 3600} h).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
