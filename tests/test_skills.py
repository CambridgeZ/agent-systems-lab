"""Skill 目录只预载元数据；正文需显式读取，并受路径和大小约束。"""

import pytest

from agent_lab.skills import SkillCatalog


def write_skill(
    root, name="code-review", description="检查补丁与验证证据", body="仅在读取代码后提出最小补丁。"
):
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "SKILL.md"
    path.write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# 正文\n{body}\n", encoding="utf-8"
    )
    return path


def test_catalog_discloses_metadata_only_until_explicit_load(tmp_path):
    body = "PRIVATE_BODY_MARKER 先阅读仓库再提出补丁，审批之前不得写入。"
    write_skill(tmp_path, body=body)
    catalog = SkillCatalog(tmp_path)
    assert catalog.catalog() == [{"name": "code-review", "description": "检查补丁与验证证据"}]
    assert body not in str(catalog.catalog())
    assert body in catalog.load("code-review")


@pytest.mark.parametrize(
    "name", ["../outside", "/etc/passwd", "code-review/SKILL.md", "CodeReview", "", "x" * 65, None]
)
def test_malicious_or_invalid_skill_names_rejected(tmp_path, name):
    with pytest.raises(ValueError):
        SkillCatalog(tmp_path).load(name)


def test_symlink_folder_and_file_cannot_read_external_material(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    external = tmp_path / "external"
    path = write_skill(external, "secret", body="secret_body")
    (root / "secret").symlink_to(path.parent, target_is_directory=True)
    (root / "linked").mkdir()
    (root / "linked/SKILL.md").symlink_to(path)
    catalog = SkillCatalog(root)
    assert catalog.catalog() == []
    for name in ("secret", "linked"):
        with pytest.raises(ValueError, match="符号链接"):
            catalog.load(name)


def test_invalid_metadata_and_oversize_skill_omitted_from_catalog(tmp_path):
    write_skill(tmp_path, "valid")
    bad = write_skill(tmp_path, "mismatch")
    bad.write_text(bad.read_text().replace("name: mismatch", "name: another"))
    write_skill(tmp_path, "large", body="x" * 25000)
    missing = tmp_path / "missing-meta"
    missing.mkdir()
    (missing / "SKILL.md").write_text("# No metadata\nbody")
    catalog = SkillCatalog(tmp_path)
    assert [entry["name"] for entry in catalog.catalog()] == ["valid"]
    for name in ("mismatch", "large", "missing-meta", "absent"):
        with pytest.raises(ValueError):
            catalog.load(name)


def test_quoted_description_preserves_colons(tmp_path):
    write_skill(tmp_path, description='"检查顺序: 读取代码、提案、审批"')
    assert SkillCatalog(tmp_path).catalog()[0]["description"] == "检查顺序: 读取代码、提案、审批"


def test_multiline_yaml_marker_is_not_a_supported_single_line_description(tmp_path):
    write_skill(tmp_path, description="|")
    assert SkillCatalog(tmp_path).catalog() == []
    with pytest.raises(ValueError):
        SkillCatalog(tmp_path).load("code-review")


def test_missing_skill_directory_has_empty_catalog(tmp_path):
    assert SkillCatalog(tmp_path / "missing").catalog() == []


def test_duplicate_metadata_is_rejected_instead_of_silently_overridden(tmp_path):
    path = write_skill(tmp_path)
    path.write_text(path.read_text().replace("description:", "name: code-review\ndescription:", 1))
    catalog = SkillCatalog(tmp_path)
    assert catalog.catalog() == []
    with pytest.raises(ValueError, match="不能重复"):
        catalog.load("code-review")
