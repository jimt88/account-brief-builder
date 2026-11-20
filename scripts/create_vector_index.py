"""Create the Atlas Vector Search index on adr_brief.stories.

The index lets MongoDB find the stories whose embeddings are closest to a
query embedding. It must match how the embeddings were made:
  - field "embedding", 768 numbers (gemini-embedding-001 at 768 dimensions),
  - "cosine" similarity, because 768-dimension Gemini vectors are not
    normalised to length 1 (dotProduct would need that).
It also has a "filter" field, industry_group, so a search can be limited to one
industry before the vector comparison (pre-filtering is faster and stricter
than filtering afterwards).

Run from the project folder:  python scripts/create_vector_index.py
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
INDEX_NAME = "story_vector_index"
EMBEDDING_DIMENSIONS = 768  # Same value as in build_story_library.py.

INDEX_DEFINITION = {
    "fields": [
        {
            "type": "vector",
            "path": "embedding",
            "numDimensions": EMBEDDING_DIMENSIONS,
            "similarity": "cosine",
        },
        {"type": "filter", "path": "industry_group"},
    ]
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
        collection = client[DB_NAME][COLLECTION_NAME]

        if find_index(collection) is None:
            print(f"Creating vector index '{INDEX_NAME}' on {DB_NAME}.{COLLECTION_NAME}...")
            collection.create_search_index(
                SearchIndexModel(
                    definition=INDEX_DEFINITION,
                    name=INDEX_NAME,
                    type="vectorSearch",
                )
            )
        elif find_index(collection).get("latestDefinition") != INDEX_DEFINITION:
            print(f"Updating vector index '{INDEX_NAME}' (definition changed)...")
            collection.update_search_index(INDEX_NAME, INDEX_DEFINITION)
            time.sleep(5)
        else:
            print(f"Index '{INDEX_NAME}' already exists.")

        # Atlas builds the index in the background; wait until it can be queried.
        for _ in range(60):
            index = find_index(collection)
            status = index.get("status") if index else "MISSING"
            if index and index.get("queryable") and index.get("latestDefinition") == INDEX_DEFINITION and status == "READY":
                print(f"Index status: {status}. Ready to query.")
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
