"""检索回归：查找真实证据、稳定引用，并拒绝目录外材料。"""

from pathlib import Path

import pytest

from agent_lab.retrieval import KnowledgeBase, tokenize


@pytest.fixture
def notes(tmp_path):
    directory = tmp_path / "notes"
    directory.mkdir()
    (directory / "retry.md").write_text(
        "# 工具重试\n\n## 网络错误\n"
        "network retry uses exponential_backoff for error 429; retry is bounded.\n"
        "网络重试应设置次数限制，并记录最终错误。\n",
        encoding="utf-8",
    )
    (directory / "approval.md").write_text(
        "# 补丁审批\n\n## 版本校验\n"
        "patch approval compares sha256 before writing; reject stale patch.\n"
        "审批绑定补丁与文件版本，拒绝覆盖并发更新。\n",
        encoding="utf-8",
    )
    return directory


def test_exact_identifier_and_error_code_rank_relevant_note(notes):
    kb = KnowledgeBase(notes)
    results = kb.search("exponential_backoff 429")
    assert len(results) == 1
    assert results[0]["source"] == "retry.md"
    assert "exponential_backoff" in results[0]["text"]
    assert results[0]["bm25_score"] > 0
    assert results[0]["vector_score"] > 0
    assert 0 < results[0]["score"] <= 2 / 61 + 1e-8


def test_chinese_query_returns_matching_evidence(notes):
    results = KnowledgeBase(notes).search("审批绑定补丁")
    assert results[0]["source"] == "approval.md"
    assert results[0]["id"] == results[0]["chunk_id"]
    assert "审批绑定补丁" in results[0]["text"]


@pytest.mark.parametrize("query", ["", "   !!!", "unseen_identifier_8f562"])
def test_no_match_does_not_invent_zero_score_evidence(notes, query):
    assert KnowledgeBase(notes).search(query) == []


def test_ids_and_order_are_stable_across_checkout_paths(notes, tmp_path):
    other = tmp_path / "another-checkout"
    other.mkdir()
    for path in notes.iterdir():
        (other / path.name).write_bytes(path.read_bytes())
    first = KnowledgeBase(notes).search("retry patch", limit=2)
    copied = KnowledgeBase(other).search("retry patch", limit=2)
    rebuilt = KnowledgeBase(notes).search("retry patch", limit=2)
    assert first == copied == rebuilt
    assert all(not Path(result["source"]).is_absolute() for result in first)


def test_heading_change_versions_citation_id(notes):
    before = KnowledgeBase(notes).search("sha256")[0]
    path = notes / "approval.md"
    path.write_text(path.read_text().replace("## 版本校验", "## 原文件版本校验"))
    after = KnowledgeBase(notes).search("sha256")[0]
    assert after["text"] != before["text"]
    assert after["id"] != before["id"]


def test_modified_body_versions_citation_id(notes):
    before = KnowledgeBase(notes).search("sha256")[0]
    path = notes / "approval.md"
    path.write_text(path.read_text().replace("sha256", "sha256 digest"))
    after = KnowledgeBase(notes).search("sha256")[0]
    assert after["id"] != before["id"]


def test_large_section_is_split_into_bounded_citable_chunks(tmp_path):
    (tmp_path / "large.md").write_text("# Long note\n\n## Content\n" + "retrieval evidence. " * 200)
    kb = KnowledgeBase(tmp_path)
    stats = kb.index_stats()
    assert stats["documents"] == 1 and stats["chunks"] > 1
    results = kb.search("evidence", limit=20)
    assert len({result["id"] for result in results}) == stats["chunks"]
    assert all(len(result["text"]) <= 1100 for result in results)


def test_external_symlinks_and_oversize_notes_are_not_indexed(tmp_path):
    directory = tmp_path / "notes"
    directory.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "private.md"
    secret.write_text("# Private\n\nsecret_marker_outside\n")
    (directory / "file.md").symlink_to(secret)
    (directory / "nested").symlink_to(outside, target_is_directory=True)
    (directory / "huge.md").write_text("# Huge\n\n" + "oversize_marker " * 9000)
    kb = KnowledgeBase(directory)
    assert kb.index_stats()["documents"] == 0
    assert kb.search("secret_marker_outside oversize_marker") == []


def test_stats_disclose_lexical_baseline(notes):
    stats = KnowledgeBase(notes).index_stats()
    assert stats["documents"] == 2
    assert stats["semantic_embeddings"] is False
    assert stats["vocabulary"] > 0
    assert "BM25" in stats["retrieval"] and "RRF" in stats["retrieval"]


@pytest.mark.parametrize("limit", [True, False, 0, 21, 1.5, "5"])
def test_invalid_result_limit_fails(notes, limit):
    with pytest.raises(ValueError):
        KnowledgeBase(notes).search("retry", limit=limit)


@pytest.mark.parametrize("query", [None, 42, "x" * 2001])
def test_invalid_query_fails(notes, query):
    with pytest.raises(ValueError):
        KnowledgeBase(notes).search(query)


def test_non_directory_and_missing_directory(tmp_path):
    file = tmp_path / "file"
    file.write_text("not a directory")
    with pytest.raises(ValueError):
        KnowledgeBase(file)
    assert KnowledgeBase(tmp_path / "missing").search("anything") == []


def test_tokenizer_preserves_code_identifiers_and_chinese_bigrams():
    terms = tokenize("exponential_backoff 429 中文检索")
    assert "exponential_backoff" in terms and "429" in terms
    assert "中文" in terms and "检索" in terms


def test_fenced_code_comments_never_become_title_or_section_headings(tmp_path):
    (tmp_path / "code_only.md").write_text(
        "Intro paragraph before code.\n\n"
        "```python\n"
        "# Not a heading\n"
        "def foo(): pass\n"
        "```\n\n"
        "   ~~~sh\n"
        "# Still not a heading\n"
        "echo done\n"
        "   ~~~\n",
        encoding="utf-8",
    )
    kb = KnowledgeBase(tmp_path)
    assert kb.index_stats()["chunks"] == 1
    results = kb.search("foo")
    assert len(results) == 1
    assert results[0]["title"] == "未命名笔记"
    assert results[0]["heading"] == "未命名笔记"
    assert "# Not a heading\ndef foo(): pass" in results[0]["text"]
    assert "# Still not a heading\necho done" in results[0]["text"]

    crlf_dir = tmp_path / "crlf"
    lf_dir = tmp_path / "lf"
    crlf_dir.mkdir()
    lf_dir.mkdir()
    markdown = "# Guide\n\n## Implementation\nBefore.\n```python\n# Not a heading\ndef foo(): pass\n```\nAfter.\n"
    (lf_dir / "guide.md").write_bytes(markdown.encode("utf-8"))
    (crlf_dir / "guide.md").write_bytes(markdown.replace("\n", "\r\n").encode("utf-8"))
    lf_hit = KnowledgeBase(lf_dir).search("Implementation")[0]
    crlf_hit = KnowledgeBase(crlf_dir).search("Implementation")[0]
    assert lf_hit == crlf_hit
    assert lf_hit["title"] == "Guide"
    assert lf_hit["heading"] == "Implementation"
    assert "# Not a heading\ndef foo(): pass" in lf_hit["text"]


def test_camelcase_and_snake_case_subtoken_matching(notes, tmp_path):
    assert tokenize("exponentialBackoff") == ["exponentialbackoff", "exponential", "backoff"]
    assert tokenize("max_discount_cents") == ["max_discount_cents", "max", "discount", "cents"]

    cross_hits = KnowledgeBase(notes).search("exponentialBackoff")
    assert len(cross_hits) == 1
    assert cross_hits[0]["source"] == "retry.md"
    assert cross_hits[0]["bm25_score"] > 0
    assert cross_hits[0]["vector_score"] > 0

    custom_dir = tmp_path / "custom"
    custom_dir.mkdir()
    (custom_dir / "pricing.md").write_text(
        "# Pricing\n\n## Limits\n"
        "Enforce max_discount_cents and exponentialBackoff during checkout.\n",
        encoding="utf-8",
    )
    kb = KnowledgeBase(custom_dir)
    assert kb.search("maxDiscountCents")[0]["source"] == "pricing.md"
    assert kb.search("exponential_backoff")[0]["source"] == "pricing.md"

    # 同时存在精确 snake_case 与跨命名 camelCase 时，精确全词匹配排在第一位，跨命名紧随其后。
    combined_hits = KnowledgeBase(tmp_path).search("exponential_backoff", limit=5)
    assert [hit["source"] for hit in combined_hits] == ["notes/retry.md", "custom/pricing.md"]


def test_pluggable_semantic_embedder_and_reranker(tmp_path):
    (tmp_path / "throttle.md").write_text(
        "# Traffic Control\n\n## Quota\nHTTP 429 upstream throttling window.\n",
        encoding="utf-8",
    )
    (tmp_path / "storage.md").write_text(
        "# Persistence\n\n## Checkpoints\nSQLite WAL checkpoint durability.\n",
        encoding="utf-8",
    )

    def fake_embedder(texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for item in texts:
            lowered = item.lower()
            if "throttling" in lowered or "rate limit" in lowered:
                # 故意返回非单位向量 [3, 4]，验证内部会做 L2 归一化。
                vectors.append([3.0, 4.0])
            elif "sqlite" in lowered:
                vectors.append([-3.0, 0.0])
            else:
                vectors.append([0.0, 5.0])
        return vectors

    kb = KnowledgeBase(tmp_path, embedder=fake_embedder)
    stats = kb.index_stats()
    assert stats["semantic_embeddings"] is True
    assert "dense semantic cosine" in stats["retrieval"]

    # 查询词与文档完全无词面重合，仅靠稠密语义向量召回，且点积归一化为 1.0。
    semantic_hits = kb.search("rate limit")
    assert len(semantic_hits) == 1
    assert semantic_hits[0]["source"] == "throttle.md"
    assert semantic_hits[0]["bm25_score"] == 0.0
    assert semantic_hits[0]["vector_score"] == pytest.approx(1.0)

    def promote_storage_reranker(query: str, candidates: list[dict]) -> list[float]:
        return [10.0 if item["source"] == "storage.md" else 1.0 for item in candidates]

    def both_positive_embedder(texts: list[str]) -> list[list[float]]:
        return [[3.0, 4.0] for _ in texts]

    reranked_kb = KnowledgeBase(tmp_path, embedder=both_positive_embedder, reranker=promote_storage_reranker)
    assert "reranker" in reranked_kb.index_stats()["retrieval"]
    reranked = reranked_kb.search("system", limit=2)
    assert [item["source"] for item in reranked] == ["storage.md", "throttle.md"]
    assert reranked[0]["rerank_score"] == 10.0

    dict_reranked_kb = KnowledgeBase(
        tmp_path,
        embedder=both_positive_embedder,
        reranker=lambda query, candidates: sorted(candidates, key=lambda c: c["source"], reverse=True),
    )
    assert [item["source"] for item in dict_reranked_kb.search("system", limit=2)] == [
        "throttle.md",
        "storage.md",
    ]
