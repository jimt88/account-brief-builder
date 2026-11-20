"""Make a full account brief for one company, end to end:
read its public website -> verified signals -> closest MongoDB story -> brief.

Run from the project folder, for example:
  python scripts/make_brief.py "Ocado Group" ocadogroup.com
Extra public links to read (up to 3) can follow the website:
  python scripts/make_brief.py "Zalando" zalando.com https://corporate.zalando.com/en/newsroom
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
from brief.pipeline import BriefError, build_brief  # noqa: E402
from brief.story_search import DB_NAME  # noqa: E402


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--json"]
    as_json = "--json" in sys.argv
    if len(args) < 2:
        print('Usage: python scripts/make_brief.py "Company name" website.com [extra links ...] [--json]')
        return 1
    company, website, extra_urls = args[0], args[1], args[2:]

    load_dotenv()
    mongo_uri = os.getenv("MONGODB_URI")
    gemini_key = os.getenv("GEMINI_API_KEY")
    if not mongo_uri or not gemini_key:
        print("ERROR: MONGODB_URI and GEMINI_API_KEY must be set (add them to .env).")
        return 1

    mongo = None
    try:
        gemini = genai.Client(api_key=gemini_key)
        mongo = MongoClient(mongo_uri, serverSelectionTimeoutMS=10_000, appname="account-brief")
        result = build_brief(
            company, website, mongo[DB_NAME], gemini,
            progress=lambda message: print(message, file=sys.stderr),
            extra_urls=extra_urls,
        )
        profile, story, brief, warnings = result.profile, result.story, result.brief, result.warnings

        if as_json:
            print(json.dumps({
                "profile": profile.model_dump(mode="json"),
                "signals_dropped": result.signals_dropped,
                "pages_read": result.pages_read,
                "notes": result.links_skipped,
                "score": result.score,
                "score_breakdown": result.score_breakdown,
                "story": story,
                "brief": brief.model_dump(),
                "warnings": warnings,
            }, indent=2, ensure_ascii=False, default=str))
            return 0

        print(f"\n=== ACCOUNT BRIEF: {profile.company} ({profile.industry}, "
              f"{profile.hq_country or 'HQ not stated'}) ===\n")
        print(f"SCORE {result.score}/100")
        for line in result.score_breakdown:
            print(f"  +{line['points']} {line['reason']}")
        print("\nWHY NOW")
        for bullet in brief.why_now:
            print(f"- {bullet}")
        print("\nWHO TO CONTACT (hypothesis)")
        for persona in brief.personas:
            print(f"- {persona.side}: {persona.role} - {persona.why}")
        print(f"\nVALUE HOOK\n{brief.value_hook}\n")
        print(f"30-SECOND COLD-CALL OPENER\n{brief.cold_call_opener}\n")
        print(f"EMAIL\nSubject: {brief.email.subject}\n\n{brief.email.body}\n")
        print("DISCOVERY QUESTIONS")
        for n, question in enumerate(brief.discovery_questions, start=1):
            print(f"{n}. {question}")
        print("\nEVIDENCE")
        for n, s in enumerate(profile.signals, start=1):
            used = " (used)" if n in brief.signals_used else ""
            print(f'{n}.{used} {s.label}\n   "{s.quote}"\n   {s.source_url}')
        print(f"\nMATCHED STORY: {story['company']} (score {story['score']:.3f}) {story['source_url']}")
        print(f"  Why: {brief.matched_story_why}")
        if warnings:
            print("\nCHECK BEFORE USING:")
            for w in warnings:
                print(f"- {w}")
        else:
            print("\nChecks passed: lengths OK, every number is in the evidence, no vendor claims.")
        print(f"\nSaved to adr_brief.accounts (domain {result.account['domain']}, status {result.account['status']}).")
        return 0
    except BriefError as exc:
        print(f"ERROR: {exc}")
        return 1
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if mongo is not None:
            mongo.close()


if __name__ == "__main__":
    sys.exit(main())
