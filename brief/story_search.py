"""Find the MongoDB customer stories closest to a description (Atlas Vector Search).

Optionally limited to one industry group (see brief/industries.py) using the
index's filter field, so the search only compares stories from that industry.
"""

from google import genai
from google.genai import types

from brief.gemini_utils import EMBEDDING_DIMENSIONS, EMBEDDING_MODEL, call_with_retry

DB_NAME = "adr_brief"
COLLECTION_NAME = "stories"
INDEX_NAME = "story_vector_index"


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
    vector_search = {
        "index": INDEX_NAME,
        "path": "embedding",
        "queryVector": embed_query(gemini, text),
        "numCandidates": max(k * 20, 50),  # Look at more candidates than we return, for good recall.
        "limit": k,
    }
    if industry:
        vector_search["filter"] = {"industry_group": industry}
    pipeline = [
        {"$vectorSearch": vector_search},
        {
            "$project": {
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
                "score": {"$meta": "vectorSearchScore"},
            }
        },
    ]
    return list(collection.aggregate(pipeline))
