"""One-off migration of adr_brief.stories to automated embedding.

  1. sets search_text on every story (the field Atlas embeds with Voyage AI),
  2. updates the stories $jsonSchema validator: search_text required, the old Gemini
     "embedding" field no longer required,
  3. removes the old "embedding" field with $unset, unless --keep-embeddings is given
     (keep them until story_autoembed_index is verified, so you can roll back).

Run from the project folder:  python scripts/migrate_search_text.py [--keep-embeddings]
Needs MONGODB_URI in the environment or in .env. Safe to re-run.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brief.story_search import COLLECTION_NAME, DB_NAME, STORY_VALIDATOR, search_text  # noqa: E402


def main() -> int:
    load_dotenv()
    uri = os.getenv("MONGODB_URI")
    if not uri:
        print("ERROR: MONGODB_URI is not set (add it to .env).")
        return 1
    client = MongoClient(uri, serverSelectionTimeoutMS=10_000, appname="account-brief")
    try:
        db = client[DB_NAME]
        stories = db[COLLECTION_NAME]
        updates = [
            UpdateOne({"_id": s["_id"]}, {"$set": {"search_text": search_text(s)}})
            for s in stories.find({}, {"embedding": 0})
        ]
        result = stories.bulk_write(updates) if updates else None
        print(f"search_text set on {len(updates)} stories ({result.modified_count if result else 0} changed).")

        db.command("collMod", COLLECTION_NAME, validator=STORY_VALIDATOR)
        print("Validator updated: search_text required, embedding not required.")

        if "--keep-embeddings" in sys.argv:
            left = stories.count_documents({"embedding": {"$exists": True}})
            print(f"Kept the old embedding field ({left} stories still have it).")
        else:
            removed = stories.update_many({"embedding": {"$exists": True}}, {"$unset": {"embedding": "", "embedding_model": ""}})
            print(f"Removed the old embedding field from {removed.modified_count} stories.")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
