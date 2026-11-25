"""持久化 Merkle-Tree 增量 AST 分块代码库语义索引（Cursor 风格）。

================================================================================
模块导读（Module Guide）
================================================================================
1. 模块角色（Role）
   本模块实现面向中大型代码仓的持久化、增量式、语义 + 词法混合检索引擎（`CodebaseMerkleIndex`），
   对标 Cursor 的 Merkle-Tree 增量代码索引架构：
   - 持久化状态落盘于 `<state_dir>/indexes/<project_slug>/code_index.sqlite`（SQLite WAL 模式），
     进程重启后直接复用已计算的 Merkle 节点与稠密向量，避免全库冷重算；
   - 文件变更检测采用两级快速路径（Fast Path）：`os.walk` 原地剪枝 + `(mtime_ns, size)` 快速命中
     直接复用缓存 SHA-256，既不读文件内容也不触发 AST 分块或 Embedding 推理；仅对真正新增或修改
     的文件执行 AST/语法分块与增量向量化，并自动清理已删除文件的节点与分块；
   - 自底向上计算目录级 Merkle Tree 哈希并写入 `merkle_nodes(path=".")`，使内容完全一致的文件树
     必然拥有全局一致的 `root_hash`，单文件变动即可 O(depth) 传导至根节点；
   - 查询阶段通过 BM25+ 代码感知词法得分与持久化稠密语义向量余弦相似度进行倒数排名融合
     （Reciprocal Rank Fusion, k=60），返回带精确起止行号与符号名的代码块。

2. 核心安全与工程不变量（Core Security & Engineering Invariants）
   - 零密钥与零软硬链接逃逸不变量：目录遍历严格设置 `followlinks=False`，逐级 `lstat` 跳过所有
     符号链接目录与文件、非常规文件、多重硬链接（`st_nlink != 1`）、二进制文件，以及 `.git` /
     `.venv` / `node_modules` / `.agent-lab` 与 `is_host_secret_segment` 命中的全部密钥凭证文件；
   - 索引数据库防符号链接劫持不变量：每次连接 SQLite 前均自顶向下逐段校验 `<state_dir>` 至
     `code_index.sqlite`（含 `-wal` / `-shm` 伴生文件）路径段无任何符号链接且解析结果严格受限于
     `state_dir` 内部；
   - 单事务原子提交与只读防 TOCTOU 读取不变量：慢路径读取使用 `O_NOFOLLOW` + `fstat` 核验常规文件
     与硬链接计数；每次 `refresh()` 的删旧、插新与目录 Merkle 树更新在单个 SQLite 事务中原子提交。
================================================================================
"""

from __future__ import annotations

import ast
from collections import Counter
import contextlib
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import sqlite3
import stat
import textwrap
import threading
from typing import Any

from .host_tools import is_host_secret_segment
from .retrieval import (
    Embedder,
    LocalSubwordDenseEmbedder,
    _normalize_dense,
    _tag_fenced_lines,
    build_default_embedder,
    tokenize,
)

# ---------------------------------------------------------------------------
# 1. 安全剪枝集合、分块参数与工具契约（CODE_INDEX_TOOL_SPECS）
# ---------------------------------------------------------------------------

_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        ".agent-lab",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".ssh",
        ".gnupg",
        ".aws",
        ".kube",
    }
)

_EXTRA_SECRET_FILENAMES: frozenset[str] = frozenset({".netrc", ".pypirc", ".npmrc"})
_EXTRA_SECRET_PREFIXES: tuple[str, ...] = ("id_ecdsa", "id_dsa")
_EXTRA_SECRET_SUFFIXES: tuple[str, ...] = (".jks", ".keystore")

_BINARY_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".ico",
        ".webp",
        ".avif",
        ".tiff",
        ".pdf",
        ".zip",
        ".tar",
        ".gz",
        ".tgz",
        ".bz2",
        ".xz",
        ".7z",
        ".rar",
        ".woff",
        ".woff2",
        ".ttf",
        ".otf",
        ".eot",
        ".so",
        ".dylib",
        ".dll",
        ".exe",
        ".bin",
        ".o",
        ".a",
        ".pyc",
        ".pyo",
        ".pyd",
        ".class",
        ".jar",
        ".war",
        ".wasm",
        ".sqlite",
        ".sqlite3",
        ".db",
        ".mp3",
        ".mp4",
        ".wav",
        ".mov",
        ".avi",
        ".mkv",
        ".flac",
        ".parquet",
        ".feather",
        ".npy",
        ".npz",
        ".pt",
        ".pth",
        ".onnx",
        ".safetensors",
    }
)

_PYTHON_EXTENSIONS: frozenset[str] = frozenset({".py", ".pyi"})
_MARKDOWN_EXTENSIONS: frozenset[str] = frozenset({".md", ".mdx", ".markdown"})
_CODE_BLOCK_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".go",
        ".rs",
        ".java",
        ".kt",
        ".kts",
        ".cs",
        ".c",
        ".h",
        ".cc",
        ".cpp",
        ".cxx",
        ".hpp",
        ".swift",
        ".php",
        ".rb",
        ".sh",
        ".bash",
        ".zsh",
    }
)

_DEFAULT_CHUNK_MAX_LINES = 80
_DEFAULT_CHUNK_MAX_CHARS = 3200
_DEFAULT_CHUNK_OVERLAP_LINES = 10
_EMBED_BATCH_SIZE = 64
_EXCERPT_MAX_CHARS = 800
_RRF_K = 60

# JSON Schema Draft 2020-12 工具契约：供 Agent 在大型代码仓中执行语义 + 词法混合检索
CODE_INDEX_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "codebase_semantic_search",
        "description": (
            "基于持久化 Merkle-Tree 增量 AST 分块语义索引在工作区代码库中执行混合语义 + 词法检索"
            "（BM25 + 稠密向量余弦 + RRF），自动增量同步变更文件，并返回最相关的函数、类、章节或代码块"
            "及其相对路径、符号名、精确起止行号与代码摘录。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": "自然语言问题、业务概念、标识符或待定位逻辑描述。",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 30,
                    "default": 8,
                    "description": "返回的最大匹配代码块数量（默认 8，上限 30）。",
                },
                "path_prefix": {
                    "type": ["string", "null"],
                    "minLength": 1,
                    "maxLength": 320,
                    "description": "可选工作区相对路径前缀过滤（例如 'src/agent_lab' 或 'web/'）。",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }
]


# ---------------------------------------------------------------------------
# 2. 数据结构与路径/安全基础原语
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CodeChunk:
    """单个经过 AST 或语法切分的代码分块。"""

    chunk_id: str
    path: str
    symbol: str
    kind: str
    start_line: int
    end_line: int
    content: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_secret_segment(name: str) -> bool:
    """复用 host_tools.is_host_secret_segment 并补充常见凭证配置与证书库拦截。"""
    if is_host_secret_segment(name):
        return True
    lower = name.casefold()
    if lower in _EXTRA_SECRET_FILENAMES:
        return True
    if lower.startswith(_EXTRA_SECRET_PREFIXES):
        return True
    if lower.endswith(_EXTRA_SECRET_SUFFIXES):
        return True
    return False


def _compute_project_slug(workspace_resolved: Path) -> str:
    """基于规范化工作区物理路径生成可读且防碰撞的稳定目录标识。"""
    raw_name = workspace_resolved.name.lower()
    ascii_part = re.sub(r"[^a-z0-9_-]+", "-", raw_name).strip("-_")[:32] or "workspace"
    if not ascii_part[0].isalnum():
        ascii_part = f"w-{ascii_part}"
    fp = hashlib.sha256(str(workspace_resolved).encode("utf-8")).hexdigest()[:12]
    return f"{ascii_part}-{fp}"


def _ensure_safe_subpath(base_root: Path, target: Path) -> None:
    """自顶向下逐段 lstat 校验目标路径无符号链接且物理路径严格受限于 base_root。"""
    if base_root.is_symlink():
        raise ValueError(f"状态根目录不能是符号链接: {base_root}")
    base_resolved = base_root.resolve()
    try:
        rel = target.absolute().relative_to(base_root.absolute())
    except ValueError as exc:
        raise ValueError(f"路径越过受控状态根目录: {target}") from exc
    if any(part in ("", ".", "..") for part in rel.parts):
        raise ValueError(f"路径包含非法相对跳转段: {target}")
    cur = base_root.absolute()
    for part in rel.parts:
        cur = cur / part
        if cur.is_symlink():
            raise ValueError(f"拒绝访问索引目录中的符号链接: {cur}")
    try:
        resolved = target.resolve()
    except RuntimeError as exc:
        raise ValueError(f"检测到符号链接循环: {target}") from exc
    if resolved != base_resolved and not resolved.is_relative_to(base_resolved):
        raise ValueError(f"索引路径解析越过状态根目录边界: {target}")


def _safe_read_text_file(full_path: Path, *, max_bytes: int) -> tuple[bytes, str] | None:
    """以 O_NOFOLLOW + fstat 防 TOCTOU 读取常规单硬链 UTF-8 文本文件；二进制或非法文件返回 None。"""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(full_path, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_size > max_bytes:
            return None
        chunks: list[bytes] = []
        total = 0
        while True:
            buf = os.read(fd, 65536)
            if not buf:
                break
            total += len(buf)
            if total > max_bytes:
                return None
            chunks.append(buf)
        raw = b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)

    if b"\x00" in raw[:8192]:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return raw, text


def _make_chunk_id(
    path: str,
    idx: int,
    kind: str,
    symbol: str,
    start_line: int,
    end_line: int,
    content: str,
) -> str:
    payload = f"{path}\0{idx}\0{kind}\0{symbol}\0{start_line}\0{end_line}\0{content}".encode("utf-8")
    digest_hex = hashlib.sha256(payload).hexdigest()[:14]
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", Path(path).stem)[:24] or "chunk"
    return f"{stem}:{start_line}-{end_line}:{digest_hex}"


def _format_chunk_index_text(path: str, kind: str, symbol: str, content: str) -> str:
    """构造含结构上下文前缀的可检索/可向量化文本（不修改落盘源码正文）。"""
    return f"{path} | {kind} {symbol}\n{content}"


def _trim_blank_span(lines: list[str], start_line: int, end_line: int) -> tuple[int, int] | None:
    """裁剪 1-based [start_line, end_line] 两端纯空白行，返回精确非空起止行号。"""
    lo = max(1, start_line)
    hi = min(len(lines), end_line)
    while lo <= hi and not lines[lo - 1].strip():
        lo += 1
    while hi >= lo and not lines[hi - 1].strip():
        hi -= 1
    if lo > hi:
        return None
    return lo, hi


def _window_line_span(
    lines: list[str],
    start_line: int,
    end_line: int,
    *,
    symbol: str,
    kind: str,
    max_lines: int = _DEFAULT_CHUNK_MAX_LINES,
    max_chars: int = _DEFAULT_CHUNK_MAX_CHARS,
    overlap_lines: int = _DEFAULT_CHUNK_OVERLAP_LINES,
) -> list[tuple[str, str, int, int, str]]:
    """将任意 1-based 行区间按行数与字符预算切分为带重叠窗口的分块元组列表。"""
    trimmed = _trim_blank_span(lines, start_line, end_line)
    if trimmed is None:
        return []
    lo, hi = trimmed
    full_text = "\n".join(lines[lo - 1 : hi])
    if (hi - lo + 1) <= max_lines and len(full_text) <= max_chars:
        return [(symbol, kind, lo, hi, full_text)]

    results: list[tuple[str, str, int, int, str]] = []
    cur = lo
    while cur <= hi:
        win_end = cur
        char_acc = 0
        while win_end <= hi and (win_end - cur + 1) <= max_lines:
            line_len = len(lines[win_end - 1]) + 1
            if win_end > cur and char_acc + line_len > max_chars:
                break
            char_acc += line_len
            win_end += 1
        actual_end = max(cur, win_end - 1)
        sub_span = _trim_blank_span(lines, cur, actual_end)
        if sub_span is not None:
            s_lo, s_hi = sub_span
            results.append((symbol, kind, s_lo, s_hi, "\n".join(lines[s_lo - 1 : s_hi])))
        if actual_end >= hi:
            break
        step_back = min(overlap_lines, max(0, actual_end - cur))
        next_cur = actual_end - step_back + 1
        cur = next_cur if next_cur > cur else actual_end + 1
    return results


# ---------------------------------------------------------------------------
# 3. Python AST 分块器与多语言语法/标题分块器
# ---------------------------------------------------------------------------


def _ast_node_start_line(node: ast.AST) -> int:
    lineno = int(getattr(node, "lineno", 1) or 1)
    decorators = getattr(node, "decorator_list", None) or []
    if decorators:
        dec_lines = [int(getattr(d, "lineno", lineno) or lineno) for d in decorators]
        return min(lineno, *dec_lines)
    return lineno


def _ast_node_end_line(node: ast.AST, total_lines: int) -> int:
    end = int(getattr(node, "end_lineno", None) or getattr(node, "lineno", 1) or 1)
    return min(total_lines, max(1, end))


def _chunk_python_class(
    cls_node: ast.ClassDef,
    lines: list[str],
    qualname: str,
    out: list[tuple[str, str, int, int, str]],
) -> None:
    """提取 Python 类声明头与各方法/嵌套类分块，保持精确 1-based 行号。"""
    total = len(lines)
    cls_start = _ast_node_start_line(cls_node)
    cls_end = _ast_node_end_line(cls_node, total)

    member_nodes = [
        stmt
        for stmt in cls_node.body
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    if not member_nodes:
        out.extend(_window_line_span(lines, cls_start, cls_end, symbol=qualname, kind="class"))
        return

    first_member_start = _ast_node_start_line(member_nodes[0])
    header_end = max(cls_node.lineno, first_member_start - 1)
    out.extend(_window_line_span(lines, cls_start, header_end, symbol=qualname, kind="class"))

    cursor = first_member_start
    for stmt in cls_node.body:
        stmt_start = _ast_node_start_line(stmt)
        stmt_end = _ast_node_end_line(stmt, total)
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if stmt_start > cursor:
                out.extend(_window_line_span(lines, cursor, stmt_start - 1, symbol=qualname, kind="class"))
            method_sym = f"{qualname}.{stmt.name}"
            out.extend(_window_line_span(lines, stmt_start, stmt_end, symbol=method_sym, kind="method"))
            cursor = max(cursor, stmt_end + 1)
        elif isinstance(stmt, ast.ClassDef):
            if stmt_start > cursor:
                out.extend(_window_line_span(lines, cursor, stmt_start - 1, symbol=qualname, kind="class"))
            _chunk_python_class(stmt, lines, f"{qualname}.{stmt.name}", out)
            cursor = max(cursor, stmt_end + 1)
    if cursor <= cls_end:
        out.extend(_window_line_span(lines, cursor, cls_end, symbol=qualname, kind="class"))


def _chunk_python_source(rel_posix: str, source: str) -> list[CodeChunk] | None:
    """使用标准库 ast.parse 将 Python 源码切分为类、方法、顶层函数与模块块；语法错误时返回 None 回退。"""
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    if not lines:
        return []
    try:
        tree = ast.parse(normalized, filename=rel_posix)
    except SyntaxError:
        return None

    raw_chunks: list[tuple[str, str, int, int, str]] = []
    total = len(lines)
    stem = Path(rel_posix).stem or "module"
    module_block_start: int | None = None
    module_block_end: int | None = None

    def _flush_module_block() -> None:
        nonlocal module_block_start, module_block_end
        if module_block_start is not None and module_block_end is not None:
            sym = f"{stem}:L{module_block_start}"
            raw_chunks.extend(
                _window_line_span(lines, module_block_start, module_block_end, symbol=sym, kind="module")
            )
            module_block_start = None
            module_block_end = None

    for stmt in tree.body:
        stmt_start = _ast_node_start_line(stmt)
        stmt_end = _ast_node_end_line(stmt, total)
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _flush_module_block()
            raw_chunks.extend(
                _window_line_span(lines, stmt_start, stmt_end, symbol=stmt.name, kind="function")
            )
        elif isinstance(stmt, ast.ClassDef):
            _flush_module_block()
            _chunk_python_class(stmt, lines, stmt.name, raw_chunks)
        else:
            if module_block_start is None:
                module_block_start = stmt_start
            module_block_end = max(module_block_end or stmt_end, stmt_end)

    _flush_module_block()

    # 若文件全为注释（tree.body 为空但文本非空），保留单个注释模块块
    if not raw_chunks and normalized.strip():
        raw_chunks.extend(_window_line_span(lines, 1, total, symbol=stem, kind="module"))

    return [
        CodeChunk(
            chunk_id=_make_chunk_id(rel_posix, idx, kind, symbol, s_line, e_line, content),
            path=rel_posix,
            symbol=symbol,
            kind=kind,
            start_line=s_line,
            end_line=e_line,
            content=content,
        )
        for idx, (symbol, kind, s_line, e_line, content) in enumerate(raw_chunks)
    ]


def _chunk_markdown_source(rel_posix: str, source: str) -> list[CodeChunk]:
    """围栏安全的 Markdown 标题分块：跳过代码块内的伪标题，记录精确 1-based 行号。"""
    tagged = _tag_fenced_lines(source)
    if not tagged:
        return []
    lines = [line for line, _ in tagged]
    stem = Path(rel_posix).stem or "document"
    sections: list[tuple[str, int, int]] = []
    cur_heading = stem
    cur_start = 1

    for idx, (line, in_fence) in enumerate(tagged, 1):
        if not in_fence:
            match = re.match(r"^#{1,6}\s+(.+)$", line)
            if match:
                if idx > cur_start:
                    sections.append((cur_heading, cur_start, idx - 1))
                cur_heading = match.group(1).strip() or stem
                cur_start = idx
    if cur_start <= len(lines):
        sections.append((cur_heading, cur_start, len(lines)))

    raw_chunks: list[tuple[str, str, int, int, str]] = []
    for heading, s_line, e_line in sections:
        raw_chunks.extend(_window_line_span(lines, s_line, e_line, symbol=heading, kind="section"))

    return [
        CodeChunk(
            chunk_id=_make_chunk_id(rel_posix, idx, kind, symbol, s_line, e_line, content),
            path=rel_posix,
            symbol=symbol,
            kind=kind,
            start_line=s_line,
            end_line=e_line,
            content=content,
        )
        for idx, (symbol, kind, s_line, e_line, content) in enumerate(raw_chunks)
    ]


# 面向 TS/JS/Go/Rust/Java/C-like/Shell 的顶层声明锚点正则
_NON_PY_DECL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # TS / JS export or plain class / interface / enum / type
    (
        re.compile(
            r"^(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:abstract\s+)?"
            r"(class|interface|enum|type)\s+([A-Za-z_$][\w$]*)\b"
        ),
        "ts_type",
    ),
    # TS / JS function declaration
    (
        re.compile(
            r"^(?:export\s+(?:default\s+)?)?(?:declare\s+)?(?:async\s+)?"
            r"function\s*\*?\s*([A-Za-z_$][\w$]*)\s*[<(]"
        ),
        "function",
    ),
    # TS / JS exported arrow function / function expression
    (
        re.compile(
            r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*"
            r"(?::[^=]+)?=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*(?::[^=]+)?=>"
        ),
        "function",
    ),
    # Go receiver method: func (r *Recv) Name(
    (
        re.compile(
            r"^func\s*\(\s*(?:[A-Za-z_]\w*\s+)?\*?([A-Za-z_]\w*)(?:\[[^\]]*\])?\s*\)\s*"
            r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*\("
        ),
        "go_method",
    ),
    # Go function: func Name(
    (re.compile(r"^func\s+([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*\("), "function"),
    # Rust / Java / C# / Kotlin / Swift declarations
    (
        re.compile(
            r"^(?:(?:pub(?:\([^)]*\))?|public|private|protected|internal|final|static|abstract|"
            r"async|unsafe|extern|override|open|sealed|data)\s+)*"
            r"(fn|struct|trait|impl|record|class|interface|enum)\s+([A-Za-z_][\w:<>]*)"
        ),
        "lang_item",
    ),
    # Ruby / Python fallback def
    (re.compile(r"^(?:async\s+)?def\s+([A-Za-z_][\w?!]*)\b"), "function"),
    # Shell function
    (re.compile(r"^function\s+([A-Za-z_][\w-]*)\b"), "function"),
    (re.compile(r"^([A-Za-z_][\w-]*)\s*\(\)\s*\{"), "function"),
)


def _match_non_py_declaration(stripped_line: str) -> tuple[str, str] | None:
    """识别非 Python 源码的顶层声明，返回 (kind, symbol)。"""
    for pattern, tag in _NON_PY_DECL_PATTERNS:
        m = pattern.match(stripped_line)
        if not m:
            continue
        if tag == "ts_type":
            return m.group(1), m.group(2)
        if tag == "go_method":
            return "method", f"{m.group(1)}.{m.group(2)}"
        if tag == "lang_item":
            kw = m.group(1)
            raw_sym = m.group(2).split("<", 1)[0].strip(":")
            kind = "function" if kw == "fn" else ("interface" if kw == "trait" else kw)
            return kind, raw_sym or kw
        return tag, m.group(1)
    return None


def _strip_line_comments_and_strings(line: str, in_block_comment: bool) -> tuple[str, bool]:
    """轻量剥离 C-style 注释与引号字面量，仅用于准确统计花括号层级。"""
    out: list[str] = []
    idx = 0
    n = len(line)
    quote: str | None = None
    while idx < n:
        ch = line[idx]
        nxt = line[idx + 1] if idx + 1 < n else ""
        if in_block_comment:
            if ch == "*" and nxt == "/":
                in_block_comment = False
                idx += 2
            else:
                idx += 1
            continue
        if quote is not None:
            if ch == "\\" and idx + 1 < n:
                idx += 2
                continue
            if ch == quote:
                quote = None
            idx += 1
            continue
        if ch == "/" and nxt == "/":
            break
        if ch == "/" and nxt == "*":
            in_block_comment = True
            idx += 2
            continue
        if ch in ('"', "'", "`"):
            quote = ch
            idx += 1
            continue
        out.append(ch)
        idx += 1
    return "".join(out), in_block_comment


def _chunk_code_block_source(rel_posix: str, source: str) -> list[CodeChunk]:
    """对 TS/JS/Go/Rust/Java 等非 Python 源码按顶层语法声明与花括号块边界切分。"""
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    if not lines:
        return []

    stem = Path(rel_posix).stem or "file"
    segments: list[tuple[str, str, int, int]] = []
    cur_kind = "block"
    cur_symbol = f"{stem}:L1"
    cur_start = 1
    has_active_decl = False
    brace_depth = 0
    in_block_comment = False

    for lineno, line in enumerate(lines, 1):
        stripped = line.strip()
        decl = _match_non_py_declaration(stripped) if (brace_depth == 0 and not in_block_comment) else None
        if decl is not None:
            if lineno > cur_start:
                segments.append((cur_symbol, cur_kind, cur_start, lineno - 1))
            cur_kind, cur_symbol = decl
            cur_start = lineno
            has_active_decl = True

        clean_for_braces, in_block_comment = _strip_line_comments_and_strings(line, in_block_comment)
        depth_before = brace_depth
        brace_depth = max(0, brace_depth + clean_for_braces.count("{") - clean_for_braces.count("}"))

        if has_active_decl and depth_before > 0 and brace_depth == 0:
            segments.append((cur_symbol, cur_kind, cur_start, lineno))
            cur_start = lineno + 1
            cur_kind = "block"
            cur_symbol = f"{stem}:L{cur_start}"
            has_active_decl = False

    if cur_start <= len(lines):
        segments.append((cur_symbol, cur_kind, cur_start, len(lines)))

    raw_chunks: list[tuple[str, str, int, int, str]] = []
    for symbol, kind, s_line, e_line in segments:
        raw_chunks.extend(_window_line_span(lines, s_line, e_line, symbol=symbol, kind=kind))

    return [
        CodeChunk(
            chunk_id=_make_chunk_id(rel_posix, idx, kind, symbol, s_line, e_line, content),
            path=rel_posix,
            symbol=symbol,
            kind=kind,
            start_line=s_line,
            end_line=e_line,
            content=content,
        )
        for idx, (symbol, kind, s_line, e_line, content) in enumerate(raw_chunks)
    ]


def _chunk_generic_text_source(rel_posix: str, source: str) -> list[CodeChunk]:
    """对 TOML/INI/YAML/JSON/普通文本按节标题与滑动行窗口切分。"""
    normalized = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.splitlines()
    if not lines:
        return []
    stem = Path(rel_posix).stem or "file"
    sections: list[tuple[str, str, int, int]] = []
    cur_sym = stem
    cur_kind = "block"
    cur_start = 1
    suffix = Path(rel_posix).suffix.lower()

    if suffix in {".toml", ".ini", ".cfg"}:
        for lineno, line in enumerate(lines, 1):
            m = re.match(r"^\s*\[+([^\[\]]+)\]+\s*$", line)
            if m:
                if lineno > cur_start:
                    sections.append((cur_sym, cur_kind, cur_start, lineno - 1))
                cur_sym = m.group(1).strip() or stem
                cur_kind = "section"
                cur_start = lineno
    if cur_start <= len(lines):
        sections.append((cur_sym, cur_kind, cur_start, len(lines)))

    raw_chunks: list[tuple[str, str, int, int, str]] = []
    for symbol, kind, s_line, e_line in sections:
        raw_chunks.extend(_window_line_span(lines, s_line, e_line, symbol=symbol, kind=kind))

    return [
        CodeChunk(
            chunk_id=_make_chunk_id(rel_posix, idx, kind, symbol, s_line, e_line, content),
            path=rel_posix,
            symbol=symbol,
            kind=kind,
            start_line=s_line,
            end_line=e_line,
            content=content,
        )
        for idx, (symbol, kind, s_line, e_line, content) in enumerate(raw_chunks)
    ]


def _chunk_file_content(rel_posix: str, source: str) -> list[CodeChunk]:
    """按文件后缀路由至 Python AST、Markdown 围栏标题、多语言语法块或通用滑窗分块器。"""
    suffix = Path(rel_posix).suffix.lower()
    if suffix in _PYTHON_EXTENSIONS:
        py_chunks = _chunk_python_source(rel_posix, source)
        if py_chunks is not None:
            return py_chunks
        return _chunk_code_block_source(rel_posix, source)
    if suffix in _MARKDOWN_EXTENSIONS:
        return _chunk_markdown_source(rel_posix, source)
    if suffix in _CODE_BLOCK_EXTENSIONS:
        return _chunk_code_block_source(rel_posix, source)
    return _chunk_generic_text_source(rel_posix, source)


# ---------------------------------------------------------------------------
# 4. 目录级 Merkle Tree 自底向上哈希计算
# ---------------------------------------------------------------------------


def _compute_merkle_tree(file_hashes: dict[str, str]) -> tuple[str, dict[str, tuple[str, int]]]:
    """基于已索引文件相对路径与内容 SHA-256 自底向上计算所有目录节点的 Merkle 哈希。

    不变量：
    - 每个目录节点的哈希等于其所有直接子节点按路径字典序排列后的 `f"{child_path}:{child_sha256}"`
      行拼接的 SHA-256；
    - 相同文件树（哪怕位于不同宿主机绝对路径）产生 100% 相同的目录与根 `root_hash`；
    - 任意文件增删改均会沿祖先链传导并改变根节点 `path="."` 的 `root_hash`。
    """
    dir_children: dict[str, list[tuple[str, str]]] = {".": []}
    all_dirs: set[str] = {"."}

    for rel_posix, file_sha in file_hashes.items():
        pure = PurePosixPath(rel_posix)
        parent = pure.parent.as_posix()
        if parent in ("", "."):
            parent = "."
        dir_children.setdefault(parent, []).append((rel_posix, file_sha))
        # 注册所有祖先目录以确保逐级自底向上聚合
        cur = parent
        while cur != ".":
            all_dirs.add(cur)
            anc = PurePosixPath(cur).parent.as_posix()
            if anc in ("", "."):
                anc = "."
            dir_children.setdefault(anc, [])
            cur = anc

    # 按路径深度从深到浅排序（根节点 "." 深度 0 排最后）
    def _dir_depth(d: str) -> tuple[int, str]:
        if d == ".":
            return (0, "")
        return (len(PurePosixPath(d).parts), d)

    ordered_dirs = sorted(all_dirs, key=lambda d: (-_dir_depth(d)[0], d))
    dir_hashes: dict[str, tuple[str, int]] = {}

    for d in ordered_dirs:
        entries = sorted(dir_children.get(d, []), key=lambda item: item[0])
        payload = "\n".join(f"{child_path}:{child_sha}" for child_path, child_sha in entries)
        d_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        dir_hashes[d] = (d_hash, len(entries))
        if d != ".":
            anc = PurePosixPath(d).parent.as_posix()
            if anc in ("", "."):
                anc = "."
            dir_children.setdefault(anc, []).append((d, d_hash))

    root_hash = dir_hashes["."][0]
    return root_hash, dir_hashes


# ---------------------------------------------------------------------------
# 5. 核心类：CodebaseMerkleIndex
# ---------------------------------------------------------------------------


class CodebaseMerkleIndex:
    """持久化 Merkle-Tree 增量 AST 分块代码库语义索引。"""

    def __init__(
        self,
        workspace: Path | str,
        state_dir: Path | str,
        *,
        embedder: Embedder | None = None,
        max_files: int = 4000,
        max_file_bytes: int = 512 * 1024,
    ) -> None:
        if embedder is not None and not callable(embedder):
            raise ValueError("embedder 必须是可调用对象")
        if isinstance(max_files, bool) or not isinstance(max_files, int) or max_files < 1:
            raise ValueError("max_files 必须是 >= 1 的整数")
        if isinstance(max_file_bytes, bool) or not isinstance(max_file_bytes, int) or max_file_bytes < 1:
            raise ValueError("max_file_bytes 必须是 >= 1 的整数")

        raw_ws = Path(workspace).expanduser()
        if raw_ws.is_symlink():
            raise ValueError(f"workspace 不能是符号链接: {raw_ws}")
        if not raw_ws.exists() or not raw_ws.is_dir():
            raise ValueError(f"workspace 必须是已存在的目录: {raw_ws}")
        self.workspace: Path = raw_ws.resolve()

        raw_state = Path(state_dir).expanduser()
        if raw_state.is_symlink():
            raise ValueError(f"state_dir 不能是符号链接: {raw_state}")
        raw_state.mkdir(parents=True, exist_ok=True)
        if raw_state.is_symlink() or not raw_state.is_dir():
            raise ValueError(f"state_dir 必须是普通目录: {raw_state}")
        self.state_dir: Path = raw_state.resolve()

        self.max_files: int = max_files
        self.max_file_bytes: int = max_file_bytes
        self.project_slug: str = _compute_project_slug(self.workspace)

        resolved_embedder = embedder if embedder is not None else build_default_embedder()
        if resolved_embedder is None:
            resolved_embedder = LocalSubwordDenseEmbedder(dim=128)
        self._embedder: Embedder = resolved_embedder

        self._index_dir: Path = self.state_dir / "indexes" / self.project_slug
        self.db_path: Path = self._index_dir / "code_index.sqlite"
        self._lock = threading.RLock()

        self._ensure_safe_db_paths(create_dirs=True)
        self._init_schema()

    def _ensure_safe_db_paths(self, *, create_dirs: bool = False) -> None:
        """校验索引目录、SQLite 数据库文件及其 WAL/SHM 伴生文件均非符号链接且在 state_dir 内。"""
        indexes_root = self.state_dir / "indexes"
        _ensure_safe_subpath(self.state_dir, indexes_root)
        if create_dirs:
            indexes_root.mkdir(parents=True, exist_ok=True)
            _ensure_safe_subpath(self.state_dir, indexes_root)

        _ensure_safe_subpath(self.state_dir, self._index_dir)
        if create_dirs:
            self._index_dir.mkdir(parents=True, exist_ok=True)
            _ensure_safe_subpath(self.state_dir, self._index_dir)

        _ensure_safe_subpath(self.state_dir, self.db_path)
        for suffix in ("-wal", "-shm", "-journal"):
            sibling = self._index_dir / f"{self.db_path.name}{suffix}"
            _ensure_safe_subpath(self.state_dir, sibling)

    def _connect(self) -> sqlite3.Connection:
        self._ensure_safe_db_paths(create_dirs=True)
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_schema(self) -> None:
        with self._lock, contextlib.closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE;")
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS merkle_nodes (
                        path TEXT PRIMARY KEY,
                        is_dir INTEGER NOT NULL,
                        mtime_ns INTEGER NOT NULL,
                        size INTEGER NOT NULL,
                        sha256 TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS code_chunks (
                        chunk_id TEXT PRIMARY KEY,
                        path TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        kind TEXT NOT NULL,
                        start_line INTEGER NOT NULL,
                        end_line INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        embedding_json TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                conn.execute("CREATE INDEX IF NOT EXISTS idx_code_chunks_path ON code_chunks(path)")
                conn.execute("COMMIT;")
            except Exception:
                conn.execute("ROLLBACK;")
                raise

    def _scan_workspace_files(self) -> list[tuple[str, Path, int, int]]:
        """原地剪枝遍历工作区，跳过隐藏/构建/密钥目录、软硬链接、超大与已知二进制文件。"""
        discovered: list[tuple[str, Path, int, int]] = []
        ws_root = self.workspace

        for dirpath, dirnames, filenames in os.walk(ws_root, topdown=True, followlinks=False):
            cur_dir = Path(dirpath)
            kept_dirs: list[str] = []
            for dname in sorted(dirnames):
                if dname in _SKIP_DIR_NAMES or _is_secret_segment(dname):
                    continue
                dpath = cur_dir / dname
                try:
                    dst = os.lstat(dpath)
                except OSError:
                    continue
                if stat.S_ISLNK(dst.st_mode) or not stat.S_ISDIR(dst.st_mode):
                    continue
                kept_dirs.append(dname)
            dirnames[:] = kept_dirs

            for fname in sorted(filenames):
                if len(discovered) >= self.max_files:
                    return discovered
                if _is_secret_segment(fname):
                    continue
                if Path(fname).suffix.lower() in _BINARY_EXTENSIONS:
                    continue
                fpath = cur_dir / fname
                try:
                    fst = os.lstat(fpath)
                except OSError:
                    continue
                # 严格拒绝符号链接、非常规文件（FIFO/Socket/设备节点）、多重硬链接与超限大文件
                if stat.S_ISLNK(fst.st_mode) or not stat.S_ISREG(fst.st_mode):
                    continue
                if fst.st_nlink != 1:
                    continue
                if fst.st_size > self.max_file_bytes:
                    continue
                try:
                    resolved = fpath.resolve()
                except RuntimeError:
                    continue
                if not resolved.is_relative_to(ws_root):
                    continue
                rel_posix = fpath.relative_to(ws_root).as_posix()
                if any(_is_secret_segment(part) for part in PurePosixPath(rel_posix).parts):
                    continue
                discovered.append((rel_posix, fpath, int(fst.st_mtime_ns), int(fst.st_size)))

        return discovered

    def _embed_chunks(self, chunks: list[CodeChunk]) -> list[str]:
        """批量调用 embedder 生成 L2 单位归一化稠密向量并编码为紧凑 JSON。"""
        if not chunks:
            return []
        encoded: list[str] = []
        for start in range(0, len(chunks), _EMBED_BATCH_SIZE):
            batch = chunks[start : start + _EMBED_BATCH_SIZE]
            texts = [
                _format_chunk_index_text(c.path, c.kind, c.symbol, c.content) for c in batch
            ]
            raw_vectors = self._embedder(texts)
            if len(raw_vectors) != len(batch):
                raise ValueError("embedder 返回向量数量与待编码分块数不一致")
            for vec in raw_vectors:
                normed = _normalize_dense(vec)
                encoded.append(
                    json.dumps([round(v, 6) for v in normed], separators=(",", ":"), allow_nan=False)
                )
        return encoded

    def refresh(self) -> dict[str, Any]:
        """增量刷新 Merkle 索引：未变更文件走 (mtime_ns, size) 快速路径，仅重算变更/新增文件。"""
        with self._lock:
            now = _now_iso()
            discovered = self._scan_workspace_files()

            with contextlib.closing(self._connect()) as conn:
                stored_files: dict[str, tuple[int, int, str]] = {}
                stored_dirs: dict[str, str] = {}
                for row in conn.execute("SELECT path, is_dir, mtime_ns, size, sha256 FROM merkle_nodes"):
                    if int(row["is_dir"]) == 0:
                        stored_files[str(row["path"])] = (
                            int(row["mtime_ns"]),
                            int(row["size"]),
                            str(row["sha256"]),
                        )
                    else:
                        stored_dirs[str(row["path"])] = str(row["sha256"])

                live_file_hashes: dict[str, str] = {}
                files_unchanged = 0
                files_reindexed = 0

                # 待提交变更集合
                meta_only_updates: list[tuple[int, int, str, str]] = []
                reindexed_file_rows: list[tuple[str, int, int, str, str]] = []
                reindexed_paths: list[str] = []
                pending_chunks: list[CodeChunk] = []

                for rel_posix, full_path, mtime_ns, size in discovered:
                    prev = stored_files.get(rel_posix)
                    # 快速路径：(mtime_ns, size) 命中则零读取、零分块、零推理直接复用缓存 SHA-256
                    if prev is not None and prev[0] == mtime_ns and prev[1] == size:
                        live_file_hashes[rel_posix] = prev[2]
                        files_unchanged += 1
                        continue

                    loaded = _safe_read_text_file(full_path, max_bytes=self.max_file_bytes)
                    if loaded is None:
                        continue
                    raw_bytes, text = loaded
                    file_sha = hashlib.sha256(raw_bytes).hexdigest()

                    # mtime 改变但内容哈希完全相同（例如 touch / git checkout 回原内容）：仅刷新元数据
                    if prev is not None and prev[2] == file_sha:
                        live_file_hashes[rel_posix] = file_sha
                        meta_only_updates.append((mtime_ns, size, now, rel_posix))
                        files_unchanged += 1
                        continue

                    file_chunks = _chunk_file_content(rel_posix, text)
                    live_file_hashes[rel_posix] = file_sha
                    reindexed_file_rows.append((rel_posix, mtime_ns, size, file_sha, now))
                    reindexed_paths.append(rel_posix)
                    pending_chunks.extend(file_chunks)
                    files_reindexed += 1

                # 仅对变更/新增文件产生的分块执行批量向量化
                chunk_embeddings = self._embed_chunks(pending_chunks) if pending_chunks else []

                deleted_paths = sorted(set(stored_files.keys()) - set(live_file_hashes.keys()))
                files_deleted = len(deleted_paths)

                root_hash, dir_hashes = _compute_merkle_tree(live_file_hashes)
                stale_dirs = sorted(set(stored_dirs.keys()) - set(dir_hashes.keys()))

                conn.execute("BEGIN IMMEDIATE;")
                try:
                    # 1. 清理已删除文件（同步清除 merkle_nodes 与 code_chunks）
                    if deleted_paths:
                        conn.executemany(
                            "DELETE FROM code_chunks WHERE path = ?",
                            [(p,) for p in deleted_paths],
                        )
                        conn.executemany(
                            "DELETE FROM merkle_nodes WHERE path = ?",
                            [(p,) for p in deleted_paths],
                        )

                    # 2. 更新仅 mtime/size 变化但 SHA-256 未变的文件节点
                    if meta_only_updates:
                        conn.executemany(
                            "UPDATE merkle_nodes SET mtime_ns = ?, size = ?, updated_at = ? WHERE path = ?",
                            meta_only_updates,
                        )

                    # 3. 替换新增或已修改文件的分块与 merkle_nodes 行
                    if reindexed_paths:
                        conn.executemany(
                            "DELETE FROM code_chunks WHERE path = ?",
                            [(p,) for p in reindexed_paths],
                        )
                    if pending_chunks:
                        conn.executemany(
                            """
                            INSERT INTO code_chunks (
                                chunk_id, path, symbol, kind, start_line, end_line,
                                content, embedding_json, updated_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            [
                                (
                                    c.chunk_id,
                                    c.path,
                                    c.symbol,
                                    c.kind,
                                    c.start_line,
                                    c.end_line,
                                    c.content,
                                    emb_json,
                                    now,
                                )
                                for c, emb_json in zip(pending_chunks, chunk_embeddings)
                            ],
                        )
                    if reindexed_file_rows:
                        conn.executemany(
                            """
                            INSERT INTO merkle_nodes (path, is_dir, mtime_ns, size, sha256, updated_at)
                            VALUES (?, 0, ?, ?, ?, ?)
                            ON CONFLICT(path) DO UPDATE SET
                                is_dir = 0,
                                mtime_ns = excluded.mtime_ns,
                                size = excluded.size,
                                sha256 = excluded.sha256,
                                updated_at = excluded.updated_at
                            """,
                            reindexed_file_rows,
                        )

                    # 4. 同步自底向上目录 Merkle 节点（含根节点 path="."）
                    if stale_dirs:
                        conn.executemany(
                            "DELETE FROM merkle_nodes WHERE path = ? AND is_dir = 1",
                            [(d,) for d in stale_dirs],
                        )
                    dir_upserts = [
                        (d_path, child_count, d_hash, now)
                        for d_path, (d_hash, child_count) in dir_hashes.items()
                        if stored_dirs.get(d_path) != d_hash
                    ]
                    if dir_upserts:
                        conn.executemany(
                            """
                            INSERT INTO merkle_nodes (path, is_dir, mtime_ns, size, sha256, updated_at)
                            VALUES (?, 1, 0, ?, ?, ?)
                            ON CONFLICT(path) DO UPDATE SET
                                is_dir = 1,
                                mtime_ns = 0,
                                size = excluded.size,
                                sha256 = excluded.sha256,
                                updated_at = excluded.updated_at
                            """,
                            dir_upserts,
                        )

                    chunks_total = int(
                        conn.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0]
                    )
                    conn.execute("COMMIT;")
                except Exception:
                    conn.execute("ROLLBACK;")
                    raise

            return {
                "ok": True,
                "root_hash": root_hash,
                "files_indexed": len(live_file_hashes),
                "files_reindexed": files_reindexed,
                "files_unchanged": files_unchanged,
                "files_deleted": files_deleted,
                "chunks_total": chunks_total,
            }

    @staticmethod
    def _normalize_path_prefix(path_prefix: str | None) -> str | None:
        if path_prefix is None:
            return None
        if not isinstance(path_prefix, str):
            raise ValueError("path_prefix 必须是字符串或 None")
        cleaned = path_prefix.strip().replace("\\", "/")
        if not cleaned or cleaned in {".", "./"}:
            return None
        while cleaned.startswith("./"):
            cleaned = cleaned[2:]
        cleaned = cleaned.strip("/")
        if not cleaned:
            return None
        pure = PurePosixPath(cleaned)
        if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
            raise ValueError("path_prefix 必须是安全的工作区相对路径前缀")
        return pure.as_posix()

    @staticmethod
    def _make_excerpt(content: str, max_chars: int = _EXCERPT_MAX_CHARS) -> str:
        text = textwrap.dedent(content).strip()
        if len(text) <= max_chars:
            return text
        return text[:max_chars].rstrip() + "\n... [truncated]"

    def search(
        self,
        query: str,
        *,
        limit: int = 8,
        path_prefix: str | None = None,
        auto_refresh: bool = True,
    ) -> dict[str, Any]:
        """执行 BM25 词法 + 持久化稠密向量余弦的双路召回与 RRF（k=60）融合检索。"""
        if not isinstance(query, str) or len(query) > 2000:
            raise ValueError("query 必须是最多 2000 字符的字符串")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
            raise ValueError("limit 必须是 1 到 50 的整数")
        norm_prefix = self._normalize_path_prefix(path_prefix)

        with self._lock:
            root_hash = ""
            if auto_refresh:
                ref_info = self.refresh()
                root_hash = str(ref_info["root_hash"])

            if not query.strip():
                if not root_hash:
                    root_hash = str(self.stats()["root_hash"])
                return {
                    "ok": True,
                    "query": query,
                    "path_prefix": norm_prefix,
                    "root_hash": root_hash,
                    "total_candidates": 0,
                    "results": [],
                }

            with contextlib.closing(self._connect()) as conn:
                if not root_hash:
                    row_root = conn.execute(
                        "SELECT sha256 FROM merkle_nodes WHERE path = '.' AND is_dir = 1"
                    ).fetchone()
                    root_hash = str(row_root["sha256"]) if row_root is not None else ""

                if norm_prefix is not None:
                    escaped = (
                        norm_prefix.replace("\\", "\\\\")
                        .replace("%", "\\%")
                        .replace("_", "\\_")
                    )
                    rows = conn.execute(
                        """
                        SELECT chunk_id, path, symbol, kind, start_line, end_line, content, embedding_json
                        FROM code_chunks
                        WHERE path = ? OR path LIKE ? ESCAPE '\\'
                        ORDER BY chunk_id ASC
                        """,
                        (norm_prefix, f"{escaped}/%"),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT chunk_id, path, symbol, kind, start_line, end_line, content, embedding_json
                        FROM code_chunks
                        ORDER BY chunk_id ASC
                        """
                    ).fetchall()

            if not rows:
                return {
                    "ok": True,
                    "query": query,
                    "path_prefix": norm_prefix,
                    "root_hash": root_hash,
                    "total_candidates": 0,
                    "results": [],
                }

            # 1. BM25+ 非负词法通道（复用 retrieval.tokenize 跨驼峰/蛇形/数字/汉字子词）
            query_terms = Counter(tokenize(query))
            doc_terms: list[Counter[str]] = []
            df: Counter[str] = Counter()
            for row in rows:
                index_text = _format_chunk_index_text(
                    str(row["path"]), str(row["kind"]), str(row["symbol"]), str(row["content"])
                )
                terms = Counter(tokenize(index_text))
                doc_terms.append(terms)
                df.update(terms.keys())

            n_docs = len(rows)
            avg_len = sum(sum(t.values()) for t in doc_terms) / n_docs if n_docs else 1.0
            if avg_len <= 0.0:
                avg_len = 1.0

            bm25_scores: list[float] = []
            if query_terms:
                k1, b = 1.5, 0.75
                for terms in doc_terms:
                    doc_len = sum(terms.values())
                    score = 0.0
                    for term in query_terms:
                        tf = terms.get(term, 0)
                        if not tf:
                            continue
                        term_df = df[term]
                        idf = math.log(1.0 + (n_docs - term_df + 0.5) / (term_df + 0.5))
                        score += idf * tf * (k1 + 1.0) / (tf + k1 * (1.0 - b + b * doc_len / avg_len))
                    bm25_scores.append(score)
            else:
                bm25_scores = [0.0] * n_docs

            # 2. 稠密语义向量余弦通道（仅推理 1 次查询向量，与 SQLite 持久化向量点积）
            cosine_scores: list[float] = [0.0] * n_docs
            if query_terms or any(ch.isalnum() for ch in query):
                raw_q = self._embedder([query])
                if len(raw_q) != 1:
                    raise ValueError("embedder 查询必须返回单个向量")
                q_vec = _normalize_dense(raw_q[0])
                q_dim = len(q_vec)
                for idx, row in enumerate(rows):
                    try:
                        d_vec = json.loads(str(row["embedding_json"]))
                    except (ValueError, TypeError):
                        continue
                    if isinstance(d_vec, list) and len(d_vec) == q_dim:
                        cosine_scores[idx] = sum(qv * float(dv) for qv, dv in zip(q_vec, d_vec))

            # 3. Reciprocal Rank Fusion (RRF k=60)，仅允许严格正相关分块参与投票
            fused: dict[int, float] = {}
            for channel_scores in (bm25_scores, cosine_scores):
                ranked = sorted(
                    (idx for idx, sc in enumerate(channel_scores) if sc > 1e-12),
                    key=lambda idx: (-channel_scores[idx], str(rows[idx]["chunk_id"])),
                )
                for rank, idx in enumerate(ranked, 1):
                    fused[idx] = fused.get(idx, 0.0) + 1.0 / (_RRF_K + rank)

            if not fused:
                return {
                    "ok": True,
                    "query": query,
                    "path_prefix": norm_prefix,
                    "root_hash": root_hash,
                    "total_candidates": n_docs,
                    "results": [],
                }

            ranked_indices = sorted(
                fused.keys(),
                key=lambda idx: (-fused[idx], str(rows[idx]["chunk_id"])),
            )[:limit]

            results = [
                {
                    "chunk_id": str(rows[idx]["chunk_id"]),
                    "path": str(rows[idx]["path"]),
                    "symbol": str(rows[idx]["symbol"]),
                    "kind": str(rows[idx]["kind"]),
                    "start_line": int(rows[idx]["start_line"]),
                    "end_line": int(rows[idx]["end_line"]),
                    "score": round(fused[idx], 8),
                    "excerpt": self._make_excerpt(str(rows[idx]["content"])),
                }
                for idx in ranked_indices
            ]

            return {
                "ok": True,
                "query": query,
                "path_prefix": norm_prefix,
                "root_hash": root_hash,
                "total_candidates": n_docs,
                "results": results,
            }

    def stats(self) -> dict[str, Any]:
        """返回当前持久化代码索引的 Merkle 根哈希、文件数、分块总数与 SQLite 路径。"""
        with self._lock, contextlib.closing(self._connect()) as conn:
            row_root = conn.execute(
                "SELECT sha256 FROM merkle_nodes WHERE path = '.' AND is_dir = 1"
            ).fetchone()
            files_indexed = int(
                conn.execute("SELECT COUNT(*) FROM merkle_nodes WHERE is_dir = 0").fetchone()[0]
            )
            chunks_total = int(
                conn.execute("SELECT COUNT(*) FROM code_chunks").fetchone()[0]
            )
        return {
            "root_hash": str(row_root["sha256"]) if row_root is not None else "",
            "files_indexed": files_indexed,
            "chunks_total": chunks_total,
            "db_path": str(self.db_path),
        }


def execute_codebase_semantic_search(
    index: CodebaseMerkleIndex,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """`codebase_semantic_search` 工具执行适配器。"""
    query = str(arguments.get("query") or "")
    raw_limit = arguments.get("limit", 8)
    limit = 8 if raw_limit is None else int(raw_limit)
    path_prefix = arguments.get("path_prefix")
    return index.search(query, limit=limit, path_prefix=path_prefix, auto_refresh=True)


__all__ = [
    "CODE_INDEX_TOOL_SPECS",
    "CodeChunk",
    "CodebaseMerkleIndex",
    "execute_codebase_semantic_search",
]
