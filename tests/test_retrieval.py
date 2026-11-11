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
