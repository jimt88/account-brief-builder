"""Offline tests for the Pipeline dashboard ($facet). No Atlas needed."""

from brief.accounts import DASHBOARD_PIPELINE, dashboard, dashboard_numbers
from brief.build import STATUSES


def test_dashboard_is_one_facet_with_six_branches():
    assert len(DASHBOARD_PIPELINE) == 1
    facet = DASHBOARD_PIPELINE[0]["$facet"]
    assert set(facet) == {"by_status", "by_industry", "signal_types", "by_source", "technologies", "totals"}
    assert facet["signal_types"][0] == {"$unwind": "$signals"}
    assert facet["signal_types"][-1]["$sort"]["count"] == -1
    assert facet["by_industry"][-1]["$sort"]["avg_score"] == -1
    assert facet["by_industry"][1]["$project"]["avg_score"] == {"$round": ["$avg_score", 0]}
    totals = facet["totals"][0]["$group"]
    assert totals["score_50_plus"] == {"$sum": {"$cond": [{"$gte": ["$score", 50]}, 1, 0]}}


def test_every_status_is_shown_in_order_with_zeros():
    numbers = dashboard_numbers({"by_status": [{"_id": "Meeting set", "count": 2}, {"_id": "New", "count": 3}]})
    assert [r["status"] for r in numbers["by_status"]] == STATUSES
    counts = {r["status"]: r["count"] for r in numbers["by_status"]}
    assert counts["New"] == 3 and counts["Meeting set"] == 2
    assert sum(counts.values()) == 5


def test_signal_types_get_readable_names():
    numbers = dashboard_numbers({"signal_types": [{"_id": "ai_initiative", "count": 4}, {"_id": "other", "count": 1}]})
    assert numbers["signal_types"] == [{"signal": "AI initiative", "count": 4}, {"signal": "Other", "count": 1}]


def test_industry_rows_and_totals():
    numbers = dashboard_numbers({
        "by_industry": [{"_id": "Retail & Consumer", "count": 2, "avg_score": 30.0}],
        "totals": [{"_id": None, "accounts": 2, "avg_score": 29.6, "score_50_plus": 0}],
    })
    assert numbers["by_industry"] == [{"industry": "Retail & Consumer", "count": 2, "avg_score": 30}]
    assert numbers["totals"] == {"accounts": 2, "avg_score": 30, "score_50_plus": 0}


class EmptyAccounts:
    def aggregate(self, pipeline):
        assert pipeline is DASHBOARD_PIPELINE
        return iter([{"by_status": [], "by_industry": [], "signal_types": [], "totals": []}])


def test_empty_collection_gives_zeros():
    numbers = dashboard(EmptyAccounts())
    assert numbers["totals"] == {"accounts": 0, "avg_score": 0, "score_50_plus": 0}
    assert all(r["count"] == 0 for r in numbers["by_status"])
    assert numbers["by_industry"] == [] and numbers["signal_types"] == []


def test_source_and_technology_facets_are_readable():
    numbers = dashboard_numbers({"by_source": [{"_id": "job", "count": 3}, {"_id": None, "count": 2}],
                                 "technologies": [{"_id": "Oracle", "count": 2}]})
    assert numbers["by_source"] == [{"source": "Jobs", "count": 3}, {"source": "Website", "count": 2}]
    assert numbers["technologies"] == [{"technology": "Oracle", "count": 2}]
    tech = DASHBOARD_PIPELINE[0]["$facet"]["technologies"]
    assert {"$match": {"signals.technology": {"$nin": [None, ""]}}} in tech
