"""Check that we can reach the MongoDB Atlas cluster.

Steps: ping the cluster, insert a test document into adr_brief.healthcheck,
read it back, delete it, and print OK.

Run from the project folder:  python scripts/check_connection.py
Needs MONGODB_URI in the environment or in a local .env file.
"""

import os
import sys
import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

DB_NAME = "adr_brief"
COLLECTION_NAME = "healthcheck"


def main() -> int:
    # Load .env if present (local dev). Real env vars take priority.
    load_dotenv()
    uri = os.getenv("MONGODB_URI")
    if not uri:
        print("ERROR: MONGODB_URI is not set. Add it to .env (see .env.example).")
        return 1

    # Short-lived script: default pool settings are fine. A 10s server
    # selection timeout makes network/IP-allowlist problems fail fast.
    client = None
    collection = None
    inserted_id = None
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=10_000, appname="account-brief-healthcheck")

        # 1. Ping the cluster.
        client.admin.command("ping")
        print("Ping: OK")

        collection = client[DB_NAME][COLLECTION_NAME]

        # 2. Insert a test document with a unique marker.
        marker = str(uuid.uuid4())
        doc = {"marker": marker, "created_at": datetime.now(timezone.utc)}
        inserted_id = collection.insert_one(doc).inserted_id
        print(f"Insert: OK ({DB_NAME}.{COLLECTION_NAME})")

        # 3. Read it back and confirm it is the same document.
        found = collection.find_one({"_id": inserted_id})
        if not found or found.get("marker") != marker:
            print("ERROR: could not read back the test document.")
            return 1
        print("Read back: OK")

        # 4. Delete it so the collection stays clean.
        deleted = collection.delete_one({"_id": inserted_id}).deleted_count
        inserted_id = None
        if deleted != 1:
            print("ERROR: could not delete the test document.")
            return 1
        print("Delete: OK")

        print("OK")
        return 0
    except Exception as exc:  # Show a readable message instead of a long traceback.
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    finally:
        # If something failed after the insert, still remove the test document.
        if inserted_id is not None:
            try:
                collection.delete_one({"_id": inserted_id})
            except Exception:
                pass
        if client is not None:
            client.close()


if __name__ == "__main__":
    sys.exit(main())
