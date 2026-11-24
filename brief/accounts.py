"""Read and update saved accounts (adr_brief.accounts) for the pipeline view."""

import csv
import io
from datetime import datetime, timezone

from brief.build import STATUSES
from brief.signals import SIGNAL_LABELS

# Readable names for Page.source_type (old signals without one came from the website).
SOURCE_GROUPS = {"job": "Jobs", "news": "News", "github": "GitHub", "website": "Website",
                 "extra_link": "Extra links"}

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


# One $facet aggregation computes every number on the Pipeline dashboard in one
# round trip. Each branch sees all accounts; that's fine at this size (one user's pipeline).
DASHBOARD_PIPELINE = [
    {"$facet": {
        "by_status": [
            {"$group": {"_id": "$status", "count": {"$sum": 1}}},
        ],
        "by_industry": [
            {"$group": {"_id": {"$ifNull": ["$profile.industry_group", "Other"]},
                        "count": {"$sum": 1}, "avg_score": {"$avg": "$score"}}},
            {"$project": {"count": 1, "avg_score": {"$round": ["$avg_score", 0]}}},
            {"$sort": {"avg_score": -1, "_id": 1}},
        ],
        "signal_types": [
            {"$unwind": "$signals"},
            {"$group": {"_id": "$signals.type", "count": {"$sum": 1}}},
            {"$sort": {"count": -1, "_id": 1}},
        ],
        "by_source": [
            {"$unwind": "$signals"},
            {"$group": {"_id": {"$ifNull": ["$signals.source_type", "website"]}, "count": {"$sum": 1}}},
            {"$sort": {"count": -1, "_id": 1}},
        ],
        "technologies": [
            {"$unwind": "$signals"},
            {"$match": {"signals.technology": {"$nin": [None, ""]}}},
            {"$group": {"_id": "$signals.technology", "count": {"$sum": 1}}},
            {"$sort": {"count": -1, "_id": 1}},
            {"$limit": 10},
        ],
        "top_accounts": [
            {"$sort": {"score": -1, "updated_at": -1}},
            {"$limit": 5},
            {"$project": {"_id": 0, "company": 1, "domain": 1, "score": 1,
                          "top_signal": {"$ifNull": [{"$first": "$score_breakdown.reason"}, "-"]}}},
        ],
        "totals": [
            {"$group": {"_id": None, "accounts": {"$sum": 1}, "avg_score": {"$avg": "$score"},
                        "score_50_plus": {"$sum": {"$cond": [{"$gte": ["$score", 50]}, 1, 0]}}}},
        ],
    }},
]


def dashboard_numbers(result: dict) -> dict:
    """Tidy one $facet result: every status in STATUSES order (0 if none), readable
    signal names, and zero totals for an empty collection."""
    status_counts = {r["_id"]: r["count"] for r in result.get("by_status", [])}
    labels = {t.value: label for t, label in SIGNAL_LABELS.items()}
    totals = (result.get("totals") or [{}])[0]
    return {
        "by_status": [{"status": s, "count": status_counts.get(s, 0)} for s in STATUSES],
        "by_industry": [{"industry": r["_id"], "count": r["count"], "avg_score": int(r["avg_score"] or 0)}
                        for r in result.get("by_industry", [])],
        "signal_types": [{"signal": labels.get(r["_id"], str(r["_id"])), "count": r["count"]}
                         for r in result.get("signal_types", [])],
        "by_source": [{"source": SOURCE_GROUPS.get(r["_id"] or "website", str(r["_id"])), "count": r["count"]}
                      for r in result.get("by_source", [])],
        "technologies": [{"technology": r["_id"], "count": r["count"]} for r in result.get("technologies", [])],
        "top_accounts": result.get("top_accounts", []),
        "totals": {"accounts": totals.get("accounts", 0),
                   "avg_score": round(totals.get("avg_score") or 0),
                   "score_50_plus": totals.get("score_50_plus", 0)},
    }


def dashboard(accounts) -> dict:
    """Pipeline dashboard numbers from a single $facet aggregation."""
    return dashboard_numbers(next(accounts.aggregate(DASHBOARD_PIPELINE), {}))


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
