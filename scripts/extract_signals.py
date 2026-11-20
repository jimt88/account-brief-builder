"""Read a company's public website, print evidence-backed signals and the
closest MongoDB customer story.

Run from the project folder, for example:
  python scripts/extract_signals.py "Ocado Group" ocadogroup.com
Add --json to print the raw result instead.
Needs MONGODB_URI and GEMINI_API_KEY in the environment or in .env.
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from pymongo import MongoClient

# Let this script import the shared code in the brief/ folder.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from brief.build import match_story  # noqa: E402
from brief.signals import extract_signals  # noqa: E402
from brief.story_search import COLLECTION_NAME, DB_NAME  # noqa: E402
from brief.website import read_website  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--json"]
    as_json = "--json" in sys.argv
    if len(args) != 2:
        print('Usage: python scripts/extract_signals.py "Company name" website.com [--json]')
        return 1
    company, website = args

    load_dotenv()
    mongo_uri = os.getenv("MONGODB_URI")
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not mongo_uri or not gemini_key:
        print("ERROR: MONGODB_URI and GEMINI_API_KEY must be set (add them to .env).")
        return 1

    mongo = None
    try:
        print(f"Reading {website} ...", file=sys.stderr)
        pages = read_website(website)
        if not pages:
            print("ERROR: no readable public pages found (the site may need JavaScript).")
            return 1
        for p in pages:
            print(f"    read {p.url} ({len(p.text)} chars)", file=sys.stderr)

        gemini = genai.Client(api_key=gemini_key)
        profile, dropped = extract_signals(gemini, company, pages)

        mongo = MongoClient(mongo_uri, serverSelectionTimeoutMS=10_000, appname="account-brief")
        story = match_story(mongo[DB_NAME][COLLECTION_NAME], gemini, profile)

        if as_json:
            result = profile.model_dump(mode="json")
            result["pages_read"] = [p.url for p in pages]
            result["signals_dropped"] = dropped
            result["closest_story"] = story
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0

        print(f"\n{profile.company} ({profile.industry}, {profile.hq_country})")
        print(f"What they do: {profile.what_they_do}")
        print(f"Situation: {profile.situation}\n")
        print(f"Signals ({len(profile.signals)} verified, {dropped} dropped because the quote wasn't on the page):")
        for n, s in enumerate(profile.signals, start=1):
            print(f"{n}. [{s.label}]")
            print(f'   Quote: "{s.quote}"')
            print(f"   Source: {s.source_url}")
        if story:
            print(f"\nClosest MongoDB story: {story['company']} ({story['industry']}, score {story['score']:.3f})")
            print(f"   {story['summary']}")
            print(f"   {story['source_url']}")
        return 0
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if mongo is not None:
            mongo.close()


if __name__ == "__main__":
    sys.exit(main())
