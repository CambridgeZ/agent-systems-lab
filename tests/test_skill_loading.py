"""验证 Skill 分层加载的读取成本、版本固定与目录安全边界。"""

import hashlib
import json
import os
from pathlib import Path

import pytest

from agent_lab.skills import SkillCatalog


def write_skill(root, name="code-review", description="代码审查与测试", body="# Review\n检查最小补丁。"):
    path = root / name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n", encoding="utf-8")
    return path


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_catalog_reads_bounded_header_once_without_loading_full_body(tmp_path, monkeypatch):
    path = write_skill(tmp_path, body="# Review\nPRIVATE_BODY_MARKER\n" + "x" * 12_000)
    original_open = Path.open
    reads = []

    class ObservedHeader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size=-1):
            reads.append(size)
            return self.handle.read(size)

    def observed_open(self, mode="r", *args, **kwargs):
        handle = original_open(self, mode, *args, **kwargs)
        if self == path and mode == "rb":
            return ObservedHeader(handle)
        return handle

    monkeypatch.setattr(Path, "open", observed_open)
    catalog = SkillCatalog(tmp_path)
    first = catalog.catalog()
    assert first == [{"name": "code-review", "description": "代码审查与测试"}]
    first[0]["description"] = "调用方篡改不会污染缓存"
    assert catalog.catalog()[0]["description"] == "代码审查与测试"
    assert reads == [catalog.MAX_HEADER_BYTES]
    assert "PRIVATE_BODY_MARKER" not in str(catalog.catalog())
    assert "PRIVATE_BODY_MARKER" in catalog.load("code-review")


def test_header_closing_marker_must_fit_bounded_header(tmp_path):
    path = write_skill(tmp_path)
    path.write_text("---\nname: code-review\ndescription: review\n" + "# padding\n" * 500 + "---\n正文")
    catalog = SkillCatalog(tmp_path)
    assert path.stat().st_size < catalog.MAX_BYTES
    assert catalog.catalog() == []
    with pytest.raises(ValueError, match="有界结束标记"):
        catalog._header("code-review")


def test_metadata_and_body_cache_invalidate_even_with_same_size_and_mtime(tmp_path):
    path = write_skill(tmp_path, description="Alpha review", body="# Review\nVersion A")
    catalog = SkillCatalog(tmp_path)
    old_metadata = catalog.catalog()
    old_snapshot = catalog.snapshot("code-review")
    old_stat = path.stat()
    replacement = path.with_suffix(".replacement")
    replacement.write_text(path.read_text().replace("Alpha", "Bravo").replace("Version A", "Version B"))
    os.utime(replacement, ns=(old_stat.st_atime_ns, old_stat.st_mtime_ns))
    replacement.replace(path)
    assert path.stat().st_size == old_stat.st_size
    assert path.stat().st_mtime_ns == old_stat.st_mtime_ns
    assert catalog.catalog() != old_metadata
    current = catalog.snapshot("code-review")
    assert current["content"] == "# Review\nVersion B"
    assert current["sha256"] != old_snapshot["sha256"]
    assert current["content_sha256"] != old_snapshot["content_sha256"]
    # 已保存快照是值，不会随磁盘 Skill 变动而悄悄改变。
    assert old_snapshot["content"] == "# Review\nVersion A"


def test_same_version_body_load_reuses_cache_and_copy_is_not_mutable_alias(tmp_path, monkeypatch):
    path = write_skill(tmp_path)
    original_read = Path.read_text
    reads = []

    def observed_read(self, *args, **kwargs):
        if self == path:
            reads.append(self)
        return original_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", observed_read)
    catalog = SkillCatalog(tmp_path)
    first = catalog.snapshot("code-review")
    first["content"] = "tampered by caller"
    second = catalog.snapshot("code-review")
    assert second["content"] == "# Review\n检查最小补丁。"
    assert len(reads) == 1


def test_body_change_during_read_is_rejected_without_caching(tmp_path, monkeypatch):
    path = write_skill(tmp_path)
    original_read = Path.read_text

    def mutating_read(self, *args, **kwargs):
        text = original_read(self, *args, **kwargs)
        if self == path:
            path.write_text(text + "\n# Concurrent change\n")
        return text

    monkeypatch.setattr(Path, "read_text", mutating_read)
    catalog = SkillCatalog(tmp_path)
    with pytest.raises(ValueError, match="读取期间发生变化"):
        catalog.load("code-review")
    monkeypatch.setattr(Path, "read_text", original_read)
    assert "Concurrent change" in catalog.load("code-review")


def test_inspect_returns_sections_and_version_without_body_text(tmp_path):
    path = write_skill(
        tmp_path, body="# Review\nPRIVATE_MARKER\n## Tests\n验证边界。\n# Shipping\n记录证据。"
    )
    info = SkillCatalog(tmp_path).inspect("code-review")
    assert info["sections"] == ["Review", "Tests", "Shipping"]
    assert info["sha256"] == sha(path.read_text())
    assert "PRIVATE_MARKER" not in str(info)
    assert info["body_chars"] > 0 and "content" not in info


def test_section_snapshot_includes_children_and_stops_before_same_level_sibling(tmp_path):
    body = "# Workflow\n准备。\n## Inspect\n读代码。\n### Evidence\n保留证据。\n## Patch\n提案。\n# Safety\n审批。"
    path = write_skill(tmp_path, body=body)
    catalog = SkillCatalog(tmp_path)
    parent = catalog.snapshot("code-review", "Workflow")
    child = catalog.snapshot("code-review", "Inspect")
    assert parent["content"] == body.split("\n# Safety", 1)[0]
    assert child["content"] == "## Inspect\n读代码。\n### Evidence\n保留证据。"
    assert parent["section"] == "Workflow" and child["section"] == "Inspect"
    assert parent["sha256"] == child["sha256"] == sha(path.read_text())
    assert child["content_sha256"] == sha(child["content"])
    assert child["content_sha256"] != parent["content_sha256"]
    assert "description:" not in child["content"]


def test_section_hash_stays_stable_when_only_another_section_changes(tmp_path):
    path = write_skill(tmp_path, body="# Review\n固定内容。\n# Tests\n旧版测试。")
    catalog = SkillCatalog(tmp_path)
    before = catalog.snapshot("code-review", "Review")
    path.write_text(path.read_text().replace("旧版测试", "新版测试"))
    after = catalog.snapshot("code-review", "Review")
    assert after["sha256"] != before["sha256"]
    assert after["content_sha256"] == before["content_sha256"]
    assert after["content"] == before["content"]


@pytest.mark.parametrize("section", ["missing", "", 42, "x" * 161])
def test_invalid_section_selection_fails_closed(tmp_path, section):
    write_skill(tmp_path)
    with pytest.raises(ValueError):
        SkillCatalog(tmp_path).snapshot("code-review", section)


def test_duplicate_section_title_requires_disambiguation_instead_of_guessing(tmp_path):
    write_skill(tmp_path, body="# Review\nA\n# Review\nB")
    with pytest.raises(ValueError, match="不唯一"):
        SkillCatalog(tmp_path).snapshot("code-review", "Review")


def test_catalog_and_cache_sizes_remain_bounded(tmp_path, monkeypatch):
    for number in range(7):
        write_skill(tmp_path, name=f"skill-{number}", body=f"# Body\nVersion {number}")
    catalog = SkillCatalog(tmp_path)
    monkeypatch.setattr(catalog, "MAX_CATALOG", 5)
    monkeypatch.setattr(catalog, "CACHE_ENTRIES", 3)
    assert len(catalog.catalog()) == 5
    assert len(catalog._metadata_cache) <= 3
    for number in range(7):
        catalog.snapshot(f"skill-{number}")
    assert len(catalog._text_cache) <= 3


def test_selection_ranks_matching_metadata_and_respects_character_and_item_budgets(tmp_path):
    write_skill(tmp_path, "alpha", description="ordinary documentation")
    write_skill(tmp_path, "review", description="review patch tests")
    write_skill(tmp_path, "testing", description="testing tests")
    catalog = SkillCatalog(tmp_path)
    selected = catalog.select("review patch tests", max_chars=140, limit=2)
    assert selected[0]["name"] == "review"
    assert len(selected) <= 2
    assert len(json.dumps(selected, ensure_ascii=False)) <= 140
    assert all(set(item) == {"name", "description"} for item in selected)
    assert catalog.select("review", max_chars=1, limit=8) == []


def test_zero_selection_limit_does_not_load_any_metadata(tmp_path):
    write_skill(tmp_path)
    assert SkillCatalog(tmp_path).select("review", limit=0) == []


def test_disallowed_paths_are_rejected_by_snapshot_inspect_and_discovery(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    external = write_skill(tmp_path / "external", "secret")
    (root / "secret").symlink_to(external.parent, target_is_directory=True)
    hardlinked = root / "hardlinked" / "SKILL.md"
    hardlinked.parent.mkdir()
    os.link(external, hardlinked)
    catalog = SkillCatalog(root)
    assert catalog.catalog() == [] and catalog.select("secret") == []
    for name in ("../external/secret", "secret", "hardlinked"):
        with pytest.raises(ValueError):
            catalog.snapshot(name)
        with pytest.raises(ValueError):
            catalog.inspect(name)
