"""Offline tests for the hybrid ($rankFusion) story search. No Atlas or Gemini."""

from unittest import mock

import pytest

from brief import build
from brief.industries import IndustryGroup
from brief.signals import CompanyProfile
from brief.story_search import INDEX_NAME, TEXT_INDEX_NAME, EmbeddingRateLimit, hybrid_pipeline, pipeline_ranks



def fusion(pipeline):
    return pipeline[0]["$rankFusion"]


def test_rank_fusion_combines_vector_and_text_with_equal_weights():
    stage = fusion(hybrid_pipeline("parcel tracking", k=2))
    pipelines = stage["input"]["pipelines"]
    assert set(pipelines) == {"vector", "text"}
    vector = pipelines["vector"][0]["$vectorSearch"]
    assert vector["index"] == INDEX_NAME and vector["path"] == "search_text"
    assert vector["query"] == {"text": "parcel tracking"} and "queryVector" not in vector
    assert pipelines["text"][0]["$search"]["index"] == TEXT_INDEX_NAME
    assert "$limit" in pipelines["text"][1]  # $search doesn't limit itself.
    assert stage["combination"]["weights"] == {"vector": 1.0, "text": 1.0}
    assert stage["scoreDetails"] is True


def test_industry_filter_is_applied_to_both_searches():
    pipelines = fusion(hybrid_pipeline("parcels", industry="Retail & Consumer"))["input"]["pipelines"]
    assert pipelines["vector"][0]["$vectorSearch"]["filter"] == {"industry_group": "Retail & Consumer"}
    assert pipelines["text"][0]["$search"]["compound"]["filter"] == [
        {"equals": {"path": "industry_group", "value": "Retail & Consumer"}}]


def test_no_filter_without_industry():
    pipelines = fusion(hybrid_pipeline("parcels"))["input"]["pipelines"]
    assert "filter" not in pipelines["vector"][0]["$vectorSearch"]
    assert "filter" not in pipelines["text"][0]["$search"]["compound"]


def test_projection_and_limit_come_after_the_fusion():
    pipeline = hybrid_pipeline("parcels", k=2)
    assert pipeline[1] == {"$limit": 2}
    project = pipeline[2]["$project"]
    assert project["fusion_score"] == {"$meta": "score"}
    assert project["score_details"] == {"$meta": "scoreDetails"}
    assert project["company"] == 1 and project["url"] == "$source_url"


def test_ranks_are_read_from_score_details():
    details = {"value": 0.03, "details": [
        {"inputPipelineName": "text", "rank": 2, "value": 6.1},
        {"inputPipelineName": "vector", "rank": 1, "value": 0.85}]}
    assert pipeline_ranks(details)["vector"]["value"] == 0.85
    assert pipeline_ranks(None) == {}


def profile():
    return CompanyProfile.model_construct(
        company="DSV", industry="Logistics", industry_group=IndustryGroup("Transport, Logistics & Travel"),
        hq_country="Denmark", what_they_do="Freight.", situation="Growing.", signals=[])


def test_hybrid_failure_falls_back_to_vector_only():
    story = {"company": "Swiss Post", "score": 0.85}
    with mock.patch.object(build, "find_similar_stories_hybrid", side_effect=RuntimeError("index not found")), \
         mock.patch.object(build, "find_similar_stories", return_value=[dict(story)]) as vector:
        result = build.match_story(None, profile(), mode="hybrid")
    assert result["company"] == "Swiss Post" and result["match_mode"] == "vector"
    assert vector.called


def test_mode_env_var_is_read_when_matching(monkeypatch):
    monkeypatch.setenv("STORY_MATCH_MODE", "vector")
    with mock.patch.object(build, "find_similar_stories_hybrid") as hybrid, \
         mock.patch.object(build, "find_similar_stories", return_value=[{"company": "Swiss Post", "score": 0.8}]):
        assert build.match_story(None, profile())["match_mode"] == "vector"
    assert not hybrid.called


def test_rate_limit_is_not_retried_and_has_a_friendly_message():
    from pymongo.errors import OperationFailure

    from brief.errors import friendly_error
    from brief.story_search import run_search

    class Limited:
        def aggregate(self, pipeline):
            raise OperationFailure("Embedding rate limit exceeded (429)")

    with pytest.raises(EmbeddingRateLimit) as caught:
        run_search(Limited(), [])
    assert friendly_error(caught.value) == "Atlas embedding limit reached, try again in a minute."
    with mock.patch.object(build, "find_similar_stories_hybrid", side_effect=caught.value), \
         mock.patch.object(build, "find_similar_stories") as vector, pytest.raises(EmbeddingRateLimit):
        build.match_story(None, profile(), mode="hybrid")
    assert not vector.called


def test_search_text_has_every_story_field():
    from brief.story_search import search_text
    text = search_text({"company": "Swiss Post", "industry": "Logistics", "use_case": "Tracking",
                        "challenge": "Silos.", "solution": "Atlas.", "summary": "Faster.",
                        "outcomes": [{"claim": "Cut costs", "quote": "q"}]})
    for part in ["Swiss Post", "Logistics", "Tracking", "Silos.", "Atlas.", "Faster.", "Cut costs"]:
        assert part in text
