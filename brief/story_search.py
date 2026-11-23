"""Find the MongoDB customer stories closest to a description.

Two ways:
  - find_similar_stories(): Atlas Vector Search only (closest in meaning),
  - find_similar_stories_hybrid(): $rankFusion of Vector Search and Atlas Search
    (keywords), so a story that is close in meaning AND shares key words ranks first.
Both can be limited to one industry group (see brief/industries.py).
"""

from google import genai
from google.genai import types

from brief.gemini_utils import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, call_with_retry

DB_NAME = "adr_brief"
COLLECTION_NAME = "stories"
INDEX_NAME = "story_vector_index"
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


def embed_query(gemini: genai.Client, text: str) -> list[float]:
    """Embed a search description. RETRIEVAL_QUERY pairs with the stories'
    RETRIEVAL_DOCUMENT embeddings (same model, same 768 dimensions)."""
    response = call_with_retry(
        gemini.models.embed_content,
        model=EMBEDDING_MODEL,
        contents=[text],
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY",
            output_dimensionality=EMBEDDING_DIMENSIONS,
        ),
    )
    return list(response.embeddings[0].values)


def find_similar_stories(
    collection, gemini: genai.Client, text: str, k: int = 2, industry: str | None = None
) -> list[dict]:
    """Return the k closest stories, best first, each with company, url, summary
    and a "score" (0-1). If industry is given, only stories in that industry group."""
    pipeline = [
        vector_stage(embed_query(gemini, text), k, industry),
        {"$project": {**STORY_FIELDS, "score": {"$meta": "vectorSearchScore"}}},
    ]
    return list(collection.aggregate(pipeline))


def vector_stage(query_vector: list[float], limit: int, industry: str | None = None) -> dict:
    """The $vectorSearch stage, pre-filtered to one industry group if given."""
    vector_search = {
        "index": INDEX_NAME,
        "path": "embedding",
        "queryVector": query_vector,
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


def hybrid_pipeline(query_vector: list[float], text: str, k: int = 2, industry: str | None = None) -> list[dict]:
    """$rankFusion of the vector and keyword searches (Reciprocal Rank Fusion: a story
    ranked high in both lists wins). Needs MongoDB 8.0+."""
    return [
        {"$rankFusion": {
            "input": {"pipelines": {
                "vector": [vector_stage(query_vector, FUSION_CANDIDATES, industry)],
                "text": [text_stage(text, industry), {"$limit": FUSION_CANDIDATES}],
            }},
            "combination": {"weights": {"vector": VECTOR_WEIGHT, "text": TEXT_WEIGHT}},
            "scoreDetails": True,
        }},
        {"$limit": k},
        {"$project": {**STORY_FIELDS, "fusion_score": {"$meta": "score"},
                      "score_details": {"$meta": "scoreDetails"}}},
    ]


def find_similar_stories_hybrid(
    collection, gemini: genai.Client, text: str, k: int = 2, industry: str | None = None
) -> list[dict]:
    """Like find_similar_stories(), ranked by $rankFusion of meaning and keywords.
    "score" stays the 0-1 vector similarity (0 if only the keyword search found the
    story); "fusion_score" and "score_details" explain the hybrid ranking."""
    stories = list(collection.aggregate(hybrid_pipeline(embed_query(gemini, text), text, k, industry)))
    for story in stories:
        ranks = pipeline_ranks(story.get("score_details"))
        story["score"] = ranks.get("vector", {}).get("value", 0.0)
        story["ranks"] = {name: r.get("rank") for name, r in ranks.items()}
    return stories


def pipeline_ranks(score_details: dict | None) -> dict:
    """{"vector": {"rank": 1, "value": 0.83}, "text": {...}} from $rankFusion score details."""
    return {d["inputPipelineName"]: d for d in (score_details or {}).get("details", [])
            if "inputPipelineName" in d}
