"""Give every saved story an industry_group (see brief/industries.py).

The groups were chosen by hand for the stories in data/story_urls.txt, so they
are easy to review here. New stories get their group from Gemini when
build_story_library.py runs. Updates both Atlas and data/stories.json.

Run from the project folder:  python scripts/tag_story_industries.py
Needs MONGODB_URI in the environment or in .env.
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from brief.industries import IndustryGroup as G  # noqa: E402
from brief.story_search import COLLECTION_NAME, DB_NAME  # noqa: E402

BASE = "https://www.mongodb.com/solutions/customer-case-studies/"
GROUPS = {
    G.FINANCIAL: ["lombard-odier", "credit-agricole", "mapfre", "price-forbes", "mypos",
                  "bpifrance", "hype", "edenred", "comercia-global-payments"],
    G.RETAIL: ["otto", "idealo", "decathlon", "vestiaire", "ica-gruppen", "loreal", "mountain-warehouse"],
    G.MANUFACTURING: ["vaillant", "electrolux", "michelin", "zf", "brembo-solutions", "thales"],
    G.ENERGY: ["repsol", "total-energies", "enpal"],
    G.TELECOMS: ["swisscom", "dt"],
    G.PUBLIC: ["poste-italiane", "department-for-work-and-pensions"],
    G.HEALTHCARE: ["novo-nordisk"],
    G.TECHNOLOGY: ["wattsense", "stackit", "teamsystem", "datev", "nexthink", "cognigy", "vainu", "elevenlabs"],
    G.TRANSPORT: ["tui", "sncfconnect", "amadeus", "swiss-post", "bollere-logistics", "fairtiq"],
    G.MEDIA: ["ft", "betclic", "bede", "futureplay-games"],
}


def main() -> int:
    load_dotenv()
    mongo = MongoClient(os.environ["MONGODB_URI"], serverSelectionTimeoutMS=10_000, appname="account-brief")
    stories = mongo[DB_NAME][COLLECTION_NAME]
    by_url = {BASE + slug: group.value for group, slugs in GROUPS.items() for slug in slugs}

    for url, group in by_url.items():
        stories.update_one({"source_url": url}, {"$set": {"industry_group": group}})
    untagged = stories.count_documents({"industry_group": {"$exists": False}})

    review_file = ROOT / "data" / "stories.json"
    review = json.loads(review_file.read_text())
    for story in review:
        story["industry_group"] = by_url.get(story["source_url"], story.get("industry_group"))
    review_file.write_text(json.dumps(review, indent=2, ensure_ascii=False) + "\n")

    print(f"Tagged {len(by_url)} stories. Stories still without a group: {untagged}")
    mongo.close()
    return 0 if untagged == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
