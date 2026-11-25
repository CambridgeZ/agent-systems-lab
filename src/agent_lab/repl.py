"""交互式终端工作台（`agent-lab code`）：类似 Claude Code / Codex CLI 的本地多轮会话与内联审批驱动器。

================================================================================
模块导读（Module Guide）
================================================================================
1. 模块角色（Role）：
   本模块是 Agent Systems Lab 的命令行交互外壳（Read-Eval-Print Loop, REPL）。它本身保持“极薄状态”
   （仅持有当前活跃 `current_run` 引用与单调递增的事件渲染游标 `event_cursor`），将对话上下文管理、
   工具调度、Epoch 租约锁、流式增量推送、检查点回滚与崩溃恢复全部委托给底层的单一事实源
   `AgentRuntime` + `Storage`。

2. 核心安全与交互不变量（Core Invariants）：
   - [反多行粘贴误批准不变量] `drain_approvals_interactively` 绝不将空回车 `""` 视为自动批准，彻底消除
     用户向终端粘贴含空行多行文本时误批危险变更的安全隐患；仅接受显式 `y`/`yes`/`a`/`f`/`p`/`n`/`s:...`。
   - [安全回合级 Ctrl-C 中断] 在活跃模型/工具回合执行期间按下 `Ctrl-C`（`KeyboardInterrupt` / `SIGINT`）
     时，通过 `runtime.cancel_active_run(run_id)` 即时协同取消在途 LLM 与子进程并返回 REPL 提示符，
     绝不直接崩溃退出整个交互会话。
   - [权限切换与会话授权零缓存失效] `/mode` 与内联 `[a/f/p]` 仅更新元数据中的 `permission_mode` /
     `session_grants`，绝不篡改 `SYSTEM` 提示词或 `<TOOLS>` JSON Schema，保持大模型 KV Cache 前缀命中。
   - [I/O 依赖注入与无 PTY 确定性测试] 生产 TTY 下自动启用 `prompt_toolkit`（或标准库 `readline`）补全、
     ANSI 彩色统一 Diff 与流式增量渲染；自动化测试注入 `input_fn` / `output_fn` 时保持 100% 确定性无 TTY 运行。
================================================================================
"""

from __future__ import annotations

import asyncio
import contextlib
import difflib
import inspect
import os
from pathlib import Path
import re
import select
import signal
import sys
import threading
import time
from typing import Any, Callable
import urllib.parse

from .a2a import (
    A2A_REDACTED_MARKER,
    A2AClientManager,
    load_a2a_config,
    redact_a2a_document,
)
from .gateways import (
    REDACTED_SECRET_MARKER,
    load_gateways_config,
    redact_gateways_document,
    resolve_provider_or_gateway,
)
from .git_repo import is_git_worktree, worktree_status
from .host_tools import is_host_secret_segment
from .memory import DreamEngine
from .permissions import is_workspace_trusted, normalize_permission_mode
from .providers import DEFAULT_MODEL_PRICING_USD_PER_MTOK
from .runtime import AgentRuntime

_DEFAULT_STATE_DIR = (Path(__file__).resolve().parents[2] / ".agent-lab").resolve()

_ANSI_GREEN = "\x1b[32m"
_ANSI_RED = "\x1b[31m"
_ANSI_CYAN = "\x1b[36m"
_ANSI_YELLOW = "\x1b[33m"
_ANSI_BOLD = "\x1b[1m"
_ANSI_DIM = "\x1b[2m"
_ANSI_RESET = "\x1b[0m"
_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_PRUNED_WALK_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build", ".pytest_cache", ".ruff_cache", ".mypy_cache"}
)

_AT_FILE_RE = re.compile(r"(?<![A-Za-z0-9._+-])@([A-Za-z0-9_./-]{1,180})")

_SLASH_COMMANDS: tuple[str, ...] = (
    "/help",
    "/mode",
    "/plan",
    "/compact",
    "/skills",
    "/mcp",
    "/a2a",
    "/steer",
    "/claude",
    "/dream",
    "/memory",
    "/rewind",
    "/fork",
    "/cost",
    "/model",
    "/gateway",
    "/hooks",
    "/bg",
    "/status",
    "/diff",
    "/clear",
    "/exit",
)


def _is_interactive_tty(input_fn: Callable[[str], str], output_fn: Callable[[str], None]) -> bool:
    """仅当使用默认标准 I/O 且标准输入输出均为真实 TTY 时启用终端富交互特性。"""
    return (
        input_fn is input
        and output_fn is print
        and bool(getattr(sys.stdin, "isatty", lambda: False)())
        and bool(getattr(sys.stdout, "isatty", lambda: False)())
    )


def _colorize_diff_line(line: str, *, use_color: bool) -> str:
    if not use_color:
        return line
    if line.startswith("+++") or line.startswith("---"):
        return f"{_ANSI_CYAN}{line}{_ANSI_RESET}"
    if line.startswith("@@"):
        return f"{_ANSI_CYAN}{line}{_ANSI_RESET}"
    if line.startswith("+"):
        return f"{_ANSI_GREEN}{line}{_ANSI_RESET}"
    if line.startswith("-"):
        return f"{_ANSI_RED}{line}{_ANSI_RESET}"
    return line


def _build_proposal_diff_lines(
    proposal: dict[str, Any],
    *,
    workspace: Path | None = None,
    max_lines: int = 120,
) -> list[str]:
    """为单文件或多文件补丁提案构造真实统一 Diff，超长时明确标注省略行数并提示按 v 展开全文。"""
    full_lines: list[str] = []
    direct_diff = proposal.get("diff")
    if isinstance(direct_diff, str) and direct_diff.strip():
        full_lines = direct_diff.splitlines()
    else:
        files = proposal.get("files")
        if isinstance(files, list) and files:
            for entry in files:
                if not isinstance(entry, dict):
                    continue
                fpath = str(entry.get("path") or "")
                fdiff = entry.get("diff")
                if isinstance(fdiff, str) and fdiff.strip():
                    full_lines.extend(fdiff.splitlines())
                    continue
                old_text = entry.get("old_content")
                if old_text is None and workspace is not None and fpath:
                    old_text = _safe_read_workspace_text(workspace, fpath)
                new_text = str(entry.get("new_content") or "")
                full_lines.extend(
                    difflib.unified_diff(
                        (old_text or "").splitlines(),
                        new_text.splitlines(),
                        fromfile=f"a/{fpath}",
                        tofile=f"b/{fpath}",
                        lineterm="",
                        n=3,
                    )
                )
        else:
            rel_path = str(proposal.get("path") or "file")
            new_content = proposal.get("new_content")
            if isinstance(new_content, str):
                old_content = proposal.get("old_content")
                if old_content is None and workspace is not None and proposal.get("path"):
                    old_content = _safe_read_workspace_text(workspace, str(proposal["path"]))
                diff_lines = list(
                    difflib.unified_diff(
                        (old_content or "").splitlines(),
                        new_content.splitlines(),
                        fromfile=f"a/{rel_path}",
                        tofile=f"b/{rel_path}",
                        lineterm="",
                        n=3,
                    )
                )
                full_lines = diff_lines if diff_lines else [f"+{ln}" for ln in new_content.splitlines()]
    if max_lines > 0 and len(full_lines) > max_lines:
        omitted = len(full_lines) - max_lines
        return [
            *full_lines[:max_lines],
            f"... [已省略剩余 {omitted} 行，共 {len(full_lines)} 行；输入 v 查看完整 Diff]",
        ]
    return full_lines


def _render_terminal_markdown(text: str, *, use_color: bool) -> str:
    """在真实终端中渲染带语法/结构高亮的 Markdown；非 TTY / 测试环境保持原始文本 100% 确定性。"""
    if not use_color or not text:
        return text
    try:
        from io import StringIO

        from rich.console import Console
        from rich.markdown import Markdown

        buf = StringIO()
        console = Console(file=buf, force_terminal=True, width=min(110, max(72, os.get_terminal_size().columns - 2)))
        console.print(Markdown(text))
        rendered = buf.getvalue().rstrip("\n")
        if rendered:
            return rendered
    except Exception:
        pass

    # 零依赖 ANSI Markdown 回退：清晰突出标题、粗体、行内代码与围栏代码块
    out: list[str] = []
    in_code = False
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            if not in_code:
                in_code = True
                out.append(f"{_ANSI_DIM}┌─ {lang or 'code'} {'─' * 36}{_ANSI_RESET}")
            else:
                in_code = False
                out.append(f"{_ANSI_DIM}└{'─' * 44}{_ANSI_RESET}")
            continue
        if in_code:
            out.append(f"{_ANSI_DIM}│{_ANSI_RESET} {_ANSI_CYAN}{raw}{_ANSI_RESET}")
            continue
        if re.match(r"^#{1,6}\s+", raw):
            out.append(f"{_ANSI_BOLD}{_ANSI_CYAN}{raw}{_ANSI_RESET}")
            continue
        line = re.sub(r"`([^`\n]+)`", f"{_ANSI_CYAN}\\1{_ANSI_RESET}", raw)
        line = re.sub(r"\*\*([^*\n]+)\*\*", f"{_ANSI_BOLD}\\1{_ANSI_RESET}", line)
        out.append(line)
    return "\n".join(out)


def _list_workspace_files_pruned(workspace: Path, *, limit: int = 200) -> list[str]:
    """使用原地剪枝的 `os.walk` 扫描工作区安全文本文件路径，避免在 `.venv`/`node_modules` 大仓库按 Tab 卡死。"""
    results: list[str] = []
    try:
        ws = workspace.resolve()
        for dirpath, dirnames, filenames in os.walk(ws, followlinks=False):
            # 原地剪枝，绝不深入 .git / .venv / node_modules / 隐藏目录
            dirnames[:] = sorted(
                d
                for d in dirnames
                if d not in _PRUNED_WALK_DIRS
                and not d.startswith(".")
                and not is_host_secret_segment(d)
                and not (Path(dirpath) / d).is_symlink()
            )
            for fname in sorted(filenames):
                if len(results) >= limit:
                    return results
                if fname.startswith(".") or is_host_secret_segment(fname):
                    continue
                fpath = Path(dirpath) / fname
                if fpath.is_symlink():
                    continue
                try:
                    rel = fpath.relative_to(ws).as_posix()
                except ValueError:
                    continue
                results.append(rel)
    except OSError:
        pass
    return results


def _resolve_active_model_label(runtime: AgentRuntime, run: dict[str, Any] | None = None) -> str:
    """获取当前实际解析出的模型 ID，便于在启动横幅与 /model 提示中如实展示。"""
    if run:
        m = (run.get("metadata") or {}).get("model")
        if isinstance(m, str) and m.strip():
            return m.strip()
    prov_model = getattr(runtime.provider, "model", None)
    if isinstance(prov_model, str) and prov_model.strip():
        return prov_model.strip()
    return runtime.provider_name


def _runtime_state_dir(runtime: AgentRuntime) -> Path | None:
    try:
        if hasattr(runtime, "_state_root_dir"):
            return Path(runtime._state_root_dir())
        return Path(runtime.storage.path).parent
    except Exception:
        return None


def _repl_history_path(runtime: AgentRuntime) -> Path | None:
    try:
        state_dir = _runtime_state_dir(runtime)
        if state_dir is None:
            return None
        p = state_dir / "repl_history.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        return p
    except Exception:
        return None


def _runtime_gateway_context(
    runtime: AgentRuntime,
    *,
    explicit_config_path: str | Path | None = None,
) -> dict[str, Any]:
    """提取运行态网关配置发现上下文（严格遵守工作区信任门控与隔离 state_dir 边界）。"""
    state_dir = _runtime_state_dir(runtime)
    cfg_path = explicit_config_path or getattr(runtime, "gateway_config_path", None)
    trust_ws = False
    if state_dir is not None:
        with contextlib.suppress(Exception):
            trust_ws = is_workspace_trusted(runtime.workspace, state_dir=state_dir)
    tenant_id: str | None = None
    if state_dir is None:
        tenant_id = "local"
    else:
        with contextlib.suppress(OSError):
            if state_dir.resolve() == _DEFAULT_STATE_DIR:
                tenant_id = "local"
    return {
        "state_dir": state_dir,
        "workspace": runtime.workspace,
        "explicit_config_path": cfg_path,
        "trust_workspace": trust_ws,
        "tenant_id": tenant_id,
    }


def _sanitize_display_base_url(raw_url: Any) -> str:
    """提取可安全展示的网关 base_url（剥离凭据、查询参数与片段）。"""
    if raw_url is None:
        return ""
    text = str(raw_url).strip()
    if not text:
        return ""
    try:
        parsed = urllib.parse.urlsplit(text)
        if not parsed.scheme or not parsed.hostname:
            return ""
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port_part = f":{parsed.port}" if parsed.port else ""
        path_part = parsed.path.rstrip("/")
        return f"{parsed.scheme.lower()}://{host}{port_part}{path_part}"
    except ValueError:
        return ""


def _describe_active_gateway(
    runtime: AgentRuntime,
    run: dict[str, Any] | None = None,
) -> dict[str, str]:
    """读取当前活跃 Provider 的网关 Profile、线协议、解析模型与脱敏端点。"""
    prov = runtime.provider
    raw_profile = (
        getattr(prov, "gateway_profile", None)
        or getattr(prov, "profile_name", None)
        or (
            runtime.provider_name[len("gateway:") :]
            if isinstance(runtime.provider_name, str) and runtime.provider_name.startswith("gateway:")
            else None
        )
    )
    raw_protocol = (
        getattr(prov, "protocol", None)
        or getattr(prov, "gateway_protocol", None)
        or ("demo" if runtime.provider_name == "demo" else runtime.provider_name)
    )
    protocol = str(raw_protocol).strip() if raw_protocol else "unknown"
    profile = str(raw_profile).strip() if raw_profile else "(direct)"
    model = _resolve_active_model_label(runtime, run)
    raw_base = getattr(prov, "base_url", None)
    if not raw_base:
        client = getattr(prov, "client", None)
        if client is not None:
            raw_base = getattr(client, "base_url", None)
    safe_url = _sanitize_display_base_url(raw_base)
    if not safe_url:
        safe_url = "(offline)" if protocol == "demo" else "(default)"
    return {
        "profile": profile,
        "protocol": protocol,
        "model": model,
        "base_url": safe_url,
    }


def _format_repl_gateway_credential_status(prof: dict[str, Any]) -> str:
    """格式化脱敏凭证就绪状态供 `/gateway` 列表展示（绝不输出任何密钥明文）。"""
    proto = str(prof.get("protocol") or "").strip().lower()
    ready = bool(prof.get("credential_configured"))
    env_name = str(prof.get("api_key_env") or "").strip()
    if proto == "demo":
        return "ready (offline)"
    if ready:
        if env_name and os.environ.get(env_name, "").strip():
            return f"ready (env:{env_name})"
        if prof.get("api_key"):
            return f"ready (inline:{REDACTED_SECRET_MARKER})"
        return "ready"
    if env_name:
        return f"missing (env:{env_name})"
    return "missing"


def _collect_gateway_profile_completions(
    runtime: AgentRuntime,
    word: str = "",
) -> list[str]:
    """从 `load_gateways_config` 读取可用网关 Profile 列表用于 `/gateway <profile>` Tab 补全。"""
    ctx = _runtime_gateway_context(runtime)
    try:
        cfg = load_gateways_config(
            explicit_path=ctx["explicit_config_path"],
            state_dir=ctx["state_dir"],
            workspace=ctx["workspace"],
            trust_workspace=ctx["trust_workspace"],
            tenant_id=ctx["tenant_id"],
        )
    except Exception:
        return []
    profiles = cfg.get("profiles") or {}
    return [name for name in sorted(profiles.keys()) if name.startswith(word)]


def _load_repl_a2a_document(runtime: AgentRuntime) -> dict[str, Any]:
    """按运行态边界安全读取 `a2a.json`（遵守工作区信任门控）。"""
    state_dir = _runtime_state_dir(runtime)
    explicit_cfg = getattr(runtime, "a2a_config_path", None)
    trust_ws = False
    if state_dir is not None:
        with contextlib.suppress(Exception):
            trust_ws = is_workspace_trusted(runtime.workspace, state_dir=state_dir)
    return load_a2a_config(
        explicit_path=explicit_cfg,
        state_dir=state_dir,
        workspace=runtime.workspace,
        trust_workspace=trust_ws,
    )


def _format_repl_a2a_credential_status(peer: dict[str, Any]) -> str:
    """格式化脱敏后的 A2A Peer 凭证就绪状态（绝不输出任何密钥原文）。"""
    ready = bool(peer.get("credential_configured"))
    env_name = str(peer.get("api_key_env") or "").strip()
    if ready:
        if env_name and os.environ.get(env_name, "").strip():
            return f"ready (env:{env_name})"
        if peer.get("api_key"):
            return f"ready (inline:{A2A_REDACTED_MARKER})"
        return "ready"
    if env_name:
        return f"unset (env:{env_name})"
    return "none"


def _collect_a2a_peer_completions(
    runtime: AgentRuntime,
    word: str = "",
) -> list[str]:
    """收集当前可用 A2A peer 名称用于 `/a2a discover <peer>` 与 `/a2a send <peer>` Tab 补全。"""
    names: set[str] = set()
    mgr = getattr(runtime, "a2a_manager", None)
    if mgr is not None and hasattr(mgr, "peer_names"):
        with contextlib.suppress(Exception):
            names.update(str(p) for p in mgr.peer_names())
    with contextlib.suppress(Exception):
        doc = _load_repl_a2a_document(runtime)
        names.update(str(p) for p in (doc.get("peers") or {}).keys())
    return [name for name in sorted(names) if name.startswith(word)]


def _collect_a2a_completions(
    runtime: AgentRuntime,
    stripped_line: str,
    word: str,
) -> list[str] | None:
    """为 `/a2a [list | discover <peer> | send <peer>]` 提供层级 Tab 补全。"""
    trailing_space = stripped_line.endswith((" ", "\t"))
    parts = stripped_line.split()
    if len(parts) == 1 or (len(parts) == 2 and not trailing_space):
        return [sub for sub in ("list", "discover", "send") if sub.startswith(word)]
    subcmd = parts[1].lower()
    if subcmd in {"discover", "send"} and (
        (len(parts) == 2 and trailing_space) or (len(parts) == 3 and not trailing_space)
    ):
        return _collect_a2a_peer_completions(runtime, word)
    return None


def _resolve_repl_a2a_manager(
    runtime: AgentRuntime,
    *,
    target_peer: str | None = None,
) -> tuple[A2AClientManager, bool]:
    """优先复用运行态 `runtime.a2a_manager`（如已配置或测试注入），否则从 `a2a.json` 临时构建。"""
    existing = getattr(runtime, "a2a_manager", None)
    if existing is not None:
        has_transport = getattr(existing, "_transport", None) is not None
        has_peer = bool(
            target_peer
            and hasattr(existing, "peer_names")
            and target_peer in existing.peer_names()
        )
        if has_peer or has_transport:
            return existing, False
        if not target_peer and getattr(existing, "enabled", False):
            return existing, False
    doc = _load_repl_a2a_document(runtime)
    return A2AClientManager(doc), True


def _safe_read_workspace_text(workspace: Path, rel_path: str, *, max_bytes: int = 128 * 1024) -> str | None:
    """安全只读工作区内相对路径普通文本文件（拦截路径穿越、符号链接、硬链接与敏感文件名）。"""
    clean = rel_path.strip().lstrip("./")
    if not clean or clean.startswith("/") or ".." in Path(clean).parts:
        return None
    parts = Path(clean).parts
    if any(is_host_secret_segment(seg) for seg in parts) or ".git" in parts:
        return None
    try:
        ws_root = workspace.resolve()
        cur = ws_root
        for seg in parts:
            cur = cur / seg
            if cur.is_symlink():
                return None
        resolved = cur.resolve()
        if not resolved.is_relative_to(ws_root) or not resolved.is_file():
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(str(resolved), flags)
        try:
            st = os.fstat(fd)
            if st.st_nlink != 1 or st.st_size > max_bytes:
                return None
            raw = os.read(fd, max_bytes + 1)
        finally:
            os.close(fd)
        if b"\x00" in raw or len(raw) > max_bytes:
            return None
        return raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _expand_at_file_mentions(
    user_text: str,
    workspace: Path,
    *,
    max_files: int = 4,
    max_chars_per_file: int = 2400,
) -> str:
    """解析提示词中的 `@relative/path` 文件引用，安全附加受控工作区文件片段。"""
    if "@" not in user_text:
        return user_text
    attached: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _AT_FILE_RE.finditer(user_text):
        candidate = match.group(1).rstrip(".,;:!?)]}\"'")
        if not candidate or candidate in seen:
            continue
        content = _safe_read_workspace_text(workspace, candidate)
        if content is None:
            continue
        seen.add(candidate)
        snippet = content[:max_chars_per_file]
        if len(content) > max_chars_per_file:
            snippet += f"\n... [已截断，总长 {len(content)} 字符]"
        attached.append((candidate, snippet))
        if len(attached) >= max_files:
            break
    if not attached:
        return user_text
    blocks = ["\n\n[已引用工作区文件上下文]"]
    for rel, snippet in attached:
        blocks.append(f"--- @{rel} ---\n{snippet}")
    return user_text + "\n".join(blocks)


def _extract_question_fields(proposal: dict[str, Any]) -> dict[str, Any]:
    """统一提取 `user_question` 提案的问题正文、背景说明、候选选项、默认项与多选标志。"""
    args = proposal.get("arguments") if isinstance(proposal.get("arguments"), dict) else {}
    question = str(
        proposal.get("question") or args.get("question") or proposal.get("summary") or ""
    ).strip()
    context = str(proposal.get("context") or args.get("context") or "").strip()
    raw_opts = (
        proposal.get("options")
        if isinstance(proposal.get("options"), list)
        else args.get("options")
    )
    options: list[str] = []
    if isinstance(raw_opts, list):
        for item in raw_opts:
            if isinstance(item, str) and item.strip():
                options.append(item.strip())
            elif isinstance(item, dict):
                label = str(
                    item.get("label") or item.get("value") or item.get("text") or ""
                ).strip()
                desc = str(item.get("description") or "").strip()
                if label:
                    options.append(f"{label} — {desc}" if desc else label)
    default_option = str(
        proposal.get("default_option") or args.get("default_option") or ""
    ).strip()
    multi_select = bool(
        proposal["multi_select"]
        if "multi_select" in proposal
        else args.get("multi_select", False)
    )
    return {
        "question": question,
        "context": context,
        "options": options,
        "default_option": default_option,
        "multi_select": multi_select,
    }


def _extract_plan_fields(proposal: dict[str, Any]) -> dict[str, Any]:
    """统一提取 `plan_approval` 提案的计划摘要、计划文件路径、完整正文与建议切换的目标权限。"""
    args = proposal.get("arguments") if isinstance(proposal.get("arguments"), dict) else {}
    summary = str(
        proposal.get("plan_summary")
        or proposal.get("summary")
        or args.get("summary")
        or ""
    ).strip()
    plan_file = str(
        proposal.get("plan_file")
        or args.get("plan_file")
        or proposal.get("path")
        or ""
    ).strip()
    plan_content = str(
        proposal.get("plan_content")
        or proposal.get("new_content")
        or args.get("plan_content")
        or ""
    ).strip()
    target_mode = str(
        proposal.get("target_permission_mode")
        or args.get("target_permission_mode")
        or ""
    ).strip()
    return {
        "summary": summary,
        "plan_file": plan_file,
        "plan_content": plan_content,
        "target_permission_mode": target_mode,
    }


def _build_approve_coro(
    runtime: AgentRuntime,
    run_id: str,
    proposal_id: str,
    approved: bool,
    *,
    grant_pattern: str | None = None,
    answer_text: str | None = None,
) -> Any:
    """安全调用 `runtime.approve(...)`，通过签名检查仅在目标实现支持时传递 `answer_text`。"""
    approve_kwargs: dict[str, Any] = {"grant_pattern": grant_pattern}
    if answer_text is not None:
        try:
            sig = inspect.signature(runtime.approve)
            params = sig.parameters
            has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
            if has_var_kw or "answer_text" in params:
                approve_kwargs["answer_text"] = answer_text
        except (TypeError, ValueError):
            pass
    return runtime.approve(run_id, proposal_id, approved, **approve_kwargs)


def _format_proposal_preview(
    proposal: dict[str, Any],
    *,
    workspace: Path | None = None,
    use_color: bool = False,
) -> str:
    """将待审批提案渲染为结构化终端边框卡片，展示真实统一 Diff、提问选项、实施计划与会话授权建议。"""
    kind = proposal.get("kind", "single_file")
    pid_short = str(proposal.get("id") or "")[:8]
    if kind == "user_question":
        qinfo = _extract_question_fields(proposal)
        lines = [f"\n┌── 智能体澄清提问 [{kind}] id={pid_short} ──"]
        if qinfo["question"]:
            for idx_q, qline in enumerate(qinfo["question"].splitlines()[:20]):
                prefix = "│ 问题       : " if idx_q == 0 else "│              "
                lines.append(f"{prefix}{qline}")
        if qinfo["context"]:
            for idx_c, cline in enumerate(qinfo["context"].splitlines()[:8]):
                prefix = "│ 背景说明   : " if idx_c == 0 else "│              "
                lines.append(f"{prefix}{cline}")
        if qinfo["options"]:
            mode_hint = "（可多选，逗号分隔编号）" if qinfo["multi_select"] else ""
            lines.append(f"│ 候选选项   :{mode_hint}")
            for opt_idx, opt_txt in enumerate(qinfo["options"], 1):
                rec = (
                    " [推荐]"
                    if qinfo["default_option"] and opt_txt == qinfo["default_option"]
                    else ""
                )
                lines.append(f"│   [{opt_idx}] {opt_txt}{rec}")
        elif qinfo["default_option"]:
            lines.append(f"│ 推荐默认   : {qinfo['default_option']}")
        lines.append("└" + "─" * 52)
        return "\n".join(lines)

    if kind == "plan_approval":
        pinfo = _extract_plan_fields(proposal)
        lines = [f"\n┌── 实施计划待批准 [{kind}] id={pid_short} ──"]
        if pinfo["plan_file"]:
            lines.append(f"│ 计划文件   : {pinfo['plan_file']}")
        if pinfo["target_permission_mode"]:
            lines.append(f"│ 批准后权限 : {pinfo['target_permission_mode']}")
        if pinfo["summary"]:
            lines.append("│ 计划摘要   :")
            for sline in pinfo["summary"].splitlines()[:40]:
                lines.append(f"│   {sline}")
        if pinfo["plan_content"] and pinfo["plan_content"] != pinfo["summary"]:
            content_lines = pinfo["plan_content"].splitlines()
            lines.append("│ 计划正文   :")
            for cline in content_lines[:40]:
                lines.append(f"│   {cline}")
            if len(content_lines) > 40:
                lines.append(
                    f"│   ... [已省略剩余 {len(content_lines) - 40} 行，输入 v 查看完整计划]"
                )
        if not pinfo["summary"] and not pinfo["plan_content"]:
            lines.append("│ 说明       : 智能体已完成方案规划，等待批准退出只读规划模式并开始执行。")
        lines.append("└" + "─" * 52)
        return "\n".join(lines)

    lines = [f"\n┌── 待人工确认动作 [{kind}] id={pid_short} ──"]
    if kind == "command":
        args = proposal.get("arguments") or {}
        lines.append(f"│ 目录 (cwd) : {args.get('cwd', '.')}")
        lines.append(f"│ 风险级别   : {proposal.get('risk_level', 'mutating')}")
        lines.append(f"│ 终端命令   : $ {args.get('command', '')}")
    elif kind == "gui_action":
        lines.append(f"│ GUI 工具   : {proposal.get('tool', '')}")
        lines.append(f"│ 参数       : {proposal.get('arguments', {})}")
    elif kind == "mcp_call":
        lines.append(f"│ MCP 工具   : {proposal.get('tool', '')}")
        lines.append(f"│ 参数       : {proposal.get('arguments', {})}")
    elif kind == "a2a_call":
        a2a_args = proposal.get("arguments") or {}
        lines.append(f"│ A2A 工具   : {proposal.get('tool', 'a2a_send_task')}")
        if a2a_args.get("peer"):
            lines.append(f"│ 目标对端   : {a2a_args.get('peer')}")
        lines.append(f"│ 参数       : {a2a_args}")
    else:
        paths = proposal.get("paths") or ([proposal["path"]] if proposal.get("path") else [])
        lines.append(f"│ 目标文件   : {', '.join(paths)}")
        if proposal.get("summary"):
            lines.append(f"│ 修改说明   : {proposal['summary']}")
        diff_lines = _build_proposal_diff_lines(proposal, workspace=workspace, max_lines=80)
        if diff_lines:
            lines.append("│ 统一差异 (Unified Diff) :")
            for dline in diff_lines:
                lines.append(f"│   {_colorize_diff_line(dline, use_color=use_color)}")
    suggested = proposal.get("suggested_grant_pattern")
    if isinstance(suggested, str) and suggested.strip():
        lines.append(f"│ 会话模式授权: {suggested.strip()}（按 p 批准并在本会话放行同类操作）")
    lines.append("└" + "─" * 52)
    return "\n".join(lines)


def _build_tty_prompt_reader(
    runtime: AgentRuntime,
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> Callable[[str], str]:
    """在真实交互式 TTY 中启用剪枝缓存补全（命令/子参数/技能/@工作区文件）与跨会话历史持久化。"""
    if not _is_interactive_tty(input_fn, output_fn):
        return input_fn

    file_cache: dict[str, Any] = {"ts": 0.0, "files": []}

    def _get_cached_files() -> list[str]:
        now = time.monotonic()
        if now - float(file_cache["ts"]) > 5.0 or not file_cache["files"]:
            file_cache["files"] = _list_workspace_files_pruned(runtime.workspace, limit=200)
            file_cache["ts"] = now
        return file_cache["files"]

    def _collect_completions_for(line_before: str, word: str) -> list[str]:
        stripped = line_before.lstrip()
        # 1. 子参数补全（例如 `/mode <TAB>`、`/model <TAB>`、`/gateway <TAB>`、`/dream <TAB>`）
        if " " in stripped and stripped.startswith("/"):
            cmd = stripped.split(maxsplit=1)[0].lower()
            if cmd == "/mode":
                return [m for m in ("ask", "auto-edit", "full-auto", "plan") if m.startswith(word)]
            if cmd == "/plan":
                return [
                    m for m in ("on", "off", "ask", "auto-edit", "full-auto") if m.startswith(word)
                ]
            if cmd == "/model":
                cands = ["default", *sorted(DEFAULT_MODEL_PRICING_USD_PER_MTOK.keys())]
                return [m for m in cands if m.startswith(word)]
            if cmd == "/gateway":
                return _collect_gateway_profile_completions(runtime, word)
            if cmd == "/a2a":
                a2a_cands = _collect_a2a_completions(runtime, stripped, word)
                if a2a_cands is not None:
                    return a2a_cands
            if cmd == "/dream":
                return [f for f in ("--dry-run", "--rollback") if f.startswith(word)]
            if cmd == "/claude":
                return [lyr for lyr in ("global", "project") if lyr.startswith(word)]
            if cmd == "/rewind":
                return [sc for sc in ("both", "files", "messages") if sc.startswith(word)]
        # 2. 仅当光标词以 `@` 开头时才扫描工作区文件，避免普通 `/` 补全触发目录遍历
        if word.startswith("@"):
            prefix = word[1:]
            return [f"@{rel}" for rel in _get_cached_files() if rel.startswith(prefix)]
        if word.startswith("/"):
            items: list[str] = list(_SLASH_COMMANDS)
            try:
                for sk in runtime.skills.catalog():
                    items.append(f"/{sk['name']}")
            except Exception:
                pass
            return [c for c in items if c.startswith(word)]
        return []

    hist_path = _repl_history_path(runtime)
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.completion import Completer, Completion
        from prompt_toolkit.history import FileHistory, InMemoryHistory

        class _ReplCompleter(Completer):
            def get_completions(self, document, complete_event):  # type: ignore[no-untyped-def]
                word = document.get_word_before_cursor(WORD=True)
                line_before = document.current_line_before_cursor
                if not word and not (line_before.lstrip().startswith("/") and " " in line_before):
                    return
                for cand in _collect_completions_for(line_before, word):
                    yield Completion(cand, start_position=-len(word))

        history_obj: Any = FileHistory(str(hist_path)) if hist_path is not None else InMemoryHistory()
        session: Any = PromptSession(history=history_obj, completer=_ReplCompleter())
        return lambda prompt_str: session.prompt(prompt_str)
    except Exception:
        pass

    try:
        import atexit
        import readline

        if hist_path is not None:
            with contextlib.suppress(OSError):
                if hist_path.exists():
                    readline.read_history_file(str(hist_path))
            readline.set_history_length(500)
            atexit.register(lambda p=str(hist_path): readline.write_history_file(p))

        def _rl_completer(text: str, state: int) -> str | None:
            line_buf = readline.get_line_buffer()
            matches = _collect_completions_for(line_buf, text)
            return matches[state] if state < len(matches) else None

        readline.set_completer_delims(" \t\n;")
        readline.set_completer(_rl_completer)
        if "libedit" in (getattr(readline, "__doc__", "") or ""):
            readline.parse_and_bind("bind ^I rl_complete")
        else:
            readline.parse_and_bind("tab: complete")
    except Exception:
        pass
    return input_fn


def _broker_latest_seq(broker: Any, run_id: str) -> int:
    """查询流代理中指定 run 当前最新事件序号，用于初始化或推进单调流游标。"""
    if broker is None:
        return 0
    recent_fn = getattr(broker, "recent_events", None)
    if callable(recent_fn):
        try:
            recent = recent_fn(run_id)
            if recent:
                return max(int(ev.get("seq", 0) or 0) for ev in recent)
        except Exception:
            pass
    return 0


def _get_stream_cursor(stream_after_seq: dict[str, int], broker: Any, run_id: str) -> int:
    """获取或惰性初始化指定 run 的单调流消费游标（首次访问时从 broker 最新序号起步）。"""
    if run_id not in stream_after_seq:
        stream_after_seq[run_id] = _broker_latest_seq(broker, run_id)
    return stream_after_seq[run_id]


async def drain_approvals_interactively(
    runtime: AgentRuntime,
    run: dict[str, Any],
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    stream_output: bool = False,
    stream_after_seq: dict[str, int] | None = None,
    stream_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """当任务处于 `waiting_approval` 时，在终端内联循环排空全部待确认提案，并支持热升级权限或会话模式授权。

    安全加固：
    - 空回车 `""` 绝不视为自动批准（防止用户粘贴多行文本中的空行触发误批），会安全重问直至收到显式指令或 EOF 挂起；
    - 捕获 `(EOFError, KeyboardInterrupt, StopIteration)` 时保持 `waiting_approval` 原样返回，保留 `--resume` 能力；
    - 支持 `[p]` 会话模式授权：调用 `runtime.approve(..., grant_pattern=suggested_pattern)`。
    """
    use_color = _is_interactive_tty(input_fn, output_fn)
    broker = getattr(runtime, "stream_broker", None)
    seq_map = stream_after_seq if stream_after_seq is not None else {}
    acted = False
    last_streamed = False
    last_interrupted = False
    current = run
    while current.get("status") == "waiting_approval":
        waiting_ids = (current.get("metadata") or {}).get("waiting_proposal_ids") or []
        if not waiting_ids:
            break
        pid = waiting_ids[0]
        proposal = runtime.storage.get_proposal(pid)
        if not proposal:
            break
        output_fn(_format_proposal_preview(proposal, workspace=runtime.workspace, use_color=use_color))
        prop_kind = str(proposal.get("kind") or "single_file")
        raw_input = ""
        invalid_attempts = 0
        paused = False
        answer_text: str | None = None
        grant_pattern: str | None = None
        approved = False

        if prop_kind == "user_question":
            qinfo = _extract_question_fields(proposal)
            options = qinfo["options"]
            multi_select = qinfo["multi_select"]
            if options:
                prompt_msg = (
                    f"请输入选项编号 (1-{len(options)}) 或直接输入回复文本 "
                    f"[n=取消/跳过 / s:<指令>=转向]: "
                )
            else:
                prompt_msg = "请输入你的回复 [n=取消/跳过 / s:<指令>=转向]: "

            while True:
                try:
                    raw_input = input_fn(prompt_msg).strip()
                except (EOFError, KeyboardInterrupt, StopIteration):
                    output_fn("\n已暂停等待回复，稍后可通过 --resume 恢复。")
                    paused = True
                    break
                if not raw_input:
                    invalid_attempts += 1
                    output_fn("请输入选项编号、回复文本或 n 跳过；空回车不会自动提交。")
                    if invalid_attempts >= 3:
                        output_fn("连续空输入，已安全暂停等待回复。")
                        paused = True
                        break
                    continue
                low = raw_input.lower()
                if low.startswith(("s:", "/steer ")):
                    break
                if low in {"n", "no", "reject", "cancel"}:
                    approved = False
                    answer_text = None
                    break
                if options:
                    if multi_select and "," in raw_input:
                        tokens = [tok.strip() for tok in raw_input.split(",") if tok.strip()]
                        if tokens and all(tok.isdigit() for tok in tokens):
                            indices = [int(tok) for tok in tokens]
                            if all(1 <= idx <= len(options) for idx in indices):
                                picked_opts: list[str] = []
                                for idx in indices:
                                    opt_val = options[idx - 1]
                                    if opt_val not in picked_opts:
                                        picked_opts.append(opt_val)
                                answer_text = ", ".join(picked_opts)
                                approved = True
                                break
                            invalid_attempts += 1
                            output_fn(
                                f"选项编号超出范围（有效范围 1-{len(options)}），请重新输入编号或文本。"
                            )
                            if invalid_attempts >= 3:
                                output_fn("多次无效输入，已安全暂停等待回复。")
                                paused = True
                                break
                            continue
                    if raw_input.isdigit():
                        idx = int(raw_input)
                        if 1 <= idx <= len(options):
                            answer_text = options[idx - 1]
                            approved = True
                            break
                        invalid_attempts += 1
                        output_fn(
                            f"选项编号超出范围（有效范围 1-{len(options)}），请重新输入编号或文本。"
                        )
                        if invalid_attempts >= 3:
                            output_fn("多次无效输入，已安全暂停等待回复。")
                            paused = True
                            break
                        continue
                answer_text = raw_input
                approved = True
                break
        elif prop_kind == "plan_approval":
            pinfo = _extract_plan_fields(proposal)
            target_mode_hint = pinfo["target_permission_mode"] or "auto-edit"
            prompt_msg = (
                f"批准该实施计划并开始执行？ "
                f"[y=批准计划(→{target_mode_hint}) / n=拒绝计划 / a=auto-edit并批 / "
                f"f=full-auto并批 / v=看完整计划 / s:<指令>=转向]: "
            )
            while True:
                try:
                    raw_input = input_fn(prompt_msg).strip()
                except (EOFError, KeyboardInterrupt, StopIteration):
                    output_fn("\n已暂停计划审批，稍后可通过 --resume 恢复。")
                    paused = True
                    break
                if not raw_input:
                    invalid_attempts += 1
                    output_fn(
                        "请输入明确选择（y / n / a / f / v / s:...）；空回车不再自动批准以防多行粘贴误批。"
                    )
                    if invalid_attempts >= 3:
                        output_fn("连续空输入，已安全暂停审批（未批准任何操作）。")
                        paused = True
                        break
                    continue
                low = raw_input.lower()
                if low in {"v", "view", "plan", "diff"}:
                    full_plan = pinfo["plan_content"] or pinfo["summary"]
                    if not full_plan and pinfo["plan_file"]:
                        full_plan = _safe_read_workspace_text(runtime.workspace, pinfo["plan_file"]) or ""
                    if full_plan:
                        output_fn(_render_terminal_markdown(full_plan, use_color=use_color))
                    else:
                        output_fn("（该计划提案无更多正文内容）")
                    continue
                if (
                    low.startswith(("s:", "/steer ", "n:"))
                    or low
                    in {
                        "y",
                        "yes",
                        "n",
                        "no",
                        "reject",
                        "a",
                        "auto",
                        "auto-edit",
                        "f",
                        "full",
                        "full-auto",
                        "yolo",
                    }
                ):
                    break
                invalid_attempts += 1
                output_fn(
                    f"无法识别的选项 {raw_input!r}（未静默拒绝，请输入 y / n / a / f / v / s:...）。"
                )
                if invalid_attempts >= 3:
                    output_fn("多次无效输入，已安全暂停审批（保持待审状态）。")
                    paused = True
                    break
        else:
            suggested_pattern = proposal.get("suggested_grant_pattern")
            has_pattern = isinstance(suggested_pattern, str) and bool(suggested_pattern.strip())
            prompt_suffix = " / p=本会话允许该模式" if has_pattern else ""
            prompt_msg = (
                f"允许执行？ [y=批准 / n=拒绝 / a=auto-edit并批 / f=full-auto并批"
                f"{prompt_suffix} / v=看完整Diff / s:<指令>=转向]: "
            )

            while True:
                try:
                    raw_input = input_fn(prompt_msg).strip()
                except (EOFError, KeyboardInterrupt, StopIteration):
                    output_fn("\n已暂停审批，稍后可通过 --resume 恢复。")
                    paused = True
                    break
                if not raw_input:
                    invalid_attempts += 1
                    output_fn("请输入明确选择（y / n / a / f / p / v / s:...）；空回车不再自动批准以防多行粘贴误批。")
                    if invalid_attempts >= 3:
                        output_fn("连续空输入，已安全暂停审批（未批准任何操作）。")
                        paused = True
                        break
                    continue
                low = raw_input.lower()
                if low in {"v", "view", "diff"}:
                    full_diff = _build_proposal_diff_lines(proposal, workspace=runtime.workspace, max_lines=0)
                    if full_diff:
                        output_fn("\n".join(_colorize_diff_line(ln, use_color=use_color) for ln in full_diff))
                    else:
                        output_fn("（该提案无额外文件 Diff）")
                    continue
                if (
                    low.startswith(("s:", "/steer "))
                    or low in {"y", "yes", "n", "no", "reject", "a", "auto", "auto-edit", "f", "full", "full-auto", "yolo"}
                    or (low in {"p", "pattern"} and has_pattern)
                ):
                    break
                invalid_attempts += 1
                output_fn(f"无法识别的选项 {raw_input!r}（未静默拒绝，请输入 y / n / a / f / v / s:...）。")
                if invalid_attempts >= 3:
                    output_fn("多次无效输入，已安全暂停审批（保持待审状态）。")
                    paused = True
                    break
        if paused:
            break

        if raw_input.lower().startswith(("s:", "/steer ")):
            steer_text = (
                raw_input[2:].strip()
                if raw_input.lower().startswith("s:")
                else raw_input[7:].strip()
            )
            if steer_text:
                output_fn(f"[用户转向接管，取消待审提案并注入新指令：{steer_text[:80]}]")
                rid = current["id"]
                start_seq = _get_stream_cursor(seq_map, broker, rid)
                current, step_interrupted, step_streamed, next_seq = await _execute_interruptible_turn(
                    runtime,
                    rid,
                    runtime.send_message(rid, steer_text, supersede_waiting_approval=True),
                    input_fn=input_fn,
                    output_fn=output_fn,
                    stream_output=stream_output,
                    after_seq=start_seq,
                )
                seq_map[rid] = max(seq_map.get(rid, 0), next_seq)
                acted = True
                last_streamed = step_streamed
                last_interrupted = step_interrupted
                if step_interrupted:
                    break
                continue

        if prop_kind != "user_question":
            suggested_pattern = proposal.get("suggested_grant_pattern")
            has_pattern = isinstance(suggested_pattern, str) and bool(suggested_pattern.strip())
            raw_choice = raw_input.lower()
            # 注意顺序：必须先调用 set_permission_mode 落库新权限，再调用 approve() 触发后续自动续跑！
            if raw_choice in {"a", "auto", "auto-edit"}:
                runtime.set_permission_mode(current["id"], "auto-edit")
                output_fn("[权限已切换为 auto-edit]")
                approved = True
            elif raw_choice in {"f", "full", "full-auto", "yolo"}:
                runtime.set_permission_mode(current["id"], "full-auto")
                output_fn("[权限已切换为 full-auto]")
                approved = True
            elif prop_kind != "plan_approval" and raw_choice in {"p", "pattern"} and has_pattern:
                grant_pattern = str(suggested_pattern).strip()
                output_fn(f"[已为本会话添加模式放行规则：{grant_pattern}]")
                approved = True
            elif raw_choice in {"y", "yes"}:
                approved = True
            elif prop_kind == "plan_approval" and raw_choice.startswith("n:"):
                approved = False
                feedback = raw_input[2:].strip()
                if feedback:
                    answer_text = feedback
            else:
                approved = False

        rid = current["id"]
        start_seq = _get_stream_cursor(seq_map, broker, rid)
        current, step_interrupted, step_streamed, next_seq = await _execute_interruptible_turn(
            runtime,
            rid,
            _build_approve_coro(
                runtime,
                rid,
                pid,
                approved,
                grant_pattern=grant_pattern,
                answer_text=answer_text,
            ),
            input_fn=input_fn,
            output_fn=output_fn,
            stream_output=stream_output,
            after_seq=start_seq,
        )
        updated_perm = (current.get("metadata") or {}).get("permission_mode")
        if isinstance(updated_perm, str) and updated_perm:
            with contextlib.suppress(ValueError):
                runtime.permission_mode = normalize_permission_mode(updated_perm)
        seq_map[rid] = max(seq_map.get(rid, 0), next_seq)
        acted = True
        last_streamed = step_streamed
        last_interrupted = step_interrupted
        if step_interrupted:
            break
    if stream_result is not None:
        stream_result["acted"] = acted
        stream_result["streamed"] = last_streamed
        stream_result["interrupted"] = last_interrupted
    return current


def _format_single_event_lines(ev: dict[str, Any]) -> list[str]:
    """将单条持久化/流式事件格式化为简洁终端遥测行。"""
    kind = ev.get("kind")
    payload = ev.get("payload") or {}
    lines: list[str] = []
    if kind == "tool_result":
        tname = payload.get("name", "tool")
        res = payload.get("result") or {}
        status_tag = "OK" if res.get("ok", True) else f"ERR:{res.get('code', 'failed')}"
        if res.get("requires_approval"):
            status_tag = "WAITING_APPROVAL"
        lines.append(f"  ● {tname} -> {status_tag}")
        if tname == "bash_exec" and isinstance(res.get("stdout"), str) and res["stdout"].strip():
            lines.append("\n".join("    │ " + ln for ln in res["stdout"].strip().splitlines()[:8]))
    elif kind == "subagent_started":
        lines.append(f"  ● [子 Agent 启动] {payload.get('count', 1)} 个并行角色: {payload.get('roles')}")
    elif kind == "subagents_finished":
        lines.append(f"  ● subagent_run 完成 ({payload.get('count')} 个子 Agent: {payload.get('roles')})")
    elif kind == "user_steering_injected":
        lines.append(f"  ● [实时补充指令已注入] {payload.get('count', 1)} 条")
    elif kind == "mcp_tool_searched":
        lines.append(
            f"  ● [MCP 工具检索] query={payload.get('query', '')!r} -> 命中 {payload.get('match_count', 0)} 项"
        )
    elif kind == "a2a_task_delegated":
        lines.append(
            f"  ● [A2A 委托] peer={payload.get('peer', '')} · "
            f"task={str(payload.get('task_id') or '-')[:12]} · state={payload.get('state', 'unknown')}"
        )
    elif kind == "skill_routed":
        lines.append(f"  ● [显式技能路由] {payload.get('skills')}")
    elif kind == "memory_note_written":
        lines.append(f"  ● [记忆写入] {payload.get('layer')}:{payload.get('path')} ({payload.get('status')})")
    elif kind == "memory_dreamed":
        lines.append(
            f"  ● [Dreaming 完成] 纠错={payload.get('corrections_count', 0)} · "
            f"Wiki更新={payload.get('pages_written_count', 0)} · "
            f"断链修复={payload.get('links_repaired_count', 0)}"
        )
    elif kind == "context_compacted":
        trig = "自动水位" if payload.get("trigger") == "auto_watermark" else "手动"
        lines.append(
            f"  ● [上下文压缩] ({trig}) 裁剪组={payload.get('dropped_groups', 0)} · "
            f"摘要={payload.get('summary_chars', 0)} 字符"
        )
    elif kind == "egress_dlp_blocked":
        lines.append(
            f"  ● [出站安全 DLP 已拦截] tool={payload.get('tool', '-')} · "
            f"reason={payload.get('reason') or payload.get('code') or 'sensitive_credential'}"
        )
    elif kind == "plan_mode_entered":
        plan_parts = [
            f"plan_file={payload['plan_file']}" if payload.get("plan_file") else "",
            f"reason={str(payload['reason'])[:80]}" if payload.get("reason") else "",
        ]
        plan_extra = " · ".join(p for p in plan_parts if p)
        lines.append(f"  ● [已进入只读规划模式]{' ' + plan_extra if plan_extra else ''}")
    return lines


def _clear_spinner_line(hud_state: dict[str, Any]) -> None:
    if hud_state.get("spinner_visible"):
        with contextlib.suppress(OSError):
            sys.stderr.write("\r\x1b[2K")
            sys.stderr.flush()
        hud_state["spinner_visible"] = False


async def _run_tty_hud_and_steer(
    runtime: AgentRuntime,
    run_id: str,
    *,
    stop_event: asyncio.Event,
    hud_state: dict[str, Any],
) -> None:
    """在真实 TTY 活跃回合中绘制轻量动态 Spinner HUD，并监听 Ctrl-G（\\x07）实时入队插话。"""
    started = time.monotonic()
    frame_idx = 0
    fd = None
    old_termios = None
    try:
        import termios

        fd = sys.stdin.fileno()
        old_termios = termios.tcgetattr(fd)
        new_attr = termios.tcgetattr(fd)
        # 仅关闭 ICANON + ECHO，保留 ISIG 让 Ctrl-C 正常触发 SIGINT 取消！
        new_attr[3] = new_attr[3] & ~(termios.ICANON | termios.ECHO)
        new_attr[6][termios.VMIN] = 0
        new_attr[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSADRAIN, new_attr)
    except Exception:
        fd = None
        old_termios = None

    try:
        while not stop_event.is_set():
            # 1. 非阻塞检查用户是否按下 Ctrl-G (\x07) 触发运行中实时插话
            if fd is not None:
                try:
                    ready, _, _ = select.select([fd], [], [], 0.0)
                    if ready:
                        ch = os.read(fd, 1)
                        if ch == b"\x07":
                            _clear_spinner_line(hud_state)
                            # 暂时恢复规范终端模式读取整行实时转向指令
                            if old_termios is not None:
                                import termios

                                termios.tcsetattr(fd, termios.TCSADRAIN, old_termios)
                            try:
                                steer_msg = await asyncio.to_thread(input, "\n[实时插话] > ")
                                steer_clean = steer_msg.strip()
                                if steer_clean:
                                    rec = runtime.storage.enqueue_steering_message(run_id, steer_clean)
                                    sys.stderr.write(
                                        f"{_ANSI_CYAN}[已入队实时指令 #{rec['id']}，当前工具步返回后立即合并]{_ANSI_RESET}\n"
                                    )
                                    sys.stderr.flush()
                            except (EOFError, KeyboardInterrupt):
                                pass
                            finally:
                                if old_termios is not None and not stop_event.is_set():
                                    import termios

                                    cur_attr = termios.tcgetattr(fd)
                                    cur_attr[3] = cur_attr[3] & ~(termios.ICANON | termios.ECHO)
                                    cur_attr[6][termios.VMIN] = 0
                                    cur_attr[6][termios.VTIME] = 0
                                    termios.tcsetattr(fd, termios.TCSADRAIN, cur_attr)
                except OSError:
                    pass

            # 2. 静默等待时绘制单行动态 Spinner 状态栏
            if not hud_state.get("paused_for_output"):
                elapsed = time.monotonic() - started
                status_label = str(hud_state.get("label") or "思考中")
                frame = _SPINNER_FRAMES[frame_idx % len(_SPINNER_FRAMES)]
                frame_idx += 1
                with contextlib.suppress(OSError):
                    sys.stderr.write(
                        f"\r\x1b[2K{_ANSI_DIM}{frame} {status_label} · {elapsed:.1f}s · "
                        f"Ctrl-G 实时插话 · Ctrl-C 中断{_ANSI_RESET}"
                    )
                    sys.stderr.flush()
                    hud_state["spinner_visible"] = True
            await asyncio.sleep(0.12)
    except asyncio.CancelledError:
        pass
    finally:
        _clear_spinner_line(hud_state)
        if fd is not None and old_termios is not None:
            with contextlib.suppress(Exception):
                import termios

                termios.tcsetattr(fd, termios.TCSADRAIN, old_termios)


async def _consume_live_stream(
    runtime: AgentRuntime,
    run_id: str,
    *,
    output_fn: Callable[[str], None],
    use_color: bool,
    stop_event: asyncio.Event,
    after_seq: int = 0,
    stream_state: dict[str, Any] | None = None,
    hud_state: dict[str, Any] | None = None,
) -> tuple[bool, int]:
    """在活跃回合期间消费 `RunStreamBroker` 增量帧，实时打印思考片段、工具进度、回答流与终端日志。

    返回 `(streamed_text, last_seq)`，其中 `last_seq` 在每条已消费信封上单调递增更新。
    """
    broker = getattr(runtime, "stream_broker", None)
    last_seq = max(0, int(after_seq))
    if stream_state is not None:
        stream_state["last_seq"] = max(int(stream_state.get("last_seq", 0)), last_seq)
        stream_state.setdefault("streamed_text", False)
        stream_state.setdefault("rendered_event_ids", set())
    if broker is None:
        return False, last_seq
    streamed_text = False
    thinking_started = False
    is_stdout = output_fn is print

    def _emit_inline(chunk: str) -> None:
        if hud_state is not None:
            _clear_spinner_line(hud_state)
            hud_state["paused_for_output"] = True
        if is_stdout:
            sys.stdout.write(chunk)
            sys.stdout.flush()
        else:
            output_fn(chunk)

    try:
        async for envelope in broker.subscribe(run_id, after_seq=last_seq):
            if stop_event.is_set():
                break
            raw_seq = envelope.get("seq")
            if isinstance(raw_seq, int) and not isinstance(raw_seq, bool) and raw_seq > last_seq:
                last_seq = raw_seq
                if stream_state is not None:
                    stream_state["last_seq"] = last_seq
            ev_type = envelope.get("type")
            payload = envelope.get("payload") or {}
            if ev_type == "thinking_delta":
                delta = str(payload.get("delta") or payload.get("text") or "")
                if delta:
                    if hud_state is not None:
                        hud_state["label"] = "深度推理中"
                    if not thinking_started:
                        thinking_started = True
                        prefix = f"{_ANSI_DIM}[Thinking] " if use_color else "[Thinking] "
                        _emit_inline(prefix)
                    _emit_inline(f"{_ANSI_DIM}{delta}{_ANSI_RESET}" if use_color else delta)
            elif ev_type == "text_delta":
                delta = str(payload.get("delta") or payload.get("text") or "")
                if delta:
                    if thinking_started:
                        thinking_started = False
                        _emit_inline("\n")
                    streamed_text = True
                    if stream_state is not None:
                        stream_state["streamed_text"] = True
                    _emit_inline(delta)
            elif ev_type == "tool_use_start":
                tname = str(payload.get("name") or "tool")
                if hud_state is not None:
                    hud_state["label"] = f"执行工具 {tname}"
                    hud_state["paused_for_output"] = False
                if thinking_started:
                    thinking_started = False
                    _emit_inline("\n")
                if use_color:
                    _emit_inline(f"\n{_ANSI_DIM}  ▸ 调用工具: {tname}...{_ANSI_RESET}\n")
            elif ev_type == "subagent_progress":
                role = str(payload.get("role") or payload.get("job_id") or "subagent")
                st = str(payload.get("status") or "running")
                if hud_state is not None:
                    hud_state["label"] = f"子 Agent {role} ({st})"
            elif ev_type == "bash_output_delta":
                chunk = str(payload.get("delta") or payload.get("text") or payload.get("chunk") or "")
                if chunk:
                    if thinking_started:
                        thinking_started = False
                        _emit_inline("\n")
                    for ln in chunk.splitlines():
                        _emit_inline(f"    │ {ln}\n")
            elif ev_type == "event":
                ev_id = payload.get("id")
                lines = _format_single_event_lines(payload)
                if lines:
                    if thinking_started:
                        thinking_started = False
                        _emit_inline("\n")
                    for ln in lines:
                        _emit_inline(f"{ln}\n")
                if isinstance(ev_id, int) and stream_state is not None:
                    stream_state["rendered_event_ids"].add(ev_id)
                if hud_state is not None:
                    hud_state["paused_for_output"] = False
            elif ev_type == "run_status":
                status = payload.get("status")
                if status in {"completed", "failed", "cancelled", "waiting_approval", "dead_letter"}:
                    break
    except asyncio.CancelledError:
        pass
    if is_stdout and (streamed_text or thinking_started):
        sys.stdout.write("\n")
        sys.stdout.flush()
    return streamed_text, last_seq


async def _execute_interruptible_turn(
    runtime: AgentRuntime,
    run_id: str,
    coro: Any,
    *,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
    stream_output: bool = False,
    after_seq: int | None = None,
    rendered_event_ids: set[int] | None = None,
) -> tuple[dict[str, Any], bool, bool, int]:
    """执行单个活跃回合，支持 Ctrl-C 优雅取消（不崩掉 REPL）、TTY 实时 HUD/Ctrl-G 插话与流式输出。

    返回 `(run_dict, was_interrupted, streamed_answer_text, next_seq)`。
    """
    use_color = _is_interactive_tty(input_fn, output_fn)
    broker = getattr(runtime, "stream_broker", None)
    enable_stream = broker is not None and (stream_output or use_color)
    start_seq = (
        max(0, int(after_seq))
        if after_seq is not None
        else _broker_latest_seq(broker, run_id)
    )
    stop_stream = asyncio.Event()
    hud_state: dict[str, Any] = {"label": "思考中", "spinner_visible": False, "paused_for_output": False}
    stream_state: dict[str, Any] = {
        "streamed_text": False,
        "last_seq": start_seq,
        "rendered_event_ids": rendered_event_ids if rendered_event_ids is not None else set(),
    }
    stream_task: asyncio.Task[tuple[bool, int]] | None = None
    hud_task: asyncio.Task[None] | None = None
    if enable_stream and broker is not None:
        reopen_fn = getattr(broker, "reopen_run", None)
        if callable(reopen_fn):
            reopen_fn(run_id)
        stream_task = asyncio.create_task(
            _consume_live_stream(
                runtime,
                run_id,
                output_fn=output_fn,
                use_color=use_color,
                stop_event=stop_stream,
                after_seq=start_seq,
                stream_state=stream_state,
                hud_state=hud_state if use_color else None,
            )
        )
    if use_color:
        hud_task = asyncio.create_task(
            _run_tty_hud_and_steer(runtime, run_id, stop_event=stop_stream, hud_state=hud_state)
        )

    turn_task: asyncio.Task[dict[str, Any]] = asyncio.create_task(coro)
    interrupted = False
    turn_finished = False
    cancel_scheduled = False
    sigint_event = threading.Event()
    prev_sigint: Any = None
    using_loop_sigint = False
    installed_raw_sigint = False
    loop = asyncio.get_running_loop()

    def _cancel_from_loop() -> None:
        nonlocal interrupted, cancel_scheduled
        if turn_finished or cancel_scheduled:
            return
        interrupted = True
        cancel_scheduled = True
        try:
            runtime.cancel_active_run(run_id, reason="repl_sigint")
        except Exception:
            pass
        if not turn_task.done():
            turn_task.cancel()

    def _on_loop_sigint() -> None:
        sigint_event.set()
        _cancel_from_loop()

    def _on_raw_sigint(_signum: int, _frame: Any) -> None:
        # 严禁在原始信号处理器内同步执行 SQLite 写或锁操作；仅原子置位并交由事件循环安全取消
        sigint_event.set()
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(_cancel_from_loop)

    if threading.current_thread() is threading.main_thread() and _is_interactive_tty(input_fn, output_fn):
        try:
            prev_sigint = signal.getsignal(signal.SIGINT)
        except Exception:
            prev_sigint = None
        try:
            loop.add_signal_handler(signal.SIGINT, _on_loop_sigint)
            using_loop_sigint = True
        except (NotImplementedError, RuntimeError):
            try:
                signal.signal(signal.SIGINT, _on_raw_sigint)
                installed_raw_sigint = True
            except Exception:
                installed_raw_sigint = False

    result_run: dict[str, Any] | None = None
    turn_succeeded = False
    try:
        result_run = await turn_task
        turn_succeeded = True
    except (KeyboardInterrupt, asyncio.CancelledError):
        interrupted = True
        if not cancel_scheduled:
            cancel_scheduled = True
            cancel_reason = "repl_sigint" if sigint_event.is_set() else "repl_keyboard_interrupt"
            try:
                runtime.cancel_active_run(run_id, reason=cancel_reason)
            except Exception:
                pass
        _clear_spinner_line(hud_state)
        output_fn("\n[已中断当前生成回合，会话仍保持活跃；可输入新指令、/rewind 或 /exit]")
        result_run = runtime.storage.get_run(run_id) or {"id": run_id, "status": "cancelled"}
    finally:
        turn_finished = True
        if using_loop_sigint:
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(signal.SIGINT)
            if prev_sigint is not None:
                with contextlib.suppress(Exception):
                    signal.signal(signal.SIGINT, prev_sigint)
        elif installed_raw_sigint:
            with contextlib.suppress(Exception):
                signal.signal(signal.SIGINT, prev_sigint)

        if hud_task is not None:
            stop_stream.set()
            if not hud_task.done():
                hud_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await hud_task
            _clear_spinner_line(hud_state)

        streamed_text = bool(stream_state.get("streamed_text", False))
        next_seq = int(stream_state.get("last_seq", start_seq))
        if stream_task is not None:
            if turn_succeeded and not interrupted:
                try:
                    done_streamed, done_seq = await asyncio.wait_for(stream_task, timeout=0.35)
                    streamed_text = bool(done_streamed) or streamed_text
                    next_seq = max(next_seq, int(done_seq))
                except (asyncio.TimeoutError, TimeoutError, asyncio.CancelledError, Exception):
                    stop_stream.set()
                    if not stream_task.done():
                        stream_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        res = await stream_task
                        if isinstance(res, tuple) and len(res) == 2:
                            streamed_text = bool(res[0]) or streamed_text
                            next_seq = max(next_seq, int(res[1]))
            else:
                stop_stream.set()
                if not stream_task.done():
                    stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    res = await stream_task
                    if isinstance(res, tuple) and len(res) == 2:
                        streamed_text = bool(res[0]) or streamed_text
                        next_seq = max(next_seq, int(res[1]))

        streamed_text = streamed_text or bool(stream_state.get("streamed_text", False))
        next_seq = max(
            next_seq,
            int(stream_state.get("last_seq", start_seq)),
            _broker_latest_seq(broker, run_id),
        )

    return result_run, interrupted, streamed_text, next_seq


def _render_turn_events(
    runtime: AgentRuntime,
    run_id: str,
    last_event_id: int,
    output_fn: Callable[[str], None],
    *,
    skip_ids: set[int] | None = None,
) -> int:
    """按单调递增主键游标 `last_event_id` 增量拉取并渲染本阶段新增事件，跳过已流式输出过的事件防重复。"""
    events = runtime.storage.events(run_id, after=last_event_id)
    new_cursor = last_event_id
    for ev in events:
        ev_id = int(ev["id"])
        new_cursor = max(new_cursor, ev_id)
        if skip_ids and ev_id in skip_ids:
            continue
        for ln in _format_single_event_lines(ev):
            output_fn(ln)
    return new_cursor


async def run_interactive_repl(
    runtime: AgentRuntime,
    *,
    initial_prompt: str | None = None,
    resume_run_id: str | None = None,
    mode: str = "agent",
    extra_metadata: dict[str, Any] | None = None,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    stream_output: bool = False,
) -> dict[str, Any] | None:
    """运行多轮交互式终端工作台，返回最后一次任务的最终字典状态。"""
    current_run: dict[str, Any] | None = None
    event_cursor = 0
    stream_after_seq: dict[str, int] = {}
    streamed_event_ids: set[int] = set()
    broker = getattr(runtime, "stream_broker", None)
    meta_template = dict(extra_metadata or {})
    meta_template["permission_mode"] = runtime.permission_mode
    pre_plan_mode = runtime.permission_mode if runtime.permission_mode != "plan" else "auto-edit"
    gateway_config_path: str | Path | None = (
        meta_template.get("gateway_config") or getattr(runtime, "gateway_config_path", None)
    )
    if gateway_config_path is not None and not getattr(runtime, "gateway_config_path", None):
        runtime.gateway_config_path = gateway_config_path  # type: ignore[attr-defined]
    prompt_reader = _build_tty_prompt_reader(runtime, input_fn=input_fn, output_fn=output_fn)
    use_color = _is_interactive_tty(input_fn, output_fn)

    git_branch = ""
    if is_git_worktree(runtime.workspace):
        with contextlib.suppress(Exception):
            git_branch = str(worktree_status(runtime.workspace).get("branch") or "")
    resolved_model = _resolve_active_model_label(runtime)
    active_gw = _describe_active_gateway(runtime)
    gw_suffix = (
        f" | 网关: {active_gw['profile']} ({active_gw['protocol']})"
        if active_gw["profile"] != "(direct)"
        else ""
    )
    ws_label = f"{runtime.workspace}" + (f" ({git_branch})" if git_branch else "")
    banner_lines = [
        f"┌── Agent Systems Lab · 本地操控工作台 ({runtime.execution_profile}) ──",
        f"│ 目录: {ws_label} | 权限: {runtime.permission_mode} | 模型: {resolved_model}{gw_suffix}",
        "│ 提示: 输入 /help 查看命令 · 支持 /<skill> 与 @文件 · Ctrl-G 运行中插话 · Ctrl-C 中断回合",
    ]
    is_demo_mode = (
        runtime.provider_name == "demo"
        or getattr(runtime.provider, "protocol", None) == "demo"
    )
    if is_demo_mode:
        warn_msg = "│ 注意: 当前为离线 DEMO 模式（运行 `agent-lab init` 或配置 ANTHROPIC_API_KEY 接入真实模型）"
        banner_lines.append(f"{_ANSI_YELLOW}{warn_msg}{_ANSI_RESET}" if use_color else warn_msg)
    banner_lines.append("└" + "─" * 64)
    output_fn("\n".join(banner_lines))

    if resume_run_id:
        current_run = await runtime.resume(resume_run_id)
        stream_after_seq[current_run["id"]] = max(
            stream_after_seq.get(current_run["id"], 0),
            _broker_latest_seq(broker, current_run["id"]),
        )
        resumed_perm = (current_run.get("metadata") or {}).get("permission_mode")
        if isinstance(resumed_perm, str) and resumed_perm:
            runtime.permission_mode = normalize_permission_mode(resumed_perm)
            meta_template["permission_mode"] = runtime.permission_mode
        event_cursor = _render_turn_events(runtime, current_run["id"], event_cursor, output_fn)
        resume_approval_stream: dict[str, Any] = {}
        current_run = await drain_approvals_interactively(
            runtime,
            current_run,
            input_fn=input_fn,
            output_fn=output_fn,
            stream_output=stream_output,
            stream_after_seq=stream_after_seq,
            stream_result=resume_approval_stream,
        )
        event_cursor = _render_turn_events(runtime, current_run["id"], event_cursor, output_fn)
        if current_run.get("answer") and not resume_approval_stream.get("streamed"):
            output_fn(f"\n{current_run['answer']}\n")

    pending_first = initial_prompt.strip() if initial_prompt and initial_prompt.strip() else None

    while True:
        if pending_first is not None:
            user_text = pending_first
            pending_first = None
        else:
            try:
                user_text = prompt_reader(f"\n[{runtime.permission_mode}] > ").strip()
            except KeyboardInterrupt:
                output_fn("\n（提示：输入 /exit 或按 Ctrl-D 退出工作台）")
                continue
            except (EOFError, StopIteration):
                output_fn("\n退出会话。")
                break

        if not user_text:
            continue
        if user_text in {"/exit", "/quit"}:
            output_fn("退出会话。")
            break
        if user_text == "/help":
            output_fn(
                "可用命令：\n"
                "  /mode <ask|auto-edit|full-auto|plan>   切换当前会话权限策略（plan 为只读规划模式）\n"
                "  /plan [on|off|ask|auto-edit|full-auto] 进入或退出只读规划模式（先出方案再执行）\n"
                "  /compact [keep_recent] [focus]         压缩早期消息组并生成 COMPACTION_SUMMARY（支持聚焦重点）\n"
                "  /skills                                查看本地多通道技能目录及当前会话已激活技能快照\n"
                "  /mcp                                   查看外部 MCP Server 连接池与延迟 ToolSearch 模式状态\n"
                "  /a2a [list|discover|send]              查看远端 A2A 协同智能体、发现 AgentCard 或直接委托任务\n"
                "  /steer <instruction>                   向运行中任务写入实时补充指令，或直接转向接管待审批状态\n"
                "  /claude [global|project] [text]        查看双层 CLAUDE.md + MEMORY.md，或追加写入长期工程准则\n"
                "  /dream [--dry-run|--rollback [id]|goal] 运行离线 Dreaming、零落盘预演 Diff 或回滚上次整理\n"
                "  /memory                                查看当前全局与项目 MEMORY.md 顶层索引与 Wiki 统计\n"
                "  /rewind [checkpoint_id|turn] [scope]   列出检查点或回退工作区文件/对话（both|files|messages）\n"
                "  /fork [title]                          从当前会话分叉创建独立新会话并切换过去\n"
                "  /cost                                  查看 Token、缓存命中率、累计 USD 费用与预算上限\n"
                "  /model [model_name]                    查看或动态覆盖当前会话使用的模型名称\n"
                "  /gateway [profile]                     查看多协议网关状态或热切换 Profile（保留对话历史）\n"
                "  /hooks                                 查看已加载的生命周期 Hooks、声明式权限规则与会话授权\n"
                "  /bg                                    查看当前活跃的后台 PTY/Shell 进程与后台子 Agent 任务\n"
                "  /status                                查看当前会话轮次、token 与缓存命中率\n"
                "  /diff                                  查看当前 Git 工作区变更摘要（含未跟踪文件）\n"
                "  /clear                                 开启全新会话上下文\n"
                "  /exit                                  退出交互工作台"
            )
            continue
        if user_text == "/skills":
            cat = runtime.skills.catalog()
            active_map = (
                ((runtime.storage.get_run(current_run["id"]) or current_run).get("metadata") or {}).get(
                    "active_skills", {}
                )
                if current_run is not None
                else {}
            )
            skill_lines = [
                f"技能目录 ({len(cat)} 项可用，当前已激活 {len(active_map)} 项；支持在提问中以 /<name> 显式预载)："
            ]
            for item in cat:
                badges: list[str] = []
                if item["name"] in active_map:
                    badges.append("ACTIVE")
                if item.get("always_apply"):
                    badges.append("always-apply")
                if item.get("disable_model_invocation"):
                    badges.append("explicit-only")
                if item.get("paths"):
                    badges.append(f"paths={','.join(item['paths'][:2])}")
                badge_str = f" [{' | '.join(badges)}]" if badges else ""
                skill_lines.append(f"  - /{item['name']}{badge_str}: {item['description'][:120]}")
            output_fn("\n".join(skill_lines))
            continue
        if user_text == "/mcp":
            fresh_mcp_run = (
                runtime.storage.get_run(current_run["id"]) or current_run
                if current_run is not None
                else None
            )
            r_meta = (fresh_mcp_run.get("metadata") or {}) if fresh_mcp_run else {}
            frozen_routes = r_meta.get("mcp_tools") or runtime.mcp_manager.route_metadata()
            srv_summaries = r_meta.get("mcp_servers") or runtime.mcp_manager.server_summaries()
            deferred_flag = r_meta.get("mcp_deferred")
            mode_label = (
                "Deferred ToolSearch (mcp_tool_search + mcp_call_tool)"
                if deferred_flag
                else "Eager Direct"
            )
            if not srv_summaries and not runtime.mcp_manager.enabled:
                output_fn("当前未配置外部 MCP Server（可通过 --mcp-config 或 .agent-lab/mcp.json 配置）。")
            else:
                mcp_lines = [
                    f"外部 MCP 状态 | 模式: {mode_label} | 已冻结工具数: {len(frozen_routes)}"
                ]
                for sname, sinfo in sorted(srv_summaries.items()):
                    err_note = f" ERR={sinfo['error']}" if sinfo.get("error") else ""
                    mcp_lines.append(
                        f"  - {sname}: tools={sinfo.get('tool_count', 0)} "
                        f"(read_only={sinfo.get('read_only_count', 0)}){err_note}"
                    )
                output_fn("\n".join(mcp_lines))
            continue
        if user_text == "/a2a" or user_text.startswith("/a2a "):
            a2a_arg = user_text[4:].strip()
            a2a_parts = a2a_arg.split(maxsplit=1) if a2a_arg else []
            a2a_sub = a2a_parts[0].lower() if a2a_parts else "list"
            a2a_rest = a2a_parts[1].strip() if len(a2a_parts) > 1 else ""

            if a2a_sub == "list":
                a2a_lines: list[str] = []
                cfg_peers: dict[str, dict[str, Any]] = {}
                cfg_path_str: str | None = None
                try:
                    raw_a2a = _load_repl_a2a_document(runtime)
                    redacted_a2a = redact_a2a_document(raw_a2a)
                    cfg_path_str = redacted_a2a.get("config_path")
                    cfg_peers = dict(redacted_a2a.get("peers") or {})
                except Exception as exc:
                    a2a_lines.append(f"[A2A 配置读取失败] {exc}")

                runtime_mgr = getattr(runtime, "a2a_manager", None)
                runtime_peer_map: dict[str, dict[str, Any]] = {}
                if runtime_mgr is not None and hasattr(runtime_mgr, "list_peers"):
                    with contextlib.suppress(Exception):
                        for item in runtime_mgr.list_peers():
                            if isinstance(item, dict) and item.get("name"):
                                runtime_peer_map[str(item["name"])] = item

                all_names = sorted(set(cfg_peers.keys()) | set(runtime_peer_map.keys()))
                if not all_names:
                    if a2a_lines:
                        output_fn("\n".join(a2a_lines))
                    else:
                        output_fn(
                            "当前未配置远端 A2A 协同智能体（可运行 `agent-lab a2a init` 创建 .agent-lab/a2a.json）。"
                        )
                    continue

                header = f"A2A 远端协同智能体 ({len(all_names)} 个)"
                if cfg_path_str:
                    header += f" | 配置: {cfg_path_str}"
                a2a_lines.insert(0, header)
                for pname in all_names:
                    pdata = cfg_peers.get(pname) or {}
                    rdata = runtime_peer_map.get(pname) or {}
                    enabled_flag = bool(pdata.get("enabled", True))
                    status_tag = "enabled" if enabled_flag else "disabled"
                    url_val = str(pdata.get("url") or rdata.get("url") or "-")
                    cred_val = (
                        _format_repl_a2a_credential_status(pdata)
                        if pdata
                        else ("ready" if rdata.get("credential_configured") else "none")
                    )
                    desc_val = str(pdata.get("description") or rdata.get("description") or "").strip()
                    skills_cached = rdata.get("skills") or []
                    sk_note = (
                        f" | skills={','.join(str(s.get('id') or s.get('name') or '') for s in skills_cached[:4])}"
                        if skills_cached
                        else ""
                    )
                    desc_note = f" — {desc_val[:100]}" if desc_val else ""
                    a2a_lines.append(
                        f"  - {pname} [{status_tag}] {url_val} (creds={cred_val}){sk_note}{desc_note}"
                    )
                a2a_lines.append("用法：/a2a discover <peer|url> | /a2a send <peer> <prompt>")
                output_fn("\n".join(a2a_lines))
                continue

            if a2a_sub == "discover":
                if not a2a_rest:
                    output_fn("用法：/a2a discover <peer|url>")
                    continue
                target = a2a_rest.split()[0]
                try:
                    mgr, should_close = _resolve_repl_a2a_manager(runtime, target_peer=target)
                except Exception as exc:
                    output_fn(f"[A2A 发现失败] {exc}")
                    continue
                try:
                    res = await mgr.discover_agent_card(target)
                except Exception as exc:
                    output_fn(f"[A2A 发现失败] {exc}")
                    continue
                finally:
                    if should_close:
                        with contextlib.suppress(Exception):
                            await mgr.aclose()
                if not res.get("ok"):
                    output_fn(f"[A2A 发现失败] {res.get('error') or res.get('code') or 'unknown error'}")
                    continue
                card = res.get("card") or {}
                caps = card.get("capabilities") or {}
                card_skills = card.get("skills") or []
                card_lines = [
                    f"[A2A AgentCard] {card.get('name', target)} "
                    f"(v{card.get('version', '-')} · A2A {card.get('protocolVersion', '-')})",
                    f"  端点 URL : {card.get('url') or res.get('card_url') or '-'}",
                ]
                if card.get("description"):
                    card_lines.append(f"  能力简介 : {str(card['description'])[:200]}")
                card_lines.append(
                    f"  协议特性 : streaming={bool(caps.get('streaming'))} · "
                    f"stateTransitionHistory={bool(caps.get('stateTransitionHistory'))}"
                )
                if card_skills:
                    card_lines.append(f"  技能列表 ({len(card_skills)}):")
                    for sk in card_skills[:8]:
                        if isinstance(sk, dict):
                            sk_id = sk.get("id") or sk.get("name") or "-"
                            sk_desc = str(sk.get("description") or "")[:100]
                            card_lines.append(f"    - {sk_id}: {sk_desc}")
                output_fn("\n".join(card_lines))
                continue

            if a2a_sub == "send":
                send_parts = a2a_rest.split(maxsplit=1) if a2a_rest else []
                if len(send_parts) < 2 or not send_parts[1].strip():
                    output_fn("用法：/a2a send <peer> <prompt>")
                    continue
                peer_name, peer_prompt = send_parts[0], send_parts[1].strip()
                try:
                    mgr, should_close = _resolve_repl_a2a_manager(runtime, target_peer=peer_name)
                except Exception as exc:
                    output_fn(f"[A2A 委托失败] {exc}")
                    continue
                try:
                    res = await mgr.send_message(peer_name, peer_prompt, blocking=True)
                except Exception as exc:
                    output_fn(f"[A2A 委托失败] {exc}")
                    continue
                finally:
                    if should_close:
                        with contextlib.suppress(Exception):
                            await mgr.aclose()
                if not res.get("ok"):
                    output_fn(f"[A2A 委托失败] {res.get('error') or res.get('code') or 'unknown error'}")
                    continue
                header_line = (
                    f"[A2A 委托响应] peer={res.get('peer', peer_name)} | "
                    f"task_id={res.get('task_id') or '-'} | state={res.get('state', 'unknown')}"
                )
                answer_body = str(res.get("answer") or "").strip()
                if answer_body:
                    output_fn(
                        f"{header_line}\n\n{_render_terminal_markdown(answer_body, use_color=use_color)}"
                    )
                else:
                    output_fn(header_line)
                continue

            output_fn("用法：/a2a [list | discover <peer> | send <peer> <prompt>]")
            continue
        if user_text == "/steer" or user_text.startswith("/steer "):
            steer_arg = user_text[6:].strip()
            if not steer_arg:
                output_fn("用法：/steer <补充或转向指令>")
                continue
            interrupted = False
            if current_run is None:
                meta_template["permission_mode"] = runtime.permission_mode
                created = runtime.create(steer_arg, mode=mode, extra_metadata=meta_template)
                start_seq = _get_stream_cursor(stream_after_seq, broker, created["id"])
                current_run, interrupted, streamed, next_seq = await _execute_interruptible_turn(
                    runtime,
                    created["id"],
                    runtime.execute(created["id"]),
                    input_fn=input_fn,
                    output_fn=output_fn,
                    stream_output=stream_output,
                    after_seq=start_seq,
                )
                stream_after_seq[current_run["id"]] = max(
                    stream_after_seq.get(current_run["id"], 0), next_seq
                )
            else:
                fresh_cur = runtime.storage.get_run(current_run["id"]) or current_run
                if fresh_cur.get("status") in {"running", "queued"}:
                    rec = runtime.storage.enqueue_steering_message(current_run["id"], steer_arg)
                    output_fn(f"[实时补充指令已入队 #{rec['id']}，将在下一工具步边界自动合并]")
                    continue
                is_waiting = fresh_cur.get("status") == "waiting_approval"
                start_seq = _get_stream_cursor(stream_after_seq, broker, current_run["id"])
                current_run, interrupted, streamed, next_seq = await _execute_interruptible_turn(
                    runtime,
                    current_run["id"],
                    runtime.send_message(
                        current_run["id"], steer_arg, supersede_waiting_approval=is_waiting
                    ),
                    input_fn=input_fn,
                    output_fn=output_fn,
                    stream_output=stream_output,
                    after_seq=start_seq,
                )
                stream_after_seq[current_run["id"]] = max(
                    stream_after_seq.get(current_run["id"], 0), next_seq
                )
            event_cursor = _render_turn_events(runtime, current_run["id"], event_cursor, output_fn)
            if not interrupted and current_run.get("status") == "waiting_approval":
                steer_approval_stream: dict[str, Any] = {}
                current_run = await drain_approvals_interactively(
                    runtime,
                    current_run,
                    input_fn=input_fn,
                    output_fn=output_fn,
                    stream_output=stream_output,
                    stream_after_seq=stream_after_seq,
                    stream_result=steer_approval_stream,
                )
                event_cursor = _render_turn_events(runtime, current_run["id"], event_cursor, output_fn)
                if steer_approval_stream.get("acted"):
                    streamed = bool(steer_approval_stream.get("streamed", False))
            if current_run.get("answer") and not streamed:
                output_fn(f"\n{current_run['answer']}")
            if current_run.get("error"):
                output_fn(f"\n[停止原因: {current_run['status']} — {current_run['error']}]")
            continue
        if user_text == "/compact" or user_text.startswith("/compact "):
            if current_run is None:
                output_fn("当前尚无活跃会话，请先发起任务后再执行 /compact。")
                continue
            raw_arg = user_text[8:].strip()
            keep_n: int | None = None
            focus_text = ""
            if raw_arg:
                parts = raw_arg.split(maxsplit=1)
                if parts[0].isdigit() or (parts[0].startswith("-") and parts[0][1:].isdigit()):
                    try:
                        keep_n = int(parts[0])
                        if keep_n < 1:
                            raise ValueError("keep_recent_groups 必须为正整数")
                    except ValueError as exc:
                        output_fn(f"[压缩失败] {exc}")
                        continue
                    focus_text = parts[1].strip() if len(parts) > 1 else ""
                else:
                    focus_text = raw_arg
            try:
                if keep_n is not None:
                    comp_res = runtime.compact_context(
                        current_run["id"], keep_recent_groups=keep_n, focus=focus_text
                    )
                else:
                    comp_res = runtime.compact_context(current_run["id"], focus=focus_text)
            except Exception as exc:
                output_fn(f"[压缩失败] {exc}")
                continue
            current_run = runtime.storage.get_run(current_run["id"]) or current_run
            for ev in runtime.storage.events(current_run["id"], after=event_cursor):
                event_cursor = max(event_cursor, int(ev["id"]))
            summary_preview = (comp_res.get("compaction_summary") or "").strip()
            preview_lines = (
                "\n".join(summary_preview.splitlines()[:10])
                if summary_preview
                else "（当前会话轮次较少，尚无需要归档的早期消息组）"
            )
            output_fn(
                f"[上下文压缩完成] 释放消息组={comp_res.get('freed_groups', 0)} · "
                f"累计裁剪组={comp_res.get('dropped_groups', 0)} · "
                f"摘要={comp_res.get('summary_chars', 0)} 字符\n{preview_lines}"
            )
            continue
        if user_text == "/claude" or user_text.startswith("/claude "):
            store = runtime._memory_store(current_run)
            arg_str = user_text[7:].strip()
            if not arg_str or arg_str in {"global", "project"}:
                layers_to_show = [arg_str] if arg_str in {"global", "project"} else ["global", "project"]
                sections_out: list[str] = [f"长期工程准则与记忆概览 | 项目标识: {store.project_slug}"]
                for lyr in layers_to_show:
                    c_txt = store.read_core_text(lyr, "CLAUDE.md")
                    m_txt = store.read_core_text(lyr, "MEMORY.md")
                    sections_out.append(f"[{lyr}/CLAUDE.md]\n{c_txt or '（未设置）'}")
                    sections_out.append(f"[{lyr}/MEMORY.md]\n{m_txt or '（未设置）'}")
                if "project" in layers_to_show:
                    ws_txt = store.read_workspace_core_text(
                        deduplicate_against=(
                            store.read_core_text("project", "CLAUDE.md"),
                            store.read_core_text("project", "MEMORY.md"),
                        )
                    )
                    if ws_txt:
                        sections_out.append(f"[workspace 自动发现]\n{ws_txt}")
                output_fn("\n\n".join(sections_out))
                continue

            parts = arg_str.split(maxsplit=1)
            if parts[0] in {"global", "project"}:
                target_layer = parts[0]
                rule_text = parts[1].strip() if len(parts) > 1 else ""
            else:
                target_layer = "project"
                rule_text = arg_str
            existing_claude = store.read_core_text(target_layer, "CLAUDE.md")
            entry_line = (
                rule_text
                if rule_text.lstrip().startswith(("#", "-", "*"))
                else f"- {rule_text}"
            )
            if existing_claude:
                next_claude = existing_claude.rstrip() + "\n" + entry_line
            else:
                hdr = (
                    "# 用户全局工程准则 (Global CLAUDE.md)"
                    if target_layer == "global"
                    else f"# 项目工程准则 ({store.project_slug}/CLAUDE.md)"
                )
                next_claude = f"{hdr}\n\n{entry_line}"
            saved_claude = store.write_core_text(target_layer, "CLAUDE.md", next_claude)
            output_fn(
                f"[CLAUDE.md 已更新] 层级={target_layer} · 行数={saved_claude['lines']} · 字符={saved_claude['chars']}"
            )
            continue
        if user_text == "/memory":
            ov = runtime._memory_store(current_run).overview()
            gs = ov["graph"]["stats"]
            output_fn(
                f"记忆库概览 | 项目标识: {ov['project_slug']} | "
                f"MEMORY.md 行数: {ov['index_budget']['used_lines']}/{ov['index_budget']['max_lines']} | "
                f"Wiki页: {gs['pages']} (已核验={gs['confirmed']}, 假设={gs['hypothesis']}, 边={gs['edges']}, 断链={gs['broken_links']})"
            )
            idx_preview = runtime._memory_store(current_run).render_pinned_index()
            if idx_preview:
                output_fn(idx_preview)
            continue
        if user_text == "/dream" or user_text.startswith("/dream "):
            raw_dream = user_text[6:].strip()
            store = runtime._memory_store(current_run)
            if raw_dream.startswith("--rollback"):
                snap_arg = raw_dream[len("--rollback") :].strip() or None
                try:
                    rb = store.rollback_last_dream(snapshot_id=snap_arg)
                    output_fn(
                        f"[Dreaming 已回滚] snapshot={rb.get('snapshot_id')} · "
                        f"恢复文件={len(rb.get('restored_files') or [])} · "
                        f"移除新增={len(rb.get('removed_files') or [])}"
                    )
                except Exception as exc:
                    output_fn(f"[Dreaming 回滚失败] {exc}")
                continue
            if raw_dream.startswith("--dry-run"):
                goal_arg = raw_dream[len("--dry-run") :].strip() or None
                llm_prov = runtime.provider if getattr(runtime.provider, "supports_dreaming", False) else None
                engine = DreamEngine(store, llm_provider=llm_prov)
                rep = await engine.dream(
                    run_ids=[current_run["id"]] if current_run else None,
                    focus_goal=goal_arg,
                    trigger="repl_dry_run",
                    dry_run=True,
                )
                diffs = rep.get("planned_diffs") or []
                lines_out = [
                    f"[Dreaming 预演完成（零落盘）] 重放会话={rep['sessions_replayed']} · "
                    f"计划纠错={rep['corrections_count']} · 计划写入={rep['pages_written_count']} · "
                    f"计划修复断链={rep['links_repaired_count']}"
                ]
                for item in diffs[:6]:
                    lines_out.append(f"  - [{item.get('action')}] {item.get('layer')}:{item.get('path')}")
                    if item.get("diff"):
                        for dl in str(item["diff"]).splitlines()[:12]:
                            lines_out.append(f"    {_colorize_diff_line(dl, use_color=use_color)}")
                output_fn("\n".join(lines_out))
                continue

            goal_arg = raw_dream or None
            rep = await runtime.dream(
                run_id=current_run["id"] if current_run else None,
                focus_goal=goal_arg,
                trigger="repl",
            )
            output_fn(
                f"[Dreaming 整理完成] 重放会话={rep['sessions_replayed']} · "
                f"纠错={rep['corrections_count']} · Wiki更新={rep['pages_written_count']} · "
                f"断链修复={rep['links_repaired_count']}"
            )
            continue
        if user_text == "/rewind" or user_text.startswith("/rewind "):
            if current_run is None:
                output_fn("当前尚无活跃会话，无法列出或回滚检查点。")
                continue
            args_parts = user_text[7:].strip().split()
            cps = runtime.list_checkpoints(current_run["id"])
            if not args_parts:
                if not cps:
                    output_fn("当前会话暂无检查点（仅在 host/computer 模式下自动记录每轮检查点）。")
                else:
                    lines_cp = [f"当前会话检查点（共 {len(cps)} 个；用法：/rewind <id|turn> [both|files|messages]）："]
                    for idx_cp, cp in enumerate(cps, 1):
                        manifest = cp.get("file_manifest") or {}
                        fnames = ", ".join(list(manifest.keys())[:3])
                        f_desc = f"{len(manifest)} 文件" + (f" ({fnames})" if fnames else "")
                        lines_cp.append(
                            f"  [{idx_cp}] turn={cp.get('turn_index')} id={str(cp.get('checkpoint_id', ''))[:12]} "
                            f"msg_idx={cp.get('message_idx')} files={len(manifest)} ({f_desc}) "
                            f"label={str(cp.get('label', ''))[:40]}"
                        )
                    output_fn("\n".join(lines_cp))
                    if use_color:
                        try:
                            pick_cp = prompt_reader("输入编号回退（回车取消）: ").strip()
                        except (EOFError, KeyboardInterrupt, StopIteration):
                            continue
                        if pick_cp.isdigit() and 1 <= int(pick_cp) <= len(cps):
                            chosen = cps[int(pick_cp) - 1]
                            try:
                                sc_in = prompt_reader("回退范围 [both/files/messages，默认 both]: ").strip() or "both"
                                confirm_rw = prompt_reader(
                                    f"确认回退到 turn={chosen.get('turn_index')} ({sc_in})？输入 y 确认: "
                                ).strip().lower()
                            except (EOFError, KeyboardInterrupt, StopIteration):
                                continue
                            if confirm_rw in {"y", "yes"} and sc_in in {"both", "files", "messages"}:
                                rw = runtime.rewind(current_run["id"], str(chosen["checkpoint_id"]), scope=sc_in)
                                current_run = runtime.storage.get_run(current_run["id"]) or current_run
                                output_fn(
                                    f"[Rewind 完成] checkpoint={str(chosen['checkpoint_id'])[:12]} · scope={sc_in} · "
                                    f"恢复文件={len(rw.get('restored_files') or [])} · 删除新增={len(rw.get('deleted_files') or [])}"
                                )
                continue
            target_sel = args_parts[0]
            scope = args_parts[1] if len(args_parts) > 1 else "both"
            if scope not in {"both", "files", "messages"}:
                output_fn("无效回滚范围，可选：both / files / messages")
                continue
            matched_id: str | None = None
            for cp in cps:
                cid = str(cp.get("checkpoint_id") or "")
                if cid == target_sel or cid.startswith(target_sel) or str(cp.get("turn_index")) == target_sel:
                    matched_id = cid
                    break
            if not matched_id:
                output_fn(f"未找到匹配的检查点：{target_sel}")
                continue
            try:
                rw = runtime.rewind(current_run["id"], matched_id, scope=scope)
                current_run = runtime.storage.get_run(current_run["id"]) or current_run
                output_fn(
                    f"[Rewind 完成] checkpoint={matched_id[:12]} · scope={scope} · "
                    f"恢复文件={len(rw.get('restored_files') or [])} · "
                    f"删除新增={len(rw.get('deleted_files') or [])}"
                )
            except Exception as exc:
                output_fn(f"[Rewind 失败] {exc}")
            continue
        if user_text == "/fork" or user_text.startswith("/fork "):
            if current_run is None:
                output_fn("当前尚无活跃会话，无法分叉。")
                continue
            fork_title = user_text[5:].strip() or None
            try:
                forked = runtime.storage.fork_run(current_run["id"], title=fork_title)
                current_run = forked
                event_cursor = 0
                output_fn(
                    f"[会话分叉成功] 已切换到新分支 Run ID: {forked['id']} "
                    f"(继承消息数={len(forked.get('messages') or [])})"
                )
            except Exception as exc:
                output_fn(f"[分叉失败] {exc}")
            continue
        if user_text == "/cost":
            if current_run is None:
                output_fn("当前尚无活跃会话。")
            else:
                fresh = runtime.storage.get_run(current_run["id"]) or current_run
                f_meta = fresh.get("metadata") or {}
                kv = f_meta.get("kv_cache") or {}
                hit_pct = round(float(kv.get("cumulative_hit_rate", 0.0)) * 100, 1)
                cost_usd = float(f_meta.get("cost_usd", 0.0) or 0.0)
                max_cost = f_meta.get("max_cost_usd")
                cb = f_meta.get("cost_breakdown") or {}
                cap_str = f" / 上限 ${float(max_cost):.4f}" if max_cost else ""
                output_fn(
                    f"费用与用量统计 | 模型: {f_meta.get('model') or runtime.provider_name} | "
                    f"Input={fresh['input_tokens']} Output={fresh['output_tokens']} "
                    f"Billed={int(f_meta.get('billed_tokens', 0) or 0)} | KV 命中率: {hit_pct}%\n"
                    f"累计费用: ${cost_usd:.6f}{cap_str} "
                    f"(in=${float(cb.get('input_cost_usd', 0.0) or 0.0):.6f}, "
                    f"cache_w=${float(cb.get('cache_write_cost_usd', 0.0) or 0.0):.6f}, "
                    f"cache_r=${float(cb.get('cache_read_cost_usd', 0.0) or 0.0):.6f}, "
                    f"out=${float(cb.get('output_cost_usd', 0.0) or 0.0):.6f})"
                )
            continue
        if user_text == "/model" or user_text.startswith("/model "):
            model_arg = user_text[6:].strip()
            known_models = sorted(DEFAULT_MODEL_PRICING_USD_PER_MTOK.keys())
            if not model_arg:
                cur_m = _resolve_active_model_label(
                    runtime,
                    (runtime.storage.get_run(current_run["id"]) or current_run) if current_run is not None else None,
                )
                if use_color:
                    lines_m = [f"当前模型：{cur_m}（输入编号切换，直接回车取消）："]
                    for idx_m, mid in enumerate(known_models[:12], 1):
                        rates = DEFAULT_MODEL_PRICING_USD_PER_MTOK[mid]
                        lines_m.append(
                            f"  [{idx_m}] {mid} (in=${rates.get('input', 0):.2f}/out=${rates.get('output', 0):.2f} /MTok)"
                        )
                    output_fn("\n".join(lines_m))
                    try:
                        pick = prompt_reader("选择模型编号或输入自定义模型名（回车取消）: ").strip()
                    except (EOFError, KeyboardInterrupt, StopIteration):
                        continue
                    if not pick:
                        continue
                    if pick.isdigit() and 1 <= int(pick) <= len(known_models[:12]):
                        model_arg = known_models[:12][int(pick) - 1]
                    else:
                        model_arg = pick
                else:
                    output_fn(f"当前模型覆盖：{cur_m}（用法：/model <model_id> 或 /model default）")
                    continue
            force_model = False
            if model_arg.endswith(" --force"):
                force_model = True
                model_arg = model_arg[:-8].strip()
            if model_arg.lower() in {"default", "reset", "none"}:
                meta_template.pop("model", None)
                if current_run is not None:
                    fresh = runtime.storage.get_run(current_run["id"]) or current_run
                    fresh.setdefault("metadata", {}).pop("model", None)
                    runtime.storage.save_run(fresh)
                    current_run = fresh
                output_fn("已重置为默认服务端模型。")
            else:
                if use_color and not force_model and model_arg not in DEFAULT_MODEL_PRICING_USD_PER_MTOK:
                    close = difflib.get_close_matches(model_arg, known_models, n=3, cutoff=0.6)
                    if close:
                        output_fn(
                            f"[提示] {model_arg!r} 不在内置模型价格表中；相近模型：{', '.join(close)}（若确认使用请加 --force）"
                        )
                        continue
                meta_template["model"] = model_arg
                if current_run is not None:
                    fresh = runtime.storage.get_run(current_run["id"]) or current_run
                    fresh.setdefault("metadata", {})["model"] = model_arg
                    runtime.storage.save_run(fresh)
                    current_run = fresh
                output_fn(f"当前会话模型已设置为：{model_arg}")
            continue
        if user_text == "/gateway" or user_text.startswith("/gateway "):
            gw_arg = user_text[8:].strip()
            gw_ctx = _runtime_gateway_context(runtime, explicit_config_path=gateway_config_path)
            if not gw_arg:
                fresh_for_gw = (
                    (runtime.storage.get_run(current_run["id"]) or current_run)
                    if current_run is not None
                    else None
                )
                active_gw_now = _describe_active_gateway(runtime, fresh_for_gw)
                gw_lines = [
                    f"当前网关 | Profile: {active_gw_now['profile']} | "
                    f"协议: {active_gw_now['protocol']} | "
                    f"模型: {active_gw_now['model']} | "
                    f"端点: {active_gw_now['base_url']}"
                ]
                profile_names: list[str] = []
                try:
                    raw_cfg = load_gateways_config(
                        explicit_path=gw_ctx["explicit_config_path"],
                        state_dir=gw_ctx["state_dir"],
                        workspace=gw_ctx["workspace"],
                        trust_workspace=gw_ctx["trust_workspace"],
                        tenant_id=gw_ctx["tenant_id"],
                    )
                    redacted_cfg = redact_gateways_document(raw_cfg)
                except ValueError as exc:
                    gw_lines.append(f"  [配置校验失败] {exc}")
                else:
                    cfg_path_label = redacted_cfg.get("config_path")
                    cfg_profiles: dict[str, dict[str, Any]] = redacted_cfg.get("profiles") or {}
                    if cfg_path_label:
                        gw_lines.append(f"配置文件: {cfg_path_label}")
                    if not cfg_profiles:
                        gw_lines.append(
                            "  暂无已配置的 gateways.json Profile（可运行 `agent-lab gateway init` 生成多协议模板）"
                        )
                    else:
                        gw_lines.append("可用 Profile（用法：/gateway <profile> 热切换，保留对话历史）：")
                        profile_names = list(cfg_profiles.keys())
                        for idx_gw, pname in enumerate(profile_names, 1):
                            pdata = cfg_profiles[pname]
                            is_cur = pname == active_gw_now["profile"]
                            marker = "*" if is_cur else " "
                            p_proto = str(pdata.get("protocol") or "-")
                            p_model = str(pdata.get("model") or "-")
                            p_url = str(pdata.get("base_url") or "(offline)")
                            p_cred = _format_repl_gateway_credential_status(pdata)
                            gw_lines.append(
                                f"  [{idx_gw}] {marker} {pname} [{p_proto}] "
                                f"model={p_model} url={p_url} ({p_cred})"
                            )
                output_fn("\n".join(gw_lines))
                if use_color and profile_names:
                    try:
                        pick_gw = prompt_reader("输入编号或 Profile 名切换网关（回车取消）: ").strip()
                    except (EOFError, KeyboardInterrupt, StopIteration):
                        continue
                    if not pick_gw:
                        continue
                    if pick_gw.isdigit() and 1 <= int(pick_gw) <= len(profile_names):
                        gw_arg = profile_names[int(pick_gw) - 1]
                    else:
                        gw_arg = pick_gw
                else:
                    continue

            try:
                new_provider, gw_info = resolve_provider_or_gateway(
                    gw_arg,
                    state_dir=gw_ctx["state_dir"],
                    workspace=gw_ctx["workspace"],
                    explicit_config_path=gw_ctx["explicit_config_path"],
                    trust_workspace=gw_ctx["trust_workspace"],
                    tenant_id=gw_ctx["tenant_id"],
                )
            except Exception as exc:
                output_fn(f"[网关切换失败] {exc}")
                continue

            old_provider = runtime.provider
            old_close = getattr(old_provider, "aclose", None)
            if callable(old_close) and old_provider is not new_provider:
                with contextlib.suppress(Exception):
                    maybe_coro = old_close()
                    if inspect.isawaitable(maybe_coro):
                        await maybe_coro

            prof_name = gw_info.get("profile")
            next_provider_name = (
                f"gateway:{prof_name}"
                if prof_name
                else str(gw_info.get("protocol") or gw_arg)
            )
            runtime.provider = new_provider
            runtime.provider_name = next_provider_name
            meta_template.pop("model", None)
            meta_template["gateway"] = gw_info
            if current_run is not None:
                fresh_gw_run = runtime.storage.get_run(current_run["id"]) or current_run
                fresh_gw_run["provider"] = next_provider_name
                fresh_meta = fresh_gw_run.setdefault("metadata", {})
                fresh_meta["gateway"] = gw_info
                fresh_meta.pop("model", None)
                runtime.storage.save_run(fresh_gw_run)
                current_run = fresh_gw_run

            switched_desc = _describe_active_gateway(runtime, current_run)
            output_fn(
                f"[网关已热切换（保留对话历史）] Profile: {switched_desc['profile']} | "
                f"协议: {gw_info.get('protocol') or switched_desc['protocol']} | "
                f"模型: {gw_info.get('model') or switched_desc['model']} | "
                f"端点: {gw_info.get('base_url') or switched_desc['base_url']}"
            )
            continue
        if user_text == "/hooks":
            hm = runtime._get_hook_manager() if hasattr(runtime, "_get_hook_manager") else runtime._hook_manager
            re_eng = runtime._get_rule_engine() if hasattr(runtime, "_get_rule_engine") else runtime._rule_engine
            grants = (
                ((runtime.storage.get_run(current_run["id"]) or current_run).get("metadata") or {}).get(
                    "session_grants", []
                )
                if current_run is not None
                else []
            )
            hook_summary = hm.summary() if hm is not None and hasattr(hm, "summary") else {}
            rule_summary = re_eng.summary() if re_eng is not None and hasattr(re_eng, "summary") else {}
            output_fn(
                f"Hooks & 权限规则概览：\n"
                f"  - Hooks 配置: {hook_summary or '（无）'}\n"
                f"  - 声明式规则: {rule_summary or '（无）'}\n"
                f"  - 本会话已放行模式 (session_grants): {grants or '（无）'}"
            )
            continue
        if user_text == "/bg":
            bg_lines = ["后台进程与异步子 Agent 状态："]
            bg_mgrs = getattr(runtime, "_bg_process_managers", {}) or {}
            proc_count = 0
            for rid, mgr in bg_mgrs.items():
                for proc in mgr.list_processes():
                    proc_count += 1
                    bg_lines.append(
                        f"  - [proc {proc.get('proc_id')}] run={rid[:8]} alive={proc.get('alive')} "
                        f"pty={proc.get('use_pty')} cmd={str(proc.get('command', ''))[:60]}"
                    )
            if proc_count == 0:
                bg_lines.append("  - 后台 Shell/PTY 进程：无")
            sub_st = runtime.subagent_status(run_id=current_run["id"] if current_run else None)
            jobs = sub_st.get("jobs") or []
            if jobs:
                for jb in jobs:
                    bg_lines.append(
                        f"  - [subagent {jb.get('job_id')}] status={jb.get('status')} roles={jb.get('roles')}"
                    )
            else:
                bg_lines.append("  - 后台异步子 Agent：无")
            output_fn("\n".join(bg_lines))
            continue
        if user_text == "/plan" or user_text.startswith("/plan "):
            plan_arg = user_text[5:].strip().lower()
            if not plan_arg:
                if runtime.permission_mode != "plan":
                    pre_plan_mode = runtime.permission_mode
                    next_plan_mode = "plan"
                else:
                    next_plan_mode = pre_plan_mode if pre_plan_mode != "plan" else "auto-edit"
            elif plan_arg in {"on", "enter", "plan", "plan-mode", "plan_mode"}:
                if runtime.permission_mode != "plan":
                    pre_plan_mode = runtime.permission_mode
                next_plan_mode = "plan"
            elif plan_arg in {"off", "exit"}:
                next_plan_mode = pre_plan_mode if pre_plan_mode != "plan" else "auto-edit"
            else:
                try:
                    next_plan_mode = normalize_permission_mode(plan_arg)
                except ValueError as exc:
                    output_fn(
                        f"{exc}（用法：/plan [on|off|ask|auto-edit|full-auto]）"
                    )
                    continue
                if next_plan_mode != "plan":
                    pre_plan_mode = next_plan_mode
            if current_run is not None:
                current_run = runtime.set_permission_mode(current_run["id"], next_plan_mode)
            else:
                runtime.permission_mode = next_plan_mode
            meta_template["permission_mode"] = runtime.permission_mode
            if runtime.permission_mode == "plan":
                output_fn(
                    "已进入只读规划模式 (plan)：仅允许只读探索、提问澄清与编写计划，源码修改与变更命令将被硬阻断（再次输入 /plan 可退出）。"
                )
            else:
                output_fn(f"已退出规划模式，权限模式已切换为：{runtime.permission_mode}")
            continue
        if user_text == "/mode" or user_text.startswith("/mode "):
            parts = user_text.split(maxsplit=1)
            if len(parts) < 2:
                if use_color:
                    output_fn(
                        f"当前权限模式：{runtime.permission_mode}\n"
                        "  [1] ask        — 每次编辑文件、执行命令与 GUI/MCP 均需确认\n"
                        "  [2] auto-edit  — 自动编辑文件与只读测试，高风险命令仍需确认\n"
                        "  [3] full-auto  — 全自动执行（仍受内核沙箱与毁机熔断保护）\n"
                        "  [4] plan       — 只读规划模式（先出方案再执行，硬阻断源码写入与变更命令）"
                    )
                    try:
                        pick_m = prompt_reader("输入编号或模式名切换（回车取消）: ").strip()
                    except (EOFError, KeyboardInterrupt, StopIteration):
                        continue
                    mode_map = {"1": "ask", "2": "auto-edit", "3": "full-auto", "4": "plan"}
                    if not pick_m:
                        continue
                    parts = ["/mode", mode_map.get(pick_m, pick_m)]
                else:
                    output_fn(
                        f"当前权限模式：{runtime.permission_mode}（可选：ask / auto-edit / full-auto / plan）"
                    )
                    continue
            try:
                next_mode = normalize_permission_mode(parts[1])
            except ValueError as exc:
                output_fn(str(exc))
                continue
            if runtime.permission_mode != "plan" and next_mode == "plan":
                pre_plan_mode = runtime.permission_mode
            elif next_mode != "plan":
                pre_plan_mode = next_mode
            if current_run is not None:
                current_run = runtime.set_permission_mode(current_run["id"], next_mode)
            else:
                runtime.permission_mode = next_mode
            meta_template["permission_mode"] = runtime.permission_mode
            output_fn(f"权限模式已切换为：{runtime.permission_mode}")
            continue
        if user_text == "/clear":
            current_run = None
            event_cursor = 0
            output_fn("已清空当前会话上下文，下一条输入将创建新会话。")
            continue
        if user_text == "/status":
            if current_run is None:
                output_fn(f"尚未发起任务 | 目录: {runtime.workspace} | 权限: {runtime.permission_mode}")
            else:
                fresh = runtime.storage.get_run(current_run["id"]) or current_run
                f_meta = fresh.get("metadata") or {}
                kv = f_meta.get("kv_cache") or {}
                ctx_st = f_meta.get("context_stats") or {}
                hit_pct = round(float(kv.get("cumulative_hit_rate", 0.0)) * 100, 1)
                util_pct = round(float(ctx_st.get("utilization_ratio", 0.0)) * 100, 1)
                dropped_g = int(ctx_st.get("dropped_groups", 0))
                cost_usd = float(f_meta.get("cost_usd", 0.0) or 0.0)
                output_fn(
                    f"Run ID: {fresh['id']} | 状态: {fresh['status']} | 轮次: {fresh['steps']} | "
                    f"Tokens: {fresh['input_tokens'] + fresh['output_tokens']} | "
                    f"费用: ${cost_usd:.5f} | "
                    f"上下文水位: {util_pct}% (已归档={dropped_g}组) | KV 命中率: {hit_pct}%"
                )
            continue
        if user_text == "/diff":
            if is_git_worktree(runtime.workspace):
                st = worktree_status(runtime.workspace)
                diff_txt = st.get("diff") or ""
                if diff_txt:
                    output_fn(
                        "\n".join(_colorize_diff_line(ln, use_color=use_color) for ln in diff_txt.splitlines())
                    )
                else:
                    output_fn("（Git 工作区当前干净，无未提交差异）")
            else:
                output_fn("当前目录不是 Git 仓库。")
            continue

        # 拦截未知 `/xxx` 斜杠命令（且非已知技能名），防止拼写错误静默发送给模型白烧 Token
        if user_text.startswith("/") and not user_text.startswith("//"):
            first_cmd = user_text.split(maxsplit=1)[0]
            cmd_name = first_cmd[1:]
            known_skill_names = {s["name"] for s in runtime.skills.catalog()}
            if first_cmd not in _SLASH_COMMANDS and cmd_name not in known_skill_names:
                suggestions = difflib.get_close_matches(
                    first_cmd, [*_SLASH_COMMANDS, *(f"/{n}" for n in known_skill_names)], n=3, cutoff=0.5
                )
                hint = f"；你是不是想输入：{', '.join(suggestions)}" if suggestions else ""
                output_fn(f"[未知命令] {first_cmd}{hint}（输入 /help 查看全部命令）")
                continue

        # 展开 @relative/path 安全工作区文件引用（不修改不含合法相对路径引用的普通文本）
        expanded_text = _expand_at_file_mentions(user_text, runtime.workspace)

        if current_run is None:
            meta_template["permission_mode"] = runtime.permission_mode
            created = runtime.create(expanded_text, mode=mode, extra_metadata=meta_template)
            start_seq = _get_stream_cursor(stream_after_seq, broker, created["id"])
            current_run, interrupted, streamed, next_seq = await _execute_interruptible_turn(
                runtime,
                created["id"],
                runtime.execute(created["id"]),
                input_fn=input_fn,
                output_fn=output_fn,
                stream_output=stream_output,
                after_seq=start_seq,
                rendered_event_ids=streamed_event_ids,
            )
            stream_after_seq[current_run["id"]] = max(
                stream_after_seq.get(current_run["id"], 0), next_seq
            )
        else:
            if current_run.get("status") == "waiting_approval":
                current_run = await drain_approvals_interactively(
                    runtime,
                    current_run,
                    input_fn=input_fn,
                    output_fn=output_fn,
                    stream_output=stream_output,
                    stream_after_seq=stream_after_seq,
                )
                event_cursor = _render_turn_events(
                    runtime, current_run["id"], event_cursor, output_fn, skip_ids=streamed_event_ids
                )
                if current_run.get("status") == "waiting_approval":
                    continue
            start_seq = _get_stream_cursor(stream_after_seq, broker, current_run["id"])
            current_run, interrupted, streamed, next_seq = await _execute_interruptible_turn(
                runtime,
                current_run["id"],
                runtime.send_message(current_run["id"], expanded_text),
                input_fn=input_fn,
                output_fn=output_fn,
                stream_output=stream_output,
                after_seq=start_seq,
                rendered_event_ids=streamed_event_ids,
            )
            stream_after_seq[current_run["id"]] = max(
                stream_after_seq.get(current_run["id"], 0), next_seq
            )

        event_cursor = _render_turn_events(
            runtime, current_run["id"], event_cursor, output_fn, skip_ids=streamed_event_ids
        )
        if not interrupted and current_run.get("status") == "waiting_approval":
            turn_approval_stream: dict[str, Any] = {}
            current_run = await drain_approvals_interactively(
                runtime,
                current_run,
                input_fn=input_fn,
                output_fn=output_fn,
                stream_output=stream_output,
                stream_after_seq=stream_after_seq,
                stream_result=turn_approval_stream,
            )
            event_cursor = _render_turn_events(
                runtime, current_run["id"], event_cursor, output_fn, skip_ids=streamed_event_ids
            )
            if turn_approval_stream.get("acted"):
                streamed = bool(turn_approval_stream.get("streamed", False))
                interrupted = bool(turn_approval_stream.get("interrupted", False))
            meta_template["permission_mode"] = runtime.permission_mode
            if runtime.permission_mode != "plan":
                pre_plan_mode = runtime.permission_mode

        if current_run.get("answer") and not streamed:
            output_fn(f"\n{_render_terminal_markdown(current_run['answer'], use_color=use_color)}")
        if current_run.get("error"):
            output_fn(f"\n[停止原因: {current_run['status']} — {current_run['error']}]")

    return current_run
