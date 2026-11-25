"""多通道技能路由器（Multi-Channel Skill Router）与三层渐进披露目录。

================================================================================
模块导读（Module Guide）
================================================================================
1. 架构定位：Anthropic Agent Skills 三层渐进披露（3-Level Progressive Disclosure）
   在现代 Coding Agent（如 Claude Code / Cursor）中，技能库往往沉淀了数十份涵盖代码审查、数据库迁移、
   安全审计、发布上线等领域的长篇 SOP（单篇可达数千至上万字符）。若在会话启动时将全部 `SKILL.md` 正文塞入
   System Prompt，不仅瞬间挤爆上下文窗口、拉低推理信噪比，还会破坏跨任务的 KV Cache 前缀共享。
   本模块严格遵循 **Anthropic Agent Skills 三层渐进披露范式**：
   - **Level 1 — 有界元数据目录（Bounded Metadata Routing，`catalog()` / `select()`）**：
     启动阶段仅读取每个 `SKILL.md` 头部最多 `4096` 字节的受限单行 Frontmatter，绝不触碰或缓存正文；
     通过多通道确定性路由器筛选出不超过 `limit` 条、`max_chars` 字符预算的精简目录注入提示词。
   - **Level 2 — 章节结构探针（Section Index Inspection，`inspect()`）**：
     模型判断某项技能潜在相关时，先调用 `skill_inspect` 获取内容寻址哈希（`sha256`）、正文字符规模与跳过
     围栏代码块伪标题后的真实 ATX 章节树（`sections`），零正文注入地完成“看目录选章节”。
   - **Level 3 — 按需章节快照（Pinned/Tail Section Snapshot，`snapshot()` / `load()`）**：
     调用 `skill_load` 将完整技能或指定单节固化为版本锚定的不可变快照（运行期中途加载自动挂载至对话尾部瞬态区，
     不撼动历史消息前缀的 KV Cache）；始终置于普通 `user` 数据层，绝不提升为 `system` 特权。

2. 四通道确定性技能路由（显式 `/skill` 调用 vs. 环境路径 Glob vs. 混合 RRF 检索）：
   `SkillCatalog.select()` 将业界主流 Agent 的路由信号融合为单一、可复现的确定性优先级阶梯：
   - **通道 A（最高优：显式命令与常驻规则）**：
     识别用户在 Prompt 中显式键入的 `/<skill-name>` 或 `$<skill-name>` 令牌（`extract_explicit_skill_names`），
     或 Frontmatter 声明 `always-apply: true` 的全局工程规范。注意：标记了 `disable-model-invocation: true`
     的高风险操作型技能（如生产发版 `/deploy-prod`）在常规环境路由中会被彻底屏蔽，**唯有**用户显式输入
     `/deploy-prod` 或 `$deploy-prod` 时才允许解锁入选，防止模型自主误触发运维流程。
   - **通道 B（次高优：Cursor 风格 `paths` / `globs` 文件上下文激活）**：
     当技能声明了 `paths`（或同义键 `globs`，如 `src/**/*.py, tests/*.py, *.sql`），路由器同时提取调用方传入的
     工作区活动文件列表（`workspace_files`）与用户 Prompt 中提及的文件路径片段，通过兼容零层子目录展开（`/**/`）
     的 `fnmatch` + `PurePosixPath.match` 匹配自动激活对应技术栈技能。
   - **通道 C（主体召回：BM25 + TF-IDF 子词余弦 + RRF 混合检索 + 单字/短词词面兜底）**：
     复用 `KnowledgeBase.from_documents` 对 `name + description + triggers` 建立临时混合索引，利用驼峰/蛇形/连字符
     子词切分（如查询 `exponentialBackoff` 命中 `exponential_backoff` 触发词）与 `k=60` 倒数排名融合（RRF）排序；
     同时保留针对中日韩单字（如“锁”、“测”）及短词子串的词面重合度回退分，确保单字或短词查询只要词面存在就绝不得 0 分。
   - **通道 D（确定性字典序决胜）**：
     所有同分候选严格按 `item["name"]` ASCII 字典序升序排列，结合向后兼容的可选字段按需挂载策略（未声明新字段的旧版
     `SKILL.md` 仍精确产出 `{"name": ..., "description": ...}`），确保既有仓库的 KV Cache 块哈希 100% 字节级恒定。
================================================================================
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable, Sequence
import contextlib
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shlex
import shutil
import stat as stat_mod
from typing import Any

from .retrieval import KnowledgeBase

# 识别用户显式调用的 `/<skill-name>` 或 `$<skill-name>` 令牌：
# 左侧负向后行断言排除路径分隔符、协议冒号与标识符字符（避免将 `src/code-review/a.py`、`./code-review`
# 或 `https://x.io/code-review` 误判为技能指令）；右侧负向前瞻排除紧随 `/` 或标识符续接字符。
_EXPLICIT_SKILL_RE = re.compile(
    r"(?<![A-Za-z0-9_.:/\\-])[/\$]([a-z][a-z0-9-]{0,63})(?![A-Za-z0-9_/-])"
)

# 从自然语言 Prompt 中提取潜在文件路径片段（如 `src/agent_lab/api.py`、`.github/workflows/ci.yml`、`pricing.py`），
# 供 Cursor 风格 `paths` / `globs` 通道在不额外传入 workspace_files 时也能感知目标文件类型。
_PROMPT_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.:/\\-])"
    r"(?:(?:\./)?(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.*-]+|[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,10})"
    r"(?![A-Za-z0-9_])"
)

_FRONTMATTER_KEY_ALIASES: dict[str, str] = {
    "name": "name",
    "description": "description",
    "triggers": "triggers",
    "tags": "triggers",
    "paths": "paths",
    "globs": "paths",
    "disable-model-invocation": "disable_model_invocation",
    "disable_model_invocation": "disable_model_invocation",
    "always-apply": "always_apply",
    "always_apply": "always_apply",
}

_YAML_MULTILINE_MARKERS = frozenset({"|", "|-", "|+", ">", ">-", ">+"})
_SKILL_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,63}")
_ARG_PLACEHOLDER_RE = re.compile(
    r"(?<![A-Za-z0-9_\\$])\$(?:\{(ARGUMENTS|[1-9][0-9]*)\}|(ARGUMENTS|[1-9][0-9]*)(?![A-Za-z0-9_]))"
)
MANIFEST_FILENAME = ".manifest.json"
_ASSET_SUBDIRS = ("references", "templates")


def _validate_skill_name(name: Any) -> str:
    if not isinstance(name, str) or not _SKILL_NAME_RE.fullmatch(name):
        raise ValueError("Skill 名称只能包含小写字母、数字和短横线")
    return name


def _interpolate_arguments(template: str, arguments: str) -> str:
    """单遍插值 `$ARGUMENTS`、`${ARGUMENTS}` 与 `$1`/`$2`/... 位置参数，杜绝二次展开注入。"""
    if not arguments:
        return template
    try:
        pos_args = shlex.split(arguments)
    except ValueError:
        pos_args = arguments.split()

    def _replace(match: re.Match[str]) -> str:
        token = match.group(1) or match.group(2)
        if token == "ARGUMENTS":
            return arguments
        index = int(token)
        return pos_args[index - 1] if 1 <= index <= len(pos_args) else ""

    return _ARG_PLACEHOLDER_RE.sub(_replace, template)


def _unquote_scalar(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_csv_frontmatter_list(
    raw_value: str,
    *,
    field_name: str,
    max_items: int = 32,
    max_item_chars: int = 120,
) -> list[str]:
    """解析单行逗号分隔列表（兼容 `a, b`、`"a, b"` 与 `[a, "b"]` 三种常见 Frontmatter 写法）。"""
    stripped = raw_value.strip()
    if len(stripped) >= 2 and stripped[0] == "[" and stripped[-1] == "]":
        inner = stripped[1:-1]
    else:
        inner = _unquote_scalar(stripped)
    items: list[str] = []
    seen: set[str] = set()
    for part in inner.split(","):
        cleaned = _unquote_scalar(part.strip()).strip()
        if not cleaned:
            continue
        if "\x00" in cleaned or len(cleaned) > max_item_chars:
            raise ValueError(f"Skill {field_name} 条目非法或超过 {max_item_chars} 字符上限")
        if cleaned not in seen:
            seen.add(cleaned)
            items.append(cleaned)
    if len(items) > max_items:
        raise ValueError(f"Skill {field_name} 条目数量不能超过 {max_items}")
    return items


def _normalize_rel_posix(raw_path: str) -> str:
    norm = raw_path.replace("\\", "/").strip()
    while norm.startswith("./"):
        norm = norm[2:]
    return norm.lstrip("/")


def _path_matches_glob(candidate_path: str, pattern: str) -> bool:
    """用 `fnmatch` + `PurePosixPath.match` 匹配文件路径与技能 Glob（兼容 `/**/` 零层子目录语义）。"""
    rel = _normalize_rel_posix(candidate_path)
    pat = _normalize_rel_posix(pattern)
    if not rel or not pat:
        return False

    expanded_patterns = {pat}
    # 标准 fnmatch 与旧版 PurePosixPath.match 中 `src/**/*.py` 往往要求至少一层中间目录；
    # 显式展开零层目录变体 `src/*.py` 与根匹配变体，对齐 Cursor / gitignore 的直觉语义。
    queue = [pat]
    while queue:
        cur = queue.pop()
        if "/**/" in cur:
            collapsed = cur.replace("/**/", "/", 1)
            if collapsed not in expanded_patterns:
                expanded_patterns.add(collapsed)
                queue.append(collapsed)
        if cur.startswith("**/") and cur[3:] and cur[3:] not in expanded_patterns:
            expanded_patterns.add(cur[3:])
            queue.append(cur[3:])
        if cur.endswith("/**") and (cur[:-3] + "/*") not in expanded_patterns:
            expanded_patterns.add(cur[:-3] + "/*")

    posix_rel = PurePosixPath(rel)
    for exp_pat in expanded_patterns:
        if fnmatch.fnmatchcase(rel, exp_pat):
            return True
        with contextlib.suppress(ValueError):
            if posix_rel.match(exp_pat):
                return True
        # 若模式本身不含目录分隔符（如 `*.sql`），允许直接匹配任意层级下的文件名
        if "/" not in exp_pat and fnmatch.fnmatchcase(posix_rel.name, exp_pat):
            return True
    return False


def _copy_skill_meta(meta: dict[str, Any]) -> dict[str, Any]:
    """深拷贝列表型元数据字段，防止调用方原地修改返回字典污染 `_metadata_cache`。"""
    return {k: list(v) if isinstance(v, list) else v for k, v in meta.items()}


class SkillCatalog:
    """受限的本地 Skill 目录与多通道确定性路由器，避免任意路径读文件和无界提示注入面。

    分层根目录优先级（Multi-Tier Roots）：
    - 单路径调用 `SkillCatalog(directory)` 保持 100% 向后兼容；
    - 传入 `extra_dirs`（例如 `[<workspace>/.claude/skills, <state_dir>/skills]`）时，
      按 `extra_dirs[0] > extra_dirs[1] > ... > directory`（项目级覆盖用户级、用户级覆盖内置级）解析同名技能；
    - 仅声明 `name/description` 的既有 `SKILL.md` 元数据仍精确产出 `{"name", "description"}`，
      保障跨会话 KV Cache 前缀 100% 字节恒定。
    """

    MAX_BYTES = 24_000
    MAX_HEADER_BYTES = 4096
    MAX_CATALOG = 32
    CACHE_ENTRIES = 32
    MAX_ASSET_BYTES = 64_000
    MAX_ASSETS = 64
    ASSET_SUBDIRS = _ASSET_SUBDIRS
    _CLOSING_FRONTMATTER = re.compile(r"\r?\n---(?:\r?\n|$)")

    def __init__(
        self,
        directory: str | Path,
        extra_dirs: Sequence[str | Path] | None = None,
    ):
        raw_base = Path(directory)
        self.directory = raw_base.resolve()

        raw_extras: list[Path] = []
        if extra_dirs is not None:
            if isinstance(extra_dirs, (str, bytes, Path)) or not isinstance(extra_dirs, Sequence):
                raise ValueError("extra_dirs 必须是路径序列")
            for item in extra_dirs:
                if not isinstance(item, (str, Path)):
                    raise ValueError("extra_dirs 元素必须是路径字符串或 Path")
                raw_extras.append(Path(item))

        # 优先级由高到低：extra_dirs（如 project > user）覆盖基础 directory（built-in）
        root_pairs: list[tuple[Path, Path, bool]] = []
        seen_resolved: set[Path] = set()
        for raw_dir, is_extra in [(p, True) for p in raw_extras] + [(raw_base, False)]:
            resolved_dir = raw_dir.resolve()
            if resolved_dir in seen_resolved:
                continue
            seen_resolved.add(resolved_dir)
            root_pairs.append((raw_dir, resolved_dir, is_extra))

        self._root_pairs: tuple[tuple[Path, Path, bool], ...] = tuple(root_pairs)
        self.extra_dirs: tuple[Path, ...] = tuple(p.resolve() for p in raw_extras)
        self.roots: tuple[Path, ...] = tuple(resolved for _, resolved, _ in root_pairs)
        self._text_cache: OrderedDict[tuple, str] = OrderedDict()
        self._metadata_cache: dict[str, tuple[tuple, dict[str, Any]]] = {}

    def _invalidate_name(self, name: str) -> None:
        self._metadata_cache.pop(name, None)
        stale_keys = [key for key in self._text_cache if key and key[0] == name]
        for key in stale_keys:
            self._text_cache.pop(key, None)

    def _path(self, name: str) -> Path:
        _validate_skill_name(name)
        for raw_root, root, is_extra in self._root_pairs:
            if is_extra and raw_root.is_symlink():
                continue
            folder = root / name
            path = folder / "SKILL.md"
            # 即使末端文件不是 symlink，父目录也可能是；逐层拒绝更容易审计。
            if folder.is_symlink() or path.is_symlink():
                raise ValueError("Skill 不允许符号链接")
            if not path.resolve().is_relative_to(root):
                raise ValueError("Skill 路径越界")
            if path.exists():
                return path
        fallback = self.directory / name / "SKILL.md"
        if not fallback.resolve().is_relative_to(self.directory):
            raise ValueError("Skill 路径越界")
        return fallback

    def _signature(self, name: str) -> tuple[Path, tuple]:
        path = self._path(name)
        if not path.is_file():
            raise ValueError(f"Skill 不存在：{name}")
        stat = path.stat()
        if stat.st_nlink != 1:
            raise ValueError("Skill 不允许多重硬链接")
        if stat.st_size > self.MAX_BYTES:
            raise ValueError("Skill 正文超过 24000 字节限制")
        return path, (name, str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)

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

    @classmethod
    def _frontmatter_bounds(
        cls,
        text: str,
        *,
        missing_end_message: str = "Skill 元数据缺少结束标记",
    ) -> tuple[int, int, int]:
        if text.startswith("---\n"):
            content_start = 4
        elif text.startswith("---\r\n"):
            content_start = 5
        else:
            raise ValueError("SKILL.md 缺少 name/description 元数据")
        match = cls._CLOSING_FRONTMATTER.search(text, 3)
        if match is None:
            raise ValueError(missing_end_message)
        return content_start, match.start(), match.end()

    def _header(self, name: str) -> dict[str, Any]:
        """目录阶段只读取有界 frontmatter，避免每轮扫描所有 Skill 正文。"""
        path, signature = self._signature(name)
        cached = self._metadata_cache.get(name)
        if cached and cached[0] == signature:
            return _copy_skill_meta(cached[1])
        with path.open("rb") as handle:
            raw_header = handle.read(self.MAX_HEADER_BYTES)
        header = raw_header.decode("utf-8", errors="ignore")
        # 只将元数据结束标记之前的部分交给解析器，不缓存或暴露正文。
        _, _, header_end = self._frontmatter_bounds(
            header, missing_end_message="Skill 元数据缺少有界结束标记"
        )
        if len(raw_header) == self.MAX_HEADER_BYTES and not header[:header_end].endswith(("\n", "\r")):
            raise ValueError("Skill 元数据缺少有界结束标记")
        metadata = self._metadata(header[:header_end], name)
        if self._signature(name)[1] != signature:
            raise ValueError("Skill 元数据在读取期间发生变化")
        if len(self._metadata_cache) >= self.CACHE_ENTRIES:
            self._metadata_cache.pop(next(iter(self._metadata_cache)))
        self._metadata_cache[name] = (signature, _copy_skill_meta(metadata))
        return _copy_skill_meta(metadata)

    @staticmethod
    def _metadata(text: str, folder: str) -> dict[str, Any]:
        """解析受限单行 Frontmatter，仅在显式声明可选路由字段时才挂载对应键以严守 KV Cache 前缀兼容。"""
        content_start, content_end, _ = SkillCatalog._frontmatter_bounds(text)
        metadata: dict[str, Any] = {}
        for line in text[content_start:content_end].splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            key, separator, value = line.partition(":")
            if not separator:
                continue
            canonical_key = _FRONTMATTER_KEY_ALIASES.get(key.strip().lower())
            if canonical_key is None:
                continue
            if canonical_key in metadata:
                raise ValueError("Skill 元数据字段不能重复")
            raw_val = value.strip()
            if raw_val in _YAML_MULTILINE_MARKERS:
                raise ValueError("Skill 仅支持单行元数据，不支持 YAML 多行标记")

            if canonical_key in {"name", "description"}:
                metadata[canonical_key] = _unquote_scalar(raw_val)
            elif canonical_key == "triggers":
                metadata["triggers"] = _parse_csv_frontmatter_list(raw_val, field_name="triggers")
            elif canonical_key == "paths":
                raw_globs = _parse_csv_frontmatter_list(raw_val, field_name="paths")
                norm_globs: list[str] = []
                for pat in raw_globs:
                    norm_pat = _normalize_rel_posix(pat)
                    if not norm_pat or ".." in PurePosixPath(norm_pat).parts:
                        raise ValueError("Skill paths/globs 包含非法或越界模式")
                    if norm_pat not in norm_globs:
                        norm_globs.append(norm_pat)
                metadata["paths"] = norm_globs
            elif canonical_key in {"disable_model_invocation", "always_apply"}:
                bool_str = _unquote_scalar(raw_val).strip().lower()
                if bool_str == "true":
                    metadata[canonical_key] = True
                elif bool_str == "false":
                    metadata[canonical_key] = False
                else:
                    raise ValueError(f"Skill {canonical_key} 仅支持布尔值 true 或 false")

        if metadata.get("name") != folder or not metadata.get("description"):
            raise ValueError("Skill name 必须等于目录名，description 不能为空")
        if len(metadata["description"]) > 600:
            raise ValueError("Skill 描述不能超过 600 字符")
        return metadata

    def _discover_entries(self) -> tuple[list[str], set[str]]:
        """按根目录优先级发现去重后的候选技能名与已跳过的非法条目名。"""
        claimed: set[str] = set()
        blocked: set[str] = set()
        invalid_names: set[str] = set()
        empty_candidates: set[str] = set()

        for raw_root, root, is_extra in self._root_pairs:
            if (is_extra and raw_root.is_symlink()) or not root.is_dir():
                continue
            try:
                children = sorted(root.iterdir())
            except OSError:
                continue
            for entry in children:
                name = entry.name
                if name in claimed or name in blocked:
                    continue
                if entry.is_symlink():
                    blocked.add(name)
                    invalid_names.add(name)
                    continue
                if not entry.is_dir():
                    continue
                skill_file = entry / "SKILL.md"
                if skill_file.is_symlink():
                    blocked.add(name)
                    invalid_names.add(name)
                    continue
                if not skill_file.exists():
                    if len(self._root_pairs) == 1:
                        invalid_names.add(name)
                    else:
                        empty_candidates.add(name)
                    continue
                claimed.add(name)

        for orphan in empty_candidates:
            if orphan not in claimed and orphan not in blocked:
                invalid_names.add(orphan)
        return sorted(claimed), invalid_names

    def catalog(self) -> list[dict]:
        """返回元数据，不向模型预载正文；损坏的 Skill 不进入可用目录。"""
        result: list[dict] = []
        if self.MAX_CATALOG <= 0:
            return result
        names, _ = self._discover_entries()
        for name in names:
            try:
                metadata = self._header(name)
            except (ValueError, UnicodeError, OSError):
                continue
            result.append(metadata)
            if len(result) >= self.MAX_CATALOG:
                break
        return result

    def catalog_status(self) -> dict:
        """返回目录元数据及截断/跳过诊断信息，避免超过上限或损坏时静默截断。"""
        skills: list[dict] = []
        total_discovered = 0
        max_catalog = max(0, self.MAX_CATALOG)
        names, invalid_names = self._discover_entries()
        for name in names:
            try:
                metadata = self._header(name)
            except (ValueError, UnicodeError, OSError):
                invalid_names.add(name)
                continue
            total_discovered += 1
            if len(skills) < max_catalog:
                skills.append(metadata)
        return {
            "skills": skills,
            "total_discovered": total_discovered,
            "truncated": total_discovered > len(skills),
            "invalid_skipped": sorted(invalid_names),
            "max_catalog": self.MAX_CATALOG,
        }

    def extract_explicit_skill_names(self, prompt: str) -> list[str]:
        """检测并提取 Prompt 中显式引用的 `/<skill-name>` 或 `$<skill-name>` 已知技能列表。

        安全与权限语义：
        - 仅返回有效分层根目录下真实存在、通过软链/硬链与 Frontmatter 完整性校验的合法技能名；
        - 包含声明了 `disable-model-invocation: true` 的受控技能（显式指令代表用户主动授权调起该工作流）。
        """
        if not isinstance(prompt, str):
            raise ValueError("prompt 必须是字符串")
        if not prompt or not any(
            not (is_extra and raw_root.is_symlink()) and root.is_dir()
            for raw_root, root, is_extra in self._root_pairs
        ):
            return []
        found: list[str] = []
        seen: set[str] = set()
        for match in _EXPLICIT_SKILL_RE.finditer(prompt):
            candidate = match.group(1)
            if candidate in seen:
                continue
            try:
                self._header(candidate)
            except (ValueError, UnicodeError, OSError):
                continue
            seen.add(candidate)
            found.append(candidate)
        return found

    def load(self, name: str) -> str:
        """显式按名称加载完整内容；调用方仍须把它放在低于系统策略的信任级别。"""
        text = self._read(name)
        self._metadata(text, name)
        return text

    def inspect(self, name: str) -> dict:
        """第二层仅返回章节索引和版本；正文由 snapshot 显式选择。"""
        text = self.load(name)
        metadata = self._metadata(text, name)
        _, _, body_start = self._frontmatter_bounds(text)
        body = text[body_start:].strip()
        headings = self._headings(body)
        return {
            **metadata,
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "sections": [match.group(2) for match in headings],
            "body_chars": len(body),
        }

    def snapshot(self, name: str, section: str | None = None, arguments: str = "") -> dict:
        """读取可固定到一次 run 的不可变快照；可只加载一个 Markdown 章节并按需插值 `$ARGUMENTS`/`$1`...。"""
        if not isinstance(arguments, str):
            raise ValueError("arguments 必须是字符串")
        if len(arguments) > self.MAX_BYTES:
            raise ValueError(f"arguments 不能超过 {self.MAX_BYTES} 字符")
        text = self.load(name)
        _, _, body_start = self._frontmatter_bounds(text)
        body = text[body_start:].strip()
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
        if arguments:
            body = _interpolate_arguments(body, arguments)
        return {
            "name": name,
            "section": section,
            "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
            "content": body,
        }

    def _resolve_skill_dir(self, skill_name: str) -> Path:
        path, _ = self._signature(skill_name)
        self._header(skill_name)
        return path.parent

    def list_assets(self, skill_name: str) -> list[str]:
        """安全只读列举技能本地 `references/` 与 `templates/` 下的普通文件（自动跳过软链、硬链与越界项）。"""
        skill_dir = self._resolve_skill_dir(skill_name)
        skill_dir_resolved = skill_dir.resolve()
        assets: list[str] = []
        for subdir_name in self.ASSET_SUBDIRS:
            sub_root = skill_dir / subdir_name
            if sub_root.is_symlink() or not sub_root.is_dir():
                continue
            sub_root_resolved = sub_root.resolve()
            if not sub_root_resolved.is_relative_to(skill_dir_resolved):
                continue
            for current_dir, subdirs, files in os.walk(sub_root, followlinks=False):
                cur_path = Path(current_dir)
                kept_subdirs: list[str] = []
                for dname in sorted(subdirs):
                    if dname.startswith("."):
                        continue
                    sub_cand = cur_path / dname
                    if sub_cand.is_symlink():
                        continue
                    if not sub_cand.resolve().is_relative_to(sub_root_resolved):
                        continue
                    kept_subdirs.append(dname)
                subdirs[:] = kept_subdirs
                for fname in sorted(files):
                    if fname.startswith("."):
                        continue
                    cand = cur_path / fname
                    if cand.is_symlink():
                        continue
                    try:
                        cand_resolved = cand.resolve()
                        if not cand_resolved.is_relative_to(sub_root_resolved):
                            continue
                        st = cand.stat()
                    except OSError:
                        continue
                    if (
                        not stat_mod.S_ISREG(st.st_mode)
                        or st.st_nlink != 1
                        or st.st_size > self.MAX_ASSET_BYTES
                    ):
                        continue
                    assets.append(cand.relative_to(skill_dir).as_posix())
        assets.sort()
        return assets[: self.MAX_ASSETS]

    def read_asset(self, skill_name: str, rel_path: str) -> str:
        """安全只读读取技能本地 `references/` 或 `templates/` 中的单个资源文件，严防路径穿越、软链与硬链。"""
        skill_dir = self._resolve_skill_dir(skill_name)
        if (
            not isinstance(rel_path, str)
            or not rel_path
            or "\x00" in rel_path
            or "\\" in rel_path
            or rel_path.startswith("/")
        ):
            raise ValueError("Skill 资源路径非法")
        parts = rel_path.split("/")
        if len(parts) < 2 or parts[0] not in self.ASSET_SUBDIRS:
            raise ValueError("Skill 资源仅允许读取 references/ 或 templates/ 目录下的文件")
        for part in parts:
            if not part or part in (".", "..") or part.startswith("."):
                raise ValueError("Skill 资源路径非法或包含越界片段")

        cur = skill_dir
        for part in parts:
            cur = cur / part
            if cur.is_symlink():
                raise ValueError("Skill 资源不允许符号链接")

        asset_root_resolved = (skill_dir / parts[0]).resolve()
        skill_dir_resolved = skill_dir.resolve()
        target_resolved = cur.resolve()
        if not target_resolved.is_relative_to(asset_root_resolved) or not target_resolved.is_relative_to(
            skill_dir_resolved
        ):
            raise ValueError("Skill 资源路径越界")
        if not cur.is_file():
            raise ValueError(f"Skill 资源不存在：{rel_path}")
        st = cur.stat()
        if not stat_mod.S_ISREG(st.st_mode):
            raise ValueError("Skill 资源必须是普通文件")
        if st.st_nlink != 1:
            raise ValueError("Skill 资源不允许多重硬链接")
        if st.st_size > self.MAX_ASSET_BYTES:
            raise ValueError(f"Skill 资源超过 {self.MAX_ASSET_BYTES} 字节限制")

        sig_before = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(cur, flags)
        except OSError as exc:
            raise ValueError("Skill 资源不允许符号链接或无法读取") from exc
        try:
            fstat = os.fstat(fd)
            if not stat_mod.S_ISREG(fstat.st_mode):
                raise ValueError("Skill 资源必须是普通文件")
            if fstat.st_nlink != 1:
                raise ValueError("Skill 资源不允许多重硬链接")
            if fstat.st_size > self.MAX_ASSET_BYTES:
                raise ValueError(f"Skill 资源超过 {self.MAX_ASSET_BYTES} 字节限制")
            with os.fdopen(fd, "rb") as fh:
                fd = -1
                raw = fh.read(self.MAX_ASSET_BYTES + 1)
        finally:
            if fd >= 0:
                os.close(fd)
        if len(raw) > self.MAX_ASSET_BYTES:
            raise ValueError(f"Skill 资源超过 {self.MAX_ASSET_BYTES} 字节限制")
        after_st = cur.stat()
        sig_after = (after_st.st_mtime_ns, after_st.st_ctime_ns, after_st.st_size, after_st.st_ino)
        if sig_after != sig_before:
            raise ValueError("Skill 资源在读取期间发生变化，请重新加载")
        return raw.decode("utf-8")

    def install_skill_from_dir(
        self,
        source_dir: str | Path,
        target_root: str | Path | None = None,
        *,
        overwrite: bool = True,
    ) -> dict[str, Any]:
        """从本地目录安装技能到指定根目录（默认 `self.directory`）并刷新缓存。"""
        result = install_skill_from_dir(
            source_dir,
            self.directory if target_root is None else target_root,
            overwrite=overwrite,
        )
        self._invalidate_name(result["name"])
        return result

    def remove_skill(
        self,
        name: str,
        target_root: str | Path | None = None,
    ) -> dict[str, Any]:
        """从指定根目录（默认 `self.directory`）移除技能并刷新缓存。"""
        result = remove_skill(name, self.directory if target_root is None else target_root)
        self._invalidate_name(name)
        return result

    @staticmethod
    def _headings(body: str) -> list:
        # 仅支持 ATX 标题，但须跳过 fenced code 内的 #，避免加载错误章节。
        headings = []
        fence = None
        offset = 0
        heading = re.compile(r"(#{1,6})[ \t]+([^\r\n]+?)[ \t]*(?:\r?\n|$)")
        for line in body.splitlines(keepends=True):
            marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
            if marker:
                token = marker.group(1)
                if fence is None:
                    if not (token[0] == "`" and "`" in line[marker.end() :]):
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

    def select(
        self,
        query: str,
        max_chars: int = 2400,
        limit: int = 8,
        *,
        workspace_files: Iterable[str] | None = None,
    ) -> list[dict]:
        """多通道有界技能路由：融合显式 `/skill` 指令、文件路径 Glob、BM25+TF-IDF+RRF 混合检索与字典序决胜。"""
        if not isinstance(query, str):
            raise ValueError("query 必须是字符串")
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in (max_chars, limit)):
            raise ValueError("max_chars/limit 必须是非负整数")
        normalized_ws_files: list[str] = []
        if workspace_files is not None:
            if isinstance(workspace_files, (str, bytes)) or not isinstance(workspace_files, Iterable):
                raise ValueError("workspace_files 必须是路径字符串序列")
            for raw_p in workspace_files:
                if not isinstance(raw_p, str):
                    raise ValueError("workspace_files 元素必须是字符串")
                clean_p = _normalize_rel_posix(raw_p)
                if clean_p:
                    normalized_ws_files.append(clean_p)

        if limit == 0 or max_chars < 2:
            return []

        explicit_names = self.extract_explicit_skill_names(query)
        explicit_rank = {name: idx for idx, name in enumerate(explicit_names)}

        raw_catalog = self.catalog()
        if self.MAX_CATALOG > 0 and explicit_names:
            seen_in_catalog = {item["name"] for item in raw_catalog}
            for exp_name in explicit_names:
                if exp_name not in seen_in_catalog:
                    with contextlib.suppress(ValueError, UnicodeError, OSError):
                        raw_catalog.append(self._header(exp_name))
                        seen_in_catalog.add(exp_name)

        # 过滤声明了 disable-model-invocation: true 的受保护技能，除非用户在当前查询中通过 /name 或 $name 显式点名
        eligible = [
            item
            for item in raw_catalog
            if not item.get("disable_model_invocation", False) or item["name"] in explicit_rank
        ]
        if not eligible:
            return []

        # 通道 B：聚合调用方传入的工作区文件列表与 Prompt 中提及的路径片段，匹配技能声明的 paths/globs
        candidate_paths = list(dict.fromkeys(normalized_ws_files))
        seen_paths = set(candidate_paths)
        for match in _PROMPT_PATH_RE.finditer(query):
            prompt_path = _normalize_rel_posix(match.group(0))
            if prompt_path and prompt_path not in seen_paths:
                seen_paths.add(prompt_path)
                candidate_paths.append(prompt_path)

        glob_hits_by_name: dict[str, int] = {}
        if candidate_paths:
            for item in eligible:
                patterns = item.get("paths")
                if isinstance(patterns, list) and patterns:
                    glob_hits_by_name[item["name"]] = sum(
                        1
                        for cand_p in candidate_paths
                        if any(_path_matches_glob(cand_p, pat) for pat in patterns)
                    )

        # 通道 C.1：通过 KnowledgeBase.from_documents 执行 BM25 + TF-IDF 子词余弦 + RRF 双路混合评分
        rrf_by_name: dict[str, float] = {}
        if query.strip():
            kb_docs = [
                {
                    "source": f"skill:{item['name']}",
                    "stem": item["name"],
                    "metadata": {"skill_name": item["name"]},
                    "content": (
                        f"# {item['name']}\n"
                        f"{item['name']} {item['name'].replace('-', '_')}\n"
                        f"{item['description']}"
                        + (
                            "\nTriggers: " + ", ".join(item["triggers"])
                            if isinstance(item.get("triggers"), list) and item["triggers"]
                            else ""
                        )
                    ),
                }
                for item in eligible
            ]
            kb = KnowledgeBase.from_documents(kb_docs)
            for hit in kb.search(query[:2000], limit=min(20, len(eligible))):
                sname = str(hit.get("skill_name") or "")
                if sname:
                    rrf_by_name[sname] = max(rrf_by_name.get(sname, 0.0), float(hit.get("score", 0.0)))

        # 通道 C.2：词面 Token 与单汉字子串重合回退（确保单汉字查询如“锁”/“测”或连字符短词在词面命中时绝不得 0 分）
        lex_tokens = set(re.findall(r"[a-z0-9_-]+|[㐀-鿿]", query.casefold()))
        lex_by_name: dict[str, tuple[int, float]] = {}
        for item in eligible:
            triggers_part = " ".join(item["triggers"]) if isinstance(item.get("triggers"), list) else ""
            haystack = f"{item['name']} {item['description']} {triggers_part}".casefold()
            hits = sum(1 for token in lex_tokens if token in haystack)
            ratio = (hits / len(lex_tokens)) if lex_tokens else 0.0
            lex_by_name[item["name"]] = (hits, ratio)

        def _routing_key(item: dict[str, Any]) -> tuple[float, int, str]:
            name = item["name"]
            score = 0.0
            if name in explicit_rank:
                score += 1000.0
            if item.get("always_apply") is True:
                score += 500.0
            g_hits = glob_hits_by_name.get(name, 0)
            if g_hits > 0:
                score += 100.0 + min(g_hits, 10) * 1.0
            rrf = rrf_by_name.get(name, 0.0)
            l_hits, l_ratio = lex_by_name.get(name, (0, 0.0))
            # RRF 最小非零相邻名次差（N<=32）放大 100 倍后 >= 0.0119；将词面重合兜底约束在 <= 0.007，
            # 既严格保持混合 RRF 主排序不被单字噪声反转，又确保单汉字/短子串在 RRF=0 时仍得正分并参与平局决胜。
            score += rrf * 100.0 + l_ratio * 0.005 + min(l_hits, 20) * 0.0001
            # 通道 D：同分候选严格按显式提及次序与名称 ASCII 字典序决胜
            return (-round(score, 8), explicit_rank.get(name, 10_000), name)

        ranked = sorted(eligible, key=_routing_key)
        selected: list[dict] = []
        for item in ranked:
            clean_item = _copy_skill_meta(item)
            if len(json.dumps(selected + [clean_item], ensure_ascii=False)) > max_chars:
                continue
            selected.append(clean_item)
            if len(selected) >= limit:
                break
        return selected


# ---------------------------------------------------------------------------
# 受控技能安装、卸载与 SHA-256 清单（.manifest.json）
# ---------------------------------------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _bundle_sha256(files_map: dict[str, str]) -> str:
    canonical = json.dumps(files_map, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _read_regular_file_bytes(path: Path, *, max_bytes: int, label: str) -> bytes:
    if path.is_symlink():
        raise ValueError(f"{label} 不允许符号链接")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} 无法安全读取：{path.name}") from exc
    try:
        st = os.fstat(fd)
        if not stat_mod.S_ISREG(st.st_mode):
            raise ValueError(f"{label} 必须是普通文件")
        if st.st_nlink != 1:
            raise ValueError(f"{label} 不允许多重硬链接")
        if st.st_size > max_bytes:
            raise ValueError(f"{label} 超过 {max_bytes} 字节限制")
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            data = fh.read(max_bytes + 1)
    finally:
        if fd >= 0:
            os.close(fd)
    if len(data) > max_bytes:
        raise ValueError(f"{label} 超过 {max_bytes} 字节限制")
    return data


def _load_manifest(target_root: Path) -> dict[str, Any]:
    manifest_path = target_root / MANIFEST_FILENAME
    if manifest_path.is_symlink():
        raise ValueError("Skill manifest 不允许符号链接")
    if not manifest_path.exists():
        return {"version": 1, "skills": {}}
    raw = _read_regular_file_bytes(manifest_path, max_bytes=1024 * 1024, label="Skill manifest")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Skill manifest JSON 已损坏") from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("skills", {}), dict):
        raise ValueError("Skill manifest 结构非法")
    skills_section = parsed.get("skills")
    return {
        "version": int(parsed.get("version", 1)),
        "skills": dict(skills_section) if isinstance(skills_section, dict) else {},
    }


def _write_manifest_atomic(target_root: Path, manifest: dict[str, Any]) -> Path:
    manifest_path = target_root / MANIFEST_FILENAME
    if manifest_path.is_symlink():
        raise ValueError("Skill manifest 不允许符号链接")
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp_path = target_root / f".manifest.{secrets.token_hex(6)}.tmp"
    try:
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, manifest_path)
    finally:
        if tmp_path.exists():
            with contextlib.suppress(OSError):
                tmp_path.unlink()
    return manifest_path


def _collect_installable_files(src: Path, name: str) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
    """校验源 Skill 目录并按白名单收集 `SKILL.md` 及 `references/`、`templates/` 内容。"""
    skill_file = src / "SKILL.md"
    if skill_file.is_symlink():
        raise ValueError("Skill 不允许符号链接")
    if not skill_file.is_file():
        raise ValueError(f"源目录缺少 SKILL.md：{src}")
    skill_bytes = _read_regular_file_bytes(skill_file, max_bytes=SkillCatalog.MAX_BYTES, label="Skill")
    try:
        skill_text = skill_bytes.decode("utf-8")
    except UnicodeError as exc:
        raise ValueError("SKILL.md 必须是合法 UTF-8 文本") from exc

    # 严格复核有界 Frontmatter 与元数据约束，确保安装后可被 SkillCatalog 正常索引。
    header_raw = skill_bytes[: SkillCatalog.MAX_HEADER_BYTES]
    header_text = header_raw.decode("utf-8", errors="ignore")
    _, _, header_end = SkillCatalog._frontmatter_bounds(
        header_text, missing_end_message="Skill 元数据缺少有界结束标记"
    )
    if len(header_raw) == SkillCatalog.MAX_HEADER_BYTES and not header_text[:header_end].endswith(
        ("\n", "\r")
    ):
        raise ValueError("Skill 元数据缺少有界结束标记")
    metadata = SkillCatalog._metadata(skill_text, name)

    collected: list[tuple[str, bytes]] = [("SKILL.md", skill_bytes)]
    asset_count = 0
    src_resolved = src.resolve()

    for subdir_name in _ASSET_SUBDIRS:
        sub_root = src / subdir_name
        if not sub_root.exists() and not sub_root.is_symlink():
            continue
        if sub_root.is_symlink():
            raise ValueError("Skill 源目录不允许符号链接")
        if not sub_root.is_dir():
            raise ValueError(f"Skill 源目录中的 {subdir_name} 必须是目录")
        sub_root_resolved = sub_root.resolve()
        if not sub_root_resolved.is_relative_to(src_resolved):
            raise ValueError("Skill 源目录资源路径越界")

        for current_dir, subdirs, files in os.walk(sub_root, followlinks=False):
            cur_path = Path(current_dir)
            subdirs.sort()
            for dname in subdirs:
                if dname.startswith("."):
                    raise ValueError(f"Skill 资源目录不允许隐藏子目录：{dname}")
                sub_cand = cur_path / dname
                if sub_cand.is_symlink():
                    raise ValueError("Skill 源目录不允许符号链接")
                if not sub_cand.resolve().is_relative_to(sub_root_resolved):
                    raise ValueError("Skill 源目录资源路径越界")
            for fname in sorted(files):
                if fname.startswith("."):
                    raise ValueError(f"Skill 资源目录不允许隐藏文件：{fname}")
                cand = cur_path / fname
                if cand.is_symlink():
                    raise ValueError("Skill 源目录不允许符号链接")
                if not cand.resolve().is_relative_to(sub_root_resolved):
                    raise ValueError("Skill 源目录资源路径越界")
                asset_count += 1
                if asset_count > SkillCatalog.MAX_ASSETS:
                    raise ValueError(f"Skill 资源文件数量超过 {SkillCatalog.MAX_ASSETS} 上限")
                rel_posix = cand.relative_to(src).as_posix()
                data = _read_regular_file_bytes(
                    cand, max_bytes=SkillCatalog.MAX_ASSET_BYTES, label=f"Skill 资源 {rel_posix}"
                )
                collected.append((rel_posix, data))

    return metadata, collected


def install_skill_from_dir(
    source_dir: str | Path,
    target_root: str | Path,
    *,
    overwrite: bool = True,
) -> dict[str, Any]:
    """从受控源目录校验并原子安装 Skill 到 `target_root`，同时记录 SHA-256 `.manifest.json`。"""
    raw_src = Path(source_dir)
    if raw_src.is_symlink():
        raise ValueError("Skill 源目录不允许符号链接")
    if not raw_src.is_dir():
        raise ValueError(f"Skill 源目录不存在或不是目录：{source_dir}")
    src = raw_src.resolve()
    name = _validate_skill_name(src.name)

    metadata, files_to_install = _collect_installable_files(src, name)

    raw_dst_root = Path(target_root)
    if raw_dst_root.is_symlink():
        raise ValueError("Skill 目标根目录不允许符号链接")
    if raw_dst_root.exists() and not raw_dst_root.is_dir():
        raise ValueError(f"Skill 目标根目录不是目录：{target_root}")
    raw_dst_root.mkdir(parents=True, exist_ok=True)
    dst_root = raw_dst_root.resolve()

    dst_dir = dst_root / name
    if dst_dir.is_symlink():
        raise ValueError("Skill 目标目录不允许符号链接")
    if not dst_dir.resolve().is_relative_to(dst_root):
        raise ValueError("Skill 目标路径越界")
    if dst_dir.resolve() == src:
        raise ValueError("源目录与目标 Skill 目录相同")
    if dst_dir.exists() and not overwrite:
        raise ValueError(f"Skill 已存在：{name}")

    manifest = _load_manifest(dst_root)
    stage_dir = dst_root / f".install-{name}-{secrets.token_hex(6)}"
    backup_dir: Path | None = None
    files_hashes: dict[str, str] = {}

    try:
        stage_dir.mkdir(parents=False, exist_ok=False)
        for rel_posix, data in files_to_install:
            out_path = stage_dir / PurePosixPath(rel_posix)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_bytes(data)
            files_hashes[rel_posix] = _sha256_bytes(data)

        if dst_dir.exists():
            backup_dir = dst_root / f".backup-{name}-{secrets.token_hex(6)}"
            os.replace(dst_dir, backup_dir)
        try:
            os.replace(stage_dir, dst_dir)
        except Exception:
            if backup_dir is not None and backup_dir.exists() and not dst_dir.exists():
                with contextlib.suppress(OSError):
                    os.replace(backup_dir, dst_dir)
            raise

        skill_sha = files_hashes["SKILL.md"]
        bundle_sha = _bundle_sha256(files_hashes)
        installed_at = datetime.now(timezone.utc).isoformat()
        record = {
            "name": name,
            "description": metadata["description"],
            "sha256": skill_sha,
            "bundle_sha256": bundle_sha,
            "files": dict(sorted(files_hashes.items())),
            "installed_at": installed_at,
        }
        manifest["skills"][name] = record
        manifest_path = _write_manifest_atomic(dst_root, manifest)
    finally:
        if stage_dir.exists():
            with contextlib.suppress(OSError):
                shutil.rmtree(stage_dir)
        if backup_dir is not None and backup_dir.exists():
            with contextlib.suppress(OSError):
                shutil.rmtree(backup_dir)

    return {
        "ok": True,
        "name": name,
        "path": str(dst_dir),
        "manifest_path": str(manifest_path),
        "sha256": skill_sha,
        "bundle_sha256": bundle_sha,
        "files": dict(sorted(files_hashes.items())),
        "installed_at": installed_at,
    }


def remove_skill(name: str, target_root: str | Path) -> dict[str, Any]:
    """安全卸载 `target_root` 下的指定 Skill 目录并同步更新 `.manifest.json`。"""
    valid_name = _validate_skill_name(name)
    raw_dst_root = Path(target_root)
    if raw_dst_root.is_symlink():
        raise ValueError("Skill 目标根目录不允许符号链接")
    if not raw_dst_root.exists():
        return {
            "ok": True,
            "name": valid_name,
            "removed": False,
            "manifest_path": str(raw_dst_root / MANIFEST_FILENAME),
        }
    if not raw_dst_root.is_dir():
        raise ValueError(f"Skill 目标根目录不是目录：{target_root}")

    dst_root = raw_dst_root.resolve()
    dst_dir = dst_root / valid_name
    if dst_dir.is_symlink():
        raise ValueError("Skill 目标目录不允许符号链接")
    if not dst_dir.resolve().is_relative_to(dst_root):
        raise ValueError("Skill 目标路径越界")

    manifest = _load_manifest(dst_root)
    removed = False
    if dst_dir.exists():
        if not dst_dir.is_dir():
            raise ValueError(f"Skill 目标路径不是目录：{valid_name}")
        shutil.rmtree(dst_dir)
        removed = True

    manifest_path = dst_root / MANIFEST_FILENAME
    if valid_name in manifest["skills"]:
        manifest["skills"].pop(valid_name, None)
        manifest_path = _write_manifest_atomic(dst_root, manifest)

    return {
        "ok": True,
        "name": valid_name,
        "removed": removed,
        "manifest_path": str(manifest_path),
    }
