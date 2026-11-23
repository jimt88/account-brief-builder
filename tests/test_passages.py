"""Phase 9 passages: chunking, rank fusion, source minimums, evidence cap. No network."""

from datetime import datetime, timezone

from pymongo.errors import OperationFailure

from brief import passages
from brief.passages import (CHUNK_CHARS, CHUNK_OVERLAP, chunk_text, fuse, make_chunks, pick_passages,
                            select_passages, store_chunks)
from brief.website import Page

SENTENCE = "Our teams ship cloud products for customers across Europe every single week. "
DATE = datetime(2026, 11, 1, tzinfo=timezone.utc)


def test_chunks_stay_under_the_size_and_overlap_the_previous_chunk():
    chunks = chunk_text(SENTENCE * 80)
    assert len(chunks) > 3
    assert all(len(c) <= CHUNK_CHARS for c in chunks)
    assert all(len(c) > CHUNK_CHARS - 200 for c in chunks[:-1])
    for first, second in zip(chunks, chunks[1:]):
        tail = second[:second.index(" Our teams")] if not second.startswith("Our teams") else ""
        assert len(tail) <= CHUNK_OVERLAP and first.endswith(tail)


def test_chunks_end_on_paragraph_boundaries():
    paragraphs = [f"Paragraph {i}. " + "Words about data platforms and AI. " * 15 for i in range(6)]
    chunks = chunk_text("\n\n".join(paragraphs))
    assert all(c.rstrip().endswith("AI.") for c in chunks)
    assert chunk_text("") == [] and chunk_text("Short page.") == ["Short page."]


def test_one_huge_sentence_is_cut_hard():
    assert all(len(c) <= CHUNK_CHARS for c in chunk_text("x" * 5_000))


def page(url, source="website", text=SENTENCE * 40, date=None):
    return Page(url, url, text, source_type=source, published_at=date)


def test_make_chunks_skips_github_and_keeps_every_page_when_over_the_cap(monkeypatch):
    monkeypatch.setattr(passages, "MAX_CHUNKS", 6)
    pages = [page("https://a.com"), page("https://a.com/jobs/1", "job"),
             page("https://github.com/a/x/blob/main/go.mod", "github"), page("https://news.test/a", "news", date=DATE)]
    chunks = make_chunks("a.com", pages)
    assert len(chunks) == 6 and "github" not in {c["source_type"] for c in chunks}
    assert [c["chunk_index"] for c in chunks] == [0, 0, 0, 1, 1, 1]  # Every page's first chunk first.
    assert chunks[2]["published_at"] == DATE and chunks[0]["domain"] == "a.com"


def chunk(url, i, source="website", text=None):
    return {"url": url, "chunk_index": i, "source_type": source, "published_at": None,
            "text": text or f"Plain text {url} {i}. " * 5}


def test_rank_fusion_adds_one_over_sixty_plus_rank():
    scores = fuse([("a", 0), ("b", 0)], [("b", 0)])
    assert scores[("a", 0)] == 1 / 61 and scores[("b", 0)] == 1 / 62 + 1 / 61


def test_both_rankings_beat_either_one_and_origins_are_labelled():
    chunks = [chunk("https://a.com", 0)] + [chunk(f"https://a.com/p{i}", 0) for i in range(4)]
    chunks[2]["text"] = "We are migrating from Oracle to the cloud with Kafka."
    chunks[3]["text"] = "Our mainframe is legacy."
    picked = pick_passages(chunks, [("https://a.com/p1", 0), ("https://a.com/p3", 0)], k=4)
    urls = [p["url"] for p in picked]
    assert urls[0] == "https://a.com"  # The home page's first chunk is always kept.
    assert urls[1] == "https://a.com/p1"  # In both rankings.
    origins = {p["url"]: p["origin"] for p in picked}
    assert origins["https://a.com/p1"] == "both" and origins["https://a.com/p2"] == "keyword"
    assert origins["https://a.com/p3"] == "vector" and origins["https://a.com"] == "added"


def test_job_and_news_minimums_are_guaranteed():
    chunks = ([chunk("https://a.com", 0)] + [chunk(f"https://a.com/p{i}", 0) for i in range(20)]
              + [chunk(f"https://jobs.test/{i}", 0, "job") for i in range(5)]
              + [chunk(f"https://news.test/{i}", 0, "news") for i in range(3)])
    vector = [(f"https://a.com/p{i}", 0) for i in range(20)]
    picked = pick_passages(chunks, vector, k=8)
    sources = [p["source_type"] for p in picked]
    assert len(picked) == 8 and sources.count("job") == 3 and sources.count("news") == 2


def test_evidence_size_cap_and_neighbouring_chunks_are_joined():
    pages = chunk_text(SENTENCE * 60)
    chunks = [chunk("https://a.com/x", i, text=t) for i, t in enumerate(pages)]
    vector = [("https://a.com/x", i) for i in range(len(pages))]
    picked = pick_passages(chunks, vector, k=15, max_chars=2 * CHUNK_CHARS + 10)
    assert len(picked) == 1 and picked[0]["chunks"] == [0, 1]
    assert picked[0]["text"].count("Our teams ship") == (pages[0] + pages[1]).count("Our teams ship") - 1 \
        or picked[0]["text"] == pages[0] + pages[1][len(passages.overlap_tail(pages[0], CHUNK_OVERLAP)):]
    assert len(picked[0]["text"]) <= 2 * CHUNK_CHARS


class FakeChunks:
    def __init__(self, vector_hits=None, fail=False):
        self.docs, self.vector_hits, self.fail, self.pipelines = [], vector_hits or [], fail, []

    def find(self, query, projection=None):
        return [d for d in self.docs if d["domain"] == query["domain"]]

    def delete_many(self, query):
        self.docs = [d for d in self.docs if d["domain"] != query["domain"]]

    def insert_many(self, docs, ordered=True):
        assert len(docs) <= passages.INSERT_BATCH
        self.docs += docs

    def aggregate(self, pipeline):
        self.pipelines.append(pipeline)
        if self.fail:
            raise OperationFailure("rate limit")
        return [{"url": u, "chunk_index": i} for u, i in self.vector_hits]


def test_store_chunks_replaces_in_batches_and_skips_identical_reruns():
    store = FakeChunks()
    store.docs = [{"domain": "a.com", "url": "old", "chunk_index": 0, "text": "old"}]
    chunks = [dict(chunk(f"https://a.com/{i}", 0), domain="a.com") for i in range(250)]
    assert store_chunks(store, "a.com", chunks) is True
    assert len(store.docs) == 250 and all(d["url"] != "old" for d in store.docs)
    assert store_chunks(store, "a.com", chunks) is False


def test_one_vector_query_with_domain_filter_and_mocked_results():
    chunks = [chunk("https://a.com", 0), chunk("https://a.com/ai", 0)]
    store = FakeChunks(vector_hits=[("https://a.com/ai", 0), ("https://elsewhere", 3)])
    picked, note = select_passages(store, chunks, "a.com")
    assert note is None and len(store.pipelines) == 1
    stage = store.pipelines[0][0]["$vectorSearch"]
    assert stage["filter"] == {"domain": "a.com"} and stage["limit"] == 30 and stage["numCandidates"] == 600
    assert stage["index"] == "chunk_autoembed_index" and stage["path"] == "text"
    assert {p["url"]: p["origin"] for p in picked}["https://a.com/ai"] == "vector"


def test_keyword_fallback_when_atlas_is_still_indexing_or_fails():
    chunks = [chunk("https://a.com", 0), chunk("https://a.com/x", 0, text="Kafka and real-time data.")]
    picked, note = select_passages(FakeChunks(), chunks, "a.com")
    assert note == "Atlas was still indexing; passages chosen by keyword."
    assert [p["origin"] for p in picked] == ["added", "keyword"]
    picked, note = select_passages(FakeChunks(fail=True), chunks, "a.com")
    assert "unavailable" in note and len(picked) == 2


def test_chunks_match_the_page_chunks_validator():
    schema = passages.CHUNK_VALIDATOR["$jsonSchema"]
    chunks = make_chunks("a.com", [page("https://a.com"), page("https://news.test/a", "news", date=DATE)])
    for c in chunks:
        assert set(schema["required"]) <= set(c) and set(c) <= set(schema["properties"])
        assert c["source_type"] in schema["properties"]["source_type"]["enum"]
        assert 0 < len(c["text"]) <= schema["properties"]["text"]["maxLength"]
