"""基于 Python AST 的代码地图与符号检索：按依赖重要度排序并在字符预算内输出。

设计要点：
1. 只用标准库 ast 静态解析函数、类、方法签名、导入与调用关系，绝不在宿主导入业务代码。
2. 每个文件记录内容 SHA-256；文件修改或删除后，下次查询自动增量更新并返回 stale_refreshed。
3. 参考 Aider RepoMap 的核心工程思想：围绕跨文件引用度高的符号组织摘要，严格遵守预算上限。
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from typing import Any

_MAX_PARSE_BYTES = 64 * 1024
_MAX_INDEX_FILES = 128


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
    stem = rel_posix.removesuffix(".py")
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
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


class RepoMapIndex:
    """带文件摘要校验的仓库符号索引：磁盘变更后自动增量刷新，绝不静默返回旧行号。"""

    def __init__(self, workspace: Path):
        root = Path(workspace).absolute()
        if root.is_symlink() or not root.is_dir():
            raise ValueError("workspace 必须是真实目录")
        self.workspace = root.resolve()
        self._cache: dict[str, FileIndex] = {}
        self._initialized = False

    def _discover_py_files(self) -> list[Path]:
        found: list[Path] = []
        for directory, subdirs, names in os.walk(self.workspace, followlinks=False):
            subdirs[:] = sorted(
                d
                for d in subdirs
                if not d.startswith(".") and d != "__pycache__" and not (Path(directory) / d).is_symlink()
            )
            for name in sorted(names):
                if name.startswith(".") or not name.endswith(".py"):
                    continue
                candidate = Path(directory) / name
                if candidate.is_symlink():
                    continue
                info = candidate.stat()
                if info.st_nlink == 1 and info.st_size <= _MAX_PARSE_BYTES:
                    found.append(candidate)
                if len(found) >= _MAX_INDEX_FILES:
                    return found
        return found

    def refresh(self) -> dict[str, Any]:
        """核对磁盘文件 SHA-256，增量重解析改动文件并剔除已删除文件。"""
        current_paths: set[str] = set()
        refreshed: list[str] = []
        for path in self._discover_py_files():
            rel = path.relative_to(self.workspace).as_posix()
            current_paths.add(rel)
            try:
                source = path.read_text(encoding="utf-8")
            except UnicodeError:
                continue
            digest = _sha256(source)
            cached = self._cache.get(rel)
            if cached is None or cached.sha256 != digest:
                if self._initialized and cached is not None:
                    refreshed.append(rel)
                self._cache[rel] = _parse_python_file(rel, source)
        removed = sorted(set(self._cache) - current_paths)
        for rel in removed:
            self._cache.pop(rel, None)
        was_initialized = self._initialized
        self._initialized = True
        manifest = [{"path": k, "sha256": self._cache[k].sha256} for k in sorted(self._cache)]
        return {
            "index_sha256": _sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":"))),
            "indexed_files": len(self._cache),
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
            for sym in entry.definitions:
                defined_in.setdefault(sym.name, []).append(rel)

        scores = {rel: 1.0 for rel in self._cache}
        for caller_rel, entry in self._cache.items():
            # 业务主文件默认略高于测试文件，方便先定位被测实现，再看测试断言。
            if not caller_rel.startswith("tests/"):
                scores[caller_rel] += 1.5
            for imp in entry.imports:
                target = modules.get(imp) or modules.get(imp.split(".")[-1])
                if target and target != caller_rel:
                    scores[target] += 3.0
                    scores[caller_rel] += 0.5
            seen_refs = {(ref.name, ref.kind) for ref in entry.references}
            for name, kind in seen_refs:
                for target in defined_in.get(name, ()):
                    if target != caller_rel:
                        scores[target] += 2.5 if kind == "call" else 1.5
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
            key=lambda d: (not d["exact"], d["path"].startswith("tests/"), d["path"], d["line"])
        )
        return {
            "ok": True,
            "symbol": query,
            **meta,
            "definitions": definitions[:25],
            "references": references[:40],
        }
