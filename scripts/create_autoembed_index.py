"""Create the automated-embedding Vector Search index on adr_brief.stories.

story_autoembed_index: Atlas embeds each story's search_text with Voyage AI
(voyage-4) and keeps the vectors itself, so this app has no embedding code.
industry_group is a filter field, so a search can be limited to one industry.

Atlas M0 allows 3 search/vector indexes across the whole cluster (sample data included);
the script prints the count before and after
and refuses to go over. After switching over and checking matches, run it again with
--drop-old to delete the old Gemini-vector index (story_vector_index).

Run from the project folder:  python scripts/create_autoembed_index.py [--drop-old]
Needs MONGODB_URI in the environment or in .env. Run
scripts/migrate_search_text.py --keep-embeddings first so every story has search_text. Safe to re-run.
"""

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.operations import SearchIndexModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brief.story_search import COLLECTION_NAME, DB_NAME, EMBED_MODEL, EMBED_PATH, INDEX_NAME  # noqa: E402

OLD_INDEX_NAME = "story_vector_index"
MAX_SEARCH_INDEXES = 3  # Atlas M0 limit (search + vector indexes together).

INDEX_DEFINITION = {
    "fields": [
        {"type": "autoEmbed", "modality": "text", "path": EMBED_PATH, "model": EMBED_MODEL},
        {"type": "filter", "path": "industry_group"},
    ]
}


def find_index(collection, name: str):
    """Return the existing index description, or None if it doesn't exist."""
    for index in collection.list_search_indexes(name):
        return index
    return None


def index_names(client) -> list[str]:
    """Every search/vector index on the cluster: the M0 limit counts all databases."""
    names = []
    for db_name in client.list_database_names():
        if db_name in ("admin", "local", "config"):
            continue
        for coll_name in client[db_name].list_collection_names():
            names += [f"{db_name}.{coll_name}:{i['name']}"
                      for i in client[db_name][coll_name].list_search_indexes()]
    return sorted(names)


def wait_until_ready(collection) -> bool:
    """Atlas embeds every story in the background; wait until the index can be queried."""
    for _ in range(90):
        index = find_index(collection, INDEX_NAME)
        status = index.get("status") if index else "MISSING"
        if index and index.get("queryable") and status == "READY":
            print(f"Index '{INDEX_NAME}' status: {status}. Ready to query.")
            return True
        print(f"    status: {status} (waiting 10s)")
        time.sleep(10)
    print("ERROR: the index was not ready after 15 minutes. Check Atlas > Search & Vector Search.")
    return False


def main() -> int:
    load_dotenv()
    uri = os.getenv("MONGODB_URI")
    if not uri:
        print("ERROR: MONGODB_URI is not set (add it to .env).")
        return 1

    client = None
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=10_000, appname="account-brief")
        collection = client[DB_NAME][COLLECTION_NAME]
        names = index_names(client)
        print(f"Search/vector indexes before: {len(names)} of {MAX_SEARCH_INDEXES} {names}")

        missing = collection.count_documents({EMBED_PATH: {"$exists": False}})
        if missing:
            print(f"ERROR: {missing} stories have no {EMBED_PATH}. Run scripts/migrate_search_text.py --keep-embeddings first.")
            return 1

        existing = find_index(collection, INDEX_NAME)
        if existing is None:
            if len(names) >= MAX_SEARCH_INDEXES:
                print("ERROR: no room for another search index on M0. Drop one first.")
                return 1
            print(f"Creating '{INDEX_NAME}' ({EMBED_MODEL} on {EMBED_PATH}, filter industry_group)...")
            collection.create_search_index(
                SearchIndexModel(definition=INDEX_DEFINITION, name=INDEX_NAME, type="vectorSearch"))
        elif existing.get("latestDefinition") != INDEX_DEFINITION:
            print(f"Updating '{INDEX_NAME}' (definition changed)...")
            collection.update_search_index(INDEX_NAME, INDEX_DEFINITION)
            time.sleep(5)
        else:
            print(f"Index '{INDEX_NAME}' already exists.")
        if not wait_until_ready(collection):
            return 1

        if "--drop-old" in sys.argv:
            if find_index(collection, OLD_INDEX_NAME) is None:
                print(f"Old index '{OLD_INDEX_NAME}' is already gone.")
            else:
                print(f"Deleting old index '{OLD_INDEX_NAME}'...")
                collection.drop_search_index(OLD_INDEX_NAME)
                for _ in range(30):
                    if find_index(collection, OLD_INDEX_NAME) is None:
                        break
                    time.sleep(5)
        names = index_names(client)
        print(f"Search/vector indexes after: {len(names)} of {MAX_SEARCH_INDEXES} {names}")
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
