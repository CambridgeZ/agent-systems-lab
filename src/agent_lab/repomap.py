"""多语言静态代码地图与符号检索：按跨文件依赖重要度排序并在字符预算内输出。

设计要点：
1. Python 使用标准库 ast 完整解析；TS/JS/Go/Rust/Java 使用纯静态行级结构解析，绝不在宿主执行业务代码。
2. 每个文件记录内容 SHA-256；文件修改或删除后，下次查询自动增量更新并返回 stale_refreshed，全程 RLock 线程安全。
3. 自动遵循 .gitignore 并排除虚拟环境、构建产物与敏感凭据文件；超大规模仓库按非测试优先截断并透明上报 index_truncated。
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

_MAX_PARSE_BYTES = 64 * 1024
_DEFAULT_MAX_INDEX_FILES = 512
_MAX_INDEX_FILES = _DEFAULT_MAX_INDEX_FILES

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


def _parse_python_file(rel_posix: str, source: str) -> FileIndex:
    entry = FileIndex(path=rel_posix, sha256=_sha256(source), module=_module_name_for(rel_posix))
    try:
        tree = ast.parse(source, filename=rel_posix)
    except SyntaxError as exc:
        entry.parse_error = f"line {exc.lineno}: {exc.msg}"
        return entry

    entry.doc = _first_doc_line(tree)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            entry.definitions.append(
                SymbolDef(
                    name=node.name,
                    qualname=node.name,
                    kind="function",
                    line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    signature=_format_signature(node),
                    doc=_first_doc_line(node),
                )
            )
        elif isinstance(node, ast.ClassDef):
            bases = [ast.unparse(base) for base in node.bases]
            cls_sig = f"class {node.name}" + (f"({', '.join(bases)})" if bases else "")
            entry.definitions.append(
                SymbolDef(
                    name=node.name,
                    qualname=node.name,
                    kind="class",
                    line=node.lineno,
                    end_line=node.end_lineno or node.lineno,
                    signature=cls_sig[:140],
                    doc=_first_doc_line(node),
                )
            )
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    entry.definitions.append(
                        SymbolDef(
                            name=item.name,
                            qualname=f"{node.name}.{item.name}",
                            kind="method",
                            line=item.lineno,
                            end_line=item.end_lineno or item.lineno,
                            signature=_format_signature(item),
                            doc=_first_doc_line(item),
                        )
                    )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                entry.imports.append(alias.name)
                entry.references.append(
                    SymbolRef(name=alias.name.split(".")[-1], line=node.lineno, kind="import")
                )
        elif isinstance(node, ast.ImportFrom):
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


def _strip_c_comments(source: str, *, single_quote_strings: bool) -> tuple[list[str], list[str]]:
    """移除 // 与 /* */ 注释并保持 1:1 行号；同时输出屏蔽字符串字面量后的代码行供括号计数与调用扫描。"""
    no_comment: list[str] = []
    masked: list[str] = []
    state: str | None = None
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
                no_comment.append("  ")
                masked.append("  ")
                i += 2
                continue
            if ch in ('"', "`") or (single_quote_strings and ch == "'"):
                state = ch
                no_comment.append(ch)
                masked.append(ch)
                i += 1
                continue
            no_comment.append(ch)
            masked.append(ch)
            i += 1
        elif state == "line":
            if ch == "\n":
                state = None
                no_comment.append("\n")
                masked.append("\n")
            else:
                no_comment.append(" ")
                masked.append(" ")
            i += 1
        elif state == "block":
            if ch == "*" and nxt == "/":
                state = None
                no_comment.append("  ")
                masked.append("  ")
                i += 2
            elif ch == "\n":
                no_comment.append("\n")
                masked.append("\n")
                i += 1
            else:
                no_comment.append(" ")
                masked.append(" ")
                i += 1
        else:
            if ch == "\\" and i + 1 < n and nxt != "\n":
                no_comment.append(ch + nxt)
                masked.append("  ")
                i += 2
                continue
            if ch == "\n":
                if state != "`":
                    state = None
                no_comment.append("\n")
                masked.append("\n")
                i += 1
                continue
            if ch == state:
                state = None
                no_comment.append(ch)
                masked.append(ch)
            else:
                no_comment.append(ch)
                masked.append(" ")
            i += 1
    return "".join(no_comment).splitlines(), "".join(masked).splitlines()


def _find_block_end(masked_lines: list[str], start_idx: int) -> int:
    depth = 0
    opened = False
    for idx in range(start_idx, len(masked_lines)):
        line = masked_lines[idx]
        if not opened and ";" in line and "{" not in line:
            return idx + 1
        opens = line.count("{")
        closes = line.count("}")
        if opens > 0:
            opened = True
        depth += opens - closes
        if opened and depth <= 0:
            return idx + 1
        if not opened and idx >= start_idx + 3:
            return start_idx + 1
    return len(masked_lines)


def _header_signature(no_comment_lines: list[str], start_idx: int) -> str:
    chunks: list[str] = []
    for idx in range(start_idx, min(len(no_comment_lines), start_idx + 4)):
        seg = no_comment_lines[idx].strip()
        if not seg:
            break
        if "{" in seg:
            seg = seg.split("{", 1)[0].strip()
            if seg:
                chunks.append(seg)
            break
        if seg.endswith(";"):
            chunks.append(seg[:-1].strip())
            break
        chunks.append(seg)
    sig = re.sub(r"\s+", " ", " ".join(chunks)).strip()
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
_TS_FUNC_RE = re.compile(
    r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\("
)
_TS_ARROW_RE = re.compile(
    r"^(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*(?::[^=]+)?=\s*(?:async\s*)?(?:<[^>]*>\s*)?\([^)]*\)\s*(?::[^=>]+)?=>"
)
_TS_METHOD_RE = re.compile(
    r"^(?:(?:public|private|protected|static|readonly|override|abstract|async|get|set)\s+)*\*?([A-Za-z_$][\w$]*)\s*(?:<[^>]*>)?\s*\("
)


def _parse_ts_js_file(rel_posix: str, source: str) -> FileIndex:
    entry = FileIndex(path=rel_posix, sha256=_sha256(source), module=_module_name_for(rel_posix))
    no_comment_lines, masked_lines = _strip_c_comments(source, single_quote_strings=True)
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
        if stripped:
            if m := _TS_CLASS_RE.match(stripped):
                opened_cls = m.group(1)
                entry.definitions.append(
                    SymbolDef(opened_cls, opened_cls, "class", lineno, _find_block_end(masked_lines, idx), _header_signature(no_comment_lines, idx))
                )
                def_by_line.setdefault(lineno, set()).add(opened_cls)
            elif m := _TS_IFACE_RE.match(stripped):
                name = m.group(1)
                entry.definitions.append(SymbolDef(name, name, "interface", lineno, _find_block_end(masked_lines, idx), _header_signature(no_comment_lines, idx)))
                def_by_line.setdefault(lineno, set()).add(name)
            elif m := _TS_ENUM_RE.match(stripped):
                name = m.group(1)
                entry.definitions.append(SymbolDef(name, name, "enum", lineno, _find_block_end(masked_lines, idx), _header_signature(no_comment_lines, idx)))
                def_by_line.setdefault(lineno, set()).add(name)
            elif depth_before == 0 and (m := _TS_TYPE_RE.match(stripped)):
                name = m.group(1)
                entry.definitions.append(SymbolDef(name, name, "type", lineno, _find_block_end(masked_lines, idx), _header_signature(no_comment_lines, idx)))
                def_by_line.setdefault(lineno, set()).add(name)
            elif depth_before == 0 and (m := _TS_FUNC_RE.match(stripped)):
                name = m.group(1)
                entry.definitions.append(SymbolDef(name, name, "function", lineno, _find_block_end(masked_lines, idx), _header_signature(no_comment_lines, idx)))
                def_by_line.setdefault(lineno, set()).add(name)
            elif depth_before == 0 and (
                m := _TS_ARROW_RE.match(" ".join(s.strip() for s in no_comment_lines[idx : idx + 4]))
            ):
                name = m.group(1)
                entry.definitions.append(SymbolDef(name, name, "function", lineno, _find_block_end(masked_lines, idx), _header_signature(no_comment_lines, idx)))
                def_by_line.setdefault(lineno, set()).add(name)
            elif containers and depth_before == containers[-1][1] and (m := _TS_METHOD_RE.match(stripped)):
                name = m.group(1)
                if name not in _CALL_SKIP_WORDS:
                    cls_name = containers[-1][0]
                    entry.definitions.append(
                        SymbolDef(name, f"{cls_name}.{name}", "method", lineno, _find_block_end(masked_lines, idx), _header_signature(no_comment_lines, idx))
                    )
                    def_by_line.setdefault(lineno, set()).add(name)
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
    no_comment_lines, masked_lines = _strip_c_comments(source, single_quote_strings=False)
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
                    SymbolDef(method_name, f"{recv_type}.{method_name}", "method", lineno, _find_block_end(masked_lines, idx), _header_signature(no_comment_lines, idx))
                )
                def_by_line.setdefault(lineno, set()).add(method_name)
            elif m := _GO_FUNC_RE.match(stripped):
                name = m.group(1)
                entry.definitions.append(
                    SymbolDef(name, name, "function", lineno, _find_block_end(masked_lines, idx), _header_signature(no_comment_lines, idx))
                )
                def_by_line.setdefault(lineno, set()).add(name)
            elif m := _GO_TYPE_RE.match(stripped):
                name, kw = m.group(1), m.group(2) or "type"
                entry.definitions.append(
                    SymbolDef(name, name, kw, lineno, _find_block_end(masked_lines, idx), _header_signature(no_comment_lines, idx))
                )
                def_by_line.setdefault(lineno, set()).add(name)
        masked = masked_lines[idx]
        brace_depth = max(0, brace_depth + masked.count("{") - masked.count("}"))

    _record_calls(entry, masked_lines, def_by_line, ("import ", "package "))
    return entry


_RS_USE_RE = re.compile(r"(?:^|\n)\s*(?:pub(?:\([^)]*\))?\s+)?use\s+([^;]+);")
_RS_ITEM_RE = re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(struct|enum|trait|type)\s+([A-Za-z_]\w*)")
_RS_IMPL_RE = re.compile(r"^impl(?:\s*<[^>]*>)?\s+(?:[\w:]+\s*(?:<[^>]*>)?\s+for\s+)?([A-Za-z_]\w*)")
_RS_FN_RE = re.compile(
    r'^(?:pub(?:\([^)]*\))?\s+)?(?:(?:async|const|unsafe|extern(?:\s+"[^"]*")?)\s+)*fn\s+([A-Za-z_]\w*)\s*(?:<[^>]*>)?\s*\('
)
_JAVA_IMPORT_RE = re.compile(r"^import\s+(?:static\s+)?([\w.]+(?:\.\*)?)\s*;")
_JAVA_TYPE_RE = re.compile(
    r"^(?:(?:public|protected|private|abstract|final|static|sealed|non-sealed)\s+)*(class|interface|enum|record)\s+([A-Za-z_$][\w$]*)"
)
_JAVA_METHOD_RE = re.compile(
    r"^(?:(?:public|protected|private|static|final|synchronized|abstract|default|native)\s+)*(?:<[^>]+>\s+)?(?!new\b|return\b|throw\b|package\b|import\b)[\w$\[\]<>.,?]+\s+([A-Za-z_$][\w$]*)\s*\("
)


def _parse_rust_file(rel_posix: str, source: str) -> FileIndex:
    entry = FileIndex(path=rel_posix, sha256=_sha256(source), module=_module_name_for(rel_posix))
    no_comment_lines, masked_lines = _strip_c_comments(source, single_quote_strings=False)
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
            if depth_before == 0 and (m := _RS_IMPL_RE.match(stripped)):
                opened_impl = m.group(1)
            elif depth_before == 0 and (m := _RS_ITEM_RE.match(stripped)):
                kw, name = m.group(1), m.group(2)
                kind = "interface" if kw == "trait" else kw
                entry.definitions.append(SymbolDef(name, name, kind, lineno, _find_block_end(masked_lines, idx), _header_signature(no_comment_lines, idx)))
                def_by_line.setdefault(lineno, set()).add(name)
            elif m := _RS_FN_RE.match(stripped):
                name = m.group(1)
                if containers and depth_before == containers[-1][1]:
                    owner = containers[-1][0]
                    entry.definitions.append(SymbolDef(name, f"{owner}.{name}", "method", lineno, _find_block_end(masked_lines, idx), _header_signature(no_comment_lines, idx)))
                    def_by_line.setdefault(lineno, set()).add(name)
                elif depth_before == 0:
                    entry.definitions.append(SymbolDef(name, name, "function", lineno, _find_block_end(masked_lines, idx), _header_signature(no_comment_lines, idx)))
                    def_by_line.setdefault(lineno, set()).add(name)
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
    no_comment_lines, masked_lines = _strip_c_comments(source, single_quote_strings=False)
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
                entry.definitions.append(SymbolDef(name, name, kind, lineno, _find_block_end(masked_lines, idx), _header_signature(no_comment_lines, idx)))
                def_by_line.setdefault(lineno, set()).add(name)
            elif containers and depth_before == containers[-1][1] and (m := _JAVA_METHOD_RE.match(stripped)):
                name = m.group(1)
                if name not in _CALL_SKIP_WORDS:
                    owner = containers[-1][0]
                    entry.definitions.append(SymbolDef(name, f"{owner}.{name}", "method", lineno, _find_block_end(masked_lines, idx), _header_signature(no_comment_lines, idx)))
                    def_by_line.setdefault(lineno, set()).add(name)
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


def _parse_source_file(rel_posix: str, source: str) -> FileIndex:
    if rel_posix.endswith(".py"):
        return _parse_python_file(rel_posix, source)
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
        terms = [t for t in focus_query.casefold().split() if t]
        defined_in: dict[str, list[str]] = {}
        modules: dict[str, str] = {}
        for rel, entry in self._cache.items():
            modules[entry.module] = rel
            modules[entry.module.split(".")[-1]] = rel
            if "/" in rel:
                parent_pkg = rel.rsplit("/", 1)[0].split("/")[-1]
                modules.setdefault(parent_pkg, rel)
            seen_defs: set[str] = set()
            for sym in entry.definitions:
                if sym.name not in seen_defs:
                    seen_defs.add(sym.name)
                    defined_in.setdefault(sym.name, []).append(rel)

        scores = {rel: 1.0 for rel in self._cache}
        for caller_rel, entry in self._cache.items():
            # 业务主文件默认略高于测试文件，方便先定位被测实现，再看测试断言。
            if not _is_test_path(caller_rel):
                scores[caller_rel] += 1.5
            for imp in entry.imports:
                norm = re.sub(r"^(?:\.\.?/)+", "", imp.strip()).replace("::", ".").replace("/", ".")
                for ext in (".tsx", ".jsx", ".java", ".py", ".ts", ".js", ".go", ".rs"):
                    if norm.endswith(ext):
                        norm = norm[: -len(ext)]
                        break
                target = modules.get(imp) or modules.get(norm) or modules.get(norm.split(".")[-1])
                if target and target != caller_rel:
                    scores[target] += 3.0
                    scores[caller_rel] += 0.5
            seen_refs = {(ref.name, ref.kind) for ref in entry.references}
            for name, kind in seen_refs:
                targets = defined_in.get(name, ())
                if not targets or len(targets) > 16:
                    continue
                base_weight = 2.5 if kind == "call" else 1.5
                weight = base_weight / len(targets) if len(targets) > 8 else base_weight
                for target in targets:
                    if target != caller_rel:
                        scores[target] += weight
            if terms:
                haystack = (
                    caller_rel.casefold()
                    + " "
                    + " ".join(
                        sym.qualname.casefold() + " " + sym.signature.casefold()
                        for sym in entry.definitions
                    )
                )
                for term in terms:
                    if term in haystack:
                        scores[caller_rel] += 5.0
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
                        indent = "    " if sym.kind == "method" else "  "
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
