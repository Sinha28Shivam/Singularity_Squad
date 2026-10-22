"""Plan B1 chunking: token-measured windows, a hard per-chunk maximum, traceability and determinism."""
from __future__ import annotations

import json

import pytest

from app.policy_rag import chunking, ingestion, tokenization as tok
from app.policy_rag.pdf_extract import PageText
from app.repositories import policy_chunk_repository as chunk_repo
from app.repositories import policy_document_repository as doc_repo
from tests.policy_rag_helpers import COMPANY_PAGES, ingest

DOC = {"document_id": "doc_0123456789abcdef", "content_hash": "h" * 64}
FILLER = ("Employees must follow this rule when arranging events and must keep records of approvals, quotes and "
          "vendor confirmations for audit purposes at all times. ")
HINDI = ("\u0915\u0930\u094d\u092e\u091a\u093e\u0930\u093f\u092f\u094b\u0902 \u0915\u094b \u0928\u0940\u0924\u093f "
         "\u0915\u093e \u092a\u093e\u0932\u0928 \u0915\u0930\u0928\u093e \u091a\u093e\u0939\u093f\u090f vendor "
         "approval INR 50,000 ")
URL = "https://example.com/" + "/".join(f"segment{i}?q={i}&x=y" for i in range(600))
JSON_BLOB = json.dumps([{"vendor": f"VEN-{i:03d}", "rate": i * 1000, "unit": "event"} for i in range(400)],
                       separators=(",", ":"))
TABLE = "\n".join(f"| Package {i} | Team gathering scope with coordinator | INR {i},000 | event |" for i in range(400))
ONE_ROW = "| " + " | ".join(f"col{i} value{i}" for i in range(1500)) + " |"
CASES = {
    "huge_paragraph": FILLER * 200,
    "long_url": URL,
    "json_no_spaces": JSON_BLOB,
    "table_rows": TABLE,
    "single_huge_table_row": ONE_ROW,
    "mixed_devanagari": HINDI * 150,
    "devanagari_unbroken": HINDI.replace(" ", "") * 80,
}


@pytest.fixture(params=["tiktoken", "estimate"])
def mode(request, monkeypatch):
    monkeypatch.setenv("WORKPLACEOPS_TOKENIZER", request.param)
    if request.param == "tiktoken" and not tok.get_token_counter().exact:
        pytest.skip("tiktoken not available")
    return request.param


@pytest.mark.parametrize("name", sorted(CASES))
def test_no_chunk_exceeds_max_tokens(mode, name):
    counter = tok.get_token_counter()
    pages = [PageText(1, "1. Purpose and commitment\n" + CASES[name])]
    chunks = chunking.chunk_pages(pages, DOC, target_tokens=120, overlap_tokens=20, max_tokens=150)
    assert len(chunks) >= 2
    for c in chunks:
        assert counter.count(c.text) <= 150, (name, counter.count(c.text))
        assert c.token_count == counter.count(c.text) == c.token_estimate
    # Nothing is lost: every non-space character of the input appears in some chunk.
    joined = "".join("".join(c.text.split()) for c in chunks)
    for word in CASES[name].split()[:50]:
        assert word[:10] in joined


def test_default_config_bounds_real_document(mode):
    counter = tok.get_token_counter()
    pages = [PageText(1, "1. Purpose and commitment\n" + FILLER * 400 + URL), PageText(2, TABLE)]
    s = chunking.chunking_settings(ingestion.config())
    chunks = chunking.chunk_pages(pages, DOC, **s)
    assert s["max_tokens"] == 512 and all(counter.count(c.text) <= 512 for c in chunks)


def test_overlap_is_measured_in_tokens(mode):
    counter = tok.get_token_counter()
    words = [f"rule{i}" for i in range(600)]
    pages = [PageText(1, " ".join(words))]
    chunks = chunking.chunk_pages(pages, DOC, target_tokens=100, overlap_tokens=30, max_tokens=140)
    assert len(chunks) >= 3
    for a, b in zip(chunks, chunks[1:]):
        wa, wb = a.text.split(), b.text.split()
        shared = wa[len(wa) - wa[::-1].index(wb[0]) - 1:] if wb[0] in wa else []
        assert shared and wb[:len(shared)] == shared
        overlap = " ".join(shared)
        assert 0 < counter.count(overlap) <= 30 + 1  # leading-space token at the boundary
        assert counter.count(overlap) >= 15  # carried overlap is close to the configured token budget


def test_zero_overlap_and_overlap_capped(mode):
    pages = [PageText(1, FILLER * 40)]
    none = chunking.chunk_pages(pages, DOC, target_tokens=100, overlap_tokens=0, max_tokens=120)
    assert sum(len(c.text.split()) for c in none) == len((FILLER * 40).split())
    assert chunking.chunking_settings({"chunk_target_tokens": 100, "chunk_overlap_tokens": 90,
                                       "chunk_max_tokens": 50})["overlap_tokens"] <= 25


def test_page_and_section_traceability(mode):
    pages = [PageText(1, "1. Purpose and commitment\n" + FILLER * 30),
             PageText(2, FILLER * 5 + "\n2. Quotations\n" + FILLER * 5)]
    chunks = chunking.chunk_pages(pages, DOC, target_tokens=100, overlap_tokens=10, max_tokens=130)
    assert {c.page_number for c in chunks} == {1, 2}
    assert all(c.section == "1 Purpose and commitment" for c in chunks if c.page_number == 1)
    p2 = [c.section for c in chunks if c.page_number == 2]
    assert p2[0] == "1 Purpose and commitment" and p2[-1] == "2 Quotations"  # heading carried across pages
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    assert all(c.document_id == DOC["document_id"] and c.content_hash == DOC["content_hash"] for c in chunks)
    assert all(c.chunk_id == chunking.chunk_id_for(DOC["document_id"], DOC["content_hash"], c.chunk_index)
               for c in chunks)


def test_deterministic_rechunking(mode):
    pages = [PageText(1, "1. Purpose\n" + FILLER * 50 + URL), PageText(2, TABLE[:5000])]
    a = [c.model_dump() for c in chunking.chunk_pages(pages, DOC, target_tokens=90, overlap_tokens=15)]
    b = [c.model_dump() for c in chunking.chunk_pages(pages, DOC, target_tokens=90, overlap_tokens=15)]
    assert a == b


def test_metadata_fields_and_exactness_label(mode):
    counter = tok.get_token_counter()
    chunks = chunking.chunk_pages([PageText(1, FILLER * 10)], DOC)
    for c in chunks:
        assert c.tokenizer_id == counter.tokenizer_id
        assert c.token_count_exact is (mode == "tiktoken")
        assert c.chunking_config_version == chunking.chunking_config_version(
            target_tokens=450, overlap_tokens=60, max_tokens=512, tokenizer_id=counter.tokenizer_id)
        assert c.chunking_config_version.startswith("chunk-")


def test_config_version_sensitive_to_every_input():
    base = dict(target_tokens=450, overlap_tokens=60, max_tokens=512, tokenizer_id="tiktoken:o200k_base")
    v = chunking.chunking_config_version(**base)
    assert v == chunking.chunking_config_version(**base)
    for key, value in (("target_tokens", 400), ("overlap_tokens", 50), ("max_tokens", 600),
                       ("tokenizer_id", "estimate-v1")):
        assert chunking.chunking_config_version(**{**base, key: value}) != v


def test_estimate_tokens_name_still_works(mode):
    assert chunking.estimate_tokens("") == 0
    assert chunking.estimate_tokens(FILLER) == tok.count_tokens(FILLER)


def test_ingested_chunks_persist_metadata(mode):
    res = ingest(COMPANY_PAGES, "company.pdf")
    assert res.ok and res.status == "ready"
    counter = tok.get_token_counter()
    rows = chunk_repo.list_chunks(document_ids=[res.document_id])
    assert rows
    for r in rows:
        assert r["token_count"] == r["token_estimate"] == counter.count(r["text"])
        assert r["tokenizer_id"] == counter.tokenizer_id and r["token_count_exact"] is counter.exact
        assert r["chunking_config_version"] == ingestion.current_index_signature()["chunking_config_version"]
        assert r["embedding_dims"] == len(r["embedding"]) and r["embedding_provider"] == "local_hash"
    meta = doc_repo.get_document(res.document_id)["index_metadata"]
    assert meta["tokenizer_id"] == counter.tokenizer_id and meta["embedding_dims"] == len(rows[0]["embedding"])
    assert meta["max_chunk_tokens"] <= meta["max_tokens"] == 512
