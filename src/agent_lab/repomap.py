"""多语言静态代码地图与符号检索：按跨文件依赖重要度排序并在字符预算内输出。

设计要点：
1. Python 使用标准库 ast 完整解析（含相对导入与嵌套类/函数）；TS/JS/Go/Rust/Java 优先尝试可选的
   tree-sitter 多语言 AST（`tree_sitter_language_pack`），未安装时无损回退至加固的零依赖词法结构扫描器，
   绝不在宿主执行业务代码。
2. 每个文件记录内容 SHA-256；文件修改或删除后，下次查询自动增量更新并返回 stale_refreshed，全程 RLock 线程安全。
3. 自动遵循 .gitignore 并排除虚拟环境、构建产物与敏感凭据文件；支持最大 512 KiB 源码解析，超大规模仓库按非测试优先截断并透明上报 index_truncated。
4. 跨文件重要度使用确定性幂迭代 Personalized PageRank + 子词（camelCase / snake_case）查询匹配增强。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
import hashlib
import json
import os
from pathlib import Path
import re
import threading
from typing import Any

_MAX_PARSE_BYTES = 512 * 1024
_DEFAULT_MAX_INDEX_FILES = 512
_MAX_INDEX_FILES = _DEFAULT_MAX_INDEX_FILES
_MAX_SIGNATURE_LOOKAHEAD = 32

_SOURCE_EXTENSIONS = (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java")
_ALWAYS_SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "__pycache__",
        "dist",
        "build",
        "target",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
    }
)
_SENSITIVE_FILES = frozenset(
    {
        "credentials.py",
        "secrets.py",
        "secret.py",
        "private_key.py",
        "tokens.py",
    }
)
_SENSITIVE_STEMS = frozenset({"credentials", "secrets", "secret", "private_key", "tokens"})


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _format_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
    parts: list[str] = []
    pos_args = list(node.args.posonlyargs) + list(node.args.args)
    defaults_offset = len(pos_args) - len(node.args.defaults)
    for idx, arg in enumerate(pos_args):
        piece = arg.arg
        if arg.annotation is not None:
            piece += ": " + ast.unparse(arg.annotation)
        if idx >= defaults_offset:
            piece += "=..."
        parts.append(piece)
    if node.args.vararg is not None:
        parts.append("*" + node.args.vararg.arg)
    elif node.args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        piece = arg.arg
        if arg.annotation is not None:
            piece += ": " + ast.unparse(arg.annotation)
        if default is not None:
            piece += "=..."
        parts.append(piece)
    if node.args.kwarg is not None:
        parts.append("**" + node.args.kwarg.arg)
    ret = f" -> {ast.unparse(node.returns)}" if node.returns is not None else ""
    signature = f"{prefix}{node.name}({', '.join(parts)}){ret}"
    return signature if len(signature) <= 140 else signature[:137] + "..."


def _first_doc_line(node: ast.AST) -> str:
    doc = (
        ast.get_docstring(node)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        else None
    )
    if not doc:
        return ""
    line = doc.strip().splitlines()[0].strip()
    return line[:80]


@dataclass
class SymbolDef:
    name: str
    qualname: str
    kind: str  # "class" | "function" | "method"
    line: int
    end_line: int
    signature: str
    doc: str = ""


@dataclass
class SymbolRef:
    name: str
    line: int
    kind: str  # "import" | "call"


@dataclass
class FileIndex:
    path: str
    sha256: str
    module: str
    doc: str = ""
    imports: list[str] = field(default_factory=list)
    definitions: list[SymbolDef] = field(default_factory=list)
    references: list[SymbolRef] = field(default_factory=list)
    parse_error: str | None = None


def _module_name_for(rel_posix: str) -> str:
    stem = rel_posix
    for ext in (".tsx", ".jsx", ".java", ".py", ".ts", ".js", ".go", ".rs"):
        if stem.endswith(ext):
            stem = stem[: -len(ext)]
            break
    for suffix in ("/__init__", "/index", "/mod"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem.replace("/", ".")


def _resolve_python_relative_module(
    rel_posix: str,
    module_name: str,
    level: int,
    target_module: str | None,
) -> str:
    if level <= 0:
        return target_module or ""
    is_init = rel_posix == "__init__.py" or rel_posix.endswith("/__init__.py")
    parts = [p for p in module_name.split(".") if p]
    if is_init:
        pkg_parts = [] if rel_posix == "__init__.py" else parts
    else:
        pkg_parts = parts[:-1]
    ascend = level - 1
    if ascend > len(pkg_parts):
        prefix = "." * level
        return f"{prefix}{target_module}" if target_module else prefix
    anchor = pkg_parts[: len(pkg_parts) - ascend] if ascend else list(pkg_parts)
    if target_module:
        anchor.extend(p for p in target_module.split(".") if p)
    return ".".join(anchor) if anchor else ("." * level)


_TRY_STAR_TYPE = getattr(ast, "TryStar", ())
_MATCH_TYPE = getattr(ast, "Match", ())


def _collect_python_defs(
    stmts: list[ast.stmt],
    entry: FileIndex,
    scope_stack: list[str],
    *,
    in_class: bool,
) -> None:
    for node in stmts:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            qualname = ".".join([*scope_stack, node.name])
            kind = "method" if in_class else "function"
            entry.definitions.append(
                SymbolDef(
                    name=node.name,
                    qualname=qualname,
                    kind=kind,
                    line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    signature=_format_signature(node),
                    doc=_first_doc_line(node),
                )
            )
            _collect_python_defs(node.body, entry, [*scope_stack, node.name], in_class=False)
        elif isinstance(node, ast.ClassDef):
            qualname = ".".join([*scope_stack, node.name])
            bases = [ast.unparse(base) for base in node.bases]
            cls_sig = f"class {node.name}" + (f"({', '.join(bases)})" if bases else "")
            entry.definitions.append(
                SymbolDef(
                    name=node.name,
                    qualname=qualname,
                    kind="class",
                    line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    signature=cls_sig[:140],
                    doc=_first_doc_line(node),
                )
            )
            _collect_python_defs(node.body, entry, [*scope_stack, node.name], in_class=True)
        elif isinstance(node, (ast.If, ast.For, ast.AsyncFor, ast.While)):
            _collect_python_defs(node.body, entry, scope_stack, in_class=in_class)
            _collect_python_defs(node.orelse, entry, scope_stack, in_class=in_class)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            _collect_python_defs(node.body, entry, scope_stack, in_class=in_class)
        elif isinstance(node, ast.Try) or (_TRY_STAR_TYPE and isinstance(node, _TRY_STAR_TYPE)):
            _collect_python_defs(node.body, entry, scope_stack, in_class=in_class)
            for handler in node.handlers:
                _collect_python_defs(handler.body, entry, scope_stack, in_class=in_class)
            _collect_python_defs(node.orelse, entry, scope_stack, in_class=in_class)
            _collect_python_defs(node.finalbody, entry, scope_stack, in_class=in_class)
        elif _MATCH_TYPE and isinstance(node, _MATCH_TYPE):
            for case in node.cases:
                _collect_python_defs(case.body, entry, scope_stack, in_class=in_class)


def _parse_python_file(rel_posix: str, source: str) -> FileIndex:
    entry = FileIndex(path=rel_posix, sha256=_sha256(source), module=_module_name_for(rel_posix))
    try:
        tree = ast.parse(source, filename=rel_posix)
    except SyntaxError as exc:
        entry.parse_error = f"line {exc.lineno}: {exc.msg}"
        return entry

    entry.doc = _first_doc_line(tree)
    _collect_python_defs(tree.body, entry, [], in_class=False)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                entry.imports.append(alias.name)
                entry.references.append(
                    SymbolRef(name=alias.name.split(".")[-1], line=node.lineno, kind="import")
                )
        elif isinstance(node, ast.ImportFrom):
            level = getattr(node, "level", 0) or 0
            if level > 0:
                if node.module:
                    resolved = _resolve_python_relative_module(
                        rel_posix, entry.module, level, node.module
                    )
                    if resolved:
                        entry.imports.append(resolved)
                else:
                    added_any = False
                    for alias in node.names:
                        if alias.name != "*":
                            resolved = _resolve_python_relative_module(
                                rel_posix, entry.module, level, alias.name
                            )
                            if resolved:
                                entry.imports.append(resolved)
                                added_any = True
                    if not added_any:
                        resolved = _resolve_python_relative_module(
                            rel_posix, entry.module, level, None
                        )
                        if resolved:
                            entry.imports.append(resolved)
            else:
                base_mod = node.module or ""
                if base_mod:
                    entry.imports.append(base_mod)
            for alias in node.names:
                if alias.name != "*":
                    entry.references.append(SymbolRef(name=alias.name, line=node.lineno, kind="import"))
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                entry.references.append(SymbolRef(name=node.func.id, line=node.lineno, kind="call"))
            elif isinstance(node.func, ast.Attribute):
                entry.references.append(SymbolRef(name=node.func.attr, line=node.lineno, kind="call"))
    return entry


_CALL_SKIP_WORDS = frozenset(
    {
        "if", "else", "for", "while", "do", "switch", "case", "catch", "return", "throw",
        "typeof", "instanceof", "sizeof", "delete", "void", "yield", "await", "function",
        "class", "interface", "enum", "struct", "trait", "impl", "fn", "func", "pub",
        "super", "this", "self", "match", "loop", "where", "type", "import", "from",
        "require", "new", "package", "use", "synchronized", "try",
    }
)
_CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
_NEW_RE = re.compile(r"\bnew\s+([A-Za-z_$][\w$]*)")

_CHAR_LIT_RE = re.compile(
    r"'(?:\\(?:u\{[0-9A-Fa-f_]{1,6}\}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8}|x[0-9A-Fa-f]{2}|[0-7]{1,3}|[^\r\n])|[^'\\\r\n])'"
)
_RS_RAW_STR_START_RE = re.compile(r'(?:[bc])?r(#*)"')
_JS_REGEX_PRECEDING_KEYWORDS = frozenset(
    {
        "return", "case", "throw", "in", "instanceof", "typeof", "void", "delete",
        "new", "yield", "await", "of", "else", "do", "export", "default",
    }
)
_JS_REGEX_PRECEDING_CHARS = frozenset("([{,;:=!&|^~+-*%<>/?}")
_NEW_DECL_START_RE = re.compile(
    r"^(?:export\s+|default\s+|async\s+|abstract\s+|declare\s+|pub(?:\([^)]*\))?\s+|unsafe\s+|"
    r"public\s+|protected\s+|private\s+|static\s+|final\s+|sealed\s+)*"
    r"(?:class|interface|enum|type|function|func|fn|struct|trait|impl|record|package|import|use)\b"
)


def _lex_c_family(source: str, *, lang: str) -> tuple[list[str], list[str], list[bool]]:
    """移除 // 与 /* */ 注释并保持 1:1 行号；正确词法化字符串、字符/rune、Rust 原始串、JS/TS 正则与模板插值。"""
    no_comment: list[str] = []
    masked: list[str] = []
    line_in_template_expr: list[bool] = [False]
    state: str | None = None
    block_depth = 0
    template_brace_stack: list[int] = []
    regex_allowed = True
    i = 0
    n = len(source)

    while i < n:
        ch = source[i]
        nxt = source[i + 1] if i + 1 < n else ""

        if state is None:
            if ch == "/" and nxt == "/":
                state = "line"
                no_comment.append("  ")
                masked.append("  ")
                i += 2
                continue
            if ch == "/" and nxt == "*":
                state = "block"
                block_depth = 1
                no_comment.append("  ")
                masked.append("  ")
                i += 2
                continue

            # Rust 原始字符串 r"..." / r#"..."# / br#"..."# / cr#"..."#
            if (
                lang == "rust"
                and ch in ("r", "b", "c")
                and (i == 0 or not (source[i - 1].isalnum() or source[i - 1] == "_"))
            ):
                rm = _RS_RAW_STR_START_RE.match(source, i)
                if rm is not None:
                    prefix = rm.group(0)
                    close_delim = '"' + "#" * len(rm.group(1))
                    no_comment.append(prefix)
                    masked.append(prefix)
                    i = rm.end()
                    while i < n and not source.startswith(close_delim, i):
                        rc = source[i]
                        if rc == "\n":
                            no_comment.append("\n")
                            masked.append("\n")
                            line_in_template_expr.append(bool(template_brace_stack))
                        else:
                            no_comment.append(rc)
                            masked.append(" ")
                        i += 1
                    if i < n:
                        no_comment.append(close_delim)
                        masked.append(close_delim)
                        i += len(close_delim)
                    continue

            # Java 文本块 """..."""
            if lang == "java" and source.startswith('"""', i):
                no_comment.append('"""')
                masked.append('"""')
                i += 3
                while i < n and not source.startswith('"""', i):
                    tc = source[i]
                    if tc == "\\" and i + 1 < n and source[i + 1] != "\n":
                        no_comment.append(source[i : i + 2])
                        masked.append("  ")
                        i += 2
                        continue
                    if tc == "\n":
                        no_comment.append("\n")
                        masked.append("\n")
                        line_in_template_expr.append(False)
                    else:
                        no_comment.append(tc)
                        masked.append(" ")
                    i += 1
                if i < n:
                    no_comment.append('"""')
                    masked.append('"""')
                    i += 3
                continue

            # Go / Rust / Java 单引号字符或 rune 字面量（兼容 '{' / '"' / '\''，不误伤 Rust 'a 生命周期）
            if lang in ("go", "rust", "java") and ch == "'":
                cm = _CHAR_LIT_RE.match(source, i)
                if cm is not None:
                    lit = cm.group(0)
                    no_comment.append(lit)
                    masked.append("'" + " " * (len(lit) - 2) + "'")
                    i = cm.end()
                    continue

            # JS/TS 正则字面量 /.../（排除 JSX 闭合标签 </ 与除法操作符）
            if (
                lang == "js"
                and ch == "/"
                and regex_allowed
                and not (i > 0 and source[i - 1] == "<")
            ):
                j = i + 1
                in_class = False
                found_end = -1
                while j < n:
                    rc = source[j]
                    if rc in ("\n", "\r"):
                        break
                    if rc == "\\" and j + 1 < n and source[j + 1] not in ("\n", "\r"):
                        j += 2
                        continue
                    if rc == "[" and not in_class:
                        in_class = True
                    elif rc == "]" and in_class:
                        in_class = False
                    elif rc == "/" and not in_class:
                        found_end = j
                        break
                    j += 1
                if found_end > i + 1:
                    lit = source[i : found_end + 1]
                    no_comment.append(lit)
                    masked.append("/" + " " * (len(lit) - 2) + "/")
                    regex_allowed = False
                    i = found_end + 1
                    continue

            # 字符串字面量
            if ch == '"':
                state = '"'
                no_comment.append(ch)
                masked.append(ch)
                i += 1
                continue
            if lang == "js" and ch == "'":
                state = "'"
                no_comment.append(ch)
                masked.append(ch)
                i += 1
                continue
            if lang == "js" and ch == "`":
                state = "template"
                no_comment.append(ch)
                masked.append(ch)
                i += 1
                continue
            if lang == "go" and ch == "`":
                state = "go_raw"
                no_comment.append(ch)
                masked.append(ch)
                i += 1
                continue

            # JS/TS 模板字符串插值 ${...} 闭合与嵌套花括号跟踪
            if lang == "js":
                if ch == "{":
                    if template_brace_stack:
                        template_brace_stack[-1] += 1
                    no_comment.append(ch)
                    masked.append(ch)
                    regex_allowed = True
                    i += 1
                    continue
                if ch == "}":
                    if template_brace_stack:
                        if template_brace_stack[-1] == 0:
                            template_brace_stack.pop()
                            state = "template"
                            no_comment.append("}")
                            masked.append(" ")
                            i += 1
                            continue
                        template_brace_stack[-1] -= 1
                    no_comment.append(ch)
                    masked.append(ch)
                    regex_allowed = True
                    i += 1
                    continue
                if ch.isalpha() or ch in ("_", "$"):
                    j = i + 1
                    while j < n and (source[j].isalnum() or source[j] in ("_", "$")):
                        j += 1
                    ident = source[i:j]
                    no_comment.append(ident)
                    masked.append(ident)
                    regex_allowed = ident in _JS_REGEX_PRECEDING_KEYWORDS
                    i = j
                    continue
                if ch.isdigit():
                    no_comment.append(ch)
                    masked.append(ch)
                    regex_allowed = False
                    i += 1
                    continue
                if (ch == "+" and nxt == "+") or (ch == "-" and nxt == "-"):
                    no_comment.append(ch + nxt)
                    masked.append(ch + nxt)
                    regex_allowed = False
                    i += 2
                    continue
                if ch in _JS_REGEX_PRECEDING_CHARS:
                    regex_allowed = True
                elif ch in (")", "]"):
                    regex_allowed = False

            no_comment.append(ch)
            masked.append(ch)
            if ch == "\n":
                line_in_template_expr.append(bool(template_brace_stack))
            i += 1

        elif state == "line":
            if ch == "\n":
                state = None
                no_comment.append("\n")
                masked.append("\n")
                line_in_template_expr.append(bool(template_brace_stack))
            else:
                no_comment.append(" ")
                masked.append(" ")
            i += 1

        elif state == "block":
            if lang == "rust" and ch == "/" and nxt == "*":
                block_depth += 1
                no_comment.append("  ")
                masked.append("  ")
                i += 2
            elif ch == "*" and nxt == "/":
                block_depth -= 1
                if block_depth <= 0:
                    state = None
                no_comment.append("  ")
                masked.append("  ")
                i += 2
            elif ch == "\n":
                no_comment.append("\n")
                masked.append("\n")
                line_in_template_expr.append(bool(template_brace_stack))
                i += 1
            else:
                no_comment.append(" ")
                masked.append(" ")
                i += 1

        elif state == "go_raw":
            if ch == "`":
                state = None
                no_comment.append(ch)
                masked.append(ch)
            elif ch == "\n":
                no_comment.append("\n")
                masked.append("\n")
                line_in_template_expr.append(False)
            else:
                no_comment.append(ch)
                masked.append(" ")
            i += 1

        elif state == "template":
            if ch == "\\" and i + 1 < n:
                if nxt == "\n":
                    no_comment.append("\\\n")
                    masked.append(" \n")
                    line_in_template_expr.append(bool(template_brace_stack))
                else:
                    no_comment.append(ch + nxt)
                    masked.append("  ")
                i += 2
                continue
            if ch == "$" and nxt == "{":
                template_brace_stack.append(0)
                state = None
                regex_allowed = True
                no_comment.append("${")
                masked.append("  ")
                i += 2
                continue
            if ch == "`":
                state = None
                regex_allowed = False
                no_comment.append(ch)
                masked.append(ch)
                i += 1
                continue
            if ch == "\n":
                no_comment.append("\n")
                masked.append("\n")
                line_in_template_expr.append(bool(template_brace_stack))
            else:
                no_comment.append(ch)
                masked.append(" ")
            i += 1

        else:
            # 双引号或 JS 单引号字符串
            if ch == "\\" and i + 1 < n and nxt != "\n":
                no_comment.append(ch + nxt)
                masked.append("  ")
                i += 2
                continue
            if ch == "\n":
                if lang != "rust":
                    state = None
                no_comment.append("\n")
                masked.append("\n")
                line_in_template_expr.append(bool(template_brace_stack))
                i += 1
                continue
            if ch == state:
                state = None
                regex_allowed = False
                no_comment.append(ch)
                masked.append(ch)
            else:
                no_comment.append(ch)
                masked.append(" ")
            i += 1

    return "".join(no_comment).splitlines(), "".join(masked).splitlines(), line_in_template_expr


def _strip_c_comments(source: str, *, single_quote_strings: bool) -> tuple[list[str], list[str]]:
    """向后兼容封装：按语言模式剥离注释并屏蔽字面量。"""
    no_comment, masked, _ = _lex_c_family(source, lang="js" if single_quote_strings else "go")
    return no_comment, masked


def _skip_balanced_angles(text: str, pos: int) -> int | None:
    """从 `<` 开始跳过任意深度嵌套的泛型参数 `<...>`，自动忽略 `->` 与 `=>` 箭头。"""
    n = len(text)
    if pos >= n or text[pos] != "<":
        return None
    depth = 0
    i = pos
    while i < n:
        ch = text[i]
        if ch in ("-", "=") and i + 1 < n and text[i + 1] == ">":
            i += 2
            continue
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _collect_header_text(
    lines: list[str],
    start_idx: int,
    *,
    max_lines: int = _MAX_SIGNATURE_LOOKAHEAD,
) -> str:
    """收集跨越最多 max_lines 行的声明头文本（截至顶层 `{` 或 `;`）。"""
    parts: list[str] = []
    paren_depth = 0
    for idx in range(start_idx, min(len(lines), start_idx + max_lines)):
        seg = lines[idx].strip()
        if not seg:
            if parts and paren_depth == 0:
                break
            continue
        stop = False
        cut = len(seg)
        for j, ch in enumerate(seg):
            if ch == "(":
                paren_depth += 1
            elif ch == ")":
                paren_depth = max(0, paren_depth - 1)
            elif paren_depth == 0 and ch in ("{", ";"):
                cut = j
                stop = True
                break
        piece = seg[:cut].strip()
        if piece:
            parts.append(piece)
        if stop:
            break
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def _find_block_end(
    masked_lines: list[str],
    start_idx: int,
    *,
    max_header_lines: int = _MAX_SIGNATURE_LOOKAHEAD,
) -> int:
    depth = 0
    paren_depth = 0
    opened = False
    for idx in range(start_idx, len(masked_lines)):
        line = masked_lines[idx]
        if idx > start_idx and not opened and paren_depth == 0:
            stripped = line.strip()
            if stripped and _NEW_DECL_START_RE.match(stripped):
                return idx
        for ch in line:
            if ch == "(":
                paren_depth += 1
            elif ch == ")":
                paren_depth = max(0, paren_depth - 1)
            elif ch == "{":
                if opened:
                    depth += 1
                elif paren_depth == 0:
                    opened = True
                    depth = 1
            elif ch == "}":
                if opened:
                    depth -= 1
                    if depth <= 0:
                        return idx + 1
            elif ch == ";" and not opened and paren_depth == 0:
                return idx + 1
        if not opened and idx >= start_idx + max_header_lines:
            return start_idx + 1
    return len(masked_lines)


def _header_signature(
    no_comment_lines: list[str],
    start_idx: int,
    *,
    max_header_lines: int = 16,
) -> str:
    sig = _collect_header_text(no_comment_lines, start_idx, max_lines=max_header_lines)
    return sig if len(sig) <= 140 else sig[:137] + "..."


def _record_calls(
    entry: FileIndex,
    masked_lines: list[str],
    def_names_by_line: dict[int, set[str]],
    skip_line_prefixes: tuple[str, ...],
) -> None:
    for idx, masked in enumerate(masked_lines):
        stripped = masked.strip()
        if not stripped or stripped.startswith(skip_line_prefixes):
            continue
        lineno = idx + 1
        defined_here = def_names_by_line.get(lineno, set())
        for m in _NEW_RE.finditer(masked):
            ident = m.group(1)
            if ident not in _CALL_SKIP_WORDS and ident not in defined_here:
                entry.references.append(SymbolRef(name=ident, line=lineno, kind="call"))
        for m in _CALL_RE.finditer(masked):
            ident = m.group(1)
            if ident not in _CALL_SKIP_WORDS and ident not in defined_here:
                entry.references.append(SymbolRef(name=ident, line=lineno, kind="call"))


_TS_IMPORT_RE = re.compile(
    r'\bimport\s+(?:type\s+)?([\s\S]*?)\s+from\s+["\']([^"\']+)["\']|\bimport\s+["\']([^"\']+)["\']'
)
_TS_REQUIRE_RE = re.compile(r'\brequire\s*\(\s*["\']([^"\']+)["\']\s*\)')
_TS_CLASS_RE = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][\w$]*)")
_TS_IFACE_RE = re.compile(r"^(?:export\s+)?(?:default\s+)?interface\s+([A-Za-z_$][\w$]*)")
_TS_ENUM_RE = re.compile(r"^(?:export\s+)?(?:const\s+)?enum\s+([A-Za-z_$][\w$]*)")
_TS_TYPE_RE = re.compile(r"^(?:export\s+)?type\s+([A-Za-z_$][\w$]*)\b")
_TS_FUNC_PREFIX_RE = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*"
)
_TS_ARROW_RE = re.compile(
    r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:<[^>]*>\s*)?\([^)]*\)\s*(?::[^=>]+)?=>"
)
_TS_METHOD_PREFIX_RE = re.compile(
    r"^(?:(?:public|private|protected|static|readonly|override|abstract|async|get|set)\s+)*\*?([A-Za-z_$][\w$]*)\s*"
)


def _match_callable_after_optional_generics(prefix_re: re.Pattern[str], text: str) -> str | None:
    m = prefix_re.match(text)
    if m is None:
        return None
    pos = m.end()
    n = len(text)
    if pos < n and text[pos] == "<":
        after = _skip_balanced_angles(text, pos)
        if after is None:
            return None
        pos = after
        while pos < n and text[pos].isspace():
            pos += 1
    if pos < n and text[pos] == "(":
        return m.group(1)
    return None


def _parse_ts_js_file(rel_posix: str, source: str) -> FileIndex:
    entry = FileIndex(path=rel_posix, sha256=_sha256(source), module=_module_name_for(rel_posix))
    no_comment_lines, masked_lines, line_in_tpl = _lex_c_family(source, lang="js")
    joined = "\n".join(no_comment_lines)
    for m in _TS_IMPORT_RE.finditer(joined):
        lineno = 1 + joined.count("\n", 0, m.start())
        spec = m.group(2) or m.group(3)
        if spec:
            entry.imports.append(spec)
        clause = (m.group(1) or "").strip()
        if "{" in clause and "}" in clause:
            braced = clause[clause.index("{") + 1 : clause.rindex("}")]
            for part in braced.split(","):
                tok = re.sub(r"^type\s+", "", part.strip())
                if not tok:
                    continue
                pieces = [p.strip() for p in re.split(r"\s+as\s+", tok) if p.strip()]
                for ident in pieces:
                    if re.match(r"^[A-Za-z_$][\w$]*$", ident):
                        entry.references.append(SymbolRef(name=ident, line=lineno, kind="import"))
            clause = (clause[: clause.index("{")] + clause[clause.rindex("}") + 1 :]).strip(" ,")
        for tok in clause.split(","):
            tok = tok.strip()
            if tok.startswith("* as "):
                ident = tok[5:].strip()
            else:
                ident = tok
            if re.match(r"^[A-Za-z_$][\w$]*$", ident):
                entry.references.append(SymbolRef(name=ident, line=lineno, kind="import"))
    for m in _TS_REQUIRE_RE.finditer(joined):
        lineno = 1 + joined.count("\n", 0, m.start())
        spec = m.group(1)
        entry.imports.append(spec)
        leaf = spec.rstrip("/").split("/")[-1].split(".")[0]
        if re.match(r"^[A-Za-z_$][\w$]*$", leaf):
            entry.references.append(SymbolRef(name=leaf, line=lineno, kind="import"))

    brace_depth = 0
    containers: list[tuple[str, int]] = []
    pending_container: str | None = None
    def_by_line: dict[int, set[str]] = {}
    for idx, clean in enumerate(no_comment_lines):
        stripped = clean.strip()
        lineno = idx + 1
        depth_before = brace_depth
        opened_cls: str | None = None
        in_tpl_expr = idx < len(line_in_tpl) and line_in_tpl[idx]
        if stripped and not in_tpl_expr:
            if m := _TS_CLASS_RE.match(stripped):
                opened_cls = m.group(1)
                entry.definitions.append(
                    SymbolDef(
                        opened_cls,
                        opened_cls,
                        "class",
                        lineno,
                        _find_block_end(masked_lines, idx),
                        _header_signature(no_comment_lines, idx),
                    )
                )
                def_by_line.setdefault(lineno, set()).add(opened_cls)
            elif m := _TS_IFACE_RE.match(stripped):
                name = m.group(1)
                entry.definitions.append(
                    SymbolDef(
                        name,
                        name,
                        "interface",
                        lineno,
                        _find_block_end(masked_lines, idx),
                        _header_signature(no_comment_lines, idx),
                    )
                )
                def_by_line.setdefault(lineno, set()).add(name)
            elif m := _TS_ENUM_RE.match(stripped):
                name = m.group(1)
                entry.definitions.append(
                    SymbolDef(
                        name,
                        name,
                        "enum",
                        lineno,
                        _find_block_end(masked_lines, idx),
                        _header_signature(no_comment_lines, idx),
                    )
                )
                def_by_line.setdefault(lineno, set()).add(name)
            elif depth_before == 0 and (m := _TS_TYPE_RE.match(stripped)):
                name = m.group(1)
                entry.definitions.append(
                    SymbolDef(
                        name,
                        name,
                        "type",
                        lineno,
                        _find_block_end(masked_lines, idx),
                        _header_signature(no_comment_lines, idx),
                    )
                )
                def_by_line.setdefault(lineno, set()).add(name)
            elif depth_before == 0 and (
                fn_name := _match_callable_after_optional_generics(
                    _TS_FUNC_PREFIX_RE,
                    _collect_header_text(no_comment_lines, idx, max_lines=8)
                    if stripped.startswith(("export ", "default ", "async ", "function"))
                    else stripped,
                )
            ):
                entry.definitions.append(
                    SymbolDef(
                        fn_name,
                        fn_name,
                        "function",
                        lineno,
                        _find_block_end(masked_lines, idx),
                        _header_signature(no_comment_lines, idx),
                    )
                )
                def_by_line.setdefault(lineno, set()).add(fn_name)
            elif depth_before == 0 and (
                m := _TS_ARROW_RE.match(" ".join(s.strip() for s in no_comment_lines[idx : idx + 8]))
            ):
                name = m.group(1)
                entry.definitions.append(
                    SymbolDef(
                        name,
                        name,
                        "function",
                        lineno,
                        _find_block_end(masked_lines, idx),
                        _header_signature(no_comment_lines, idx),
                    )
                )
                def_by_line.setdefault(lineno, set()).add(name)
            elif containers and depth_before == containers[-1][1] and (
                method_name := _match_callable_after_optional_generics(
                    _TS_METHOD_PREFIX_RE,
                    _collect_header_text(no_comment_lines, idx, max_lines=8)
                    if "<" in stripped and "(" not in stripped
                    else stripped,
                )
            ):
                if method_name not in _CALL_SKIP_WORDS:
                    cls_name = containers[-1][0]
                    entry.definitions.append(
                        SymbolDef(
                            method_name,
                            f"{cls_name}.{method_name}",
                            "method",
                            lineno,
                            _find_block_end(masked_lines, idx),
                            _header_signature(no_comment_lines, idx),
                        )
                    )
                    def_by_line.setdefault(lineno, set()).add(method_name)
        masked = masked_lines[idx]
        if opened_cls and ";" not in masked.split("{", 1)[0]:
            pending_container = opened_cls
        brace_depth = max(0, brace_depth + masked.count("{") - masked.count("}"))
        if pending_container and brace_depth > depth_before:
            containers.append((pending_container, brace_depth))
            pending_container = None
        while containers and brace_depth < containers[-1][1]:
            containers.pop()

    _record_calls(entry, masked_lines, def_by_line, ("import ", "export {"))
    return entry


_GO_IMPORT_ITEM_RE = re.compile(r'^(?:([A-Za-z_]\w*|\.)\s+)?"([^"]+)"')
_GO_SINGLE_IMPORT_RE = re.compile(r'^import\s+(?:([A-Za-z_]\w*|\.)\s+)?"([^"]+)"')
_GO_TYPE_RE = re.compile(r"^type\s+([A-Za-z_]\w*)\s+(struct|interface)?\b")
_GO_METHOD_RE = re.compile(
    r"^func\s*\(\s*(?:[A-Za-z_]\w*\s+)?\*?([A-Za-z_]\w*)(?:\[[^\]]*\])?\s*\)\s*([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*\("
)
_GO_FUNC_RE = re.compile(r"^func\s+([A-Za-z_]\w*)\s*(?:\[[^\]]*\])?\s*\(")


def _parse_go_file(rel_posix: str, source: str) -> FileIndex:
    entry = FileIndex(path=rel_posix, sha256=_sha256(source), module=_module_name_for(rel_posix))
    no_comment_lines, masked_lines, _ = _lex_c_family(source, lang="go")
    in_import_block = False
    brace_depth = 0
    def_by_line: dict[int, set[str]] = {}
    for idx, clean in enumerate(no_comment_lines):
        stripped = clean.strip()
        lineno = idx + 1
        if in_import_block:
            if stripped.startswith(")"):
                in_import_block = False
            elif m := _GO_IMPORT_ITEM_RE.match(stripped):
                alias, pkg = m.group(1), m.group(2)
                entry.imports.append(pkg)
                leaf = pkg.rstrip("/").split("/")[-1]
                entry.references.append(SymbolRef(name=leaf, line=lineno, kind="import"))
                if alias and alias != "." and alias != leaf:
                    entry.references.append(SymbolRef(name=alias, line=lineno, kind="import"))
        elif stripped.startswith("import") and "(" in stripped:
            in_import_block = ")" not in stripped
        elif m := _GO_SINGLE_IMPORT_RE.match(stripped):
            alias, pkg = m.group(1), m.group(2)
            entry.imports.append(pkg)
            leaf = pkg.rstrip("/").split("/")[-1]
            entry.references.append(SymbolRef(name=leaf, line=lineno, kind="import"))
            if alias and alias != "." and alias != leaf:
                entry.references.append(SymbolRef(name=alias, line=lineno, kind="import"))
        elif brace_depth == 0 and stripped:
            if m := _GO_METHOD_RE.match(stripped):
                recv_type, method_name = m.group(1), m.group(2)
                entry.definitions.append(
                    SymbolDef(
                        method_name,
                        f"{recv_type}.{method_name}",
                        "method",
                        lineno,
                        _find_block_end(masked_lines, idx),
                        _header_signature(no_comment_lines, idx),
                    )
                )
                def_by_line.setdefault(lineno, set()).add(method_name)
            elif m := _GO_FUNC_RE.match(stripped):
                name = m.group(1)
                entry.definitions.append(
                    SymbolDef(
                        name,
                        name,
                        "function",
                        lineno,
                        _find_block_end(masked_lines, idx),
                        _header_signature(no_comment_lines, idx),
                    )
                )
                def_by_line.setdefault(lineno, set()).add(name)
            elif m := _GO_TYPE_RE.match(stripped):
                name, kw = m.group(1), m.group(2) or "type"
                entry.definitions.append(
                    SymbolDef(
                        name,
                        name,
                        kw,
                        lineno,
                        _find_block_end(masked_lines, idx),
                        _header_signature(no_comment_lines, idx),
                    )
                )
                def_by_line.setdefault(lineno, set()).add(name)
        masked = masked_lines[idx]
        brace_depth = max(0, brace_depth + masked.count("{") - masked.count("}"))

    _record_calls(entry, masked_lines, def_by_line, ("import ", "package "))
    return entry


_RS_USE_RE = re.compile(r"(?:^|\n)\s*(?:pub(?:\([^)]*\))?\s+)?use\s+([^;]+);")
_RS_ITEM_RE = re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(struct|enum|trait|type)\s+([A-Za-z_]\w*)")
_RS_IMPL_START_RE = re.compile(r"^(?:unsafe\s+)?impl(?=\s|<)")
_RS_FN_PREFIX_RE = re.compile(
    r'^(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|const|unsafe|extern(?:\s+"[^"]*")?)\s+)*fn\s+([A-Za-z_]\w*)\s*'
)
_JAVA_IMPORT_RE = re.compile(r"^import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;")
_JAVA_TYPE_RE = re.compile(
    r"^(?:(?:public|protected|private|abstract|final|static|sealed|non-sealed)\s+)*(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)"
)
_JAVA_METHOD_MODS_RE = re.compile(
    r"^(?:(?:public|protected|private|static|final|synchronized|abstract|default|native)\s+)*"
)


def _extract_rust_impl_target(header: str) -> str | None:
    """解析任意嵌套泛型与 trait-for 形式的 Rust `impl` 目标类型名。"""
    m = _RS_IMPL_START_RE.match(header)
    if m is None:
        return None
    pos = m.end()
    n = len(header)
    while pos < n and header[pos].isspace():
        pos += 1
    if pos < n and header[pos] == "<":
        after_gen = _skip_balanced_angles(header, pos)
        if after_gen is None:
            return None
        pos = after_gen
    body_part = header[pos:]
    angle = paren = bracket = 0
    where_idx: int | None = None
    for_idx: int | None = None
    i = 0
    m_len = len(body_part)
    while i < m_len:
        ch = body_part[i]
        if ch in ("-", "=") and i + 1 < m_len and body_part[i + 1] == ">":
            i += 2
            continue
        if ch == "<":
            angle += 1
        elif ch == ">":
            angle = max(0, angle - 1)
        elif ch == "(":
            paren += 1
        elif ch == ")":
            paren = max(0, paren - 1)
        elif ch == "[":
            bracket += 1
        elif ch == "]":
            bracket = max(0, bracket - 1)
        elif angle == 0 and paren == 0 and bracket == 0:
            if ch in ("{", ";"):
                where_idx = i if where_idx is None else min(where_idx, i)
                break
            if (
                body_part.startswith("where", i)
                and (i == 0 or not (body_part[i - 1].isalnum() or body_part[i - 1] == "_"))
                and (i + 5 == m_len or not (body_part[i + 5].isalnum() or body_part[i + 5] == "_"))
            ):
                where_idx = i
                break
            if (
                for_idx is None
                and body_part.startswith("for", i)
                and (i == 0 or not (body_part[i - 1].isalnum() or body_part[i - 1] == "_"))
                and (i + 3 == m_len or not (body_part[i + 3].isalnum() or body_part[i + 3] == "_"))
            ):
                for_idx = i
        i += 1
    if where_idx is not None:
        body_part = body_part[:where_idx]
    target_expr = body_part[for_idx + 3 :].strip() if for_idx is not None else body_part.strip()
    if not target_expr:
        return None
    target_expr = re.sub(
        r"^(?:&\s*(?:'[A-Za-z_]\w*\s+)?(?:mut\s+)?|dyn\s+|impl\s+)+", "", target_expr
    ).strip()
    top_head = target_expr.split("<", 1)[0].strip()
    ident_matches = re.findall(r"[A-Za-z_]\w*", top_head)
    return ident_matches[-1] if ident_matches else None


def _match_java_method(stripped: str) -> str | None:
    """支持嵌套泛型返回类型与方法级泛型参数列表的 Java 方法声明匹配。"""
    m = _JAVA_METHOD_MODS_RE.match(stripped)
    pos = m.end() if m else 0
    n = len(stripped)
    if pos < n and stripped[pos] == "<":
        after = _skip_balanced_angles(stripped, pos)
        if after is None:
            return None
        pos = after
        while pos < n and stripped[pos].isspace():
            pos += 1
    # 解析返回类型（含嵌套泛型与数组维度）
    ret_start = pos
    ret_word = re.match(r"([A-Za-z_$][\w$.]*)", stripped[pos:])
    if ret_word is None:
        return None
    if ret_word.group(1) in {"new", "return", "throw", "package", "import", "class", "interface", "enum", "record"}:
        return None
    pos += ret_word.end()
    if pos < n and stripped[pos] == "<":
        after = _skip_balanced_angles(stripped, pos)
        if after is None:
            return None
        pos = after
    while pos < n and stripped[pos : pos + 2] == "[]":
        pos += 2
    if pos == ret_start or pos >= n or not stripped[pos].isspace():
        return None
    while pos < n and stripped[pos].isspace():
        pos += 1
    name_m = re.match(r"([A-Za-z_$][\w$]*)\s*\(", stripped[pos:])
    return name_m.group(1) if name_m else None


def _parse_rust_file(rel_posix: str, source: str) -> FileIndex:
    entry = FileIndex(path=rel_posix, sha256=_sha256(source), module=_module_name_for(rel_posix))
    no_comment_lines, masked_lines, _ = _lex_c_family(source, lang="rust")
    joined = "\n".join(no_comment_lines)
    for m in _RS_USE_RE.finditer(joined):
        lineno = 1 + joined.count("\n", 0, m.start(1))
        expr = m.group(1).strip()
        if "{" in expr and "}" in expr:
            base = expr.split("{", 1)[0].rstrip(": ")
            if base:
                entry.imports.append(base)
            inner = expr[expr.index("{") + 1 : expr.rindex("}")]
            for ident in re.findall(r"[A-Za-z_]\w*", inner):
                if ident not in {"self", "super", "crate", "as"}:
                    entry.references.append(SymbolRef(name=ident, line=lineno, kind="import"))
        else:
            raw_path = expr.split(" as ", 1)[0].strip()
            entry.imports.append(raw_path)
            leaf = raw_path.split("::")[-1].strip()
            if leaf and leaf != "*" and leaf not in {"self", "super", "crate"}:
                entry.references.append(SymbolRef(name=leaf, line=lineno, kind="import"))

    brace_depth = 0
    containers: list[tuple[str, int]] = []
    pending_impl: str | None = None
    def_by_line: dict[int, set[str]] = {}
    for idx, clean in enumerate(no_comment_lines):
        stripped = clean.strip()
        lineno = idx + 1
        depth_before = brace_depth
        opened_impl: str | None = None
        if stripped:
            if depth_before == 0 and _RS_IMPL_START_RE.match(stripped):
                header = _collect_header_text(no_comment_lines, idx, max_lines=16)
                opened_impl = _extract_rust_impl_target(header)
            elif depth_before == 0 and (m := _RS_ITEM_RE.match(stripped)):
                kw, name = m.group(1), m.group(2)
                kind = "interface" if kw == "trait" else kw
                entry.definitions.append(
                    SymbolDef(
                        name,
                        name,
                        kind,
                        lineno,
                        _find_block_end(masked_lines, idx),
                        _header_signature(no_comment_lines, idx),
                    )
                )
                def_by_line.setdefault(lineno, set()).add(name)
            elif fn_name := _match_callable_after_optional_generics(
                _RS_FN_PREFIX_RE,
                _collect_header_text(no_comment_lines, idx, max_lines=8)
                if "fn " in stripped or stripped.startswith("fn ")
                else stripped,
            ):
                if containers and depth_before == containers[-1][1]:
                    owner = containers[-1][0]
                    entry.definitions.append(
                        SymbolDef(
                            fn_name,
                            f"{owner}.{fn_name}",
                            "method",
                            lineno,
                            _find_block_end(masked_lines, idx),
                            _header_signature(no_comment_lines, idx),
                        )
                    )
                    def_by_line.setdefault(lineno, set()).add(fn_name)
                elif depth_before == 0:
                    entry.definitions.append(
                        SymbolDef(
                            fn_name,
                            fn_name,
                            "function",
                            lineno,
                            _find_block_end(masked_lines, idx),
                            _header_signature(no_comment_lines, idx),
                        )
                    )
                    def_by_line.setdefault(lineno, set()).add(fn_name)
        masked = masked_lines[idx]
        if opened_impl and ";" not in masked.split("{", 1)[0]:
            pending_impl = opened_impl
        brace_depth = max(0, brace_depth + masked.count("{") - masked.count("}"))
        if pending_impl and brace_depth > depth_before:
            containers.append((pending_impl, brace_depth))
            pending_impl = None
        while containers and brace_depth < containers[-1][1]:
            containers.pop()

    _record_calls(entry, masked_lines, def_by_line, ("use ", "pub use ", "mod "))
    return entry


def _parse_java_file(rel_posix: str, source: str) -> FileIndex:
    entry = FileIndex(path=rel_posix, sha256=_sha256(source), module=_module_name_for(rel_posix))
    no_comment_lines, masked_lines, _ = _lex_c_family(source, lang="java")
    brace_depth = 0
    containers: list[tuple[str, int]] = []
    pending_type: str | None = None
    def_by_line: dict[int, set[str]] = {}
    for idx, clean in enumerate(no_comment_lines):
        stripped = clean.strip()
        lineno = idx + 1
        depth_before = brace_depth
        opened_type: str | None = None
        if stripped:
            if m := _JAVA_IMPORT_RE.match(stripped):
                imp = m.group(1)
                entry.imports.append(imp)
                leaf = imp.split(".")[-1]
                if leaf != "*":
                    entry.references.append(SymbolRef(name=leaf, line=lineno, kind="import"))
            elif m := _JAVA_TYPE_RE.match(stripped):
                kw, name = m.group(1), m.group(2)
                kind = "struct" if kw == "record" else kw
                opened_type = name
                entry.definitions.append(
                    SymbolDef(
                        name,
                        name,
                        kind,
                        lineno,
                        _find_block_end(masked_lines, idx),
                        _header_signature(no_comment_lines, idx),
                    )
                )
                def_by_line.setdefault(lineno, set()).add(name)
            elif containers and depth_before == containers[-1][1] and (
                method_name := _match_java_method(stripped)
            ):
                if method_name not in _CALL_SKIP_WORDS:
                    owner = containers[-1][0]
                    entry.definitions.append(
                        SymbolDef(
                            method_name,
                            f"{owner}.{method_name}",
                            "method",
                            lineno,
                            _find_block_end(masked_lines, idx),
                            _header_signature(no_comment_lines, idx),
                        )
                    )
                    def_by_line.setdefault(lineno, set()).add(method_name)
        masked = masked_lines[idx]
        if opened_type and ";" not in masked.split("{", 1)[0]:
            pending_type = opened_type
        brace_depth = max(0, brace_depth + masked.count("{") - masked.count("}"))
        if pending_type and brace_depth > depth_before:
            containers.append((pending_type, brace_depth))
            pending_type = None
        while containers and brace_depth < containers[-1][1]:
            containers.pop()

    _record_calls(entry, masked_lines, def_by_line, ("import ", "package "))
    return entry


# ---------------------------------------------------------------------------
# 可选 tree-sitter 多语言 AST 提取（优先尝试 tree_sitter_language_pack，缺失时无损回退）
# ---------------------------------------------------------------------------

_TS_LANG_BY_EXT: dict[str, str] = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
}
_TS_PARSER_TLS = threading.local()


def _tree_sitter_lang_for(rel_posix: str) -> str | None:
    for ext, lang in _TS_LANG_BY_EXT.items():
        if rel_posix.endswith(ext):
            return lang
    return None


def _get_tree_sitter_parser(lang_name: str) -> Any | None:
    """线程隔离地懒加载 `tree_sitter_language_pack` 解析器；未安装或加载失败时返回 None。"""
    cache: dict[str, Any] | None = getattr(_TS_PARSER_TLS, "parsers", None)
    if cache is None:
        cache = {}
        _TS_PARSER_TLS.parsers = cache
    if lang_name in cache:
        return cache[lang_name]
    try:
        import tree_sitter_language_pack as tslp  # type: ignore[import-not-found]
    except ImportError:
        return None
    try:
        parser = tslp.get_parser(lang_name)
    except Exception:
        cache[lang_name] = None
        return None
    cache[lang_name] = parser
    return parser


def _node_text(source_bytes: bytes, node: Any) -> str:
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")


def _node_signature(source_bytes: bytes, node: Any) -> str:
    raw = _node_text(source_bytes, node)
    head = raw.split("{", 1)[0].split(";", 1)[0]
    sig = re.sub(r"\s+", " ", head).strip()
    return sig if len(sig) <= 140 else sig[:137] + "..."


def _child_by_field(node: Any, field_name: str) -> Any | None:
    getter = getattr(node, "child_by_field_name", None)
    if callable(getter):
        return getter(field_name)
    return None


def _first_named_child(node: Any, types: tuple[str, ...]) -> Any | None:
    for child in getattr(node, "named_children", ()):
        if child.type in types:
            return child
    return None


def _extract_ts_node_defs_and_refs(
    lang: str,
    root: Any,
    source_bytes: bytes,
    entry: FileIndex,
) -> None:
    def _record_call_name(name: str, line: int) -> None:
        clean = name.strip()
        if clean and clean not in _CALL_SKIP_WORDS:
            entry.references.append(SymbolRef(name=clean, line=line, kind="call"))

    def _walk_js(node: Any, containers: list[str]) -> None:
        ntype = node.type
        line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        if ntype == "import_statement":
            src_node = _child_by_field(node, "source") or _first_named_child(node, ("string",))
            if src_node is not None:
                spec = _node_text(source_bytes, src_node).strip("\"'`")
                if spec:
                    entry.imports.append(spec)
            for child in node.named_children:
                if child.type == "import_clause":
                    for sub in child.named_children:
                        if sub.type == "identifier":
                            entry.references.append(SymbolRef(_node_text(source_bytes, sub), line, "import"))
                        elif sub.type == "namespace_import":
                            ident = _first_named_child(sub, ("identifier",))
                            if ident is not None:
                                entry.references.append(SymbolRef(_node_text(source_bytes, ident), line, "import"))
                        elif sub.type == "named_imports":
                            for spec_node in sub.named_children:
                                if spec_node.type == "import_specifier":
                                    for part_field in ("name", "alias"):
                                        part = _child_by_field(spec_node, part_field)
                                        if part is not None:
                                            entry.references.append(
                                                SymbolRef(_node_text(source_bytes, part), line, "import")
                                            )
            return

        if ntype in ("class_declaration", "abstract_class_declaration"):
            name_node = _child_by_field(node, "name") or _first_named_child(
                node, ("type_identifier", "identifier")
            )
            if name_node is not None:
                cls_name = _node_text(source_bytes, name_node)
                entry.definitions.append(
                    SymbolDef(cls_name, cls_name, "class", line, end_line, _node_signature(source_bytes, node))
                )
                body = _child_by_field(node, "body") or _first_named_child(node, ("class_body",))
                if body is not None:
                    for ch in body.named_children:
                        _walk_js(ch, [*containers, cls_name])
                return

        if ntype == "interface_declaration":
            name_node = _child_by_field(node, "name") or _first_named_child(node, ("type_identifier", "identifier"))
            if name_node is not None:
                name = _node_text(source_bytes, name_node)
                entry.definitions.append(
                    SymbolDef(name, name, "interface", line, end_line, _node_signature(source_bytes, node))
                )
            return

        if ntype == "enum_declaration":
            name_node = _child_by_field(node, "name") or _first_named_child(node, ("identifier",))
            if name_node is not None:
                name = _node_text(source_bytes, name_node)
                entry.definitions.append(
                    SymbolDef(name, name, "enum", line, end_line, _node_signature(source_bytes, node))
                )
            return

        if ntype == "type_alias_declaration":
            name_node = _child_by_field(node, "name") or _first_named_child(node, ("type_identifier", "identifier"))
            if name_node is not None:
                name = _node_text(source_bytes, name_node)
                entry.definitions.append(
                    SymbolDef(name, name, "type", line, end_line, _node_signature(source_bytes, node))
                )
            return

        if ntype in ("function_declaration", "generator_function_declaration"):
            name_node = _child_by_field(node, "name") or _first_named_child(node, ("identifier",))
            if name_node is not None and not containers:
                name = _node_text(source_bytes, name_node)
                entry.definitions.append(
                    SymbolDef(name, name, "function", line, end_line, _node_signature(source_bytes, node))
                )
        elif ntype == "variable_declarator" and not containers:
            name_node = _child_by_field(node, "name")
            val_node = _child_by_field(node, "value")
            if (
                name_node is not None
                and name_node.type == "identifier"
                and val_node is not None
                and val_node.type in ("arrow_function", "function_expression", "function")
            ):
                name = _node_text(source_bytes, name_node)
                entry.definitions.append(
                    SymbolDef(name, name, "function", line, end_line, _node_signature(source_bytes, node))
                )
        elif ntype in ("method_definition", "abstract_method_signature", "method_signature") and containers:
            name_node = _child_by_field(node, "name") or _first_named_child(
                node, ("property_identifier", "identifier")
            )
            if name_node is not None:
                mname = _node_text(source_bytes, name_node)
                if mname not in _CALL_SKIP_WORDS:
                    entry.definitions.append(
                        SymbolDef(
                            mname,
                            f"{containers[-1]}.{mname}",
                            "method",
                            line,
                            end_line,
                            _node_signature(source_bytes, node),
                        )
                    )
        elif ntype == "call_expression":
            fn_node = _child_by_field(node, "function")
            if fn_node is not None:
                if fn_node.type == "identifier":
                    fn_name = _node_text(source_bytes, fn_node)
                    if fn_name == "require":
                        args = _child_by_field(node, "arguments")
                        str_arg = _first_named_child(args, ("string",)) if args is not None else None
                        if str_arg is not None:
                            spec = _node_text(source_bytes, str_arg).strip("\"'`")
                            if spec:
                                entry.imports.append(spec)
                                leaf = spec.rstrip("/").split("/")[-1].split(".")[0]
                                if re.match(r"^[A-Za-z_$][\w$]*$", leaf):
                                    entry.references.append(SymbolRef(leaf, line, "import"))
                    else:
                        _record_call_name(fn_name, line)
                elif fn_node.type == "member_expression":
                    prop = _child_by_field(fn_node, "property")
                    if prop is not None:
                        _record_call_name(_node_text(source_bytes, prop), line)
        elif ntype == "new_expression":
            ctor = _child_by_field(node, "constructor")
            if ctor is not None:
                if ctor.type == "identifier":
                    _record_call_name(_node_text(source_bytes, ctor), line)
                elif ctor.type == "member_expression":
                    prop = _child_by_field(ctor, "property")
                    if prop is not None:
                        _record_call_name(_node_text(source_bytes, prop), line)

        for ch in node.named_children:
            _walk_js(ch, containers)

    def _walk_go(node: Any) -> None:
        ntype = node.type
        line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        if ntype == "import_spec":
            path_node = _child_by_field(node, "path") or _first_named_child(
                node, ("interpreted_string_literal", "raw_string_literal")
            )
            if path_node is not None:
                pkg = _node_text(source_bytes, path_node).strip('"`')
                if pkg:
                    entry.imports.append(pkg)
                    leaf = pkg.rstrip("/").split("/")[-1]
                    entry.references.append(SymbolRef(leaf, line, "import"))
                    alias_node = _child_by_field(node, "name")
                    if alias_node is not None:
                        alias = _node_text(source_bytes, alias_node).strip()
                        if alias and alias not in (".", "_", leaf):
                            entry.references.append(SymbolRef(alias, line, "import"))
            return

        if ntype == "type_spec":
            name_node = _child_by_field(node, "name") or _first_named_child(node, ("type_identifier",))
            type_node = _child_by_field(node, "type")
            if name_node is not None:
                name = _node_text(source_bytes, name_node)
                kind = "type"
                if type_node is not None:
                    if type_node.type == "struct_type":
                        kind = "struct"
                    elif type_node.type == "interface_type":
                        kind = "interface"
                entry.definitions.append(
                    SymbolDef(name, name, kind, line, end_line, _node_signature(source_bytes, node))
                )
        elif ntype == "function_declaration":
            name_node = _child_by_field(node, "name") or _first_named_child(node, ("identifier",))
            if name_node is not None:
                name = _node_text(source_bytes, name_node)
                entry.definitions.append(
                    SymbolDef(name, name, "function", line, end_line, _node_signature(source_bytes, node))
                )
        elif ntype == "method_declaration":
            name_node = _child_by_field(node, "name") or _first_named_child(node, ("field_identifier",))
            recv_node = _child_by_field(node, "receiver")
            if name_node is not None and recv_node is not None:
                mname = _node_text(source_bytes, name_node)
                recv_text = _node_text(source_bytes, recv_node)
                recv_m = re.search(r"\*?([A-Za-z_]\w*)(?:\[[^\]]*\])?\s*\)$", recv_text.strip())
                recv_type = recv_m.group(1) if recv_m else ""
                qual = f"{recv_type}.{mname}" if recv_type else mname
                entry.definitions.append(
                    SymbolDef(mname, qual, "method", line, end_line, _node_signature(source_bytes, node))
                )
        elif ntype == "call_expression":
            fn_node = _child_by_field(node, "function")
            if fn_node is not None:
                if fn_node.type == "identifier":
                    _record_call_name(_node_text(source_bytes, fn_node), line)
                elif fn_node.type == "selector_expression":
                    fld = _child_by_field(fn_node, "field")
                    if fld is not None:
                        _record_call_name(_node_text(source_bytes, fld), line)

        for ch in node.named_children:
            _walk_go(ch)

    def _walk_rust(node: Any, owner: str | None) -> None:
        ntype = node.type
        line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        if ntype == "use_declaration":
            raw = _node_text(source_bytes, node)
            m = _RS_USE_RE.search("\n" + raw)
            if m is not None:
                expr = m.group(1).strip()
                if "{" in expr and "}" in expr:
                    base = expr.split("{", 1)[0].rstrip(": ")
                    if base:
                        entry.imports.append(base)
                    inner = expr[expr.index("{") + 1 : expr.rindex("}")]
                    for ident in re.findall(r"[A-Za-z_]\w*", inner):
                        if ident not in {"self", "super", "crate", "as"}:
                            entry.references.append(SymbolRef(ident, line, "import"))
                else:
                    raw_path = expr.split(" as ", 1)[0].strip()
                    entry.imports.append(raw_path)
                    leaf = raw_path.split("::")[-1].strip()
                    if leaf and leaf != "*" and leaf not in {"self", "super", "crate"}:
                        entry.references.append(SymbolRef(leaf, line, "import"))
            return

        if ntype in ("struct_item", "enum_item", "trait_item", "type_item"):
            name_node = _child_by_field(node, "name") or _first_named_child(node, ("type_identifier",))
            if name_node is not None:
                name = _node_text(source_bytes, name_node)
                kind_map = {
                    "struct_item": "struct",
                    "enum_item": "enum",
                    "trait_item": "interface",
                    "type_item": "type",
                }
                entry.definitions.append(
                    SymbolDef(name, name, kind_map[ntype], line, end_line, _node_signature(source_bytes, node))
                )
        elif ntype == "impl_item":
            type_node = _child_by_field(node, "type")
            impl_owner = None
            if type_node is not None:
                t_head = _node_text(source_bytes, type_node).split("<", 1)[0]
                idents = re.findall(r"[A-Za-z_]\w*", t_head)
                impl_owner = idents[-1] if idents else None
            body = _child_by_field(node, "body") or _first_named_child(node, ("declaration_list",))
            if body is not None:
                for ch in body.named_children:
                    _walk_rust(ch, impl_owner)
            return
        elif ntype == "function_item":
            name_node = _child_by_field(node, "name") or _first_named_child(node, ("identifier",))
            if name_node is not None:
                fname = _node_text(source_bytes, name_node)
                if owner:
                    entry.definitions.append(
                        SymbolDef(
                            fname,
                            f"{owner}.{fname}",
                            "method",
                            line,
                            end_line,
                            _node_signature(source_bytes, node),
                        )
                    )
                else:
                    entry.definitions.append(
                        SymbolDef(fname, fname, "function", line, end_line, _node_signature(source_bytes, node))
                    )
        elif ntype == "call_expression":
            fn_node = _child_by_field(node, "function")
            if fn_node is not None:
                if fn_node.type == "generic_function":
                    fn_node = _child_by_field(fn_node, "function") or fn_node
                if fn_node.type == "identifier":
                    _record_call_name(_node_text(source_bytes, fn_node), line)
                elif fn_node.type == "scoped_identifier":
                    nm = _child_by_field(fn_node, "name")
                    if nm is not None:
                        _record_call_name(_node_text(source_bytes, nm), line)
                elif fn_node.type == "field_expression":
                    fld = _child_by_field(fn_node, "field")
                    if fld is not None:
                        _record_call_name(_node_text(source_bytes, fld), line)

        for ch in node.named_children:
            _walk_rust(ch, owner)

    def _walk_java(node: Any, containers: list[str]) -> None:
        ntype = node.type
        line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1

        if ntype == "import_declaration":
            raw = _node_text(source_bytes, node).strip()
            m = _JAVA_IMPORT_RE.match(raw)
            if m is not None:
                imp = m.group(1)
                entry.imports.append(imp)
                leaf = imp.split(".")[-1]
                if leaf != "*":
                    entry.references.append(SymbolRef(leaf, line, "import"))
            return

        if ntype in ("class_declaration", "interface_declaration", "enum_declaration", "record_declaration"):
            name_node = _child_by_field(node, "name") or _first_named_child(node, ("identifier",))
            if name_node is not None:
                tname = _node_text(source_bytes, name_node)
                kind_map = {
                    "class_declaration": "class",
                    "interface_declaration": "interface",
                    "enum_declaration": "enum",
                    "record_declaration": "struct",
                }
                entry.definitions.append(
                    SymbolDef(tname, tname, kind_map[ntype], line, end_line, _node_signature(source_bytes, node))
                )
                body = _child_by_field(node, "body")
                if body is not None:
                    for ch in body.named_children:
                        _walk_java(ch, [*containers, tname])
                return
        elif ntype == "method_declaration" and containers:
            name_node = _child_by_field(node, "name") or _first_named_child(node, ("identifier",))
            if name_node is not None:
                mname = _node_text(source_bytes, name_node)
                if mname not in _CALL_SKIP_WORDS:
                    entry.definitions.append(
                        SymbolDef(
                            mname,
                            f"{containers[-1]}.{mname}",
                            "method",
                            line,
                            end_line,
                            _node_signature(source_bytes, node),
                        )
                    )
        elif ntype == "method_invocation":
            nm = _child_by_field(node, "name")
            if nm is not None:
                _record_call_name(_node_text(source_bytes, nm), line)
        elif ntype == "object_creation_expression":
            tp = _child_by_field(node, "type")
            if tp is not None:
                head = _node_text(source_bytes, tp).split("<", 1)[0].split(".")[-1].strip()
                if head:
                    _record_call_name(head, line)

        for ch in node.named_children:
            _walk_java(ch, containers)

    if lang in ("typescript", "tsx", "javascript"):
        _walk_js(root, [])
    elif lang == "go":
        _walk_go(root)
    elif lang == "rust":
        _walk_rust(root, None)
    elif lang == "java":
        _walk_java(root, [])


def _parse_with_tree_sitter(rel_posix: str, source: str) -> FileIndex | None:
    """尝试使用可选 `tree_sitter_language_pack` 解析多语言 AST；不可用或失败时返回 None 以回退零依赖扫描器。"""
    lang = _tree_sitter_lang_for(rel_posix)
    if lang is None:
        return None
    parser = _get_tree_sitter_parser(lang)
    if parser is None:
        return None
    source_bytes = source.encode("utf-8")
    try:
        tree = parser.parse(source_bytes)
        root = getattr(tree, "root_node", None)
        if root is None:
            return None
        entry = FileIndex(path=rel_posix, sha256=_sha256(source), module=_module_name_for(rel_posix))
        _extract_ts_node_defs_and_refs(lang, root, source_bytes, entry)
        return entry
    except Exception:
        return None


def _parse_source_file(rel_posix: str, source: str) -> FileIndex:
    if rel_posix.endswith(".py"):
        return _parse_python_file(rel_posix, source)
    ts_entry = _parse_with_tree_sitter(rel_posix, source)
    if ts_entry is not None:
        return ts_entry
    if rel_posix.endswith((".ts", ".tsx", ".js", ".jsx")):
        return _parse_ts_js_file(rel_posix, source)
    if rel_posix.endswith(".go"):
        return _parse_go_file(rel_posix, source)
    if rel_posix.endswith(".rs"):
        return _parse_rust_file(rel_posix, source)
    if rel_posix.endswith(".java"):
        return _parse_java_file(rel_posix, source)
    return FileIndex(path=rel_posix, sha256=_sha256(source), module=_module_name_for(rel_posix))


def _load_gitignore_patterns(workspace: Path) -> list[str]:
    gi = workspace / ".gitignore"
    if gi.is_symlink() or not gi.is_file():
        return []
    try:
        if gi.stat().st_size > _MAX_PARSE_BYTES:
            return []
        raw = gi.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return []
    patterns: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or stripped.startswith("!"):
            continue
        patterns.append(stripped)
    return patterns


def _matches_gitignore(rel_posix: str, *, is_dir: bool, patterns: list[str]) -> bool:
    if not patterns:
        return False
    parts = rel_posix.split("/")
    basename = parts[-1]
    dir_parts = parts if is_dir else parts[:-1]
    for raw in patterns:
        anchored = raw.startswith("/")
        dir_only = raw.endswith("/")
        pat = raw.strip("/")
        if pat.startswith("**/"):
            pat = pat[3:]
            anchored = False
        if not pat:
            continue
        alt_pat = pat.replace("/**/", "/")
        if dir_only:
            if anchored or "/" in pat:
                if (
                    rel_posix == pat
                    or rel_posix.startswith(pat + "/")
                    or (is_dir and (fnmatchcase(rel_posix, pat) or fnmatchcase(rel_posix, alt_pat)))
                ):
                    return True
            elif any(fnmatchcase(seg, pat) for seg in dir_parts):
                return True
            continue
        if anchored:
            if rel_posix == pat or rel_posix.startswith(pat + "/"):
                return True
            if "/" in pat and (fnmatchcase(rel_posix, pat) or fnmatchcase(rel_posix, alt_pat)):
                return True
            if "/" not in pat and "/" not in rel_posix and fnmatchcase(rel_posix, pat):
                return True
        elif "/" in pat:
            if (
                rel_posix == pat
                or rel_posix.startswith(pat + "/")
                or fnmatchcase(rel_posix, pat)
                or fnmatchcase(rel_posix, alt_pat)
            ):
                return True
        elif fnmatchcase(basename, pat) or any(fnmatchcase(seg, pat) for seg in parts):
            return True
    return False


def _is_sensitive_file(filename: str) -> bool:
    lowered = filename.casefold()
    if lowered in _SENSITIVE_FILES:
        return True
    return Path(lowered).stem in _SENSITIVE_STEMS


_SUBTOKEN_BOUNDARY_1 = re.compile(r"([A-Z]+)([A-Z][a-z])")
_SUBTOKEN_BOUNDARY_2 = re.compile(r"([a-z0-9])([A-Z])")
_RAW_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[㐀-鿿]")


def _extract_subtokens(text: str) -> set[str]:
    """拆分 camelCase、PascalCase 缩写、snake_case、kebab-case 与汉字子词。"""
    tokens: set[str] = set()
    for raw in _RAW_WORD_RE.findall(text):
        lowered = raw.casefold()
        if len(lowered) >= 2 or "㐀" <= lowered <= "鿿":
            tokens.add(lowered)
        split = _SUBTOKEN_BOUNDARY_1.sub(r"\1_\2", raw)
        split = _SUBTOKEN_BOUNDARY_2.sub(r"\1_\2", split)
        for part in split.split("_"):
            sub = part.casefold()
            if len(sub) >= 2:
                tokens.add(sub)
    return tokens


def _personalized_pagerank(
    nodes: list[str],
    out_edges: dict[str, dict[str, float]],
    personalization: dict[str, float],
    *,
    reference_prior: float = 1.0,
    damping: float = 0.85,
    max_iters: int = 30,
    tol: float = 1e-10,
) -> tuple[dict[str, float], float]:
    """确定性幂迭代 Personalized PageRank，返回收敛分布与零入度参考基线。"""
    n = len(nodes)
    if n == 0:
        return {}, 1.0
    total_p = sum(personalization.get(v, 0.0) for v in nodes)
    if total_p <= 0.0:
        p = {v: 1.0 / n for v in nodes}
        ref_p = 1.0 / n
    else:
        p = {v: personalization.get(v, 0.0) / total_p for v in nodes}
        ref_p = (reference_prior / total_p) if reference_prior > 0.0 else (1.0 / n)
    out_total = {u: sum(out_edges.get(u, {}).values()) for u in nodes}
    dangling = [u for u in nodes if out_total[u] <= 0.0]

    rank = dict(p)
    for _ in range(max_iters):
        dangling_mass = sum(rank[u] for u in dangling)
        base_coeff = (1.0 - damping) + damping * dangling_mass
        next_rank = {v: base_coeff * p[v] for v in nodes}
        for u in nodes:
            tw = out_total[u]
            if tw <= 0.0:
                continue
            share = damping * rank[u] / tw
            for v, w in out_edges[u].items():
                next_rank[v] += share * w
        diff = sum(abs(next_rank[v] - rank[v]) for v in nodes)
        rank = next_rank
        if diff < tol:
            break

    final_dangling = sum(rank[u] for u in dangling)
    baseline = ((1.0 - damping) + damping * final_dangling) * ref_p
    return rank, baseline


def _is_test_path(rel_posix: str) -> bool:
    return rel_posix.startswith("tests/") or "/tests/" in rel_posix


class RepoMapIndex:
    """带文件摘要校验的仓库符号索引：磁盘变更后自动增量刷新，绝不静默返回旧行号。"""

    def __init__(self, workspace: Path, max_index_files: int = _DEFAULT_MAX_INDEX_FILES):
        root = Path(workspace).absolute()
        if root.is_symlink() or not root.is_dir():
            raise ValueError("workspace 必须是真实目录")
        if type(max_index_files) is not int or max_index_files < 1:
            raise ValueError("max_index_files 必须是正整数")
        self.workspace = root.resolve()
        self.max_index_files = max_index_files
        self._lock = threading.RLock()
        self._cache: dict[str, FileIndex] = {}
        self._initialized = False

    def _discover_source_files(self) -> tuple[list[Path], int]:
        patterns = _load_gitignore_patterns(self.workspace)
        eligible: list[tuple[str, Path]] = []
        for directory, subdirs, names in os.walk(self.workspace, followlinks=False):
            dir_path = Path(directory)
            kept_subdirs: list[str] = []
            for d in sorted(subdirs):
                if d.startswith(".") or d in _ALWAYS_SKIP_DIRS:
                    continue
                sub_path = dir_path / d
                if sub_path.is_symlink():
                    continue
                sub_rel = sub_path.relative_to(self.workspace).as_posix()
                if _matches_gitignore(sub_rel, is_dir=True, patterns=patterns):
                    continue
                kept_subdirs.append(d)
            subdirs[:] = kept_subdirs
            for name in sorted(names):
                if name.startswith(".") or not name.endswith(_SOURCE_EXTENSIONS) or _is_sensitive_file(name):
                    continue
                candidate = dir_path / name
                if candidate.is_symlink():
                    continue
                rel = candidate.relative_to(self.workspace).as_posix()
                if _matches_gitignore(rel, is_dir=False, patterns=patterns):
                    continue
                try:
                    info = candidate.stat()
                except OSError:
                    continue
                if info.st_nlink == 1 and info.st_size <= _MAX_PARSE_BYTES:
                    eligible.append((rel, candidate))
        eligible.sort(key=lambda item: (_is_test_path(item[0]), item[0]))
        total_found = len(eligible)
        selected = [cand for _, cand in eligible[: self.max_index_files]]
        return selected, total_found

    def refresh(self) -> dict[str, Any]:
        """核对磁盘文件 SHA-256，增量重解析改动文件并剔除已删除文件。"""
        with self._lock:
            selected_paths, total_found = self._discover_source_files()
            current_paths: set[str] = set()
            refreshed: list[str] = []
            for path in selected_paths:
                rel = path.relative_to(self.workspace).as_posix()
                try:
                    source = path.read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    continue
                current_paths.add(rel)
                digest = _sha256(source)
                cached = self._cache.get(rel)
                if cached is None or cached.sha256 != digest:
                    if self._initialized and cached is not None:
                        refreshed.append(rel)
                    self._cache[rel] = _parse_source_file(rel, source)
            removed = sorted(set(self._cache) - current_paths)
            for rel in removed:
                self._cache.pop(rel, None)
            was_initialized = self._initialized
            self._initialized = True
            manifest = [{"path": k, "sha256": self._cache[k].sha256} for k in sorted(self._cache)]
            return {
                "index_sha256": _sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"))),
                "indexed_files": len(self._cache),
                "discovered_files": total_found,
                "index_truncated": total_found > self.max_index_files,
                "stale_refreshed": refreshed if was_initialized else [],
                "removed_files": removed if was_initialized else [],
            }

    def _dependency_scores(self, focus_query: str = "") -> dict[str, float]:
        nodes = sorted(self._cache.keys())
        if not nodes:
            return {}
        raw_terms = [t for t in focus_query.casefold().split() if t]
        query_subtokens = _extract_subtokens(focus_query) if focus_query.strip() else set()

        defined_in: dict[str, list[str]] = {}
        modules: dict[str, str] = {}
        for rel in nodes:
            entry = self._cache[rel]
            modules[entry.module] = rel
            if entry.module.startswith("src."):
                modules.setdefault(entry.module[4:], rel)
            modules.setdefault(entry.module.split(".")[-1], rel)
            if "/" in rel:
                parent_pkg = rel.rsplit("/", 1)[0].split("/")[-1]
                modules.setdefault(parent_pkg, rel)
            seen_defs: set[str] = set()
            for sym in entry.definitions:
                if sym.name not in seen_defs:
                    seen_defs.add(sym.name)
                    defined_in.setdefault(sym.name, []).append(rel)

        out_edges: dict[str, dict[str, float]] = {rel: {} for rel in nodes}
        in_edges: dict[str, dict[str, float]] = {rel: {} for rel in nodes}
        caller_import_bonus: dict[str, float] = {rel: 0.0 for rel in nodes}
        query_boost: dict[str, float] = {rel: 0.0 for rel in nodes}

        for caller_rel in nodes:
            entry = self._cache[caller_rel]
            for imp in entry.imports:
                norm = re.sub(r"^(?:\.\.?/)+", "", imp.strip()).replace("::", ".").replace("/", ".")
                for ext in (".tsx", ".jsx", ".java", ".py", ".ts", ".js", ".go", ".rs"):
                    if norm.endswith(ext):
                        norm = norm[: -len(ext)]
                        break
                target = modules.get(imp) or modules.get(norm) or modules.get(norm.split(".")[-1])
                if target and target != caller_rel:
                    out_edges[caller_rel][target] = out_edges[caller_rel].get(target, 0.0) + 3.0
                    in_edges[target][caller_rel] = in_edges[target].get(caller_rel, 0.0) + 3.0
                    caller_import_bonus[caller_rel] += 0.5

            seen_refs = {(ref.name, ref.kind) for ref in entry.references}
            for name, kind in sorted(seen_refs):
                targets = defined_in.get(name, ())
                if not targets or len(targets) > 16:
                    continue
                base_weight = 2.5 if kind == "call" else 1.5
                weight = base_weight / len(targets) if len(targets) > 8 else base_weight
                for target in targets:
                    if target != caller_rel:
                        out_edges[caller_rel][target] = out_edges[caller_rel].get(target, 0.0) + weight
                        in_edges[target][caller_rel] = in_edges[target].get(caller_rel, 0.0) + weight

            if raw_terms or query_subtokens:
                raw_text = (
                    caller_rel
                    + " "
                    + " ".join(
                        sym.qualname + " " + sym.signature + " " + sym.doc
                        for sym in entry.definitions
                    )
                )
                haystack = raw_text.casefold()
                exact_hits = sum(1 for term in raw_terms if term in haystack)
                file_subtokens = _extract_subtokens(raw_text)
                sub_hits = sum(1 for tok in query_subtokens if tok in file_subtokens)
                sub_ratio = (sub_hits / len(query_subtokens)) if query_subtokens else 0.0
                query_boost[caller_rel] = exact_hits * 5.0 + sub_hits * 1.5 + sub_ratio * 3.0

        personalization = {
            rel: (2.5 if not _is_test_path(rel) else 1.0) + query_boost[rel]
            for rel in nodes
        }
        ref_prior = 2.5 if any(not _is_test_path(rel) for rel in nodes) else 1.0
        ppr_rank, baseline = _personalized_pagerank(
            nodes, out_edges, personalization, reference_prior=ref_prior
        )
        amp = {
            rel: (ppr_rank[rel] / baseline if baseline > 0.0 else 1.0)
            for rel in nodes
        }

        scores: dict[str, float] = {}
        for rel in nodes:
            base = (
                1.0
                + (1.5 if not _is_test_path(rel) else 0.0)
                + caller_import_bonus[rel]
                + query_boost[rel]
            )
            propagated = sum(amp[caller] * w for caller, w in in_edges[rel].items())
            scores[rel] = base + propagated
        return scores

    def build_map(self, max_chars: int = 2000, focus_query: str = "") -> dict[str, Any]:
        """在给定字符预算内生成按依赖重要度排序的仓库符号地图。"""
        if type(max_chars) is not int or not 200 <= max_chars <= 8000:
            raise ValueError("max_chars 必须是 200—8000 之间的整数")
        with self._lock:
            meta = self.refresh()
            scores = self._dependency_scores(focus_query)
            ordered = sorted(self._cache.values(), key=lambda item: (-scores[item.path], item.path))
            sections: list[str] = []
            included: list[str] = []
            used = 0
            truncated = False
            for entry in ordered:
                lines = [f"{entry.path} (sha256:{entry.sha256[:10]})"]
                if entry.parse_error:
                    lines.append(f"  ! syntax_error: {entry.parse_error}")
                else:
                    local_imports = sorted(set(entry.imports))[:6]
                    if local_imports:
                        lines.append(f"  imports: {', '.join(local_imports)}")
                    for sym in entry.definitions:
                        depth = min(3, sym.qualname.count("."))
                        indent = "  " * (1 + depth) if depth else ("    " if sym.kind == "method" else "  ")
                        lines.append(f"{indent}L{sym.line}: {sym.signature}")
                block = "\n".join(lines)
                sep = 1 if sections else 0
                if used + sep + len(block) > max_chars:
                    # 若完整文件块超预算，尝试保留文件头与前几条核心定义，绝不半行截断。
                    compact: list[str] = []
                    for line in lines:
                        extra = (1 if compact else 0) + len(line)
                        current_compact = sum(len(x) for x in compact) + max(0, len(compact) - 1)
                        if used + sep + current_compact + extra > max_chars:
                            truncated = True
                            break
                        compact.append(line)
                    if len(compact) >= 2:
                        part = "\n".join(compact)
                        sections.append(part)
                        included.append(entry.path)
                        used += sep + len(part)
                    else:
                        truncated = True
                    break
                sections.append(block)
                included.append(entry.path)
                used += sep + len(block)
            rendered = "\n".join(sections)
            return {
                "ok": True,
                **meta,
                "max_chars": max_chars,
                "rendered_chars": len(rendered),
                "truncated": truncated,
                "files_included": included,
                "map": rendered,
            }

    def search_symbol(self, symbol: str) -> dict[str, Any]:
        """跨文件定位符号定义、签名、行号及调用/导入位置。"""
        query = symbol.strip()
        if not query or len(query) > 120 or "://" in query:
            raise ValueError("symbol 必须是 1—120 字符的标识符查询")
        with self._lock:
            meta = self.refresh()
            needle = query.casefold()
            definitions: list[dict[str, Any]] = []
            references: list[dict[str, Any]] = []
            for entry in sorted(self._cache.values(), key=lambda item: item.path):
                for sym in entry.definitions:
                    exact = sym.name.casefold() == needle or sym.qualname.casefold() == needle
                    if exact or needle in sym.qualname.casefold():
                        definitions.append(
                            {
                                "path": entry.path,
                                "file_sha256": entry.sha256,
                                "name": sym.name,
                                "qualname": sym.qualname,
                                "kind": sym.kind,
                                "line": sym.line,
                                "end_line": sym.end_line,
                                "signature": sym.signature,
                                "doc": sym.doc,
                                "exact": exact,
                            }
                        )
                for ref in entry.references:
                    if ref.name.casefold() == needle or needle in ref.name.casefold():
                        references.append(
                            {
                                "path": entry.path,
                                "file_sha256": entry.sha256,
                                "symbol": ref.name,
                                "line": ref.line,
                                "kind": ref.kind,
                            }
                        )
            definitions.sort(
                key=lambda d: (not d["exact"], _is_test_path(d["path"]), d["path"], d["line"])
            )
            return {
                "ok": True,
                "symbol": query,
                **meta,
                "definitions": definitions[:25],
                "references": references[:40],
            }
