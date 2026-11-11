"""Skill 渐进披露：先给模型元数据，选中后才加载正文。

这里只读取受版本控制的 Markdown 指令，不执行 Skill 内的脚本。为保持最小依赖，
元数据只支持 name/description 两个单行字符串，不声称实现完整 YAML 或 Skills 标准。
"""

from __future__ import annotations

from pathlib import Path
import re


class SkillCatalog:
    """受限的本地 Skill 目录，避免任意路径读文件和无界提示注入面。"""

    MAX_BYTES = 24_000

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()

    def _path(self, name: str) -> Path:
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", name):
            raise ValueError("Skill 名称只能包含小写字母、数字和短横线")
        path = self.directory / name / "SKILL.md"
        # 即使末端文件不是 symlink，父目录也可能是；逐层拒绝更容易审计。
        if (self.directory / name).is_symlink() or path.is_symlink():
            raise ValueError("Skill 不允许符号链接")
        if not path.resolve().is_relative_to(self.directory):
            raise ValueError("Skill 路径越界")
        return path

    def _read(self, name: str) -> str:
        path = self._path(name)
        if not path.is_file():
            raise ValueError(f"Skill 不存在：{name}")
        if path.stat().st_size > self.MAX_BYTES:
            raise ValueError("Skill 正文超过 24000 字节限制")
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _metadata(text: str, folder: str) -> dict:
        if not text.startswith("---\n"):
            raise ValueError("SKILL.md 缺少 name/description 元数据")
        end = text.find("\n---", 4)
        if end == -1:
            raise ValueError("Skill 元数据缺少结束标记")
        metadata: dict[str, str] = {}
        for line in text[4:end].splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, separator, value = line.partition(":")
            if separator and key.strip() in {"name", "description"}:
                if key.strip() in metadata:
                    raise ValueError("Skill 元数据字段不能重复")
                value = value.strip()
                if value in {"|", "|-", "|+", ">", ">-", ">+"}:
                    raise ValueError("Skill 仅支持单行 name/description，不支持 YAML 多行标记")
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                    value = value[1:-1]
                metadata[key.strip()] = value
        if metadata.get("name") != folder or not metadata.get("description"):
            raise ValueError("Skill name 必须等于目录名，description 不能为空")
        if len(metadata["description"]) > 600:
            raise ValueError("Skill 描述不能超过 600 字符")
        return metadata

    def catalog(self) -> list[dict]:
        """返回元数据，不向模型预载正文；损坏的 Skill 不进入可用目录。"""
        result = []
        if not self.directory.is_dir():
            return result
        for folder in sorted(self.directory.iterdir()):
            if not folder.is_dir() or folder.is_symlink():
                continue
            try:
                metadata = self._metadata(self._read(folder.name), folder.name)
            except (ValueError, UnicodeError, OSError):
                continue
            result.append(metadata)
        return result

    def load(self, name: str) -> str:
        """显式按名称加载完整内容；调用方仍须把它放在低于系统策略的信任级别。"""
        text = self._read(name)
        self._metadata(text, name)
        return text
