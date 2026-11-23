"""Read and update saved accounts (adr_brief.accounts) for the pipeline view."""

import csv
import io
from datetime import datetime, timezone

from brief.build import STATUSES

# Aggregation pipeline: ranked by score, newest first on ties, only the columns we show.
PIPELINE = [
    {"$sort": {"score": -1, "updated_at": -1}},
    {"$project": {
        "_id": 0,
        "domain": 1,
        "company": 1,
        "website": 1,
        "score": 1,
        "status": 1,
        "updated_at": 1,
        "description": "$profile.what_they_do",
        # The highest-scoring reason (the breakdown is stored highest points first).
        "top_signal": {"$ifNull": [{"$first": "$score_breakdown.reason"}, "-"]},
        # A quote for that same signal type; else the first quote; else "".
        "top_quote": {"$let": {
            "vars": {"matching": {"$filter": {
                "input": {"$ifNull": ["$signals", []]},
                "cond": {"$eq": ["$$this.type", {"$first": "$score_breakdown.type"}]},
            }}},
            "in": {"$ifNull": [{"$first": "$$matching.quote"}, {"$first": "$signals.quote"}, ""]},
        }},
        "story": "$matched_story.company",
    }},
]


def pipeline_rows(accounts) -> list[dict]:
    return list(accounts.aggregate(PIPELINE))


def set_status(accounts, domain: str, status: str) -> None:
    if status not in STATUSES:
        raise ValueError(f"Unknown status: {status}")
    accounts.update_one({"domain": domain}, {"$set": {"status": status, "status_updated_at": datetime.now(timezone.utc)}})


def safe_cell(value) -> str:
    """Stop spreadsheets running a cell as a formula (e.g. a name starting with "=")."""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def pipeline_csv(rows: list[dict]) -> str:
    """Salesforce-style lead export: Company, Website, Lead Status, Description."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["Company", "Website", "Lead Status", "Description"])
    for r in rows:
        description = f"Score {r['score']}/100. Top signal: {r['top_signal']}. {r.get('description') or ''}".strip()
        if r.get("story"):
            description += f" Similar MongoDB customer: {r['story']}."
        writer.writerow([safe_cell(v) for v in (r["company"], r.get("website") or r["domain"], r["status"], description)])
    return out.getvalue()
