"""AST 代码地图与跨文件符号检索（含字符预算与过期检测）测试。"""

import shutil

from agent_lab.api import ROOT
from agent_lab.repomap import RepoMapIndex


def test_repomap_obeys_char_budget_and_ranks_cross_file_dependencies(tmp_path):
    ws = tmp_path / "repo"
    shutil.copytree(ROOT / "data/multi_file_repo", ws)
    index = RepoMapIndex(ws)

    full_map = index.build_map(max_chars=2400, focus_query="checkout discount")
    assert full_map["ok"] is True
    assert full_map["indexed_files"] == 4  # models.py, discounts.py, checkout.py, tests/test_checkout.py
    assert len(full_map["map"]) <= 2400
    assert "discounts.py" in full_map["map"]
    assert "compute_discount_cents" in full_map["map"]

    tight_map = index.build_map(max_chars=220, focus_query="Coupon")
    assert len(tight_map["map"]) <= 220
    assert tight_map["truncated"] is True


def test_symbol_search_finds_definitions_and_cross_file_usages(tmp_path):
    ws = tmp_path / "repo"
    shutil.copytree(ROOT / "data/multi_file_repo", ws)
    index = RepoMapIndex(ws)

    found = index.search_symbol("compute_discount_cents")
    assert found["ok"] is True
    def_paths = [d["path"] for d in found["definitions"]]
    ref_paths = [r["path"] for r in found["references"]]
    assert "discounts.py" in def_paths
    assert "checkout.py" in ref_paths

    class_found = index.search_symbol("LineItem")
    assert any(d["path"] == "models.py" and d["kind"] == "class" for d in class_found["definitions"])


def test_repomap_detects_file_hash_change_and_incrementally_refreshes(tmp_path):
    ws = tmp_path / "repo"
    shutil.copytree(ROOT / "data/multi_file_repo", ws)
    index = RepoMapIndex(ws)
    first = index.build_map(max_chars=2000)
    first_digest = first["index_sha256"]

    # 未修改文件时再次刷新：stale_refreshed 为空，索引摘要保持一致
    second = index.build_map(max_chars=2000)
    assert second["stale_refreshed"] == []
    assert second["index_sha256"] == first_digest

    # 修改单个文件并新增符号：增量仅重解析被改动的 discounts.py
    target = ws / "discounts.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n\ndef clamp_discount(v: int, cap: int) -> int:\n    return min(v, cap)\n",
        encoding="utf-8",
    )
    third = index.build_map(max_chars=2000, focus_query="clamp_discount")
    assert third["stale_refreshed"] == ["discounts.py"]
    assert third["index_sha256"] != first_digest
    sym = index.search_symbol("clamp_discount")
    assert any(d["path"] == "discounts.py" for d in sym["definitions"])
