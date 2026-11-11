"""Skill 渐进披露：先给模型元数据，选中后才加载正文。

这里只读取受版本控制的 Markdown 指令，不执行 Skill 内的脚本。为保持最小依赖，
元数据只支持 name/description 两个单行字符串，不声称实现完整 YAML 或 Skills 标准。
"""

from __future__ import annotations

from pathlib import Path
from collections import OrderedDict
import hashlib
import re


class SkillCatalog:
    """受限的本地 Skill 目录，避免任意路径读文件和无界提示注入面。"""

    MAX_BYTES = 24_000
    MAX_HEADER_BYTES = 4096
    MAX_CATALOG = 32
    CACHE_ENTRIES = 32

    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self._text_cache: OrderedDict[tuple, str] = OrderedDict()
        self._metadata_cache: dict[str, tuple[tuple, dict]] = {}

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

    def _signature(self, name: str) -> tuple[Path, tuple]:
        path = self._path(name)
        if not path.is_file():
            raise ValueError(f"Skill 不存在：{name}")
        stat = path.stat()
        if stat.st_nlink != 1:
            raise ValueError("Skill 不允许多重硬链接")
        if stat.st_size > self.MAX_BYTES:
            raise ValueError("Skill 正文超过 24000 字节限制")
        return path, (name, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)

    def _read(self, name: str) -> str:
        path, signature = self._signature(name)
        if signature in self._text_cache:
            self._text_cache.move_to_end(signature)
            return self._text_cache[signature]
        text = path.read_text(encoding="utf-8")
        if self._signature(name)[1] != signature:
            raise ValueError("Skill 在读取期间发生变化，请重新加载")
        self._text_cache[signature] = text
        while len(self._text_cache) > self.CACHE_ENTRIES:
            self._text_cache.popitem(last=False)
        return text

    def _header(self, name: str) -> dict:
        """目录阶段只读取有界 frontmatter，避免每轮扫描所有 Skill 正文。"""
        path, signature = self._signature(name)
        cached = self._metadata_cache.get(name)
        if cached and cached[0] == signature:
            return dict(cached[1])
        with path.open("rb") as handle:
            header = handle.read(self.MAX_HEADER_BYTES).decode("utf-8", errors="ignore")
        # 只将元数据结束标记之前的部分交给解析器，不缓存或暴露正文。
        end = header.find("\n---", 4)
        if end == -1:
            raise ValueError("Skill 元数据缺少有界结束标记")
        metadata = self._metadata(header[: end + 4], name)
        if self._signature(name)[1] != signature:
            raise ValueError("Skill 元数据在读取期间发生变化")
        if len(self._metadata_cache) >= self.CACHE_ENTRIES:
            self._metadata_cache.pop(next(iter(self._metadata_cache)))
        self._metadata_cache[name] = (signature, metadata)
        return dict(metadata)

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
                metadata = self._header(folder.name)
            except (ValueError, UnicodeError, OSError):
                continue
            result.append(metadata)
            if len(result) >= self.MAX_CATALOG:
                break
        return result

    def load(self, name: str) -> str:
        """显式按名称加载完整内容；调用方仍须把它放在低于系统策略的信任级别。"""
        text = self._read(name)
        self._metadata(text, name)
        return text

    def inspect(self, name: str) -> dict:
        """第二层仅返回章节索引和版本；正文由 snapshot 显式选择。"""
        text = self.load(name)
        metadata = self._metadata(text, name)
        body = text[text.find("\n---", 4) + 4 :].strip()
        headings = self._headings(body)
        return {
            **metadata,
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "sections": [match.group(2) for match in headings],
            "body_chars": len(body),
        }

    def snapshot(self, name: str, section: str | None = None) -> dict:
        """读取可固定到一次 run 的不可变快照；可只加载一个 Markdown 章节。"""
        text = self.load(name)
        body = text[text.find("\n---", 4) + 4 :].strip()
        if section is not None:
            if not isinstance(section, str) or not section or len(section) > 160:
                raise ValueError("section 必须是目录里的章节标题")
            headings = self._headings(body)
            matches = [i for i, match in enumerate(headings) if match.group(2) == section]
            if len(matches) != 1:
                raise ValueError("章节不存在或标题不唯一，请先调用 skill_inspect")
            index = matches[0]
            match = headings[index]
            end = next(
                (
                    other.start()
                    for other in headings[index + 1 :]
                    if len(other.group(1)) <= len(match.group(1))
                ),
                len(body),
            )
            body = body[match.start() : end].strip()
        return {
            "name": name,
            "section": section,
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "content": body,
        }

    @staticmethod
    def _headings(body: str) -> list:
        # 仅支持 ATX 标题，但须跳过 fenced code 内的 #，避免加载错误章节。
        headings = []
        fence = None
        offset = 0
        heading = re.compile(r"(#{1,6})[ \t]+(.+?)[ \t]*(?:\r?\n|$)")
        for line in body.splitlines(keepends=True):
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
            if marker:
                token = marker.group(1)
                if fence is None:
                    fence = token
                elif token[0] == fence[0] and len(token) >= len(fence) and not line[marker.end() :].strip():
                    fence = None
            elif fence is None:
                match = heading.match(body, offset)
                if match:
                    # 使用原始文本位置，snapshot 才能按原样保留源码/换行。
                    headings.append(match)
            offset += len(line)
        return headings

    def select(self, query: str, max_chars: int = 2400, limit: int = 8) -> list[dict]:
        """有界词面技能发现，不把所有元数据常驻 prompt，也不冒称语义路由。"""
        import json

        if not isinstance(query, str):
            raise ValueError("query 必须是字符串")
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (max_chars, limit)):
            raise ValueError("max_chars/limit 必须是非负整数")
        if limit == 0 or max_chars < 2:
            return []
        tokens = set(re.findall(r"[a-z0-9_-]+|[\u4e00-\u9fff]", query.casefold()))
        ranked = sorted(
            self.catalog(),
            key=lambda item: (
                -sum(token in (item["name"] + " " + item["description"]).casefold() for token in tokens),
                item["name"],
            ),
        )
        selected = []
        for item in ranked:
            if len(json.dumps(selected + [item], ensure_ascii=False)) > max_chars:
                continue
            selected.append(item)
            if len(selected) >= limit:
                break
        return selected
