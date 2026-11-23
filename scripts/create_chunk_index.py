"""Create adr_brief.page_chunks and its automated-embedding Vector Search index.

page_chunks holds ~1,200-character pieces of every page read for a brief (brief/passages.py):
  - schema validation ($jsonSchema): domain, url, source_type, chunk_index, text, fetched_at,
  - a unique index on (domain, url, chunk_index),
  - a TTL index on fetched_at: MongoDB deletes chunks about a day after they were written,
  - chunk_autoembed_index: Atlas embeds `text` with Voyage AI (voyage-4, as for stories);
    domain and source_type are filter fields, so a search stays inside one company.
There is no lexical (Atlas Search) index on chunks: keyword ranking runs in Python, so
the cluster stays within the M0 limit of 3 search/vector indexes.

Run once from the project folder:  python scripts/create_chunk_index.py
Needs MONGODB_URI (admin user) in the environment or in .env. Safe to re-run.
"""

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.operations import SearchIndexModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from brief.passages import (CHUNK_INDEX_DEFINITION, CHUNK_INDEX_NAME, CHUNK_TTL_SECONDS,  # noqa: E402
                            CHUNK_VALIDATOR, CHUNKS_COLLECTION)
from brief.story_search import DB_NAME  # noqa: E402
from create_autoembed_index import MAX_SEARCH_INDEXES, find_index, index_names  # noqa: E402

TTL_INDEX = "fetched_at_ttl"
POLL_SECONDS, POLL_LIMIT_SECONDS = 3, 45


def setup_collection(db) -> None:
    if CHUNKS_COLLECTION in db.list_collection_names():
        db.command("collMod", CHUNKS_COLLECTION, validator=CHUNK_VALIDATOR)
    else:
        db.create_collection(CHUNKS_COLLECTION, validator=CHUNK_VALIDATOR)
    chunks = db[CHUNKS_COLLECTION]
    chunks.create_index([("domain", 1), ("url", 1), ("chunk_index", 1)], unique=True, name="domain_url_chunk")
    ttl = chunks.index_information().get(TTL_INDEX)
    if ttl is None:
        chunks.create_index("fetched_at", name=TTL_INDEX, expireAfterSeconds=CHUNK_TTL_SECONDS)
    elif ttl.get("expireAfterSeconds") != CHUNK_TTL_SECONDS:
        db.command("collMod", CHUNKS_COLLECTION, index={"name": TTL_INDEX, "expireAfterSeconds": CHUNK_TTL_SECONDS})
    print(f"{DB_NAME}.{CHUNKS_COLLECTION} ready: validator, unique (domain, url, chunk_index), "
          f"TTL index ({CHUNK_TTL_SECONDS // 3600} h).")


def wait_until_ready(chunks) -> bool:
    """Poll the index status every POLL_SECONDS for up to POLL_LIMIT_SECONDS."""
    for _ in range(POLL_LIMIT_SECONDS // POLL_SECONDS):
        index = find_index(chunks, CHUNK_INDEX_NAME)
        status = index.get("status") if index else "MISSING"
        if index and index.get("queryable") and status == "READY":
            print(f"Index '{CHUNK_INDEX_NAME}' status: {status}. Ready to query.")
            return True
        print(f"    status: {status} (waiting {POLL_SECONDS}s)")
        time.sleep(POLL_SECONDS)
    print(f"Index '{CHUNK_INDEX_NAME}' is still building. Briefs choose passages by keyword until it's ready.")
    return False


def main() -> int:
    load_dotenv()
    uri = os.getenv("MONGODB_URI")
    if not uri:
        print("ERROR: MONGODB_URI is not set (add it to .env).")
        return 1
    client = MongoClient(uri, serverSelectionTimeoutMS=10_000, appname="account-brief")
    try:
        db = client[DB_NAME]
        setup_collection(db)
        chunks = db[CHUNKS_COLLECTION]
        names = index_names(client)
        print(f"Search/vector indexes before: {len(names)} of {MAX_SEARCH_INDEXES} {names}")
        existing = find_index(chunks, CHUNK_INDEX_NAME)
        if existing is None:
            if len(names) >= MAX_SEARCH_INDEXES:
                print("ERROR: no room for another search index on M0 (3 max). Drop one first.")
                return 1
            print(f"Creating '{CHUNK_INDEX_NAME}' (voyage-4 on text, filters domain and source_type)...")
            chunks.create_search_index(
                SearchIndexModel(definition=CHUNK_INDEX_DEFINITION, name=CHUNK_INDEX_NAME, type="vectorSearch"))
        elif existing.get("latestDefinition") != CHUNK_INDEX_DEFINITION:
            print(f"Updating '{CHUNK_INDEX_NAME}' (definition changed)...")
            chunks.update_search_index(CHUNK_INDEX_NAME, CHUNK_INDEX_DEFINITION)
        else:
            print(f"Index '{CHUNK_INDEX_NAME}' already exists.")
        wait_until_ready(chunks)
        names = index_names(client)
        print(f"Search/vector indexes after: {len(names)} of {MAX_SEARCH_INDEXES} {names}")
        return 0
    except Exception as exc:  # noqa: BLE001 - one friendly line for a setup script.
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
