"""Create the Atlas Search (keyword) index on adr_brief.stories: story_text_index.

Hybrid story matching ($rankFusion, MongoDB 8.0+) combines this keyword index with
the vector index. Mapped fields:
  - company, industry, use_case, challenge, solution, summary, outcomes.claim as
    "string" (standard analyzer: words are lower-cased and split, so "Payments"
    matches "payments"),
  - industry_group as "token", an exact-match value used as a filter.
Atlas M0 allows 3 search/vector indexes per cluster; the script checks the count first.

Run from the project folder:  python scripts/create_search_index.py
Needs MONGODB_URI in the environment or in .env.
Safe to re-run: an existing index is updated if its definition changed.
"""

import os
import sys
import time

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.operations import SearchIndexModel

DB_NAME = "adr_brief"
COLLECTION_NAME = "stories"
INDEX_NAME = "story_text_index"
MAX_SEARCH_INDEXES = 3  # Atlas M0 limit (search + vector indexes together).
MIN_VERSION = (8, 0)    # $rankFusion needs MongoDB 8.0+.
TEXT_FIELDS = ["company", "industry", "use_case", "challenge", "solution", "summary"]

INDEX_DEFINITION = {
    "mappings": {
        "dynamic": False,
        "fields": {
            **{field: {"type": "string", "analyzer": "lucene.standard"} for field in TEXT_FIELDS},
            "outcomes": {"type": "document", "fields": {"claim": {"type": "string", "analyzer": "lucene.standard"}}},
            "industry_group": {"type": "token"},
        },
    }
}


def find_index(collection):
    """Return the existing index description, or None if it doesn't exist."""
    for index in collection.list_search_indexes(INDEX_NAME):
        return index
    return None


def main() -> int:
    load_dotenv()
    uri = os.getenv("MONGODB_URI")
    if not uri:
        print("ERROR: MONGODB_URI is not set (add it to .env).")
        return 1

    client = None
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=10_000, appname="account-brief")
        version = client[DB_NAME].command("buildInfo")["version"]
        if tuple(int(part) for part in version.split(".")[:2]) < MIN_VERSION:
            print(f"ERROR: the cluster runs MongoDB {version}; hybrid search ($rankFusion) needs 8.0+.")
            return 1
        print(f"MongoDB {version}: $rankFusion is available.")

        collection = client[DB_NAME][COLLECTION_NAME]
        existing = find_index(collection)
        count = len(list(collection.list_search_indexes()))
        print(f"Search/vector indexes on {COLLECTION_NAME} before: {count} of {MAX_SEARCH_INDEXES}.")
        if existing is None:
            if count >= MAX_SEARCH_INDEXES:
                print("ERROR: no room for another search index on M0. Drop one first.")
                return 1
            print(f"Creating search index '{INDEX_NAME}'...")
            collection.create_search_index(SearchIndexModel(definition=INDEX_DEFINITION, name=INDEX_NAME, type="search"))
        elif existing.get("latestDefinition") != INDEX_DEFINITION:
            print(f"Updating search index '{INDEX_NAME}' (definition changed)...")
            collection.update_search_index(INDEX_NAME, INDEX_DEFINITION)
            time.sleep(5)
        else:
            print(f"Index '{INDEX_NAME}' already exists.")

        # Atlas builds the index in the background; wait until it can be queried.
        for _ in range(60):
            index = find_index(collection)
            status = index.get("status") if index else "MISSING"
            if index and index.get("queryable") and status == "READY":
                count = len(list(collection.list_search_indexes()))
                print(f"Index status: {status}. Ready to query. Search/vector indexes now: {count}.")
                return 0
            print(f"    status: {status} (waiting 10s)")
            time.sleep(10)

        print("ERROR: the index was not ready after 10 minutes. Check Atlas > Search.")
        return 1
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
