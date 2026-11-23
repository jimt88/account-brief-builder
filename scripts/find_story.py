"""Find the MongoDB customer stories most similar to a short description.

How it works:
  1. turn the description into an embedding with gemini-embedding-001
     (same model and 768 dimensions as the stored stories),
  2. ask Atlas Vector Search ($vectorSearch) for the closest story embeddings,
  3. print each match with its similarity score (0 to 1, higher = closer).

Run from the project folder, for example:
  python scripts/find_story.py "German retailer with a slow product catalogue"
  python scripts/find_story.py --industry "Retail & Consumer" "slow product catalogue"
  python scripts/find_story.py --mode hybrid "parcel tracking in real time"
(--mode vector = Vector Search only, the default here; hybrid = $rankFusion with Atlas Search.)
Needs MONGODB_URI and GEMINI_API_KEY in the environment or in .env.

The search itself lives in brief/story_search.py so the app can reuse it.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from pymongo import MongoClient

# Let this script import the shared code in the brief/ folder.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brief.story_search import COLLECTION_NAME, DB_NAME, find_similar_stories, find_similar_stories_hybrid  # noqa: E402


def main() -> int:
    args = sys.argv[1:]
    industry, mode = None, "vector"
    while len(args) >= 2 and args[0] in ("--industry", "--mode"):
        if args[0] == "--industry":
            industry = args[1]
        else:
            mode = args[1]
        args = args[2:]
    if not args or mode not in ("vector", "hybrid"):
        print('Usage: python scripts/find_story.py [--mode vector|hybrid] [--industry "Retail & Consumer"] "short description"')
        return 1
    description = " ".join(args)

    load_dotenv()
    mongo_uri = os.getenv("MONGODB_URI")
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not mongo_uri or not gemini_key:
        print("ERROR: MONGODB_URI and GEMINI_API_KEY must be set (add them to .env).")
        return 1

    mongo = None
    try:
        mongo = MongoClient(mongo_uri, serverSelectionTimeoutMS=10_000, appname="account-brief")
        gemini = genai.Client(api_key=gemini_key)
        search = find_similar_stories_hybrid if mode == "hybrid" else find_similar_stories
        stories = search(mongo[DB_NAME][COLLECTION_NAME], gemini, description, k=3, industry=industry)

        if not stories:
            print("No matches. Has scripts/create_vector_index.py been run?")
            return 1

        print(f'Closest stories to: "{description}"\n')
        for rank, story in enumerate(stories, start=1):
            print(f"{rank}. {story['company']} ({story.get('industry_group', story['industry'])}, {story['hq_country']})  score {story['score']:.3f}")
            if mode == "hybrid":
                print(f"   fusion {story['fusion_score']:.4f}, ranks {story['ranks']}")
            print(f"   {story['summary']}")
            if story.get("outcomes"):
                print(f"   Result: {story['outcomes'][0]['claim']}")
            print(f"   {story['source_url']}\n")
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if mongo is not None:
            mongo.close()


if __name__ == "__main__":
    sys.exit(main())
