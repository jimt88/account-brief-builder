"""Find the MongoDB customer stories closest to a description.

Two ways:
  - find_similar_stories(): Atlas Vector Search only (closest in meaning),
  - find_similar_stories_hybrid(): $rankFusion of Vector Search and Atlas Search
    (keywords), so a story that is close in meaning AND shares key words ranks first.
Both can be limited to one industry group (see brief/industries.py).

The vectors are made by Atlas itself (automated embedding with Voyage AI): the index
story_autoembed_index embeds each story's search_text, and $vectorSearch takes the
query as plain text. No embedding code or embedding API key in this app.
On M0 without a payment method Atlas allows about 3 query embeddings per minute.
"""

from pymongo.errors import OperationFailure

from brief.industries import INDUSTRY_GROUPS

DB_NAME = "adr_brief"
COLLECTION_NAME = "stories"
INDEX_NAME = "story_autoembed_index"  # Created by scripts/create_autoembed_index.py.
EMBED_PATH = "search_text"            # The text field Atlas embeds.
EMBED_MODEL = "voyage-4"
TEXT_INDEX_NAME = "story_text_index"  # Created by scripts/create_search_index.py.
TEXT_FIELDS = ["company", "industry", "use_case", "challenge", "solution", "summary", "outcomes.claim"]
# $rankFusion weights: equal to start with; raise one to trust that search more.
VECTOR_WEIGHT = 1.0
TEXT_WEIGHT = 1.0
FUSION_CANDIDATES = 10  # Results each search passes to $rankFusion before the final limit.
STORY_FIELDS = {
    "_id": 0,
    "company": 1,
    "url": "$source_url",
    "hq_country": 1,
    "industry_group": 1,
    "industry": 1,
    "use_case": 1,
    "challenge": 1,
    "solution": 1,
    "summary": 1,
    "outcomes": 1,
    "source_url": 1,
}


# MongoDB-side validation for stories: search_text (what Atlas embeds) is required;
# the old Gemini "embedding" field is no longer needed.
STORY_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["source_url", "company", "industry", "summary", "outcomes", "search_text", "schema_version"],
        "properties": {
            "source_url": {"bsonType": "string"},
            "company": {"bsonType": "string", "minLength": 1},
            "industry": {"bsonType": "string"},
            "industry_group": {"enum": INDUSTRY_GROUPS},
            "summary": {"bsonType": "string", "minLength": 1},
            "outcomes": {
                "bsonType": "array",
                "maxItems": 4,
                "items": {"bsonType": "object", "required": ["claim", "quote"]},
            },
            "search_text": {"bsonType": "string", "minLength": 1},
            "schema_version": {"bsonType": "int"},
        },
    }
}


class EmbeddingRateLimit(Exception):
    """Atlas refused to embed the query: the free M0 limit (about 3 a minute) was hit."""


def search_text(story: dict) -> str:
    """The one text field Atlas embeds for a story: company, industry, use case,
    challenge, solution, summary and outcome claims."""
    claims = " ".join(o["claim"] for o in story.get("outcomes", []))
    return (
        f"{story['company']} ({story.get('industry', '')}). Use case: {story.get('use_case', '')}. "
        f"Challenge: {story.get('challenge', '')} Solution: {story.get('solution', '')} "
        f"Summary: {story.get('summary', '')} Outcomes: {claims}"
    )


def is_rate_limit(exc: Exception) -> bool:
    """True if a MongoDB error is Atlas's embedding rate limit."""
    text = str(exc).lower()
    return isinstance(exc, OperationFailure) and ("rate limit" in text or "too many requests" in text or "429" in text)


def run_search(collection, pipeline: list[dict]) -> list[dict]:
    """Run an aggregation; Atlas's embedding rate limit becomes EmbeddingRateLimit."""
    try:
        return list(collection.aggregate(pipeline))
    except OperationFailure as exc:
        if is_rate_limit(exc):
            raise EmbeddingRateLimit(str(exc)) from exc
        raise


def find_similar_stories(collection, text: str, k: int = 2, industry: str | None = None) -> list[dict]:
    """Return the k closest stories, best first, each with company, url, summary
    and a "score" (0-1). If industry is given, only stories in that industry group."""
    pipeline = [
        vector_stage(text, k, industry),
        {"$project": {**STORY_FIELDS, "score": {"$meta": "vectorSearchScore"}}},
    ]
    return run_search(collection, pipeline)


def vector_stage(text: str, limit: int, industry: str | None = None) -> dict:
    """The $vectorSearch stage with a text query (Atlas embeds it), pre-filtered to
    one industry group if given."""
    vector_search = {
        "index": INDEX_NAME,
        "path": EMBED_PATH,
        "query": {"text": text},
        "numCandidates": max(limit * 20, 50),  # Look at more candidates than we return, for good recall.
        "limit": limit,
    }
    if industry:
        vector_search["filter"] = {"industry_group": industry}
    return {"$vectorSearch": vector_search}


def text_stage(text: str, industry: str | None = None) -> dict:
    """The Atlas Search ($search) stage: any of the words, across the story fields,
    with an exact industry_group filter if given."""
    compound = {"should": [{"text": {"query": text, "path": TEXT_FIELDS}}], "minimumShouldMatch": 1}
    if industry:
        compound["filter"] = [{"equals": {"path": "industry_group", "value": industry}}]
    return {"$search": {"index": TEXT_INDEX_NAME, "compound": compound}}


def hybrid_pipeline(text: str, k: int = 2, industry: str | None = None) -> list[dict]:
    """$rankFusion of the vector and keyword searches (Reciprocal Rank Fusion: a story
    ranked high in both lists wins). Needs MongoDB 8.0+."""
    return [
        {"$rankFusion": {
            "input": {"pipelines": {
                "vector": [vector_stage(text, FUSION_CANDIDATES, industry)],
                "text": [text_stage(text, industry), {"$limit": FUSION_CANDIDATES}],
            }},
            "combination": {"weights": {"vector": VECTOR_WEIGHT, "text": TEXT_WEIGHT}},
            "scoreDetails": True,
        }},
        {"$limit": k},
        {"$project": {**STORY_FIELDS, "fusion_score": {"$meta": "score"},
                      "score_details": {"$meta": "scoreDetails"}}},
    ]


def find_similar_stories_hybrid(collection, text: str, k: int = 2, industry: str | None = None) -> list[dict]:
    """Like find_similar_stories(), ranked by $rankFusion of meaning and keywords.
    "score" stays the 0-1 vector similarity (0 if only the keyword search found the
    story); "fusion_score" and "score_details" explain the hybrid ranking."""
    stories = run_search(collection, hybrid_pipeline(text, k, industry))
    for story in stories:
        ranks = pipeline_ranks(story.get("score_details"))
        story["score"] = ranks.get("vector", {}).get("value", 0.0)
        story["ranks"] = {name: r.get("rank") for name, r in ranks.items()}
    return stories


def pipeline_ranks(score_details: dict | None) -> dict:
    """{"vector": {"rank": 1, "value": 0.83}, "text": {...}} from $rankFusion score details."""
    return {d["inputPipelineName"]: d for d in (score_details or {}).get("details", [])
            if "inputPipelineName" in d}
