"""Atlas picks the passages Gemini reads (Phase 9).

A 30-page crawl plus job ads and news is far more text than one Gemini prompt should
hold. So every page (website, extra links, jobs, news; not GitHub, which already gives
direct signals) is cut into ~1,200-character chunks with 150 characters of overlap and
stored in adr_brief.page_chunks. Atlas embeds each chunk itself (Voyage AI, index
chunk_autoembed_index, scripts/create_chunk_index.py).

Then one $vectorSearch with a single "buying intent" query (database migration, AI,
real-time data, cloud, hiring, new digital products) ranks the company's chunks by
meaning, a keyword scan ranks them by strong terms (Oracle, mainframe, Kafka, LLM ...),
and the two rankings are merged with reciprocal rank fusion: score = sum 1 / (60 + rank).
The top passages, with a few job and news passages guaranteed, go to Gemini. Quotes are
still checked against the full page text.
"""

import logging
import os
import re
import time
from datetime import datetime, timezone

from pymongo.errors import PyMongoError

from brief.signals import MAX_EVIDENCE_CHARS
from brief.website import Page

log = logging.getLogger(__name__)

CHUNKS_COLLECTION = "page_chunks"
CHUNK_INDEX_NAME = "chunk_autoembed_index"
CHUNK_CHARS = 1_200
CHUNK_OVERLAP = 150
MAX_CHUNKS = 400  # Per company.
INSERT_BATCH = 100
CHUNK_TTL_SECONDS = 24 * 60 * 60
# Atlas embeds new chunks in the background and gives no per-document "ready" signal,
# so the vector query waits this long after the chunks were written.
CHUNK_INDEX_WAIT_S = float(os.getenv("CHUNK_INDEX_WAIT_S", "20"))
VECTOR_LIMIT = 30
RRF_K = 60
PASSAGES = 15
SOURCE_MINIMUMS = {"job": 3, "news": 2}
CHUNKED_SOURCES = {"website", "extra_link", "job", "news"}

CHUNK_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["domain", "url", "source_type", "chunk_index", "text", "fetched_at"],
        "properties": {
            "domain": {"bsonType": "string", "minLength": 1},
            "url": {"bsonType": "string", "minLength": 1},
            "source_type": {"enum": sorted(CHUNKED_SOURCES)},
            "published_at": {"bsonType": ["date", "null"]},
            "chunk_index": {"bsonType": "int", "minimum": 0},
            "text": {"bsonType": "string", "minLength": 1, "maxLength": CHUNK_CHARS},
            "fetched_at": {"bsonType": "date"},
        },
    }
}
CHUNK_INDEX_DEFINITION = {
    "fields": [
        {"type": "autoEmbed", "modality": "text", "path": "text", "model": "voyage-4"},
        {"type": "filter", "path": "domain"},
        {"type": "filter", "path": "source_type"},
    ]
}

INTENT_QUERY = (
    "Replacing or migrating legacy or relational databases; AI, generative AI or machine "
    "learning initiatives; scaling real-time, streaming or event-driven data; cloud "
    "migration or application modernisation; hiring software, data and platform "
    "engineers; launching new digital products or entering new markets."
)
STRONG_TERMS = [
    "mongodb", "postgresql", "postgres", "oracle", "mysql", "sql server", "dynamodb", "cosmos db",
    "cassandra", "redis", "db2", "mainframe", "legacy", "migration", "migrate", "modernisation",
    "modernization", "real-time", "real time", "streaming", "event-driven", "kafka", "microservices",
    "generative ai", "genai", "llm", "machine learning", "artificial intelligence", "vector",
    "cloud-native", "data platform", "digital transformation",
]
TERM_PATTERNS = [re.compile(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", re.I) for t in STRONG_TERMS]


# ---- Chunking -----------------------------------------------------------------------

def split_units(text: str, max_len: int) -> list[str]:
    """Paragraphs (line breaks), then sentences for long paragraphs, then hard cuts."""
    units = []
    for paragraph in re.split(r"\n+", text):
        paragraph = " ".join(paragraph.split())
        if len(paragraph) <= max_len:
            units += [paragraph] if paragraph else []
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", paragraph):
            while len(sentence) > max_len:
                units.append(sentence[:max_len])
                sentence = sentence[max_len:]
            units += [sentence] if sentence else []
    return units


def overlap_tail(chunk: str, overlap: int) -> str:
    """The last <= overlap characters of a chunk, starting at a word."""
    tail = chunk[-overlap:]
    return tail[tail.find(" ") + 1:] if len(chunk) > overlap and " " in tail else tail


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """~size-character chunks that end on paragraph or sentence boundaries where possible;
    each chunk after the first starts with the last <= overlap characters of the one before."""
    chunks, current = [], ""
    for unit in split_units(text, size - overlap - 1):
        if current and len(current) + 1 + len(unit) > size:
            chunks.append(current)
            current = f"{overlap_tail(current, overlap)} {unit}"
        else:
            current = f"{current} {unit}" if current else unit
    return chunks + ([current] if current else [])


def make_chunks(domain: str, pages: list[Page], now: datetime | None = None) -> list[dict]:
    """page_chunks documents for one company, at most MAX_CHUNKS. Pages come best first;
    when there are too many chunks, every page's first chunk is kept before any page's
    second, and so on, so the best pages keep the most."""
    now = now or datetime.now(timezone.utc)
    per_page, seen = [], set()
    for page in pages:
        if page.source_type not in CHUNKED_SOURCES or page.url in seen:
            continue
        seen.add(page.url)
        per_page.append([
            {"domain": domain, "url": page.url, "source_type": page.source_type,
             "published_at": page.published_at, "chunk_index": i, "text": text, "fetched_at": now}
            for i, text in enumerate(chunk_text(page.text))])
    chunks, depth = [], 0
    while len(chunks) < MAX_CHUNKS and any(depth < len(c) for c in per_page):
        chunks += [c[depth] for c in per_page if depth < len(c)]
        depth += 1
    return chunks[:MAX_CHUNKS]


def store_chunks(collection, domain: str, chunks: list[dict]) -> bool:
    """Replace the company's chunks (delete, then insert in batches of INSERT_BATCH).
    Returns True if anything was written, False if the same chunks are already stored
    (a cached re-run), so there's no need to wait for Atlas to embed them."""
    stored = {(d["url"], d["chunk_index"], d["text"])
              for d in collection.find({"domain": domain}, {"_id": 0, "url": 1, "chunk_index": 1, "text": 1})}
    if stored == {(c["url"], c["chunk_index"], c["text"]) for c in chunks}:
        return False
    collection.delete_many({"domain": domain})
    for start in range(0, len(chunks), INSERT_BATCH):
        collection.insert_many([dict(c) for c in chunks[start:start + INSERT_BATCH]], ordered=False)
    return True


def wait_for_embeddings(written_at: float | None) -> None:
    """Sleep out the rest of CHUNK_INDEX_WAIT_S since the chunks were written."""
    if written_at is not None:
        time.sleep(max(0.0, written_at + CHUNK_INDEX_WAIT_S - time.monotonic()))


# ---- Ranking ------------------------------------------------------------------------

def key(chunk: dict) -> tuple[str, int]:
    return chunk["url"], chunk["chunk_index"]


def vector_ranking(collection, domain: str) -> list[tuple[str, int]]:
    """The one $vectorSearch per brief: the company's chunks closest to INTENT_QUERY."""
    pipeline = [
        {"$vectorSearch": {
            "index": CHUNK_INDEX_NAME,
            "path": "text",
            "query": {"text": INTENT_QUERY},
            "filter": {"domain": domain},
            "numCandidates": 20 * VECTOR_LIMIT,
            "limit": VECTOR_LIMIT,
        }},
        {"$project": {"_id": 0, "url": 1, "chunk_index": 1}},
    ]
    return [key(d) for d in collection.aggregate(pipeline)]


def keyword_ranking(chunks: list[dict]) -> list[tuple[str, int]]:
    """Chunks that mention strong terms, most distinct terms first."""
    hits = [(sum(1 for p in TERM_PATTERNS if p.search(c["text"])), i) for i, c in enumerate(chunks)]
    return [key(chunks[i]) for n, i in sorted(hits, key=lambda h: (-h[0], h[1])) if n]


def fuse(*rankings: list[tuple[str, int]]) -> dict[tuple[str, int], float]:
    """Reciprocal rank fusion: each ranking adds 1 / (RRF_K + rank), rank from 1."""
    scores = {}
    for ranking in rankings:
        for rank, k in enumerate(ranking, start=1):
            scores[k] = scores.get(k, 0.0) + 1 / (RRF_K + rank)
    return scores


def merge_overlap(first: str, second: str) -> str:
    """Join two neighbouring chunks, dropping the overlap second starts with."""
    for n in range(min(len(first), len(second), CHUNK_OVERLAP + 1), 0, -1):
        if first.endswith(second[:n]):
            return first + second[n:]
    return f"{first} {second}"


def pick_passages(chunks: list[dict], vector: list[tuple[str, int]], k: int = PASSAGES,
                  max_chars: int = MAX_EVIDENCE_CHARS) -> list[dict]:
    """Pure selection step: fuse the vector and keyword rankings, keep the home page's
    first chunk, guarantee SOURCE_MINIMUMS job/news chunks, fill to k, stop at max_chars,
    then join neighbouring chunks of the same page into one passage."""
    by_key = {key(c): c for c in chunks}
    vector = [v for v in vector if v in by_key]
    keyword = keyword_ranking(chunks)
    scores = fuse(vector, keyword)
    ranked = sorted(scores, key=lambda x: -scores[x])

    chosen = []
    if chunks and chunks[0]["source_type"] == "website" and chunks[0]["chunk_index"] == 0:
        chosen.append(key(chunks[0]))  # Who the company is: helps industry and country.
    for source, minimum in SOURCE_MINIMUMS.items():
        pool = [x for x in ranked if by_key[x]["source_type"] == source]
        pool += [key(c) for c in chunks if c["source_type"] == source and key(c) not in scores]
        have = sum(1 for x in chosen if by_key[x]["source_type"] == source)
        chosen += [x for x in pool if x not in chosen][:max(0, minimum - have)]
    chosen += [x for x in ranked if x not in chosen]
    chosen = chosen[:k]

    kept, total = [], 0
    for x in chosen:
        if total + len(by_key[x]["text"]) > max_chars:
            break
        kept.append(x)
        total += len(by_key[x]["text"])

    def origin(x):
        in_vector, in_keyword = x in vector, x in keyword
        return "both" if in_vector and in_keyword else "vector" if in_vector else "keyword" if in_keyword else "added"

    passages, order = {}, {x: i for i, x in enumerate(kept)}
    for x in sorted(kept, key=lambda x: (x[0], x[1])):
        chunk = by_key[x]
        last = passages.get(x[0], [None])[-1]
        if last and last["chunks"][-1] == x[1] - 1:
            last["text"] = merge_overlap(last["text"], chunk["text"])
            last["chunks"].append(x[1])
            last["origins"].add(origin(x))
            last["rank"] = min(last["rank"], order[x])
            continue
        passages.setdefault(x[0], []).append({
            "url": chunk["url"], "source_type": chunk["source_type"], "published_at": chunk["published_at"],
            "text": chunk["text"], "chunks": [x[1]], "origins": {origin(x)}, "rank": order[x]})
    result = sorted((p for group in passages.values() for p in group), key=lambda p: p["rank"])
    for p in result:
        origins = p.pop("origins") - {"added"} or {"added"}
        p["origin"] = "both" if "both" in origins or {"vector", "keyword"} <= origins else origins.pop()
        p.pop("rank")
    return result


def select_passages(collection, chunks: list[dict], domain: str, k: int = PASSAGES) -> tuple[list[dict], str | None]:
    """Run the one vector query and pick passages. Returns (passages, note). If the
    vector query fails or finds nothing yet, passages are chosen by keyword alone."""
    note = None
    try:
        vector = vector_ranking(collection, domain)
    except PyMongoError as exc:
        log.warning("Chunk vector search failed: %s", exc)
        vector, note = [], "Atlas Vector Search was unavailable; passages chosen by keyword."
    if not vector and note is None:
        note = "Atlas was still indexing; passages chosen by keyword."
    return pick_passages(chunks, vector, k), note


def format_passages(passages: list[dict], titles: dict[str, str]) -> str:
    """Passages for the prompt, labelled like whole pages: source, date, URL, title."""
    blocks = []
    for p in passages:
        date = f" ({p['published_at']:%Y-%m-%d})" if p["published_at"] else ""
        blocks.append(f"=== {p['source_type'].upper()}{date}: {p['url']} ===\n"
                      f"TITLE: {titles.get(p['url'], '')}\n{p['text']}")
    return "\n\n".join(blocks)
