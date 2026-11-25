"""命令行统一控制面入口：覆盖本地多轮工作台、沙箱/宿主单次运行、持久队列 Worker、离线睡眠记忆与基准评测。

================================================================================
模块导读（Module Guide）
================================================================================
1. 模块角色（Role）：
   本模块将同一套底层 `AgentRuntime` + `Storage` 暴露为面向开发者与自动化流水线的 CLI 子命令矩阵：
   - 引导与环境体检：`init`（快速创建 `0600` 权限 `.env`）、`gateway`（多协议模型网关 `list`/`use`/`init`，支持 Anthropic / OpenAI Chat / Responses / Gemini）、`a2a`（Agent2Agent 双向协议 `init`/`list`/`card`/`send`）、`doctor`（全栈环境/网关/A2A/密钥/Docker/OS 沙箱/LSP/可选依赖/macOS TCC 诊断）；
   - 交互研发与单次执行：`code` / `chat`（多轮 REPL，支持 `-c/--continue` 与短前缀 `--resume`）、`run`（真实/脚本单次任务）、`demo`（内置回归演示）；
   - 服务与分布式调度：`serve`（FastAPI 网页与控制面）、`worker`（持久队列租约消费守护进程）；
   - 任务治理与状态审计：`approve`、`resume`、`cancel`、`gc`、`list`（对齐表格 / `--json`）、`trace`、`calls`、`result`；
   - 技能包管理与离线基准：`skill`（`list`/`install`/`remove`）、`dream`（四阶段离线记忆整理，支持 `--dry-run` 与 `--rollback`）、`index`（持久化 Merkle-Tree 代码库增量索引与混合语义检索）、`eval`（`--suite core|subsystems|all`）、`model-eval`、`kv-benchmark`。

2. 核心工程与安全不变量（Core Invariants）：
   - [零覆盖 `.env` 自动加载] 启动时自动解析当前目录或仓库根目录的 `.env` 并通过 `os.environ.setdefault`
     仅填充未设置变量，绝不覆盖显式导出的 Shell 环境变量，且从不打印任何密钥明文。
   - [非回环监听强制鉴权门禁] `agent-lab serve` 绑定非回环地址（如 `0.0.0.0`）时，除非显式声明仅宿主机
     回环端口映射（`AGENT_LAB_ALLOW_INSECURE_LOCAL=1`），否则启动前强制要求已配置 `AGENT_LAB_TOKEN`
     或 `AGENT_LAB_TOKENS_JSON`，并自动注入 `AGENT_LAB_REQUIRE_AUTH=1` 防裸奔暴露本地执行面。
   - [Worker 信号优雅退场] `_serve_worker_gracefully` 同时接管 `SIGINT` 与 `SIGTERM`（兼容非主线程回退
     注册），置位 `stop_event` 并取消轮询任务，使 `LeaseWorker` 将已领取未完成的任务归还为 `queued`
     （`worker_shutdown_requeued`）而非误标 `failed`，保障滚动重启零丢单。
   - [直连目录与临时副本边界隔离] 显式传入 `--cwd`（或运行 `code`/`chat`）时标记 `direct_workspace=True`，
     经 `validate_direct_workspace_path` 阻断系统禁区与根软链，且后续 `agent-lab gc` 绝对豁免删除该目录；
     未传 `--cwd` 时一律在 `.agent-lab/workspaces/<uuid>` 下创建隔离副本或独立 Git worktree。
   - [断点恢复零预算重置] `approve` 与 `resume` 严格从持久化 `run["metadata"]` 还原工作区路径、执行档位、
     权限策略及原始 `max_steps` / `max_tokens` 配额，防止通过重启进程绕过累计成本与轮次熔断上限。
================================================================================
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from datetime import datetime, timezone
import getpass
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
from typing import Any
import unicodedata
import urllib.parse
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .a2a import (
    A2A_REDACTED_MARKER,
    A2AClientManager,
    build_agent_card,
    default_a2a_template,
    load_a2a_config,
    redact_a2a_document,
    save_a2a_config,
)
from .api import ROOT
from .code_index import CodebaseMerkleIndex
from .eval_flywheel import grade_trajectory, harvest_failure_traces
from .gateways import (
    REDACTED_SECRET_MARKER,
    default_gateways_template,
    discover_gateways_config_path,
    load_gateways_config,
    redact_gateways_document,
    resolve_provider_or_gateway,
    save_gateways_config,
    set_active_gateway_profile,
)
from .git_repo import create_task_worktree, ensure_git_origin_atomic
from .horizon import (
    HorizonCampaignManager,
    MechanicalInvariantChecker,
    run_horizon_session,
)
from .kv_cache import simulate_kv_cache_ablation
from .lease import LeaseWorker, build_worker_runtime_factory
from .permissions import is_workspace_trusted, validate_direct_workspace_path
from .repl import drain_approvals_interactively, run_interactive_repl
from .runtime import AgentRuntime
from .sandbox import discover_test_oracles, reap_orphan_containers
from .storage import Storage
from .streaming import RunStreamBroker
from .triggers import CronSchedule, TriggerScheduler

_DOTENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_DOTENV_MAX_BYTES = 256 * 1024
_CLI_PROVIDER_CHOICES: tuple[str, ...] = (
    "demo",
    "anthropic",
    "openai",
    "openai_compat",
    "openai_responses",
    "gemini",
)
_LSP_SERVER_BINARIES: tuple[str, ...] = (
    "basedpyright-langserver",
    "pyright-langserver",
    "typescript-language-server",
    "gopls",
    "rust-analyzer",
)
_OPTIONAL_PYTHON_EXTRAS: tuple[tuple[str, str], ...] = (
    ("mcp", "mcp"),
    ("psycopg", "postgres"),
    ("tree_sitter_language_pack", "treesitter"),
    ("prompt_toolkit", "tui"),
    ("rich", "tui"),
)
_MACOS_TCC_ACCESSIBILITY_PATH = (
    "System Settings -> Privacy & Security -> Accessibility（系统设置 -> 隐私与安全性 -> 辅助功能）"
)
_MACOS_TCC_SCREEN_RECORDING_PATH = (
    "System Settings -> Privacy & Security -> Screen & System Audio Recording"
    "（系统设置 -> 隐私与安全性 -> 录屏与系统录音）"
)
_DEFAULT_ENV_TEMPLATE = (
    "# demo 无需密钥；真实调用需显式 AGENT_PROVIDER=anthropic 或 AGENT_PROVIDER=openai。\n"
    "AGENT_PROVIDER=demo\n"
    "# 调度模式：inline（单进程协程）或 queue（由独立 agent-lab worker 进程基于租约与 epoch 消费）。\n"
    "AGENT_LAB_DISPATCH=inline\n"
    "ANTHROPIC_API_KEY=\n"
    "# 填写你账户当前可用的模型 ID，不假设固定模型始终可用。\n"
    "ANTHROPIC_MODEL=\n"
    "OPENAI_API_KEY=\n"
    "OPENAI_MODEL=\n"
    "# 默认仅连接官方 API；若确需自定义网关，需显式设置 AGENT_ALLOW_CUSTOM_BASE_URL=1。\n"
    "AGENT_ALLOW_CUSTOM_BASE_URL=0\n"
    "# 默认仅监听本机。若配置 TOKEN，所有 /api 请求都需 Bearer token。\n"
    "AGENT_LAB_TOKEN=\n"
)


def _parse_dotenv_line(raw_line: str) -> tuple[str, str] | None:
    """解析单行 `.env` 配置（支持注释、`export` 前缀与单/双引号值），无效行返回 `None`。"""
    line = raw_line.lstrip(chr(0xFEFF)).strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith(("export ", "export\t")):
        line = line[6:].strip()
    if "=" not in line:
        return None
    key, raw_val = line.split("=", 1)
    key = key.strip()
    if not _DOTENV_KEY_RE.match(key):
        return None
    val = raw_val.strip()
    if not val:
        return key, ""
    if val[0] == '"':
        chars: list[str] = []
        idx = 1
        closed = False
        while idx < len(val):
            ch = val[idx]
            if ch == "\\" and idx + 1 < len(val):
                nxt = val[idx + 1]
                escape_map = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
                chars.append(escape_map.get(nxt, nxt))
                idx += 2
                continue
            if ch == '"':
                closed = True
                break
            chars.append(ch)
            idx += 1
        return key, ("".join(chars) if closed else val)
    if val[0] == "'":
        end_idx = val.find("'", 1)
        return key, (val[1:end_idx] if end_idx != -1 else val)
    unquoted = re.split(r"\s+#", val, maxsplit=1)[0].strip()
    return key, unquoted


def _autoload_dotenv(cwd: Path | None = None) -> list[Path]:
    """零依赖自动加载 `cwd` 或仓库根目录的 `.env`，仅对未设置变量调用 `os.environ.setdefault`。"""
    if os.environ.get("AGENT_LAB_NO_DOTENV", "").strip() == "1":
        return []
    candidates: list[Path] = []
    search_roots: list[Path] = []
    if cwd is not None:
        search_roots.append(Path(cwd))
    else:
        search_roots.append(Path.cwd())
    search_roots.append(ROOT)

    for root in search_roots:
        try:
            env_file = root.resolve() / ".env"
        except OSError:
            continue
        if env_file not in candidates:
            candidates.append(env_file)

    loaded: list[Path] = []
    for env_file in candidates:
        try:
            if not env_file.is_file() or env_file.stat().st_size > _DOTENV_MAX_BYTES:
                continue
            content = env_file.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for raw_line in content.splitlines():
            parsed = _parse_dotenv_line(raw_line)
            if parsed is None:
                continue
            key, val = parsed
            if not val:
                continue
            os.environ.setdefault(key, val)
        loaded.append(env_file)
    return loaded


def _detect_default_cli_provider() -> str:
    """按环境变量优先级自动推断默认模型适配方；无密钥时安全回退离线 `demo`。"""
    env_p = os.environ.get("AGENT_PROVIDER", "").strip().lower()
    if env_p in _CLI_PROVIDER_CHOICES:
        return env_p
    if os.environ.get("ANTHROPIC_API_KEY", "").strip() or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip():
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return "openai"
    if os.environ.get("GEMINI_API_KEY", "").strip() or os.environ.get("GOOGLE_API_KEY", "").strip():
        return "gemini"
    return "demo"


def _provider_env_readiness(provider: str) -> tuple[bool, list[str], str | None]:
    """检查指定 provider 的环境变量就绪度，返回 `(ready, missing_vars, model_name)`，绝不暴露密钥值。"""
    if provider == "demo":
        return True, [], "demo-deterministic"
    if provider == "anthropic":
        missing: list[str] = []
        has_key = bool(
            os.environ.get("ANTHROPIC_API_KEY", "").strip()
            or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
        )
        model = os.environ.get("ANTHROPIC_MODEL", "").strip() or None
        if not has_key:
            missing.append("ANTHROPIC_API_KEY")
        if not model:
            missing.append("ANTHROPIC_MODEL")
        return len(missing) == 0, missing, model
    if provider in {"openai", "openai_compat"}:
        missing = []
        has_key = bool(os.environ.get("OPENAI_API_KEY", "").strip())
        model = os.environ.get("OPENAI_MODEL", "").strip() or None
        if not has_key:
            missing.append("OPENAI_API_KEY")
        if not model:
            missing.append("OPENAI_MODEL")
        return len(missing) == 0, missing, model
    if provider == "openai_responses":
        missing = []
        has_key = bool(os.environ.get("OPENAI_API_KEY", "").strip())
        model = os.environ.get("OPENAI_MODEL", "").strip() or "gpt-5"
        if not has_key:
            missing.append("OPENAI_API_KEY")
        return len(missing) == 0, missing, model
    if provider == "gemini":
        missing = []
        has_key = bool(
            os.environ.get("GEMINI_API_KEY", "").strip()
            or os.environ.get("GOOGLE_API_KEY", "").strip()
        )
        model = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-2.5-pro"
        if not has_key:
            missing.append("GEMINI_API_KEY")
        return len(missing) == 0, missing, model
    return False, [f"AGENT_PROVIDER({provider})"], None


def _cli_gateway_tenant_id(state_dir: Path) -> str | None:
    """默认 state_dir 下允许回退 `~/.agent-lab/gateways.json`；显式隔离 `--state` 下严禁串读宿主家目录。"""
    try:
        if state_dir.resolve() == (ROOT / ".agent-lab").resolve():
            return "local"
    except OSError:
        pass
    return None


def _is_loopback_base_url_display(base_url: str) -> bool:
    if not base_url:
        return False
    try:
        host = (urllib.parse.urlsplit(base_url).hostname or "").strip().lower().rstrip(".")
        return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost")
    except ValueError:
        return False


def _format_gateway_credential_status(prof: dict[str, Any]) -> str:
    """将脱敏后的网关 Profile 凭证就绪度格式化为可读标签（永不包含任何密钥原文）。"""
    proto = str(prof.get("protocol") or "").strip().lower()
    ready = bool(prof.get("credential_configured"))
    env_name = str(prof.get("api_key_env") or "").strip()
    has_inline = bool(prof.get("api_key"))
    base_url = str(prof.get("base_url") or "").strip()
    if proto == "demo":
        return "ready (offline)"
    if ready:
        if env_name and os.environ.get(env_name, "").strip():
            return f"ready (env:{env_name})"
        if has_inline:
            return f"ready (inline:{REDACTED_SECRET_MARKER})"
        if _is_loopback_base_url_display(base_url):
            return "ready (loopback)"
        return "ready"
    if env_name:
        return f"missing (env:{env_name})"
    return "missing"


def _close_provider_sync(provider: Any) -> None:
    """安全回收探测构造的 Provider 底层异步资源。"""
    close_fn = getattr(provider, "aclose", None)
    if callable(close_fn):
        with contextlib.suppress(Exception):
            maybe = close_fn()
            if inspect.isawaitable(maybe):
                asyncio.run(maybe)


def _render_gateways_table(
    redacted: dict[str, Any],
    *,
    env_gateway_profile: str | None = None,
) -> str:
    """将脱敏后的 `gateways.json` 渲染为对齐的终端表格（带活跃标记与脱敏凭证状态）。"""
    config_path = redacted.get("config_path")
    active_profile = redacted.get("active_profile")
    effective_active = env_gateway_profile or active_profile
    profiles: dict[str, dict[str, Any]] = redacted.get("profiles") or {}

    header_note = f"配置文件: {config_path}" if config_path else "配置文件: 未发现（可运行 `agent-lab gateway init` 创建）"
    if env_gateway_profile:
        header_note += f" | 环境变量覆盖: AGENT_GATEWAY_PROFILE={env_gateway_profile}"

    if not profiles:
        return (
            f"Agent Systems Lab · 多协议模型网关\n"
            f"{header_note}\n"
            "暂无已配置的网关 Profile。运行 `agent-lab gateway init` 生成涵盖 Anthropic / OpenAI Chat / "
            "OpenAI Responses / Gemini / Ollama / Demo 的起始配置。"
        )

    use_color = bool(getattr(sys.stdout, "isatty", lambda: False)())
    rows: list[dict[str, str]] = []
    for pname, pdata in profiles.items():
        is_active = bool(effective_active and pname == effective_active)
        rows.append(
            {
                "mark": "*" if is_active else " ",
                "profile": str(pname),
                "protocol": str(pdata.get("protocol") or ""),
                "model": str(pdata.get("model") or "-"),
                "base_url": _truncate_display(str(pdata.get("base_url") or "(offline)"), 42),
                "creds": _format_gateway_credential_status(pdata),
                "ready": "1" if pdata.get("credential_configured") else "0",
            }
        )

    prof_w = max(len("PROFILE"), max(_display_width(r["profile"]) for r in rows))
    proto_w = max(len("PROTOCOL"), max(_display_width(r["protocol"]) for r in rows))
    model_w = max(len("MODEL"), max(_display_width(r["model"]) for r in rows))
    url_w = max(len("BASE_URL"), max(_display_width(r["base_url"]) for r in rows))

    tbl_header = (
        f"  {_pad_display('PROFILE', prof_w)}  "
        f"{_pad_display('PROTOCOL', proto_w)}  "
        f"{_pad_display('MODEL', model_w)}  "
        f"{_pad_display('BASE_URL', url_w)}  "
        f"CREDENTIALS"
    )
    sep = "-" * min(120, max(_display_width(tbl_header) + 18, 78))
    lines = [
        "Agent Systems Lab · 多协议模型网关",
        header_note,
        sep,
        tbl_header,
        sep,
    ]
    for r in rows:
        mark = r["mark"]
        creds_text = r["creds"]
        if use_color:
            if mark == "*":
                mark = "\x1b[32m*\x1b[0m"
            creds_text = (
                f"\x1b[32m{creds_text}\x1b[0m"
                if r["ready"] == "1"
                else f"\x1b[33m{creds_text}\x1b[0m"
            )
        lines.append(
            f"{mark} {_pad_display(r['profile'], prof_w)}  "
            f"{_pad_display(r['protocol'], proto_w)}  "
            f"{_pad_display(r['model'], model_w)}  "
            f"{_pad_display(r['base_url'], url_w)}  "
            f"{creds_text}"
        )
    lines.append(sep)
    lines.append(
        f"共 {len(rows)} 个 Profile（使用 `agent-lab gateway use <profile>` 切换默认网关，或在 REPL 中输入 `/gateway <profile>` 热切换）"
    )
    return "\n".join(lines)


def _resolve_cli_provider_or_gateway(
    *,
    explicit_provider: str | None,
    explicit_gateway: str | None,
    gateway_config: Path | None,
    state_dir: Path,
    workspace: Path | None,
    parser: argparse.ArgumentParser,
    resumed_provider: str | None = None,
    emit_demo_tip: bool = True,
) -> tuple[Any, str, dict[str, Any] | None]:
    """统一解析 CLI 的模型提供方或声明式网关 Profile。

    返回 `(provider_arg_for_runtime, provider_name, gateway_info_or_none)`。
    """
    if explicit_provider is not None and explicit_gateway is not None:
        parser.error("--provider 与 --gateway 不能同时使用")

    tenant_id = _cli_gateway_tenant_id(state_dir)
    trust_ws = bool(workspace is not None and is_workspace_trusted(workspace, state_dir=state_dir))

    # 1. 显式 --gateway <profile>
    if explicit_gateway is not None:
        raw_gw = explicit_gateway.strip()
        if not raw_gw:
            parser.error("--gateway 不能为空")
        spec = raw_gw if raw_gw.startswith("gateway:") else f"gateway:{raw_gw}"
        try:
            prov, gw_info = resolve_provider_or_gateway(
                spec,
                state_dir=state_dir,
                workspace=workspace,
                explicit_config_path=gateway_config,
                trust_workspace=trust_ws,
                tenant_id=tenant_id,
            )
        except (ValueError, RuntimeError, FileNotFoundError, NotImplementedError) as exc:
            parser.error(f"解析模型网关失败：{exc}")
        prof_label = str(gw_info.get("profile") or raw_gw)
        return prov, f"gateway:{prof_label}", gw_info

    # 2. 显式 --provider <name>
    if explicit_provider is not None:
        resolved = _resolve_runnable_provider(
            explicit_provider,
            explicit=True,
            parser=parser,
            emit_demo_tip=emit_demo_tip,
        )
        return resolved, resolved, None

    # 3. 恢复历史会话时沿用已持久化的 provider / gateway:<profile>
    clean_resumed = (resumed_provider or "").strip()
    if clean_resumed.startswith("gateway:"):
        try:
            prov, gw_info = resolve_provider_or_gateway(
                clean_resumed,
                state_dir=state_dir,
                workspace=workspace,
                explicit_config_path=gateway_config,
                trust_workspace=trust_ws,
                tenant_id=tenant_id,
            )
        except (ValueError, RuntimeError, FileNotFoundError, NotImplementedError) as exc:
            parser.error(f"恢复会话网关失败：{exc}")
        return prov, clean_resumed, gw_info
    if clean_resumed in _CLI_PROVIDER_CHOICES:
        resolved = _resolve_runnable_provider(
            clean_resumed,
            explicit=False,
            parser=parser,
            emit_demo_tip=False,
        )
        return resolved, resolved, None

    # 4. 未传 --provider：若传入 --gateway-config 或设置了 AGENT_GATEWAY_PROFILE / gateways.json active_profile，则走网关解析
    env_gw = os.environ.get("AGENT_GATEWAY_PROFILE", "").strip()
    try:
        cfg_doc = load_gateways_config(
            explicit_path=gateway_config,
            state_dir=state_dir,
            workspace=workspace,
            trust_workspace=trust_ws,
            tenant_id=tenant_id,
        )
    except ValueError as exc:
        parser.error(f"gateways 配置校验失败：{exc}")

    has_active_profile = bool(cfg_doc.get("active_profile"))
    if gateway_config is not None or env_gw or has_active_profile:
        try:
            prov, gw_info = resolve_provider_or_gateway(
                None,
                state_dir=state_dir,
                workspace=workspace,
                explicit_config_path=gateway_config,
                trust_workspace=trust_ws,
                tenant_id=tenant_id,
            )
        except (ValueError, RuntimeError, FileNotFoundError, NotImplementedError) as exc:
            parser.error(
                f"解析模型网关失败：{exc}。"
                "请补全对应环境变量、运行 `agent-lab gateway use demo`，或显式传入 `--provider demo`。"
            )
        prof_label = gw_info.get("profile")
        prov_name = (
            f"gateway:{prof_label}"
            if prof_label
            else str(gw_info.get("protocol") or "demo")
        )
        return prov, prov_name, gw_info

    # 5. 无网关配置时沿用默认环境变量推断与回退
    raw_provider = _detect_default_cli_provider()
    resolved = _resolve_runnable_provider(
        raw_provider,
        explicit=False,
        parser=parser,
        emit_demo_tip=emit_demo_tip,
    )
    return resolved, resolved, None


def _run_gateway_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """处理 `agent-lab gateway list|use|init` 子命令组（全程脱敏，零密钥回显）。"""
    state_dir = args.state.resolve()
    tenant_id = _cli_gateway_tenant_id(state_dir)

    if args.gateway_command == "list":
        try:
            doc = load_gateways_config(
                explicit_path=args.config,
                state_dir=state_dir,
                tenant_id=tenant_id,
            )
        except ValueError as exc:
            parser.error(str(exc))
        redacted = redact_gateways_document(doc)
        raw_env_gw = os.environ.get("AGENT_GATEWAY_PROFILE", "").strip()
        if raw_env_gw.startswith("gateway:"):
            raw_env_gw = raw_env_gw[len("gateway:") :].strip()
        env_gw = raw_env_gw or None
        effective_active = env_gw or redacted.get("active_profile")
        if args.json:
            enriched_profiles: dict[str, dict[str, Any]] = {}
            for pname, pdata in (redacted.get("profiles") or {}).items():
                enriched_profiles[pname] = {
                    **pdata,
                    "credential_status": _format_gateway_credential_status(pdata),
                }
            show(
                {
                    "ok": True,
                    **redacted,
                    "profiles": enriched_profiles,
                    "env_gateway_profile": env_gw,
                    "effective_active_profile": effective_active,
                }
            )
        else:
            print(_render_gateways_table(redacted, env_gateway_profile=env_gw))
        return

    if args.gateway_command == "use":
        if args.config is not None:
            target_path = Path(args.config).expanduser()
        else:
            env_file = os.environ.get("AGENT_LAB_GATEWAYS_FILE", "").strip()
            if env_file:
                target_path = Path(env_file).expanduser()
            else:
                state_candidate = state_dir / "gateways.json"
                if state_candidate.is_symlink() or state_candidate.exists():
                    target_path = state_candidate
                else:
                    discovered = discover_gateways_config_path(
                        state_dir=state_dir,
                        tenant_id=tenant_id,
                    )
                    target_path = discovered if discovered is not None else state_candidate
        try:
            updated = set_active_gateway_profile(target_path, args.profile)
        except FileNotFoundError as exc:
            parser.error(f"{exc}（请先运行 `agent-lab gateway init` 生成配置文件）")
        except (ValueError, OSError) as exc:
            parser.error(str(exc))

        redacted = redact_gateways_document(updated)
        active_name = str(redacted.get("active_profile") or args.profile)
        prof = (redacted.get("profiles") or {}).get(active_name, {})
        cred_status = _format_gateway_credential_status(prof)
        if getattr(args, "json", False):
            show(
                {
                    "ok": True,
                    "config_path": redacted.get("config_path"),
                    "active_profile": active_name,
                    "profile": {**prof, "credential_status": cred_status},
                }
            )
        else:
            print(
                f"已切换默认模型网关: {active_name} | "
                f"协议: {prof.get('protocol') or '-'} | "
                f"模型: {prof.get('model') or '-'} | "
                f"端点: {prof.get('base_url') or '(offline)'} | "
                f"凭证: {cred_status}\n"
                f"配置文件: {redacted.get('config_path')}"
            )
        return

    if args.gateway_command == "init":
        target_path = Path(args.path).expanduser() if args.path is not None else (state_dir / "gateways.json")
        if target_path.is_symlink():
            parser.error(f"目标路径是符号链接，已拒绝写入：{target_path}")
        if target_path.exists() and not args.force:
            print(
                f"[跳过] gateways.json 已存在：{target_path}\n"
                "为保护已有网关配置，默认不覆盖；如需重建请显式传入 `--force`。",
                file=sys.stderr,
            )
            raise SystemExit(1)
        try:
            saved = save_gateways_config(target_path, default_gateways_template())
        except (ValueError, OSError) as exc:
            parser.error(f"写入 gateways.json 失败：{exc}")
        redacted = redact_gateways_document(saved)
        if getattr(args, "json", False):
            show({"ok": True, **redacted})
        else:
            prof_count = len(redacted.get("profiles") or {})
            print(
                f"已创建 {redacted.get('config_path')}（权限 0600，共 {prof_count} 个多协议 Profile，"
                f"active_profile={redacted.get('active_profile')}）\n"
                "下一步：\n"
                "  1. uv run agent-lab gateway list                 # 查看协议、模型与凭证就绪状态\n"
                "  2. uv run agent-lab gateway use openai-responses # 切换默认活跃网关 Profile\n"
                "  3. uv run agent-lab doctor                       # 体检网关配置与全栈环境"
            )
        return


def _format_a2a_credential_status(peer: dict[str, Any]) -> str:
    """将脱敏后的 A2A Peer 凭证就绪度格式化为可读标签（绝不输出任何密钥原文）。"""
    ready = bool(peer.get("credential_configured"))
    env_name = str(peer.get("api_key_env") or "").strip()
    has_inline = bool(peer.get("api_key"))
    if ready:
        if env_name and os.environ.get(env_name, "").strip():
            return f"ready (env:{env_name})"
        if has_inline:
            return f"ready (inline:{A2A_REDACTED_MARKER})"
        return "ready"
    if env_name:
        return f"unset (env:{env_name})"
    return "none"


def _render_a2a_peers_table(redacted: dict[str, Any]) -> str:
    """将脱敏后的 `a2a.json` 渲染为对齐的终端表格（零密钥回显）。"""
    config_path = redacted.get("config_path")
    peers: dict[str, dict[str, Any]] = redacted.get("peers") or {}
    header_note = (
        f"配置文件: {config_path}"
        if config_path
        else "配置文件: 未发现（可运行 `agent-lab a2a init` 创建）"
    )
    if not peers:
        return (
            "Agent Systems Lab · A2A 远端协同智能体 (Agent2Agent)\n"
            f"{header_note}\n"
            "暂无已配置的 A2A 远端对端智能体。运行 `agent-lab a2a init` 生成起始 a2a.json 模板。"
        )

    use_color = bool(getattr(sys.stdout, "isatty", lambda: False)())
    rows: list[dict[str, str]] = []
    for pname, pdata in sorted(peers.items()):
        enabled = bool(pdata.get("enabled", True))
        rows.append(
            {
                "peer": str(pname),
                "enabled": "yes" if enabled else "no",
                "url": _truncate_display(str(pdata.get("url") or "-"), 38),
                "private": "yes" if pdata.get("allow_private_network") else "no",
                "creds": _format_a2a_credential_status(pdata),
                "desc": _truncate_display(str(pdata.get("description") or "-"), 36),
            }
        )

    peer_w = max(len("PEER"), max(_display_width(r["peer"]) for r in rows))
    en_w = max(len("ENABLED"), max(_display_width(r["enabled"]) for r in rows))
    url_w = max(len("URL"), max(_display_width(r["url"]) for r in rows))
    priv_w = max(len("PRIVATE"), max(_display_width(r["private"]) for r in rows))
    cred_w = max(len("CREDENTIALS"), max(_display_width(r["creds"]) for r in rows))

    tbl_header = (
        f"{_pad_display('PEER', peer_w)}  "
        f"{_pad_display('ENABLED', en_w)}  "
        f"{_pad_display('URL', url_w)}  "
        f"{_pad_display('PRIVATE', priv_w)}  "
        f"{_pad_display('CREDENTIALS', cred_w)}  "
        "DESCRIPTION"
    )
    sep = "-" * min(124, max(_display_width(tbl_header) + 16, 78))
    lines = [
        "Agent Systems Lab · A2A 远端协同智能体 (Agent2Agent)",
        header_note,
        sep,
        tbl_header,
        sep,
    ]
    for r in rows:
        en_col = _pad_display(r["enabled"], en_w)
        if use_color:
            en_col = f"\x1b[32m{en_col}\x1b[0m" if r["enabled"] == "yes" else f"\x1b[33m{en_col}\x1b[0m"
        lines.append(
            f"{_pad_display(r['peer'], peer_w)}  "
            f"{en_col}  "
            f"{_pad_display(r['url'], url_w)}  "
            f"{_pad_display(r['private'], priv_w)}  "
            f"{_pad_display(r['creds'], cred_w)}  "
            f"{r['desc']}"
        )
    lines.append(sep)
    enabled_cnt = sum(1 for r in rows if r["enabled"] == "yes")
    lines.append(
        f"共 {len(rows)} 个对端（已启用 {enabled_cnt} 个；"
        "使用 `agent-lab a2a card --url <peer|url>` 发现能力，或 `agent-lab a2a send <peer> <prompt>` 委托任务）"
    )
    return "\n".join(lines)


def _run_a2a_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """处理 `agent-lab a2a init|list|card|send` 子命令组（全程脱敏，零密钥回显）。"""
    state_dir = args.state.resolve()
    cwd_ws: Path | None = None
    with contextlib.suppress(OSError):
        cwd_ws = Path.cwd().resolve()
    trust_ws = bool(cwd_ws is not None and is_workspace_trusted(cwd_ws, state_dir=state_dir))

    if args.a2a_command == "init":
        target_path = Path(args.path).expanduser() if args.path is not None else (state_dir / "a2a.json")
        if target_path.is_symlink():
            parser.error(f"目标路径是符号链接，已拒绝写入：{target_path}")
        if target_path.exists() and not args.force:
            print(
                f"[跳过] a2a.json 已存在：{target_path}\n"
                "为保护已有 A2A 对端配置，默认不覆盖；如需重建请显式传入 `--force`。",
                file=sys.stderr,
            )
            raise SystemExit(1)
        try:
            saved = save_a2a_config(target_path, default_a2a_template())
        except (ValueError, OSError) as exc:
            parser.error(f"写入 a2a.json 失败：{exc}")
        redacted = redact_a2a_document(saved)
        if getattr(args, "json", False):
            show({"ok": True, **redacted})
        else:
            peer_count = len(redacted.get("peers") or {})
            print(
                f"已创建 {redacted.get('config_path')}（权限 0600，共 {peer_count} 个示例 A2A Peer）\n"
                "下一步：\n"
                "  1. uv run agent-lab a2a list                       # 查看脱敏后的对端 Agent 与凭证状态\n"
                "  2. uv run agent-lab a2a card                       # 打印本节点对外发布的 AgentCard JSON\n"
                "  3. uv run agent-lab a2a send <peer> '<prompt>'     # 通过 A2A JSON-RPC 2.0 委托任务"
            )
        return

    if args.a2a_command == "list":
        try:
            doc = load_a2a_config(
                explicit_path=args.config,
                state_dir=state_dir,
                workspace=cwd_ws,
                trust_workspace=trust_ws,
            )
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        redacted = redact_a2a_document(doc)
        peers_map: dict[str, dict[str, Any]] = redacted.get("peers") or {}
        if args.json:
            enriched_peers = {
                pname: {
                    **pdata,
                    "credential_status": _format_a2a_credential_status(pdata),
                }
                for pname, pdata in peers_map.items()
            }
            enabled_count = sum(1 for p in enriched_peers.values() if p.get("enabled", True))
            show(
                {
                    "ok": True,
                    **redacted,
                    "peers": enriched_peers,
                    "peer_count": len(enriched_peers),
                    "enabled_count": enabled_count,
                }
            )
        else:
            print(_render_a2a_peers_table(redacted))
        return

    if args.a2a_command == "card":
        if not args.url:
            from .skills import SkillCatalog

            skills_list: list[dict[str, Any]] = []
            with contextlib.suppress(Exception):
                skills_list = SkillCatalog(ROOT / "skills").catalog()
            auth_enabled = bool(
                os.environ.get("AGENT_LAB_TOKEN", "").strip()
                or os.environ.get("AGENT_LAB_TOKENS_JSON", "").strip()
            )
            gw_proto = _detect_default_cli_provider()
            with contextlib.suppress(Exception):
                gw_doc = load_gateways_config(
                    state_dir=state_dir,
                    workspace=cwd_ws,
                    trust_workspace=trust_ws,
                    tenant_id=_cli_gateway_tenant_id(state_dir),
                )
                active_prof_name = gw_doc.get("active_profile")
                active_prof = (gw_doc.get("profiles") or {}).get(active_prof_name) if active_prof_name else None
                if isinstance(active_prof, dict) and active_prof.get("protocol"):
                    gw_proto = str(active_prof["protocol"])
            card = build_agent_card(
                base_url=args.base_url,
                skills=skills_list,
                auth_enabled=auth_enabled,
                gateway_protocol=gw_proto,
                extended=bool(getattr(args, "extended", False)),
            )
            show(card)
            return

        doc: dict[str, Any] = {"schema_version": 1, "peers": {}}
        try:
            doc = load_a2a_config(
                explicit_path=getattr(args, "config", None),
                state_dir=state_dir,
                workspace=cwd_ws,
                trust_workspace=trust_ws,
            )
        except (ValueError, OSError) as exc:
            if getattr(args, "config", None) is not None:
                parser.error(str(exc))

        async def _discover_remote_card() -> dict[str, Any]:
            mgr = A2AClientManager(doc)
            try:
                return await mgr.discover_agent_card(
                    args.url,
                    allow_private_network=bool(getattr(args, "allow_private", False)),
                )
            finally:
                await mgr.aclose()

        try:
            res = asyncio.run(_discover_remote_card())
        except Exception as exc:
            parser.error(f"发现远端 AgentCard 失败：{exc}")
        if not res.get("ok"):
            show(res)
            raise SystemExit(1)
        card_payload = res.get("card") if isinstance(res.get("card"), dict) else res
        show(card_payload)
        return

    if args.a2a_command == "send":
        try:
            doc = load_a2a_config(
                explicit_path=args.config,
                state_dir=state_dir,
                workspace=cwd_ws,
                trust_workspace=trust_ws,
            )
        except (ValueError, OSError) as exc:
            parser.error(str(exc))

        async def _send_remote_task() -> dict[str, Any]:
            mgr = A2AClientManager(doc)
            try:
                return await mgr.send_message(
                    args.peer,
                    args.prompt,
                    task_id=args.task_id,
                    context_id=getattr(args, "context_id", None),
                    blocking=not bool(args.async_mode),
                )
            finally:
                await mgr.aclose()

        try:
            res = asyncio.run(_send_remote_task())
        except Exception as exc:
            parser.error(f"A2A 委托任务失败：{exc}")
        show(res)
        if not res.get("ok"):
            raise SystemExit(1)
        return


def _parse_horizon_feature_spec(raw: str) -> dict[str, str]:
    """解析 `--feature` 特性规格字符串（支持 `F001:Title:Description`、`F001:Title` 或 `Title`）。"""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("feature 参数不能为空")
    parts = [p.strip() for p in text.split(":", 2)]
    if len(parts) == 3:
        fid, title, desc = parts
        feat: dict[str, str] = {"title": title, "description": desc or title}
        if fid:
            feat["id"] = fid
        return feat
    if len(parts) == 2:
        fid, title = parts
        feat = {"title": title, "description": title}
        if fid:
            feat["id"] = fid
        return feat
    return {"title": parts[0], "description": parts[0]}


def _render_horizon_campaigns_table(campaigns: list[dict[str, Any]], workspace: Path) -> str:
    """将工作区下的 Horizon Campaign 列表渲染为等宽对齐表格。"""
    if not campaigns:
        return (
            f"Agent Systems Lab · Horizon 超长程任务编排 (cwd={workspace})\n"
            "暂无 Horizon Campaign。运行 `agent-lab horizon init --goal \"...\" "
            "--feature \"F001:标题:描述\"` 创建。"
        )

    use_color = bool(getattr(sys.stdout, "isatty", lambda: False)())
    rows: list[dict[str, str]] = []
    for item in campaigns:
        summary = item.get("summary") if isinstance(item.get("summary"), dict) else {}
        passed = int(summary.get("passed_features") or 0)
        total = int(summary.get("total_features") or 0)
        completed = bool(summary.get("completed", False))
        pct = int(round((passed / total) * 100)) if total > 0 else 0
        updated = str(item.get("updated_at") or "-")
        if len(updated) > 19:
            updated = updated[:19].replace("T", " ")
        rows.append(
            {
                "id": str(item.get("campaign_id") or "-"),
                "progress": f"{passed}/{total} ({pct}%)",
                "status": "completed" if completed else "active",
                "updated": updated,
                "goal": _truncate_display(str(item.get("goal") or ""), 44),
            }
        )

    id_w = max(len("CAMPAIGN_ID"), max(_display_width(r["id"]) for r in rows))
    prog_w = max(len("PROGRESS"), max(_display_width(r["progress"]) for r in rows))
    st_w = max(len("STATUS"), max(_display_width(r["status"]) for r in rows))
    upd_w = max(len("UPDATED"), max(_display_width(r["updated"]) for r in rows))

    header = (
        f"{_pad_display('CAMPAIGN_ID', id_w)}  "
        f"{_pad_display('PROGRESS', prog_w)}  "
        f"{_pad_display('STATUS', st_w)}  "
        f"{_pad_display('UPDATED', upd_w)}  "
        "GOAL"
    )
    sep = "-" * min(124, max(_display_width(header) + 20, 78))
    lines = [
        f"Agent Systems Lab · Horizon 超长程任务编排 (cwd={workspace})",
        sep,
        header,
        sep,
    ]
    for r in rows:
        st_col = _pad_display(r["status"], st_w)
        if use_color:
            st_col = f"\x1b[32m{st_col}\x1b[0m" if r["status"] == "completed" else f"\x1b[36m{st_col}\x1b[0m"
        lines.append(
            f"{_pad_display(r['id'], id_w)}  "
            f"{_pad_display(r['progress'], prog_w)}  "
            f"{st_col}  "
            f"{_pad_display(r['updated'], upd_w)}  "
            f"{r['goal']}"
        )
    lines.append(sep)
    lines.append(
        f"共 {len(rows)} 个 Horizon Campaign"
        "（使用 `agent-lab horizon status <campaign_id>` 查看特征账本，或 `agent-lab horizon step <campaign_id>` 推进单特性）"
    )
    return "\n".join(lines)


def _render_horizon_status(doc: dict[str, Any]) -> str:
    """将单个 Horizon Campaign 的完整特征账本、验收收据与交接进度渲染为终端报告。"""
    use_color = bool(getattr(sys.stdout, "isatty", lambda: False)())
    summary = doc.get("summary") if isinstance(doc.get("summary"), dict) else {}
    total = int(summary.get("total_features") or len(doc.get("features") or []))
    passed = int(summary.get("passed_features") or 0)
    remaining = int(summary.get("remaining_features") or max(0, total - passed))
    completed = bool(summary.get("completed", False))
    state_label = "COMPLETED" if completed else "IN_PROGRESS"
    if use_color:
        state_label = (
            f"\x1b[32m{state_label}\x1b[0m" if completed else f"\x1b[36m{state_label}\x1b[0m"
        )

    features: list[dict[str, Any]] = [
        f for f in (doc.get("features") or []) if isinstance(f, dict)
    ]
    lines = [
        f"Horizon Campaign · {doc.get('campaign_id')} [{state_label}]",
        f"目标 (Goal): {doc.get('goal') or '-'}",
        f"冒烟命令 (Smoke): {doc.get('smoke_cmd') or '-'}",
        f"进度 (Progress): {passed}/{total} passed, {remaining} remaining",
        "-" * 92,
    ]
    if not features:
        lines.append("（无特性记录）")
    else:
        rows: list[dict[str, str]] = []
        for feat in features:
            is_pass = feat.get("passes") is True
            receipt = str(feat.get("receipt_sha256") or "")
            rows.append(
                {
                    "id": str(feat.get("id") or "-"),
                    "pass_mark": "[PASS]" if is_pass else "[TODO]",
                    "passed": "1" if is_pass else "0",
                    "status": str(feat.get("status") or ("passed" if is_pass else "pending")),
                    "attempts": str(int(feat.get("attempts") or 0)),
                    "receipt": receipt[:12] if receipt else "-",
                    "title": str(feat.get("title") or ""),
                    "description": str(feat.get("description") or ""),
                    "verification_cmd": str(feat.get("verification_cmd") or ""),
                }
            )
        id_w = max(len("ID"), max(_display_width(r["id"]) for r in rows))
        st_w = max(len("STATUS"), max(_display_width(r["status"]) for r in rows))
        att_w = max(len("TRY"), max(len(r["attempts"]) for r in rows))
        rcpt_w = max(len("RECEIPT"), max(len(r["receipt"]) for r in rows))
        lines.append(
            f"{'PASS':<6}  {_pad_display('ID', id_w)}  {_pad_display('STATUS', st_w)}  "
            f"{_pad_display('TRY', att_w, align='right')}  {_pad_display('RECEIPT', rcpt_w)}  TITLE"
        )
        lines.append("-" * 92)
        for r in rows:
            badge = r["pass_mark"]
            if use_color:
                badge = (
                    f"\x1b[32m{badge}\x1b[0m"
                    if r["passed"] == "1"
                    else f"\x1b[33m{badge}\x1b[0m"
                )
            lines.append(
                f"{badge}  {_pad_display(r['id'], id_w)}  {_pad_display(r['status'], st_w)}  "
                f"{_pad_display(r['attempts'], att_w, align='right')}  "
                f"{_pad_display(r['receipt'], rcpt_w)}  {r['title']}"
            )
            if r["description"] and r["description"] != r["title"]:
                lines.append(f"        描述: {r['description']}")
            if r["verification_cmd"]:
                lines.append(f"        验收: {r['verification_cmd']}")

    progress_md = str(doc.get("progress_markdown") or "").strip()
    if "### Session " in progress_md:
        tail_lines = progress_md.splitlines()[-16:]
        lines.append("-" * 92)
        lines.append("近期会话交接日志 (progress.md):")
        lines.extend(tail_lines)
    lines.append("-" * 92)
    return "\n".join(lines)


def _render_horizon_lint(report: dict[str, Any], workspace: Path) -> str:
    """将机械化架构边界检查结果渲染为带可执行修复指引的终端报告。"""
    use_color = bool(getattr(sys.stdout, "isatty", lambda: False)())
    ok = bool(report.get("ok", False))
    rules_checked = int(report.get("rules_checked") or 0)
    violations_count = int(report.get("violations_count") or 0)
    violations: list[dict[str, Any]] = [
        v for v in (report.get("violations") or []) if isinstance(v, dict)
    ]

    badge = "[PASS]" if ok else "[FAIL]"
    if use_color:
        badge = f"\x1b[32m{badge}\x1b[0m" if ok else f"\x1b[31m{badge}\x1b[0m"

    lines = [
        f"Horizon Architectural Invariants · {workspace}",
        "-" * 84,
        f"{badge} 已检查 {rules_checked} 条架构不变量规则，发现 {violations_count} 处违规。",
    ]
    if violations:
        lines.append("-" * 84)
        for v in violations:
            rid = str(v.get("rule_id") or "INV")
            kind = str(v.get("kind") or "rule")
            path = str(v.get("path") or "-")
            lineno = v.get("line")
            loc = f"{path}:{lineno}" if lineno is not None else path
            msg = str(v.get("message") or "")
            rem = str(v.get("remediation") or "")
            lines.append(f"- [{rid}] ({kind}) {loc}")
            if msg:
                lines.append(f"  违规详情: {msg}")
            if rem:
                lines.append(f"  修复指引 (Remediation): {rem}")
    lines.append("-" * 84)
    return "\n".join(lines)


def _run_horizon_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """处理 `agent-lab horizon init|list|status|step|lint` 子命令组。"""
    try:
        ws_path = validate_direct_workspace_path(args.cwd)
    except ValueError as exc:
        parser.error(str(exc))

    if args.horizon_command == "init":
        try:
            parsed_features = [_parse_horizon_feature_spec(raw) for raw in (args.feature or [])]
            mgr = HorizonCampaignManager(ws_path)
            doc = mgr.init_campaign(
                args.goal,
                parsed_features,
                smoke_cmd=args.smoke_cmd,
                campaign_id=args.campaign_id,
            )
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        show({"ok": True, **doc})
        return

    if args.horizon_command == "list":
        try:
            campaigns = HorizonCampaignManager(ws_path).list_campaigns()
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        if args.json:
            show(
                {
                    "ok": True,
                    "workspace": str(ws_path),
                    "count": len(campaigns),
                    "campaigns": campaigns,
                }
            )
        else:
            print(_render_horizon_campaigns_table(campaigns, ws_path))
        return

    if args.horizon_command == "status":
        try:
            doc = HorizonCampaignManager(ws_path).get_campaign(args.campaign_id)
        except (FileNotFoundError, ValueError, OSError) as exc:
            parser.error(str(exc))
        if args.json:
            show({"ok": True, **doc})
        else:
            print(_render_horizon_status(doc))
        return

    if args.horizon_command == "step":
        state_dir = args.state.resolve()
        state_dir.mkdir(parents=True, exist_ok=True)
        storage = Storage(state_dir / "state.db")
        provider, provider_name, _gw_info = _resolve_cli_provider_or_gateway(
            explicit_provider=args.provider,
            explicit_gateway=getattr(args, "gateway", None),
            gateway_config=getattr(args, "gateway_config", None),
            state_dir=state_dir,
            workspace=ws_path,
            parser=parser,
            emit_demo_tip=False,
        )
        eff_perm = getattr(args, "permission_mode", None) or "auto-edit"
        factory_called = False

        def _runtime_factory() -> AgentRuntime:
            nonlocal factory_called
            factory_called = True
            rt = AgentRuntime(
                storage,
                ws_path,
                ROOT / "data/knowledge",
                ROOT / "skills",
                provider=provider,
                execution_profile="host",
                permission_mode=eff_perm,
                auto_dream=False,
            )
            if not isinstance(provider, str):
                rt.provider_name = provider_name
            return rt

        try:
            res = asyncio.run(
                run_horizon_session(
                    ws_path,
                    args.campaign_id,
                    _runtime_factory,
                    run_smoke=bool(args.run_smoke),
                    run_verification_cmd=bool(args.verify),
                )
            )
        except (FileNotFoundError, ValueError, re.error, OSError) as exc:
            parser.error(str(exc))
        finally:
            if not factory_called and not isinstance(provider, str):
                _close_provider_sync(provider)
        show(res)
        return

    if args.horizon_command == "lint":
        try:
            checker = MechanicalInvariantChecker.from_workspace(ws_path)
            report = checker.check(ws_path)
        except (ValueError, re.error, OSError) as exc:
            parser.error(f"架构边界检查失败：{exc}")
        if args.json:
            show({"workspace": str(ws_path), **report})
        else:
            print(_render_horizon_lint(report, ws_path))
        if not report.get("ok"):
            raise SystemExit(1)
        return


def _render_index_report(payload: dict[str, Any]) -> str:
    """将代码库 Merkle 增量索引刷新统计与可选混合语义检索结果渲染为可读终端报告。"""
    use_color = bool(getattr(sys.stdout, "isatty", lambda: False)())
    root_hash = str(payload.get("root_hash") or "")
    root_display = f"\x1b[36m{root_hash}\x1b[0m" if (use_color and root_hash) else (root_hash or "-")
    lines = [
        f"Agent Systems Lab · 代码库 Merkle 增量语义索引 (cwd={payload.get('workspace')})",
        "-" * 88,
        f"Merkle Root: {root_display}",
        (
            f"文件统计: indexed={int(payload.get('files_indexed') or 0)}, "
            f"reindexed={int(payload.get('files_reindexed') or 0)}, "
            f"unchanged={int(payload.get('files_unchanged') or 0)}, "
            f"deleted={int(payload.get('files_deleted') or 0)}, "
            f"chunks={int(payload.get('chunks_total') or 0)}"
        ),
        f"索引库路径: {payload.get('db_path')}",
    ]
    if "query" in payload:
        results: list[dict[str, Any]] = [
            r for r in (payload.get("results") or []) if isinstance(r, dict)
        ]
        total_cand = int(payload.get("total_candidates") or 0)
        lines.append("-" * 88)
        prefix_note = f", path_prefix={payload['path_prefix']}" if payload.get("path_prefix") else ""
        lines.append(
            f"混合语义检索: query={payload.get('query')!r}{prefix_note} "
            f"(hits={len(results)}/{total_cand})"
        )
        if not results:
            lines.append("（未命中相关代码块）")
        else:
            for rank, hit in enumerate(results, 1):
                path = str(hit.get("path") or "-")
                s_line = int(hit.get("start_line") or 0)
                e_line = int(hit.get("end_line") or 0)
                kind = str(hit.get("kind") or "chunk")
                symbol = str(hit.get("symbol") or "-")
                score = float(hit.get("score") or 0.0)
                lines.append(
                    f"  {rank}. {path}:{s_line}-{e_line}  [{kind}] {symbol}  (score={score:.4f})"
                )
                excerpt = str(hit.get("excerpt") or "").strip()
                if excerpt:
                    preview_lines = [ln for ln in excerpt.splitlines() if ln.strip()][:3]
                    for pln in preview_lines:
                        lines.append(f"     {_truncate_display(pln, 92)}")
    lines.append("-" * 88)
    return "\n".join(lines)


def _run_index_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """处理 `agent-lab index [--cwd .] [--query "..."] [--limit 8] [--json]` 子命令。"""
    try:
        ws_path = validate_direct_workspace_path(args.cwd)
    except ValueError as exc:
        parser.error(str(exc))

    if isinstance(args.limit, bool) or not isinstance(args.limit, int) or not 1 <= args.limit <= 50:
        parser.error("--limit 必须在 1 到 50 之间")

    try:
        idx = CodebaseMerkleIndex(ws_path, args.state.expanduser())
        refresh_info = idx.refresh()
    except (ValueError, OSError) as exc:
        parser.error(f"代码库索引刷新失败：{exc}")

    payload: dict[str, Any] = {
        "ok": True,
        "workspace": str(idx.workspace),
        "project_slug": idx.project_slug,
        "db_path": str(idx.db_path),
        "root_hash": refresh_info["root_hash"],
        "files_indexed": refresh_info["files_indexed"],
        "files_reindexed": refresh_info["files_reindexed"],
        "files_unchanged": refresh_info["files_unchanged"],
        "files_deleted": refresh_info["files_deleted"],
        "chunks_total": refresh_info["chunks_total"],
    }

    if args.query is not None:
        try:
            search_info = idx.search(
                args.query,
                limit=args.limit,
                path_prefix=getattr(args, "path_prefix", None),
                auto_refresh=False,
            )
        except (ValueError, OSError) as exc:
            parser.error(f"代码库语义检索失败：{exc}")
        payload.update(
            {
                "query": search_info["query"],
                "path_prefix": search_info["path_prefix"],
                "total_candidates": search_info["total_candidates"],
                "results": search_info["results"],
            }
        )

    if args.json:
        show(payload)
    else:
        print(_render_index_report(payload))


def _mask_trigger_cli(trigger: dict[str, Any]) -> dict[str, Any]:
    """脱敏触发器 CLI 输出：始终将 webhook_secret 掩码为 ***REDACTED***，绝不回显明文。"""
    out = dict(trigger)
    has_secret = bool(out.get("webhook_secret")) or bool(out.get("has_webhook_secret"))
    out["has_webhook_secret"] = has_secret
    out["webhook_secret"] = REDACTED_SECRET_MARKER if has_secret else None
    out.pop("metadata_json", None)
    return out


def _resolve_trigger_by_id_or_prefix(storage: Storage, raw_id: str) -> dict[str, Any]:
    """按完整 trigger_id 或唯一短前缀解析持久化触发器。"""
    needle = raw_id.strip()
    if not needle:
        raise LookupError("trigger_id 不能为空")
    exact = storage.get_trigger(needle)
    if exact is not None:
        return exact
    lower_needle = needle.lower()
    candidates = [
        item
        for item in storage.list_triggers()
        if str(item.get("id") or "").lower().startswith(lower_needle)
    ]
    if not candidates:
        raise LookupError(f"未找到匹配 '{needle}' 的触发器")
    if len(candidates) > 1:
        preview = ", ".join(f"{c['id'][:10]}({c.get('name', '?')})" for c in candidates[:5])
        suffix = " ..." if len(candidates) > 5 else ""
        raise ValueError(
            f"前缀 '{needle}' 存在歧义，匹配到 {len(candidates)} 个触发器：{preview}{suffix}；请提供更长前缀。"
        )
    return candidates[0]


def _render_triggers_table(triggers: list[dict[str, Any]]) -> str:
    """将脱敏后的触发器列表渲染为对齐终端表格（零密钥回显）。"""
    if not triggers:
        return (
            "Agent Systems Lab · 自主 Cron / Webhook 触发器\n"
            '暂无触发器定义。运行 `agent-lab trigger create --name "nightly" --kind cron '
            '--cron "*/15 * * * *" --prompt "Run tests"` 创建。'
        )

    use_color = bool(getattr(sys.stdout, "isatty", lambda: False)())
    rows: list[dict[str, str]] = []
    for item in triggers:
        tid = str(item.get("id") or "")
        kind = str(item.get("kind") or "")
        if kind == "cron":
            spec = str(item.get("cron_expr") or "-")
        else:
            spec = "secret:configured" if item.get("has_webhook_secret") else "secret:none"
        last_fired = str(item.get("last_fired_at") or "-")
        if len(last_fired) > 19:
            last_fired = last_fired[:19].replace("T", " ")
        rows.append(
            {
                "id": tid[:10],
                "name": _truncate_display(str(item.get("name") or "-"), 24),
                "kind": kind,
                "spec": _truncate_display(spec, 22),
                "enabled": "yes" if item.get("enabled") else "no",
                "last_fired": last_fired,
                "prompt": _truncate_display(str(item.get("prompt_template") or ""), 36),
            }
        )

    id_w = max(len("ID"), max(_display_width(r["id"]) for r in rows))
    name_w = max(len("NAME"), max(_display_width(r["name"]) for r in rows))
    kind_w = max(len("KIND"), max(_display_width(r["kind"]) for r in rows))
    spec_w = max(len("SCHEDULE/SECRET"), max(_display_width(r["spec"]) for r in rows))
    en_w = max(len("ENABLED"), max(_display_width(r["enabled"]) for r in rows))
    lf_w = max(len("LAST_FIRED"), max(_display_width(r["last_fired"]) for r in rows))

    header = (
        f"{_pad_display('ID', id_w)}  "
        f"{_pad_display('NAME', name_w)}  "
        f"{_pad_display('KIND', kind_w)}  "
        f"{_pad_display('SCHEDULE/SECRET', spec_w)}  "
        f"{_pad_display('ENABLED', en_w)}  "
        f"{_pad_display('LAST_FIRED', lf_w)}  "
        "PROMPT"
    )
    sep = "-" * min(126, max(_display_width(header) + 18, 82))
    lines = [
        "Agent Systems Lab · 自主 Cron / Webhook 触发器",
        sep,
        header,
        sep,
    ]
    for r in rows:
        en_col = _pad_display(r["enabled"], en_w)
        if use_color:
            en_col = f"\x1b[32m{en_col}\x1b[0m" if r["enabled"] == "yes" else f"\x1b[33m{en_col}\x1b[0m"
        lines.append(
            f"{_pad_display(r['id'], id_w)}  "
            f"{_pad_display(r['name'], name_w)}  "
            f"{_pad_display(r['kind'], kind_w)}  "
            f"{_pad_display(r['spec'], spec_w)}  "
            f"{en_col}  "
            f"{_pad_display(r['last_fired'], lf_w)}  "
            f"{r['prompt']}"
        )
    lines.append(sep)
    lines.append(
        f"共 {len(rows)} 个触发器（使用 `agent-lab trigger tick` 扫描到期 Cron 任务，或 `agent-lab trigger delete <id>` 删除）"
    )
    return "\n".join(lines)


def _run_trigger_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    storage: Storage,
) -> None:
    """处理 `agent-lab trigger create|list|delete|tick` 子命令组（全程脱敏，零密钥回显）。"""
    state_dir = args.state.resolve()

    if args.trigger_command == "create":
        kind = str(args.kind).strip().lower()
        clean_cron: str | None = None
        if kind == "cron":
            if not args.cron or not str(args.cron).strip():
                parser.error("创建 cron 触发器时必须提供 --cron 5 字段表达式")
            try:
                CronSchedule.parse(str(args.cron))
            except ValueError as exc:
                parser.error(f"非法的 --cron 表达式：{exc}")
            clean_cron = str(args.cron).strip()

        raw_prompt = args.prompt.strip() if isinstance(args.prompt, str) and args.prompt.strip() else ""
        if not raw_prompt:
            raw_prompt = (
                "Handle incoming webhook event '{event}' for {repo}.\n\n{context}"
                if kind == "webhook"
                else f"Run scheduled trigger: {args.name.strip()}"
            )

        webhook_secret: str | None = None
        meta: dict[str, Any] = {}
        if args.secret_env is not None:
            env_name = str(args.secret_env).strip()
            if not env_name:
                parser.error("--secret-env 不能为空")
            env_val = os.environ.get(env_name, "").strip()
            if not env_val:
                parser.error(f"环境变量 {env_name!r} 未设置或为空，无法读取 Webhook 签名密钥")
            webhook_secret = env_val
            meta["secret_env"] = env_name

        ws_str: str | None = None
        if args.cwd is not None:
            try:
                ws_str = str(validate_direct_workspace_path(args.cwd))
            except ValueError as exc:
                parser.error(str(exc))

        if getattr(args, "timezone", None):
            tz_str = str(args.timezone).strip()
            try:
                ZoneInfo(tz_str)
            except ZoneInfoNotFoundError as exc:
                parser.error(f"非法的 --timezone 时区名称：{tz_str} ({exc})")
            meta["timezone"] = tz_str

        try:
            created = storage.create_trigger(
                args.name,
                kind,
                raw_prompt,
                cron_expr=clean_cron,
                webhook_secret=webhook_secret,
                workspace_path=ws_str,
                enabled=not bool(getattr(args, "disabled", False)),
                metadata=meta,
            )
        except ValueError as exc:
            parser.error(str(exc))
        masked = _mask_trigger_cli(created)
        show({"ok": True, **masked})
        return

    if args.trigger_command == "list":
        triggers = storage.list_triggers(enabled_only=bool(getattr(args, "enabled_only", False)))
        masked = [_mask_trigger_cli(item) for item in triggers]
        if args.json:
            show({"ok": True, "count": len(masked), "triggers": masked})
        else:
            print(_render_triggers_table(masked))
        return

    if args.trigger_command == "delete":
        try:
            target = _resolve_trigger_by_id_or_prefix(storage, args.trigger_id)
        except (LookupError, ValueError) as exc:
            parser.error(str(exc))
        resolved_id = str(target["id"])
        deleted = storage.delete_trigger(resolved_id)
        if not deleted:
            parser.error(f"触发器不存在：{args.trigger_id}")
        show({"ok": True, "id": resolved_id, "trigger_id": resolved_id, "deleted": True})
        return

    if args.trigger_command == "tick":
        parsed_now: datetime | None = None
        if getattr(args, "now", None):
            raw_now = str(args.now).strip()
            try:
                parsed_now = datetime.fromisoformat(raw_now.replace("Z", "+00:00"))
                if parsed_now.tzinfo is None:
                    parsed_now = parsed_now.replace(tzinfo=timezone.utc)
            except ValueError as exc:
                parser.error(f"非法的 --now ISO-8601 时间格式：{raw_now} ({exc})")

        async def _cli_dispatch_cron(trigger: dict[str, Any], dedup_key: str) -> dict[str, Any]:
            raw_ws = trigger.get("workspace_path")
            is_direct = False
            if isinstance(raw_ws, str) and raw_ws.strip():
                ws_path = validate_direct_workspace_path(raw_ws.strip())
                is_direct = True
                eff_profile = "host"
            else:
                ws_root = state_dir / "workspaces"
                ws_root.mkdir(parents=True, exist_ok=True)
                ws_path = ws_root / uuid.uuid4().hex
                shutil.copytree(ROOT / "data/sample_repo", ws_path)
                eff_profile = "sandbox"

            provider, provider_name, gw_info = _resolve_cli_provider_or_gateway(
                explicit_provider=getattr(args, "provider", None),
                explicit_gateway=getattr(args, "gateway", None),
                gateway_config=getattr(args, "gateway_config", None),
                state_dir=state_dir,
                workspace=ws_path,
                parser=parser,
                emit_demo_tip=False,
            )
            extra_meta: dict[str, Any] = {
                "template": "local_cwd" if is_direct else "sample",
                "direct_workspace": is_direct,
                "trigger_id": str(trigger["id"]),
                "trigger_kind": "cron",
                "trigger_dedup_key": dedup_key,
            }
            if gw_info:
                extra_meta["gateway"] = gw_info
            runtime = AgentRuntime(
                storage,
                ws_path,
                ROOT / "data/knowledge",
                ROOT / "skills",
                provider=provider,
                execution_profile=eff_profile,
                auto_dream=False,
            )
            if not isinstance(provider, str):
                runtime.provider_name = provider_name
            prompt_text = str(
                trigger.get("prompt_template") or f"Run scheduled trigger: {trigger.get('name', '')}"
            ).strip()
            trig_meta = trigger.get("metadata") if isinstance(trigger.get("metadata"), dict) else {}
            mode = str(trig_meta.get("mode") or "agent")
            if mode not in {"agent", "workflow", "parallel"}:
                mode = "agent"
            created = runtime.create(prompt_text, mode, extra_metadata=extra_meta)
            with contextlib.suppress(Exception):
                with storage._connect() as con:
                    con.execute("BEGIN IMMEDIATE")
                    con.execute(
                        "UPDATE trigger_fires SET run_id=? WHERE dedup_key=?",
                        (created["id"], dedup_key),
                    )
            if getattr(args, "enqueue_only", False):
                await runtime.aclose()
                storage.enqueue_run(created["id"], action="execute")
                return {"run_id": created["id"], "status": "queued"}
            res = await with_runtime(runtime, runtime.execute(created["id"]))
            return {"run_id": res["id"], "status": res.get("status")}

        scheduler = TriggerScheduler(storage, _cli_dispatch_cron)
        fired = asyncio.run(scheduler.tick_once(now=parsed_now))
        show({"ok": True, "fired_count": len(fired), "fired": fired})
        return


def _resolve_runnable_provider(
    provider: str,
    *,
    explicit: bool,
    parser: argparse.ArgumentParser,
    emit_demo_tip: bool = True,
) -> str:
    """校验选定 provider 凭据/模型完整性；未显式指定且缺模型时友好提示并安全回退 `demo`。"""
    ready, missing, _model = _provider_env_readiness(provider)
    env_explicit = bool(os.environ.get("AGENT_PROVIDER", "").strip())
    if not ready:
        missing_str = " / ".join(missing)
        if explicit or env_explicit:
            parser.error(
                f"已选择 provider={provider}，但缺少必需环境变量：{missing_str}。"
                "请运行 `agent-lab init` 配置 .env，或传入 `--provider demo` 离线运行。"
            )
        print(
            f"[提示] 检测到 {provider} 密钥但缺少 {missing_str}，已暂退至离线 demo 模式。"
            "请运行 `agent-lab init` 或在 .env 中补全模型 ID。",
            file=sys.stderr,
        )
        return "demo"
    if provider == "demo" and emit_demo_tip and not explicit and not env_explicit:
        print(
            "[提示] 未检测到真实模型 API Key，正以离线 demo 模式运行。"
            "运行 `agent-lab init` 配置 Claude/OpenAI，或 `agent-lab doctor` 检查环境。",
            file=sys.stderr,
        )
    return provider


def _check_macos_tcc_permissions() -> dict[str, Any]:
    """非阻塞探测 macOS TCC 辅助功能与屏幕录制权限（仅在 Darwin 上执行，不触发系统弹窗）。"""
    if sys.platform != "darwin":
        return {
            "applicable": False,
            "available": False,
            "accessibility": None,
            "screen_recording": None,
            "missing": [],
        }
    from .computer import ComputerToolError, MacOSNativeBackend

    try:
        backend = MacOSNativeBackend()
    except (ComputerToolError, OSError) as exc:
        return {
            "applicable": True,
            "available": False,
            "accessibility": False,
            "screen_recording": False,
            "missing": ["accessibility", "screen_recording"],
            "error": str(exc),
        }

    acc_ok = True
    screen_ok = True
    missing: list[str] = []
    try:
        backend._require_accessibility()
    except ComputerToolError:
        acc_ok = False
        missing.append("accessibility")
    try:
        backend._require_screen_recording()
    except ComputerToolError:
        screen_ok = False
        missing.append("screen_recording")
    return {
        "applicable": True,
        "available": True,
        "accessibility": acc_ok,
        "screen_recording": screen_ok,
        "missing": missing,
    }


def _warn_macos_gui_tcc_if_needed() -> None:
    """在 macOS `--gui` 启动前执行快速 TCC 预检，未授权时在消耗任何 LLM Token 前给出修复路径。"""
    if sys.platform != "darwin":
        return
    tcc = _check_macos_tcc_permissions()
    if not tcc.get("applicable") or not tcc.get("missing"):
        return
    lines = ["[警告] 已启用 --gui（macOS 桌面操控），但当前终端尚未获得完整 TCC 隐私权限："]
    if not tcc.get("accessibility"):
        lines.append(
            f"  - 缺少『辅助功能 (Accessibility)』权限（未授权时键鼠注入会被系统静默丢弃）\n"
            f"    开启路径: {_MACOS_TCC_ACCESSIBILITY_PATH}"
        )
    if not tcc.get("screen_recording"):
        lines.append(
            f"  - 缺少『屏幕录制 (Screen Recording)』权限（未授权时截屏仅显示桌面壁纸）\n"
            f"    开启路径: {_MACOS_TCC_SCREEN_RECORDING_PATH}"
        )
    lines.append("  授权后请重启当前终端应用再重试（或运行 `agent-lab doctor` 复检）。")
    print("\n".join(lines), file=sys.stderr)


def _display_width(text: str) -> int:
    """计算字符串在等宽终端中的视觉列宽（兼容中文等 East Asian 全角字符）。"""
    width = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1
    return width


def _truncate_display(text: str, max_width: int) -> str:
    """按终端视觉列宽安全截断文本并在超长时追加省略号。"""
    collapsed = " ".join(text.split())
    if _display_width(collapsed) <= max_width:
        return collapsed
    if max_width <= 3:
        return collapsed[:max_width]
    budget = max_width - 3
    out: list[str] = []
    used = 0
    for ch in collapsed:
        cw = 0 if unicodedata.combining(ch) else (2 if unicodedata.east_asian_width(ch) in {"W", "F"} else 1)
        if used + cw > budget:
            break
        out.append(ch)
        used += cw
    return "".join(out) + "..."


def _pad_display(text: str, width: int, *, align: str = "left") -> str:
    """按视觉列宽左右补齐空格，使中英文混排表格保持严格列对齐。"""
    pad = max(0, width - _display_width(text))
    if align == "right":
        return (" " * pad) + text
    return text + (" " * pad)


def _collect_doctor_report(
    cwd: Path,
    loaded_dotenv_paths: list[Path] | None = None,
    *,
    state_dir: Path | None = None,
    gateway_config: Path | None = None,
    a2a_config: Path | None = None,
) -> dict[str, Any]:
    """收集全栈环境体检报告（绝不包含任何环境变量密钥明文）。"""
    from .host_tools import HostKernelSandbox

    resolved_cwd = cwd.resolve()
    resolved_state = (state_dir or (ROOT / ".agent-lab")).resolve()
    cwd_env = resolved_cwd / ".env"
    root_env = ROOT.resolve() / ".env"
    found_envs: list[str] = []
    for p in (cwd_env, root_env):
        if p.is_file() and str(p) not in found_envs:
            found_envs.append(str(p))

    checks: list[dict[str, Any]] = []

    # 1) .env & active provider / model / key readiness (never print secrets!)
    active_provider = _detect_default_cli_provider()
    env_provider = os.environ.get("AGENT_PROVIDER", "").strip().lower() or None
    ready, missing_vars, active_model = _provider_env_readiness(active_provider)
    anthropic_key_set = bool(
        os.environ.get("ANTHROPIC_API_KEY", "").strip()
        or os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    )
    anthropic_model = os.environ.get("ANTHROPIC_MODEL", "").strip() or None
    openai_key_set = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    openai_model = os.environ.get("OPENAI_MODEL", "").strip() or None
    gemini_key_set = bool(
        os.environ.get("GEMINI_API_KEY", "").strip()
        or os.environ.get("GOOGLE_API_KEY", "").strip()
    )
    gemini_model = os.environ.get("GEMINI_MODEL", "").strip() or None

    if active_provider == "demo":
        provider_status = "pass" if env_provider == "demo" else "warn"
        provider_summary = (
            f".env: {'已发现 (' + ', '.join(found_envs) + ')' if found_envs else '未创建'} | "
            f"provider=demo（离线确定性模式，无需密钥）"
        )
        provider_remediation = (
            None
            if env_provider == "demo" and found_envs
            else "运行 `agent-lab init` 快速配置 Claude / OpenAI 密钥（离线体验可直接跳过）"
        )
    elif ready:
        provider_status = "pass"
        provider_summary = (
            f".env: {'已发现' if found_envs else '未创建（来自 Shell 环境）'} | "
            f"provider={active_provider}, model={active_model}, key=configured"
        )
        provider_remediation = None
    else:
        provider_status = "fail"
        provider_summary = (
            f"provider={active_provider} 缺少必需配置: {', '.join(missing_vars)}"
        )
        provider_remediation = (
            f"运行 `agent-lab init --provider {active_provider}` 或在 .env 中设置 {', '.join(missing_vars)}"
        )

    checks.append(
        {
            "id": "dotenv_and_provider",
            "name": ".env & Provider",
            "status": provider_status,
            "summary": provider_summary,
            "remediation": provider_remediation,
            "details": {
                "cwd_dotenv_exists": cwd_env.is_file(),
                "found_dotenv_paths": found_envs,
                "loaded_dotenv_paths": [str(p) for p in (loaded_dotenv_paths or [])],
                "active_provider": active_provider,
                "explicit_agent_provider": env_provider,
                "ready": ready,
                "missing_vars": missing_vars,
                "active_model": active_model,
                "providers": {
                    "anthropic": {
                        "key_configured": anthropic_key_set,
                        "model_configured": bool(anthropic_model),
                        "model": anthropic_model,
                    },
                    "openai": {
                        "key_configured": openai_key_set,
                        "model_configured": bool(openai_model),
                        "model": openai_model,
                    },
                    "gemini": {
                        "key_configured": gemini_key_set,
                        "model_configured": bool(gemini_model),
                        "model": gemini_model,
                    },
                },
            },
        }
    )

    # 1b) Multi-protocol gateway configuration readiness (never print secrets!)
    gw_tenant_id = _cli_gateway_tenant_id(resolved_state)
    gw_trust_ws = is_workspace_trusted(resolved_cwd, state_dir=resolved_state)
    raw_env_gw = os.environ.get("AGENT_GATEWAY_PROFILE", "").strip()
    if raw_env_gw.startswith("gateway:"):
        raw_env_gw = raw_env_gw[len("gateway:") :].strip()
    env_gw_profile = raw_env_gw or None

    try:
        gw_doc = load_gateways_config(
            explicit_path=gateway_config,
            state_dir=resolved_state,
            workspace=resolved_cwd,
            trust_workspace=gw_trust_ws,
            tenant_id=gw_tenant_id,
        )
    except ValueError as exc:
        checks.append(
            {
                "id": "gateway_config",
                "name": "Gateway Config",
                "status": "fail",
                "summary": f"gateways.json 校验失败: {exc}",
                "remediation": "修复 gateways.json 或运行 `agent-lab gateway init --force` 重建合法模板",
                "details": {
                    "config_path": str(Path(gateway_config).expanduser()) if gateway_config else None,
                    "env_gateway_profile": env_gw_profile,
                    "active_profile": None,
                    "protocol": None,
                    "model": None,
                    "base_url": None,
                    "credential_configured": False,
                    "credential_status": "invalid_config",
                    "profile_count": 0,
                    "error": str(exc),
                },
            }
        )
    else:
        gw_redacted = redact_gateways_document(gw_doc)
        gw_cfg_path = gw_redacted.get("config_path")
        gw_profiles: dict[str, dict[str, Any]] = gw_redacted.get("profiles") or {}
        gw_active = env_gw_profile or gw_redacted.get("active_profile")
        active_prof = gw_profiles.get(gw_active) if gw_active else None
        safe_profiles_summary = {
            pname: {
                "protocol": pdata.get("protocol"),
                "model": pdata.get("model") or None,
                "base_url": pdata.get("base_url") or "",
                "credential_configured": bool(pdata.get("credential_configured")),
                "credential_status": _format_gateway_credential_status(pdata),
            }
            for pname, pdata in gw_profiles.items()
        }

        if env_gw_profile and active_prof is None:
            gw_status = "fail"
            gw_summary = (
                f"AGENT_GATEWAY_PROFILE={env_gw_profile!r} 未在 gateways.json 中找到"
                f"（config={gw_cfg_path or '未发现'}）"
            )
            gw_remediation = "运行 `agent-lab gateway list` 查看可用 Profile 或修正 AGENT_GATEWAY_PROFILE"
            gw_proto = None
            gw_model = None
            gw_base_url = None
            gw_cred_ok = False
            gw_cred_status = "profile_not_found"
        elif not gw_cfg_path:
            gw_status = "warn"
            gw_summary = "未发现 gateways.json（当前使用环境变量 Provider 回退）"
            gw_remediation = "运行 `agent-lab gateway init` 生成多协议网关模板（Anthropic / OpenAI Chat / Responses / Gemini）"
            gw_proto = None
            gw_model = None
            gw_base_url = None
            gw_cred_ok = False
            gw_cred_status = "not_configured"
        elif active_prof is None:
            gw_status = "warn"
            gw_summary = f"config={gw_cfg_path}（{len(gw_profiles)} 个 Profile），未设置 active_profile"
            gw_remediation = "运行 `agent-lab gateway use <profile>` 激活默认模型网关"
            gw_proto = None
            gw_model = None
            gw_base_url = None
            gw_cred_ok = False
            gw_cred_status = "no_active_profile"
        else:
            gw_proto = active_prof.get("protocol")
            gw_model = active_prof.get("model") or None
            gw_base_url = active_prof.get("base_url") or ""
            gw_cred_ok = bool(active_prof.get("credential_configured"))
            gw_cred_status = _format_gateway_credential_status(active_prof)
            if gw_cred_ok:
                gw_status = "pass"
                gw_summary = (
                    f"config={gw_cfg_path} | profile={gw_active} | "
                    f"protocol={gw_proto} | model={gw_model or '-'} | credentials={gw_cred_status}"
                )
                gw_remediation = None
            else:
                gw_status = "fail"
                gw_summary = (
                    f"config={gw_cfg_path} | profile={gw_active} | "
                    f"protocol={gw_proto} | credentials={gw_cred_status}"
                )
                env_hint = str(active_prof.get("api_key_env") or "").strip()
                gw_remediation = (
                    f"请设置环境变量 {env_hint}，或运行 `agent-lab gateway use demo` 切换离线模式"
                    if env_hint
                    else "请为该 Profile 配置 api_key_env，或运行 `agent-lab gateway use demo`"
                )

        checks.append(
            {
                "id": "gateway_config",
                "name": "Gateway Config",
                "status": gw_status,
                "summary": gw_summary,
                "remediation": gw_remediation,
                "details": {
                    "config_path": gw_cfg_path,
                    "env_gateway_profile": env_gw_profile,
                    "active_profile": gw_active,
                    "protocol": gw_proto,
                    "model": gw_model,
                    "base_url": gw_base_url,
                    "credential_configured": gw_cred_ok,
                    "credential_status": gw_cred_status,
                    "profile_count": len(gw_profiles),
                    "profiles": safe_profiles_summary,
                },
            }
        )

    # 1c) A2A (Agent2Agent) remote peer configuration readiness (never print secrets!)
    try:
        a2a_doc = load_a2a_config(
            explicit_path=a2a_config,
            state_dir=resolved_state,
            workspace=resolved_cwd,
            trust_workspace=gw_trust_ws,
        )
    except (ValueError, OSError) as exc:
        checks.append(
            {
                "id": "a2a_peers",
                "name": "A2A Peers",
                "status": "fail",
                "summary": f"a2a.json 校验失败: {exc}",
                "remediation": "修复 a2a.json 或运行 `agent-lab a2a init --force` 重建合法模板",
                "details": {
                    "config_path": str(Path(a2a_config).expanduser()) if a2a_config else None,
                    "peer_count": 0,
                    "enabled_count": 0,
                    "enabled_peers": [],
                    "peers": {},
                    "error": str(exc),
                },
            }
        )
    else:
        a2a_redacted = redact_a2a_document(a2a_doc)
        a2a_cfg_path = a2a_redacted.get("config_path")
        a2a_peers_map: dict[str, dict[str, Any]] = a2a_redacted.get("peers") or {}
        a2a_total = len(a2a_peers_map)
        a2a_enabled_names = sorted(
            pname for pname, pdata in a2a_peers_map.items() if pdata.get("enabled", True)
        )
        safe_a2a_summary = {
            pname: {
                "url": pdata.get("url") or "",
                "enabled": bool(pdata.get("enabled", True)),
                "allow_private_network": bool(pdata.get("allow_private_network", False)),
                "credential_configured": bool(pdata.get("credential_configured", False)),
                "credential_status": _format_a2a_credential_status(pdata),
            }
            for pname, pdata in sorted(a2a_peers_map.items())
        }
        if not a2a_cfg_path:
            a2a_status = "warn"
            a2a_summary = "未发现 a2a.json（已配置 0 个远端对端 Agent；本节点 AgentCard 与 /a2a 默认就绪）"
            a2a_remediation: str | None = "运行 `agent-lab a2a init` 生成远端 A2A 协同智能体起始配置"
        elif a2a_total == 0:
            a2a_status = "warn"
            a2a_summary = f"config={a2a_cfg_path} | 已配置 0 个远端对端 Agent"
            a2a_remediation = "编辑 a2a.json 添加对端或运行 `agent-lab a2a init --force` 重建示例配置"
        else:
            a2a_status = "pass"
            a2a_summary = (
                f"config={a2a_cfg_path} | 已配置 {a2a_total} 个远端对端 Agent"
                f"（已启用 {len(a2a_enabled_names)} 个）"
            )
            a2a_remediation = None
        checks.append(
            {
                "id": "a2a_peers",
                "name": "A2A Peers",
                "status": a2a_status,
                "summary": a2a_summary,
                "remediation": a2a_remediation,
                "details": {
                    "config_path": a2a_cfg_path,
                    "peer_count": a2a_total,
                    "enabled_count": len(a2a_enabled_names),
                    "enabled_peers": a2a_enabled_names,
                    "peers": safe_a2a_summary,
                },
            }
        )

    # 2) Docker daemon availability & python:3.12-slim image
    docker_image = "python:3.12-slim"
    docker_bin = shutil.which("docker")
    remote_sandbox = bool(os.environ.get("AGENT_LAB_SANDBOX_URL", "").strip())
    daemon_ok = False
    image_ok = False
    docker_error: str | None = None
    if docker_bin:
        try:
            info_res = subprocess.run(
                [docker_bin, "info", "--format", "{{.ServerVersion}}"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=4.0,
                check=False,
            )
            daemon_ok = info_res.returncode == 0
            if not daemon_ok:
                docker_error = (info_res.stderr or info_res.stdout or "docker info failed").strip().splitlines()[0]
        except (OSError, subprocess.SubprocessError) as exc:
            docker_error = str(exc)
        if daemon_ok:
            try:
                img_res = subprocess.run(
                    [docker_bin, "image", "inspect", docker_image, "--format", "{{.Id}}"],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    timeout=4.0,
                    check=False,
                )
                image_ok = img_res.returncode == 0
            except (OSError, subprocess.SubprocessError) as exc:
                docker_error = str(exc)

    if daemon_ok and image_ok:
        docker_status = "pass"
        docker_summary = f"Docker 守护进程在线，镜像 {docker_image} 已就绪"
        docker_remediation = None
    elif daemon_ok and not image_ok:
        docker_status = "warn"
        docker_summary = f"Docker 守护进程在线，但本地缺少镜像 {docker_image}"
        docker_remediation = f"docker pull {docker_image}"
    elif docker_bin:
        docker_status = "pass" if remote_sandbox else "warn"
        docker_summary = (
            "远程沙盒已配置（本地 Docker 未运行）"
            if remote_sandbox
            else "已找到 docker CLI，但守护进程未运行或不可访问"
        )
        docker_remediation = (
            None if remote_sandbox else f"启动 Docker Desktop / dockerd 后执行: docker pull {docker_image}"
        )
    else:
        docker_status = "pass" if remote_sandbox else "warn"
        docker_summary = (
            "远程沙盒已配置（本机未安装 docker CLI）"
            if remote_sandbox
            else "未检测到 docker CLI（容器验收将返回 sandbox_unavailable）"
        )
        docker_remediation = (
            None if remote_sandbox else f"安装 Docker 后执行: docker pull {docker_image}"
        )

    checks.append(
        {
            "id": "docker_sandbox",
            "name": "Docker Sandbox",
            "status": docker_status,
            "summary": docker_summary,
            "remediation": docker_remediation,
            "details": {
                "docker_cli": docker_bin,
                "daemon_available": daemon_ok,
                "image": docker_image,
                "image_present": image_ok,
                "remote_sandbox_configured": remote_sandbox,
                **({"error": docker_error} if docker_error else {}),
            },
        }
    )

    # 3) OS kernel sandbox readiness (macOS /usr/bin/sandbox-exec or Linux bwrap)
    try:
        os_sandbox_mode = HostKernelSandbox.normalize_mode()
    except Exception:
        os_sandbox_mode = os.environ.get("AGENT_LAB_HOST_SANDBOX", "auto").strip() or "auto"
    os_sandbox_backend = HostKernelSandbox.detect_backend()
    if os_sandbox_mode == "off":
        os_status = "warn"
        os_summary = "已通过 AGENT_LAB_HOST_SANDBOX=off 禁用 OS 内核沙箱"
        os_remediation = "移除 AGENT_LAB_HOST_SANDBOX=off 以恢复内核沙箱防护"
    elif os_sandbox_backend:
        os_status = "pass"
        os_summary = f"内核沙箱就绪: {os_sandbox_backend} (mode={os_sandbox_mode})"
        os_remediation = None
    else:
        os_status = "fail" if os_sandbox_mode == "strict" else "warn"
        os_summary = f"未检测到可用 OS 内核沙箱 (platform={sys.platform}, mode={os_sandbox_mode})"
        if sys.platform == "darwin":
            os_remediation = "确认 /usr/bin/sandbox-exec 存在且当前权限允许执行"
        elif sys.platform.startswith("linux"):
            os_remediation = "安装 bubblewrap（例如: sudo apt install bubblewrap）"
        else:
            os_remediation = "当前平台暂不支持内核级沙箱，命令将直接执行"

    checks.append(
        {
            "id": "os_kernel_sandbox",
            "name": "OS Kernel Sandbox",
            "status": os_status,
            "summary": os_summary,
            "remediation": os_remediation,
            "details": {
                "platform": sys.platform,
                "mode": os_sandbox_mode,
                "backend": os_sandbox_backend,
                "available": bool(os_sandbox_backend),
            },
        }
    )

    # 4) Installed LSP servers
    lsp_map: dict[str, dict[str, Any]] = {}
    installed_lsp: list[str] = []
    for binary in _LSP_SERVER_BINARIES:
        found_path = shutil.which(binary)
        lsp_map[binary] = {"installed": bool(found_path), "path": found_path}
        if found_path:
            installed_lsp.append(binary)
    if installed_lsp:
        lsp_status = "pass"
        lsp_summary = f"已检测 {len(installed_lsp)}/{len(_LSP_SERVER_BINARIES)}: {', '.join(installed_lsp)}"
        lsp_remediation = None
    else:
        lsp_status = "warn"
        lsp_summary = f"未检测到已安装的 LSP 语言服务器（0/{len(_LSP_SERVER_BINARIES)}，将零报错降级）"
        lsp_remediation = "按需安装，如: uv tool install basedpyright 或 npm i -g typescript-language-server"

    checks.append(
        {
            "id": "lsp_servers",
            "name": "LSP Servers",
            "status": lsp_status,
            "summary": lsp_summary,
            "remediation": lsp_remediation,
            "details": {"installed": installed_lsp, "servers": lsp_map},
        }
    )

    # 5) Optional Python extras
    extras_map: dict[str, dict[str, Any]] = {}
    installed_extras: list[str] = []
    missing_extra_groups: list[str] = []
    for mod_name, extra_group in _OPTIONAL_PYTHON_EXTRAS:
        is_installed = importlib.util.find_spec(mod_name) is not None
        extras_map[mod_name] = {"installed": is_installed, "extra": extra_group}
        if is_installed:
            installed_extras.append(mod_name)
        elif extra_group not in missing_extra_groups:
            missing_extra_groups.append(extra_group)

    if len(installed_extras) == len(_OPTIONAL_PYTHON_EXTRAS):
        extras_status = "pass"
        extras_summary = f"全部可选扩展已安装 ({len(installed_extras)}/{len(_OPTIONAL_PYTHON_EXTRAS)})"
        extras_remediation = None
    else:
        extras_status = "pass" if installed_extras else "warn"
        missing_mods = [m for m, info in extras_map.items() if not info["installed"]]
        installed_str = ", ".join(installed_extras) if installed_extras else "无"
        extras_summary = f"已安装: {installed_str} | 未安装(按需): {', '.join(missing_mods)}"
        extra_flags = " ".join(f"--extra {g}" for g in missing_extra_groups)
        extras_remediation = f"按需补装: uv sync {extra_flags}"

    checks.append(
        {
            "id": "python_extras",
            "name": "Python Extras",
            "status": extras_status,
            "summary": extras_summary,
            "remediation": extras_remediation,
            "details": {"installed": installed_extras, "extras": extras_map},
        }
    )

    # 6) macOS TCC Accessibility & Screen Recording permissions (when on Darwin)
    if sys.platform == "darwin":
        tcc = _check_macos_tcc_permissions()
        if tcc.get("available") and tcc.get("accessibility") and tcc.get("screen_recording"):
            tcc_status = "pass"
            tcc_summary = "Accessibility 与 Screen Recording 均已授权（--gui 就绪）"
            tcc_remediation = None
        else:
            tcc_status = "warn"
            missing_parts: list[str] = []
            remediation_parts: list[str] = []
            if not tcc.get("accessibility"):
                missing_parts.append("Accessibility(未授权)")
                remediation_parts.append(f"辅助功能: {_MACOS_TCC_ACCESSIBILITY_PATH}")
            if not tcc.get("screen_recording"):
                missing_parts.append("Screen Recording(未授权)")
                remediation_parts.append(f"屏幕录制: {_MACOS_TCC_SCREEN_RECORDING_PATH}")
            tcc_summary = f"仅 --gui 桌面操控需要: {', '.join(missing_parts)}"
            tcc_remediation = " ; ".join(remediation_parts)
        checks.append(
            {
                "id": "macos_tcc",
                "name": "macOS TCC (--gui)",
                "status": tcc_status,
                "summary": tcc_summary,
                "remediation": tcc_remediation,
                "details": {
                    **tcc,
                    "settings_paths": {
                        "accessibility": _MACOS_TCC_ACCESSIBILITY_PATH,
                        "screen_recording": _MACOS_TCC_SCREEN_RECORDING_PATH,
                    },
                },
            }
        )

    counts = {"pass": 0, "warn": 0, "fail": 0}
    for item in checks:
        st = item["status"]
        if st in counts:
            counts[st] += 1

    return {
        "ok": counts["fail"] == 0,
        "platform": sys.platform,
        "cwd": str(resolved_cwd),
        "summary": counts,
        "checks": checks,
    }


def _render_doctor_table(report: dict[str, Any]) -> str:
    """将体检报告渲染为清晰对齐的 PASS/WARN/FAIL 检查清单表。"""
    use_color = bool(getattr(sys.stdout, "isatty", lambda: False)())

    def _badge(status: str) -> str:
        raw = {"pass": "[PASS]", "warn": "[WARN]", "fail": "[FAIL]"}.get(status, f"[{status.upper()}]")
        if not use_color:
            return raw
        color = {"pass": "\x1b[32m", "warn": "\x1b[33m", "fail": "\x1b[31m"}.get(status, "")
        return f"{color}{raw}\x1b[0m" if color else raw

    checks = report.get("checks") or []
    name_w = max([len("CHECK")] + [_display_width(str(c["name"])) for c in checks])
    lines = [
        f"Agent Systems Lab · 环境体检 (cwd={report['cwd']}, platform={report['platform']})",
        "-" * 78,
        f"{'STATUS':<6}  {_pad_display('CHECK', name_w)}  DETAIL",
        "-" * 78,
    ]
    for item in checks:
        badge = _badge(str(item["status"]))
        name_col = _pad_display(str(item["name"]), name_w)
        lines.append(f"{badge}  {name_col}  {item['summary']}")
        if item.get("remediation"):
            lines.append(f"{'':<6}  {_pad_display('', name_w)}  -> {item['remediation']}")
    counts = report.get("summary") or {}
    lines.append("-" * 78)
    lines.append(
        f"汇总: {counts.get('pass', 0)} pass, {counts.get('warn', 0)} warn, {counts.get('fail', 0)} fail"
    )
    return "\n".join(lines)


def _render_runs_table(runs: list[dict[str, Any]], storage: Storage) -> str:
    """将任务列表渲染为等宽对齐的人类可读表格（自动补充累计 USD 费用并对齐中英文宽度）。"""
    if not runs:
        return "暂无历史任务。运行 `agent-lab demo` 或 `agent-lab code` 开始体验。"

    use_color = bool(getattr(sys.stdout, "isatty", lambda: False)())
    rows: list[dict[str, str]] = []
    for item in runs:
        run_id = str(item.get("id") or "")
        meta = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        cost_val = item.get("cost_usd")
        if cost_val is None:
            cost_val = meta.get("cost_usd")
        if cost_val is None and run_id:
            full_doc = storage.get_run(run_id)
            if full_doc and isinstance(full_doc.get("metadata"), dict):
                cost_val = full_doc["metadata"].get("cost_usd", 0.0)
        try:
            cost_num = float(cost_val or 0.0)
        except (TypeError, ValueError):
            cost_num = 0.0
        cost_str = f"${cost_num:.4f}" if cost_num < 10 else f"${cost_num:.2f}"
        title = str(item.get("title") or meta.get("title") or item.get("prompt") or "").strip()
        if item.get("archived"):
            title = f"[archived] {title}"
        rows.append(
            {
                "id": run_id[:8],
                "status": str(item.get("status") or ""),
                "steps": str(int(item.get("steps") or 0)),
                "cost": cost_str,
                "mode": str(item.get("mode") or "agent"),
                "title": _truncate_display(title, 48),
            }
        )

    id_w = max(8, max(len(r["id"]) for r in rows))
    st_w = max(len("STATUS"), max(len(r["status"]) for r in rows))
    steps_w = max(len("STEPS"), max(len(r["steps"]) for r in rows))
    cost_w = max(len("COST"), max(len(r["cost"]) for r in rows))
    mode_w = max(len("MODE"), max(len(r["mode"]) for r in rows))

    def _color_status(st: str, padded: str) -> str:
        if not use_color:
            return padded
        color = {
            "completed": "\x1b[32m",
            "waiting_approval": "\x1b[33m",
            "running": "\x1b[36m",
            "queued": "\x1b[36m",
            "failed": "\x1b[31m",
            "budget_exceeded": "\x1b[31m",
            "cancelled": "\x1b[31m",
        }.get(st, "")
        return f"{color}{padded}\x1b[0m" if color else padded

    header = (
        f"{_pad_display('ID', id_w)}  "
        f"{_pad_display('STATUS', st_w)}  "
        f"{_pad_display('STEPS', steps_w, align='right')}  "
        f"{_pad_display('COST', cost_w, align='right')}  "
        f"{_pad_display('MODE', mode_w)}  "
        f"TITLE / PROMPT"
    )
    sep = "-" * min(110, max(len(header) + 16, 78))
    lines = [header, sep]
    for r in rows:
        st_padded = _pad_display(r["status"], st_w)
        lines.append(
            f"{_pad_display(r['id'], id_w)}  "
            f"{_color_status(r['status'], st_padded)}  "
            f"{_pad_display(r['steps'], steps_w, align='right')}  "
            f"{_pad_display(r['cost'], cost_w, align='right')}  "
            f"{_pad_display(r['mode'], mode_w)}  "
            f"{r['title']}"
        )
    lines.append(sep)
    lines.append(
        f"共 {len(rows)} 条（使用 `agent-lab code --resume <short_id>` 恢复，或 `agent-lab code -c` 继续最近会话）"
    )
    return "\n".join(lines)


def _resolve_run_by_id_or_prefix(storage: Storage, raw_id: str) -> dict[str, Any]:
    """按完整 `run_id` 或唯一短前缀（如前 6~8 位）查找完整任务记录；歧义时抛出明确错误。"""
    needle = raw_id.strip()
    if not needle:
        raise LookupError("run_id 不能为空")
    exact = storage.get_run(needle)
    if exact is not None:
        return exact
    lower_needle = needle.lower()
    candidates = [
        item
        for item in storage.list_runs(limit=1000, include_archived=True)
        if str(item.get("id") or "").lower().startswith(lower_needle)
    ]
    if not candidates:
        raise LookupError(f"未找到匹配 '{needle}' 的任务 run_id")
    if len(candidates) > 1:
        preview = ", ".join(
            f"{c['id'][:10]}({c.get('status', '?')})" for c in candidates[:5]
        )
        suffix = " ..." if len(candidates) > 5 else ""
        raise ValueError(
            f"前缀 '{needle}' 存在歧义，匹配到 {len(candidates)} 个任务：{preview}{suffix}；请提供更长前缀。"
        )
    full = storage.get_run(candidates[0]["id"])
    if full is None:
        raise LookupError(f"任务 '{candidates[0]['id']}' 已不存在")
    return full


def _find_continue_run(
    storage: Storage,
    base_ws: Path,
    proj_slug: str,
) -> tuple[dict[str, Any] | None, bool]:
    """查找当前工作区/project_slug 下最近一条未归档会话；无匹配时回退至全局最近未归档会话。"""
    summaries = storage.list_runs(limit=200, include_archived=False)
    if not summaries:
        return None, False
    resolved_base = base_ws.resolve()
    fallback_existing: dict[str, Any] | None = None
    fallback_first: dict[str, Any] | None = None

    for item in summaries:
        full = storage.get_run(item["id"])
        if full is None:
            continue
        if fallback_first is None:
            fallback_first = full
        meta = full.get("metadata") if isinstance(full.get("metadata"), dict) else {}
        ws_raw = meta.get("workspace")
        ws_path: Path | None = None
        if isinstance(ws_raw, str) and ws_raw.strip():
            with contextlib.suppress(OSError):
                ws_path = Path(ws_raw).resolve()
        ws_exists = ws_path is not None and ws_path.is_dir()
        if ws_exists and fallback_existing is None:
            fallback_existing = full

        matched = False
        if meta.get("project_slug") == proj_slug:
            matched = True
        elif ws_path is not None and ws_path == resolved_base:
            matched = True
        else:
            git_meta = meta.get("git")
            if isinstance(git_meta, dict) and git_meta.get("origin_repo"):
                with contextlib.suppress(OSError):
                    if Path(str(git_meta["origin_repo"])).resolve() == resolved_base:
                        matched = True
        if matched and ws_exists:
            return full, True

    return (fallback_existing or fallback_first), False


def _format_dotenv_value(value: str) -> str:
    """安全格式化写入 `.env` 的值，剔除换行符并在包含空格或 `#` 时自动双引号转义。"""
    clean = value.replace("\r", "").replace("\n", "").strip()
    if not clean:
        return ""
    if any(ch in clean for ch in (" ", "\t", "#", '"', "'")):
        escaped = clean.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return clean


def _render_init_dotenv(template_text: str, updates: dict[str, str]) -> str:
    """在保留 `.env.example` 中文注释与结构的前提下更新指定键值。"""
    remaining = dict(updates)
    out_lines: list[str] = []
    for raw_line in template_text.splitlines():
        parsed = _parse_dotenv_line(raw_line)
        if parsed is not None and parsed[0] in remaining:
            key = parsed[0]
            out_lines.append(f"{key}={_format_dotenv_value(remaining.pop(key))}")
        else:
            out_lines.append(raw_line)
    for key, val in remaining.items():
        out_lines.append(f"{key}={_format_dotenv_value(val)}")
    return "\n".join(out_lines) + "\n"


def _write_env_file_0600(target_path: Path, content: str) -> None:
    """原子创建 `0600` 权限 `.env` 文件（拒绝符号链接，避免密钥落地窗口泄露）。"""
    if target_path.is_symlink():
        raise ValueError(f"拒绝写入符号链接目标：{target_path}")
    tmp_path = target_path.parent / f".env.init.{uuid.uuid4().hex[:10]}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp_path, flags, 0o600)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        data = content.encode("utf-8")
        view = memoryview(data)
        written = 0
        while written < len(data):
            written += os.write(fd, view[written:])
        os.fsync(fd)
    except Exception:
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    else:
        os.close(fd)
    try:
        os.replace(tmp_path, target_path)
        os.chmod(target_path, 0o600)
    except Exception:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _run_init_command(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """交互或非交互式初始化 `.env`（0600 权限，默认不覆盖已有 `.env`）。"""
    try:
        target_dir = validate_direct_workspace_path(args.cwd)
    except ValueError as exc:
        parser.error(str(exc))

    env_path = target_dir / ".env"
    if env_path.is_symlink():
        parser.error(f"目标路径是符号链接，已拒绝写入：{env_path}")
    if env_path.exists() and not args.force:
        print(
            f"[跳过] .env 已存在：{env_path}\n"
            "为保护已有密钥与配置，默认不覆盖；如需重建请显式传入 `--force`。",
            file=sys.stderr,
        )
        raise SystemExit(1)

    example_path = ROOT / ".env.example"
    try:
        template_text = (
            example_path.read_text(encoding="utf-8") if example_path.is_file() else _DEFAULT_ENV_TEMPLATE
        )
    except OSError:
        template_text = _DEFAULT_ENV_TEMPLATE

    interactive = (not args.non_interactive) and bool(getattr(sys.stdin, "isatty", lambda: False)())
    provider = args.provider
    api_key = ""
    model = (args.model or "").strip()

    if interactive:
        print("Agent Systems Lab · 首次配置向导（<30 秒生成本地 0600 权限 .env）")
        try:
            if provider is None:
                choice = input(
                    "选择默认模型提供方 [1=demo (离线零密钥, 默认), 2=anthropic (Claude), 3=openai (OpenAI/兼容网关)]: "
                ).strip().lower()
                provider_map = {
                    "": "demo",
                    "1": "demo",
                    "demo": "demo",
                    "2": "anthropic",
                    "anthropic": "anthropic",
                    "3": "openai",
                    "openai": "openai",
                }
                provider = provider_map.get(choice)
                if provider is None:
                    parser.error(f"无效选项：{choice!r}（可选 1/demo、2/anthropic、3/openai）")
            if provider == "anthropic":
                try:
                    api_key = getpass.getpass("ANTHROPIC_API_KEY（不回显，留空可稍后编辑 .env）: ").strip()
                except Exception:
                    api_key = input("ANTHROPIC_API_KEY（留空可稍后编辑 .env）: ").strip()
                if not model:
                    model = input("ANTHROPIC_MODEL（填写账户可用模型 ID，留空可稍后填写）: ").strip()
            elif provider == "openai":
                try:
                    api_key = getpass.getpass("OPENAI_API_KEY（不回显，留空可稍后编辑 .env）: ").strip()
                except Exception:
                    api_key = input("OPENAI_API_KEY（留空可稍后编辑 .env）: ").strip()
                if not model:
                    model = input("OPENAI_MODEL（填写账户可用模型 ID，留空可稍后填写）: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n已取消初始化，未写入 .env。", file=sys.stderr)
            raise SystemExit(130)
    else:
        provider = provider or "demo"

    updates: dict[str, str] = {"AGENT_PROVIDER": provider}
    if provider == "anthropic":
        if api_key:
            updates["ANTHROPIC_API_KEY"] = api_key
        if model:
            updates["ANTHROPIC_MODEL"] = model
    elif provider == "openai":
        if api_key:
            updates["OPENAI_API_KEY"] = api_key
        if model:
            updates["OPENAI_MODEL"] = model

    rendered = _render_init_dotenv(template_text, updates)
    try:
        _write_env_file_0600(env_path, rendered)
    except (OSError, ValueError) as exc:
        parser.error(f"写入 .env 失败：{exc}")

    print(f"已创建 {env_path}（权限 0600，AGENT_PROVIDER={provider}）")
    if provider != "demo" and (not api_key or not model):
        print("提示：请按需补全 .env 中的密钥与模型 ID（切勿将 .env 提交至 Git）。")
    print("下一步：")
    print("  1. uv run agent-lab doctor   # 检查环境、密钥、Docker/沙箱与权限")
    print("  2. uv run agent-lab code     # 启动交互式研发工作台")
    print("  3. uv run agent-lab serve    # 启动本地 Web 控制面 (http://127.0.0.1:8765)")


def show(value):
    """以可读 UTF-8 缩进 JSON 向标准输出打印命令执行回执，便于管道脚本与人工排查。"""
    print(json.dumps(value, ensure_ascii=False, indent=2))


async def with_runtime(runtime, operation):
    """包裹单次异步 Runtime 操作并在退出前必定调用 `aclose()` 回收外部 MCP 会话与后台句柄。"""
    try:
        return await operation
    finally:
        await runtime.aclose()


async def _serve_worker_gracefully(daemon: LeaseWorker, poll_interval: float) -> None:
    """运行持久队列 Worker 主循环，并将进程中断信号转化为无损归还租约的协作式停机。"""
    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()
    main_task = asyncio.create_task(
        daemon.serve_forever(poll_interval=poll_interval, stop_event=stop_event)
    )
    def _on_signal() -> None:
        # 先置位 stop_event 停止后续轮询，再取消当前阻塞在执行/休眠中的协程以触发优雅重入队。
        stop_event.set()
        if not main_task.done():
            main_task.cancel()

    registered_signals: list[signal.Signals] = []
    previous_handlers: list[tuple[signal.Signals, object]] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
            registered_signals.append(sig)
        except (NotImplementedError, RuntimeError):
            # Windows 事件循环或非主线程环境不支持 loop.add_signal_handler 时，平滑回退标准信号钩子。
            prev = signal.getsignal(sig)
            previous_handlers.append((sig, prev))
            signal.signal(sig, lambda _s, _f: loop.call_soon_threadsafe(_on_signal))
    try:
        with contextlib.suppress(asyncio.CancelledError):
            await main_task
    finally:
        for sig in registered_signals:
            with contextlib.suppress(NotImplementedError, RuntimeError):
                loop.remove_signal_handler(sig)
        for sig, prev in previous_handlers:
            with contextlib.suppress(Exception):
                signal.signal(sig, prev)


def main():
    """解析 CLI 子命令并分发至控制面服务、队列 Worker、交互工作台、单次运行或离线评测。"""
    parser = argparse.ArgumentParser(description="Agent Systems Lab · 代码 Agent 学习工程")
    parser.add_argument("--state", type=Path, default=ROOT / ".agent-lab")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor_cmd = sub.add_parser(
        "doctor",
        help="检查 .env/密钥就绪度、多协议网关与 A2A 对端配置、Docker、OS 内核沙箱、LSP、Python 可选依赖与 macOS TCC 权限",
    )
    doctor_cmd.add_argument("--cwd", type=Path, default=Path("."), help="目标工作目录（默认当前目录）")
    doctor_cmd.add_argument("--gateway-config", type=Path, default=None, help="可选：显式指定要体检的 gateways.json 路径")
    doctor_cmd.add_argument("--a2a-config", type=Path, default=None, help="可选：显式指定要体检的 a2a.json 路径")
    doctor_cmd.add_argument("--json", action="store_true", help="以 JSON 格式输出结构化体检报告")

    init_cmd = sub.add_parser("init", help="30 秒引导创建 0600 权限本地 .env 配置文件")
    init_cmd.add_argument("--cwd", type=Path, default=Path("."), help="生成 .env 的目标目录（默认当前目录）")
    init_cmd.add_argument(
        "--provider",
        choices=["anthropic", "openai", "demo"],
        default=None,
        help="默认模型提供方（demo / anthropic / openai）",
    )
    init_cmd.add_argument("--model", default=None, help="可选：写入对应的默认模型 ID")
    init_cmd.add_argument(
        "--non-interactive",
        action="store_true",
        help="非交互模式直接从 .env.example 生成 .env，不提示终端输入",
    )
    init_cmd.add_argument("--force", action="store_true", help="允许覆盖已存在的 .env 文件")

    gateway_cmd = sub.add_parser(
        "gateway",
        help="管理声明式多协议模型网关配置（list / use / init；支持 Anthropic / OpenAI Chat / Responses / Gemini）",
    )
    gateway_sub = gateway_cmd.add_subparsers(dest="gateway_command", required=True)
    gateway_list = gateway_sub.add_parser(
        "list",
        help="列出已配置的网关 Profile、活跃标记、线协议、模型、端点与脱敏凭证就绪状态",
    )
    gateway_list.add_argument("--config", type=Path, default=None, help="显式指定 gateways.json 配置文件路径")
    gateway_list.add_argument("--json", action="store_true", help="以脱敏 JSON 格式输出网关配置")

    gateway_use = gateway_sub.add_parser(
        "use",
        help="原子切换 gateways.json 中的默认 active_profile",
    )
    gateway_use.add_argument("profile", help="目标网关 Profile 名称")
    gateway_use.add_argument("--config", type=Path, default=None, help="显式指定 gateways.json 配置文件路径")
    gateway_use.add_argument("--json", action="store_true", help="以脱敏 JSON 格式输出切换回执")

    gateway_init = gateway_sub.add_parser(
        "init",
        help="生成涵盖全协议模板的 0600 权限起始 gateways.json 文件",
    )
    gateway_init.add_argument("--path", type=Path, default=None, help="目标文件路径（默认 <state_dir>/gateways.json）")
    gateway_init.add_argument("--force", action="store_true", help="允许覆盖已存在的 gateways.json 文件")
    gateway_init.add_argument("--json", action="store_true", help="以脱敏 JSON 格式输出初始化回执")

    a2a_cmd = sub.add_parser(
        "a2a",
        help="管理 A2A（Agent2Agent）双向协议对端配置、AgentCard 发现与远端任务委托（init / list / card / send）",
    )
    a2a_sub = a2a_cmd.add_subparsers(dest="a2a_command", required=True)
    a2a_init = a2a_sub.add_parser(
        "init",
        help="生成 0600 权限起始 a2a.json 对端智能体配置文件",
    )
    a2a_init.add_argument("--path", type=Path, default=None, help="目标文件路径（默认 <state_dir>/a2a.json）")
    a2a_init.add_argument("--force", action="store_true", help="允许覆盖已存在的 a2a.json 文件")
    a2a_init.add_argument("--json", action="store_true", help="以脱敏 JSON 格式输出初始化回执")

    a2a_list = a2a_sub.add_parser(
        "list",
        help="列出已配置的远端 A2A 对端智能体（全程脱敏，不回显密钥）",
    )
    a2a_list.add_argument("--config", type=Path, default=None, help="显式指定 a2a.json 配置文件路径")
    a2a_list.add_argument("--json", action="store_true", help="以脱敏 JSON 格式输出 A2A 对端配置")

    a2a_card = a2a_sub.add_parser(
        "card",
        help="无 --url 时输出本节点 AgentCard JSON；传 --url 时探测并输出远端 AgentCard",
    )
    a2a_card.add_argument("--url", default=None, help="可选：远端 A2A Agent URL 或已配置的 peer 名称")
    a2a_card.add_argument(
        "--base-url",
        default="http://127.0.0.1:8787",
        help="输出本节点 AgentCard 时使用的服务基址（默认 http://127.0.0.1:8787）",
    )
    a2a_card.add_argument("--config", type=Path, default=None, help="可选：显式指定 a2a.json 配置文件路径")
    a2a_card.add_argument(
        "--allow-private",
        action="store_true",
        help="允许通过 --url 直接访问回环/私网地址的远端 AgentCard",
    )
    a2a_card.add_argument("--extended", action="store_true", help="输出本节点扩展 AgentCard 元数据")

    a2a_send = a2a_sub.add_parser(
        "send",
        help="通过 A2A JSON-RPC 2.0 向已配置对端智能体委托任务",
    )
    a2a_send.add_argument("peer", help="在 a2a.json 中配置的远端对端名称")
    a2a_send.add_argument("prompt", help="要委托给远端智能体的任务描述")
    a2a_send.add_argument("--config", type=Path, default=None, help="显式指定 a2a.json 配置文件路径")
    a2a_send.add_argument("--task-id", default=None, help="可选：继续已有远端任务时传入 task_id")
    a2a_send.add_argument("--context-id", default=None, help="可选：跨轮次共享的 A2A contextId")
    a2a_send.add_argument(
        "--async",
        dest="async_mode",
        action="store_true",
        help="非阻塞提交任务（blocking=false）",
    )

    horizon_cmd = sub.add_parser(
        "horizon",
        help="管理多会话超长程 Horizon 任务编排与架构边界检查（init / list / status / step / lint）",
    )
    horizon_sub = horizon_cmd.add_subparsers(dest="horizon_command", required=True)

    horizon_init = horizon_sub.add_parser(
        "init",
        help="初始化 Horizon Campaign（生成不可变基线 feature_list.json、init.sh 与 progress.md）",
    )
    horizon_init.add_argument("--goal", required=True, help="超长程任务的总体目标描述")
    horizon_init.add_argument(
        "--feature",
        action="append",
        required=True,
        help='特性规格，格式 "F001:Title:Description"（可重复传入多个）',
    )
    horizon_init.add_argument(
        "--smoke-cmd",
        default="pytest -q",
        help="写入 init.sh 的基线冒烟命令（默认 pytest -q）",
    )
    horizon_init.add_argument("--cwd", type=Path, default=Path("."), help="目标工作目录（默认当前目录）")
    horizon_init.add_argument("--campaign-id", default=None, help="可选：自定义 Campaign ID")
    horizon_init.add_argument("--json", action="store_true", help="以 JSON 格式输出初始化回执")

    horizon_list = horizon_sub.add_parser(
        "list",
        help="列出工作区下的所有 Horizon Campaign 及特性通过进度",
    )
    horizon_list.add_argument("--cwd", type=Path, default=Path("."), help="目标工作目录（默认当前目录）")
    horizon_list.add_argument("--json", action="store_true", help="以 JSON 格式输出 Campaign 列表")

    horizon_status = horizon_sub.add_parser(
        "status",
        help="查看指定 Horizon Campaign 的完整特征账本、收据与交接进度摘要",
    )
    horizon_status.add_argument("campaign_id", help="目标 Horizon Campaign ID")
    horizon_status.add_argument("--cwd", type=Path, default=Path("."), help="目标工作目录（默认当前目录）")
    horizon_status.add_argument("--json", action="store_true", help="以 JSON 格式输出完整 Campaign 状态")

    horizon_step = horizon_sub.add_parser(
        "step",
        help="以全新干净上下文窗口执行单步 Horizon 增量会话并更新交接账本",
    )
    horizon_step.add_argument("campaign_id", help="目标 Horizon Campaign ID")
    horizon_step.add_argument("--cwd", type=Path, default=Path("."), help="目标工作目录（默认当前目录）")
    horizon_step.add_argument(
        "--provider",
        choices=list(_CLI_PROVIDER_CHOICES),
        default=None,
        help="模型提供方（demo / anthropic / openai / openai_compat / openai_responses / gemini）",
    )
    horizon_step.add_argument("--gateway", default=None, help="可选：使用 gateways.json 中指定的模型网关 Profile")
    horizon_step.add_argument("--gateway-config", type=Path, default=None, help="可选：显式指定 gateways.json 配置文件路径")
    horizon_step.add_argument(
        "--permission-mode",
        choices=["ask", "auto-edit", "full-auto", "plan"],
        default=None,
        help="可选：权限控制档位（默认 auto-edit）",
    )
    horizon_step.add_argument(
        "--run-smoke",
        action="store_true",
        help="启动增量会话前先执行 init.sh 基线冒烟检查",
    )
    horizon_step.add_argument(
        "--verify",
        action="store_true",
        help="会话结束后运行目标特性的 verification_cmd 验收命令",
    )
    horizon_step.add_argument("--json", action="store_true", help="以 JSON 格式输出单步执行结果")

    horizon_lint = horizon_sub.add_parser(
        "lint",
        help="运行机械化架构边界检查（.agent-lab/invariants.json），存在违规时退出码为 1 并输出修复指引",
    )
    horizon_lint.add_argument("--cwd", type=Path, default=Path("."), help="目标工作目录（默认当前目录）")
    horizon_lint.add_argument("--json", action="store_true", help="以 JSON 格式输出结构化违规诊断")

    index_cmd = sub.add_parser(
        "index",
        help="刷新持久化 Merkle-Tree 代码库语义索引并打印根哈希与增量统计（传 --query 执行混合语义检索）",
    )
    index_cmd.add_argument("--cwd", type=Path, default=Path("."), help="目标代码库目录（默认当前目录）")
    index_cmd.add_argument(
        "--query",
        default=None,
        help="可选：刷新索引后执行 BM25 + 稠密向量 RRF 混合语义检索",
    )
    index_cmd.add_argument(
        "--limit",
        type=int,
        default=8,
        help="混合语义检索返回的最大代码块数量（默认 8，上限 50）",
    )
    index_cmd.add_argument(
        "--path-prefix",
        default=None,
        help="可选：限定检索的工作区相对路径前缀",
    )
    index_cmd.add_argument("--json", action="store_true", help="以 JSON 格式输出索引刷新与检索结果")

    serve = sub.add_parser("serve", help="启动本机网页")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--dispatch",
        choices=["inline", "queue"],
        default=None,
        help="任务调度方式：inline 为单进程协程，queue 由独立 agent-lab worker 消费",
    )
    serve.add_argument("--gateway", default=None, help="指定声明式模型网关 Profile（覆盖 gateways.json 默认 active_profile）")
    serve.add_argument("--gateway-config", type=Path, default=None, help="显式指定 gateways.json 配置文件路径")

    worker = sub.add_parser("worker", help="启动持久队列 Worker（支持租约续期与 epoch fencing）")
    worker.add_argument("--worker-id", default=None)
    worker.add_argument("--lease-ttl", type=float, default=15.0)
    worker.add_argument("--poll-interval", type=float, default=0.8)
    worker.add_argument("--once", action="store_true", help="仅尝试消费队列中的一个任务后退出")
    worker.add_argument(
        "--metrics-port",
        type=int,
        default=None,
        help="可选：开启独立 Worker Prometheus /metrics 与 /healthz 监控 HTTP 端口（0 表示自动分配）",
    )
    worker.add_argument(
        "--metrics-host",
        default="127.0.0.1",
        help="Worker 监控 HTTP 服务绑定地址（默认 127.0.0.1）",
    )
    worker.add_argument("--gateway", default=None, help="强制使用指定声明式模型网关 Profile")
    worker.add_argument("--gateway-config", type=Path, default=None, help="显式指定 gateways.json 配置文件路径")

    run = sub.add_parser("run", help="运行任务（默认隔离沙盒；传 --cwd 启用本地目录直连操控）")
    run.add_argument("prompt")
    run.add_argument(
        "--provider",
        choices=list(_CLI_PROVIDER_CHOICES),
        default=None,
        help="模型提供方（demo / anthropic / openai / openai_compat / openai_responses / gemini）",
    )
    run.add_argument("--gateway", default=None, help="使用 gateways.json 中指定的模型网关 Profile（与 --provider 互斥）")
    run.add_argument("--gateway-config", type=Path, default=None, help="显式指定 gateways.json 配置文件路径")
    run.add_argument("--mode", choices=["agent", "workflow", "parallel"], default="agent")
    run.add_argument("--template", choices=["sample", "multi_file"], default="sample")
    run.add_argument("--repo", type=Path, default=None, help="指定本地真实 Git 仓库，基于其创建独立 worktree")
    run.add_argument("--cwd", type=Path, default=None, help="直接操作指定本地目录（启用终端命令与全功能文件读写）")
    run.add_argument(
        "--profile",
        choices=["sandbox", "host", "computer"],
        default=None,
        help="执行能力档位（--cwd 时默认 host，开启 --gui 时自动升为 computer）",
    )
    run.add_argument(
        "--permission-mode",
        choices=["ask", "auto-edit", "full-auto"],
        default=None,
        help="权限控制策略：ask / auto-edit / full-auto",
    )
    run.add_argument("--gui", action="store_true", help="启用桌面级 GUI 截图与键鼠操控工具")
    run.add_argument("--worktree", action="store_true", help="与 --cwd 配合：基于本地 Git 仓库创建独立并发 worktree 以免多个 Agent 互相覆盖未提交修改")
    run.add_argument("--mcp-config", type=Path, default=None, help="外部 MCP Server 配置文件路径")
    run.add_argument("--no-auto-dream", action="store_true", help="禁用本次任务结束后的默认自动 Dreaming 整理")
    run.add_argument("--ref", default="HEAD", help="Git 基线分支或 commit SHA，默认 HEAD")
    run.add_argument("--max-repair-rounds", type=int, default=2)

    for repl_cmd in ("code", "chat"):
        code_p = sub.add_parser(
            repl_cmd,
            help="启动类似 Claude Code / Codex 的本地目录交互式多轮工作台",
        )
        code_p.add_argument("prompt", nargs="?", default=None, help="可选首轮任务指令")
        code_p.add_argument("--cwd", type=Path, default=Path("."), help="直接操作的本地工作目录（默认当前目录）")
        code_p.add_argument(
            "--provider",
            choices=list(_CLI_PROVIDER_CHOICES),
            default=None,
            help="模型提供方（demo / anthropic / openai / openai_compat / openai_responses / gemini）",
        )
        code_p.add_argument("--gateway", default=None, help="使用 gateways.json 中指定的模型网关 Profile（与 --provider 互斥）")
        code_p.add_argument("--gateway-config", type=Path, default=None, help="显式指定 gateways.json 配置文件路径")
        code_p.add_argument("--mode", choices=["agent", "workflow", "parallel"], default="agent")
        code_p.add_argument(
            "--permission-mode",
            choices=["ask", "auto-edit", "full-auto"],
            default=None,
            help="三级权限档位（默认 auto-edit；恢复会话时默认沿用原会话权限）",
        )
        code_p.add_argument("--gui", action="store_true", help="同时启用桌面级 GUI 操控（Computer Use）")
        code_p.add_argument("--worktree", action="store_true", help="基于当前 Git 仓库创建独立并发 worktree（适合同时开多个终端跑多个 Agent）")
        code_p.add_argument("--mcp-config", type=Path, default=None, help="外部 MCP Server 配置文件路径")
        code_p.add_argument("--no-auto-dream", action="store_true", help="禁用每轮会话结束后的默认自动 Dreaming")
        code_p.add_argument(
            "-c",
            "--continue",
            dest="continue_latest",
            action="store_true",
            help="自动恢复当前工作区最近一次未归档会话（无匹配时回退至全局最近会话）",
        )
        code_p.add_argument(
            "--resume",
            default=None,
            help="恢复指定的已有会话 run_id（支持 6~8 位等唯一 UUID 短前缀）",
        )

    skill_cmd = sub.add_parser("skill", help="管理本地分层技能库（list / install / remove）")
    skill_sub = skill_cmd.add_subparsers(dest="skill_command", required=True)
    skill_list = skill_sub.add_parser(
        "list",
        help="列出当前生效的分层技能目录（项目级 .claude/skills > 用户级 <state>/skills > 内置级）",
    )
    skill_list.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="项目工作目录（用于发现 <cwd>/.claude/skills，默认当前目录）",
    )
    skill_install = skill_sub.add_parser(
        "install",
        help="校验并从本地目录原子安装 Skill（写入 SHA-256 .manifest.json）",
    )
    skill_install.add_argument("source_dir", type=Path, help="待安装的 Skill 源目录路径")
    skill_install.add_argument(
        "--target",
        type=Path,
        default=None,
        help="目标技能根目录（默认 <state>/skills）",
    )
    skill_remove = skill_sub.add_parser(
        "remove",
        help="安全卸载目标根目录下的指定 Skill 并更新 .manifest.json",
    )
    skill_remove.add_argument("name", help="要卸载的 Skill 名称")
    skill_remove.add_argument(
        "--target",
        type=Path,
        default=None,
        help="目标技能根目录（默认 <state>/skills）",
    )

    dream_cmd = sub.add_parser(
        "dream",
        help="运行离线 Dreaming 引擎（对照历史会话 Transcript 纠错、合成互链 Wiki、修复断链并整理 MEMORY.md 索引）",
    )
    dream_cmd.add_argument("--cwd", type=Path, default=Path("."), help="目标项目工作目录（默认当前目录）")
    dream_cmd.add_argument("--run-id", default=None, help="可选：针对指定会话 run_id 进行重放整理")
    dream_cmd.add_argument("--goal", default=None, help="可选：指定本次记忆整理的聚焦目标（Focus Goal）")
    dream_cmd.add_argument(
        "--provider",
        choices=list(_CLI_PROVIDER_CHOICES),
        default=None,
        help="可选：使用真实模型辅助语义合并（默认自动检测已配置 API Key）",
    )
    dream_cmd.add_argument("--gateway", default=None, help="可选：使用 gateways.json 中指定的模型网关 Profile")
    dream_cmd.add_argument("--gateway-config", type=Path, default=None, help="显式指定 gateways.json 配置文件路径")
    dream_cmd.add_argument(
        "--dry-run",
        action="store_true",
        help="零落盘预演 Dreaming 变更并打印计划 Diff 摘要，不修改任何文件",
    )
    dream_cmd.add_argument(
        "--rollback",
        nargs="?",
        const="",
        default=None,
        metavar="SNAPSHOT_ID",
        help="回滚最近一次（或指定 snapshot_id）Dreaming 快照",
    )
    demo = sub.add_parser("demo", help="运行内置折扣 bug 场景")
    demo.add_argument("--auto-approve", action="store_true", help="显式授权应用内置示例补丁，仅用于此示例")
    demo.add_argument("--mode", choices=["agent", "workflow", "parallel"], default="agent")
    demo.add_argument("--template", choices=["sample", "multi_file"], default="sample")
    approve = sub.add_parser("approve", help="审批保存的补丁并恢复任务")
    approve.add_argument("run_id")
    approve.add_argument("proposal_id")
    approve.add_argument("--reject", action="store_true")
    resume = sub.add_parser("resume", help="恢复中断/失败任务，不重置预算")
    resume.add_argument("run_id")
    cancel = sub.add_parser("cancel", help="请求取消排队、等待审批或正在运行的任务")
    cancel.add_argument("run_id")
    cancel.add_argument("--reason", default="cli_cancel", help="取消原因")
    gc_cmd = sub.add_parser("gc", help="回收过期终态工作区、归档快照、陈旧锁与孤儿沙箱容器")
    gc_cmd.add_argument("--retention-seconds", type=float, default=86400.0)
    list_cmd = sub.add_parser("list", help="查看本机任务（默认对齐表格；支持 --json）")
    list_cmd.add_argument("--json", action="store_true", help="以 JSON 数组输出（适合脚本解析）")
    list_cmd.add_argument("--table", action="store_true", help="强制以对齐表格输出")
    list_cmd.add_argument("--limit", type=int, default=20, help="最多显示条数（默认 20，最大 1000）")
    list_cmd.add_argument("--all", dest="include_archived", action="store_true", help="同时显示已归档会话")
    trace = sub.add_parser("trace", help="查看持久化事件")
    trace.add_argument("run_id")
    calls = sub.add_parser("calls", help="查看持久化工具调用状态摘要")
    calls.add_argument("run_id")
    result = sub.add_parser("result", help="按调用 ID 分页查看完整结果 JSON 文本与哈希")
    result.add_argument("run_id")
    result.add_argument("call_id")
    result.add_argument("--offset", type=int, default=0)
    result.add_argument("--limit", type=int, default=4000)
    trigger_cmd = sub.add_parser(
        "trigger",
        help="管理自主定时（Cron）与外部 Webhook 触发器（create / list / delete / tick）",
    )
    trigger_sub = trigger_cmd.add_subparsers(dest="trigger_command", required=True)

    trigger_create = trigger_sub.add_parser(
        "create",
        help="创建一条持久化 Cron 或 Webhook 触发器定义",
    )
    trigger_create.add_argument("--name", required=True, help="触发器名称")
    trigger_create.add_argument(
        "--kind",
        choices=["cron", "webhook"],
        required=True,
        help="触发器类型：cron 或 webhook",
    )
    trigger_create.add_argument(
        "--cron",
        default=None,
        help='5 字段 Cron 表达式（kind=cron 时必填，如 "*/15 * * * *"）',
    )
    trigger_create.add_argument(
        "--prompt",
        default=None,
        help="触发执行时使用的提示词模板",
    )
    trigger_create.add_argument(
        "--secret-env",
        default=None,
        help="可选：从指定环境变量读取 Webhook HMAC-SHA256 签名密钥（不回显明文）",
    )
    trigger_create.add_argument(
        "--cwd",
        type=Path,
        default=None,
        help="可选：绑定触发器运行的本地工作目录",
    )
    trigger_create.add_argument(
        "--timezone",
        default=None,
        help="可选：Cron 触发时区（IANA 名称，如 UTC、Asia/Shanghai）",
    )
    trigger_create.add_argument(
        "--disabled",
        action="store_true",
        help="创建时默认禁用该触发器",
    )
    trigger_create.add_argument("--json", action="store_true", help="以脱敏 JSON 格式输出结果")

    trigger_list = trigger_sub.add_parser(
        "list",
        help="列出已配置的持久化触发器（始终脱敏 webhook_secret）",
    )
    trigger_list.add_argument("--enabled-only", action="store_true", help="仅显示已启用的触发器")
    trigger_list.add_argument("--json", action="store_true", help="以脱敏 JSON 格式输出触发器列表")

    trigger_delete = trigger_sub.add_parser(
        "delete",
        help="删除指定触发器及其历史触发记录",
    )
    trigger_delete.add_argument("trigger_id", help="要删除的触发器 ID（支持唯一短前缀）")
    trigger_delete.add_argument("--json", action="store_true", help="以 JSON 格式输出删除回执")

    trigger_tick = trigger_sub.add_parser(
        "tick",
        help="执行单次 Cron TriggerScheduler.tick_once() 扫描并派发到期触发器",
    )
    trigger_tick.add_argument(
        "--now",
        default=None,
        help="可选：指定 ISO-8601 UTC 时间戳用于确定性调度扫描（默认当前 UTC 时间）",
    )
    trigger_tick.add_argument(
        "--provider",
        choices=list(_CLI_PROVIDER_CHOICES),
        default=None,
        help="可选：派发任务时使用的模型提供方",
    )
    trigger_tick.add_argument("--gateway", default=None, help="可选：使用 gateways.json 中指定的模型网关 Profile")
    trigger_tick.add_argument("--gateway-config", type=Path, default=None, help="可选：显式指定 gateways.json 配置文件路径")
    trigger_tick.add_argument(
        "--enqueue-only",
        action="store_true",
        help="仅创建并入队任务供 Worker 消费，不在本次 tick 中内联执行",
    )
    trigger_tick.add_argument("--json", action="store_true", help="以 JSON 格式输出本拍触发结果")

    evaluate = sub.add_parser("eval", help="运行隔离离线评测、失败轨迹回归采集与单轨迹四维打分")
    evaluate.add_argument("--report", type=Path, default=ROOT / "reports/eval.json")
    evaluate.add_argument("--trials", type=int, default=1)
    evaluate.add_argument(
        "--suite",
        choices=["core", "subsystems", "all"],
        default="core",
        help="评测套件范围：core（核心回归，默认）、subsystems（7 大子系统契约评测）或 all（全量）",
    )
    evaluate.add_argument(
        "--harvest",
        action="store_true",
        help="扫描近期失败/高摩擦轨迹并脱敏落盘为离线回归用例",
    )
    evaluate.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="配合 --harvest 使用：回归用例与 manifest.json 输出目录（默认 <state>/harvested_evals）",
    )
    evaluate.add_argument(
        "--limit",
        type=int,
        default=20,
        help="配合 --harvest 使用：最多扫描的近期运行轨迹条数（默认 20）",
    )
    evaluate.add_argument(
        "--min-friction",
        type=int,
        default=1,
        help="配合 --harvest 使用：纳入回归集的最小摩擦信号数阈值（默认 1）",
    )
    evaluate.add_argument(
        "--grade-run",
        default=None,
        help="对指定 run_id（支持唯一短前缀）的运行轨迹执行四维打分并输出 JSON",
    )
    model_eval = sub.add_parser("model-eval", help="运行 5 任务真实仓库编码基准评测（Docker 真值 + 失败分类）")
    model_eval.add_argument("--provider", choices=["anthropic", "openai", "reference"], default="anthropic")
    model_eval.add_argument("--report", type=Path, default=ROOT / "reports/model-eval.json")
    model_eval.add_argument("--trials", type=int, default=1)
    model_eval.add_argument("--task", action="append", default=None, help="仅运行指定 task_id，可重复传入")
    model_eval.add_argument("--max-repair-rounds", type=int, default=2)
    model_eval.add_argument("--skip-baseline-check", action="store_true")
    kv_bench = sub.add_parser("kv-benchmark", help="运行 KV Cache 前缀命中率与阶梯压缩离线消融实验")
    kv_bench.add_argument("--turns", type=int, default=16)
    kv_bench.add_argument("--max-chars", type=int, default=6800)
    kv_bench.add_argument("--block-chars", type=int, default=64)
    kv_bench.add_argument("--report", type=Path, default=None)
    args = parser.parse_args()
    dotenv_cwd: Path | None = None
    raw_cwd_arg = getattr(args, "cwd", None)
    if isinstance(raw_cwd_arg, Path):
        with contextlib.suppress(OSError):
            if raw_cwd_arg.is_dir():
                dotenv_cwd = raw_cwd_arg
    loaded_dotenv_paths = _autoload_dotenv(dotenv_cwd)

    if args.command == "doctor":
        doctor_cwd = args.cwd
        if not doctor_cwd.exists() or not doctor_cwd.is_dir():
            parser.error(f"--cwd 目录不存在或不是目录：{doctor_cwd}")
        report = _collect_doctor_report(
            doctor_cwd,
            loaded_dotenv_paths=loaded_dotenv_paths,
            state_dir=args.state.resolve(),
            gateway_config=args.gateway_config,
            a2a_config=args.a2a_config,
        )
        if args.json:
            show(report)
        else:
            print(_render_doctor_table(report))
        return
    if args.command == "init":
        _run_init_command(args, parser)
        return
    if args.command == "gateway":
        _run_gateway_command(args, parser)
        return
    if args.command == "a2a":
        _run_a2a_command(args, parser)
        return
    if args.command == "horizon":
        _run_horizon_command(args, parser)
        return
    if args.command == "index":
        _run_index_command(args, parser)
        return

    args.state = args.state.resolve()
    args.state.mkdir(parents=True, exist_ok=True)
    if args.command == "serve":
        # 安全门禁：非回环监听会向局域网/容器网桥暴露控制面，未配置 Bearer 令牌时默认拒绝启动。
        non_loopback = args.host not in {"127.0.0.1", "localhost", "::1"}
        has_auth = bool(
            os.environ.get("AGENT_LAB_TOKEN", "").strip()
            or os.environ.get("AGENT_LAB_TOKENS_JSON", "").strip()
        )
        allow_insecure = os.environ.get("AGENT_LAB_ALLOW_INSECURE_LOCAL", "").strip() == "1"
        if non_loopback:
            if not has_auth and not allow_insecure:
                parser.error(
                    "监听非回环地址必须设置 AGENT_LAB_TOKEN 或 AGENT_LAB_TOKENS_JSON"
                    "（仅限宿主端 127.0.0.1 端口映射时可显式设置 AGENT_LAB_ALLOW_INSECURE_LOCAL=1）"
                )
            if not allow_insecure:
                os.environ["AGENT_LAB_REQUIRE_AUTH"] = "1"
        tenant_id = _cli_gateway_tenant_id(args.state)
        gw_config = getattr(args, "gateway_config", None)
        gw_profile = getattr(args, "gateway", None)
        if gw_config is not None:
            os.environ["AGENT_LAB_GATEWAYS_FILE"] = str(gw_config.expanduser().resolve())
        env_gw = os.environ.get("AGENT_GATEWAY_PROFILE", "").strip()
        explicit_env_provider = os.environ.get("AGENT_PROVIDER", "").strip()
        should_use_gateway = bool(gw_profile or gw_config is not None or env_gw)
        if not should_use_gateway and not explicit_env_provider:
            try:
                cfg_doc = load_gateways_config(
                    explicit_path=gw_config,
                    state_dir=args.state,
                    tenant_id=tenant_id,
                )
                if cfg_doc.get("active_profile"):
                    should_use_gateway = True
            except ValueError as exc:
                parser.error(f"gateways 配置校验失败：{exc}")
        if should_use_gateway:
            gw_spec: str | None = None
            if gw_profile:
                raw_gw = gw_profile.strip()
                gw_spec = raw_gw if raw_gw.startswith("gateway:") else f"gateway:{raw_gw}"
            try:
                probe_prov, gw_info = resolve_provider_or_gateway(
                    gw_spec,
                    state_dir=args.state,
                    explicit_config_path=gw_config,
                    tenant_id=tenant_id,
                )
            except (ValueError, RuntimeError, FileNotFoundError, NotImplementedError) as exc:
                if gw_profile or gw_config is not None or env_gw:
                    parser.error(f"解析模型网关失败：{exc}")
                print(
                    f"[提示] gateways.json active_profile 暂未就绪（{exc}），"
                    "Web 服务暂以离线 demo 模式启动；补全凭证或在网页切换网关后生效。",
                    file=sys.stderr,
                )
            else:
                _close_provider_sync(probe_prov)
                resolved_prof = gw_info.get("profile")
                if resolved_prof:
                    os.environ["AGENT_GATEWAY_PROFILE"] = str(resolved_prof)
                    os.environ["AGENT_PROVIDER"] = f"gateway:{resolved_prof}"
                elif gw_info.get("protocol"):
                    os.environ["AGENT_PROVIDER"] = str(gw_info["protocol"])
                found_cfg = discover_gateways_config_path(
                    explicit_path=gw_config,
                    state_dir=args.state,
                    tenant_id=tenant_id,
                )
                if found_cfg is not None:
                    os.environ.setdefault("AGENT_LAB_GATEWAYS_FILE", str(found_cfg))
                print(
                    f"[网关] profile={resolved_prof or '(direct)'} | "
                    f"protocol={gw_info.get('protocol')} | "
                    f"model={gw_info.get('model') or '-'} | "
                    f"base_url={gw_info.get('base_url') or '(offline)'}",
                    file=sys.stderr,
                )
        elif not explicit_env_provider:
            detected_provider = _detect_default_cli_provider()
            ready, missing_vars, _model = _provider_env_readiness(detected_provider)
            if detected_provider != "demo" and ready:
                os.environ["AGENT_PROVIDER"] = detected_provider
            elif detected_provider != "demo" and not ready:
                print(
                    f"[提示] 检测到 {detected_provider} 密钥但缺少 {' / '.join(missing_vars)}，"
                    "Web 服务暂以离线 demo 模式启动；补全 .env 后重启即可启用真实模型。",
                    file=sys.stderr,
                )
        os.environ["AGENT_LAB_STATE"] = str(args.state)
        if args.dispatch:
            os.environ["AGENT_LAB_DISPATCH"] = args.dispatch
        import uvicorn

        uvicorn.run("agent_lab.api:create_app", factory=True, host=args.host, port=args.port)
        return
    if args.command == "kv-benchmark":
        report = simulate_kv_cache_ablation(
            turns=args.turns, max_chars=args.max_chars, block_chars=args.block_chars
        )
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        show(report)
        if report["optimized"]["cumulative_hit_rate"] <= report["legacy"]["cumulative_hit_rate"]:
            raise SystemExit(1)
        return
    if args.command == "skill":
        from .skills import SkillCatalog, install_skill_from_dir, remove_skill

        if args.skill_command == "list":
            try:
                cwd_path = validate_direct_workspace_path(args.cwd)
            except ValueError as exc:
                parser.error(str(exc))
            extra_dirs = [cwd_path / ".claude" / "skills", args.state / "skills"]
            catalog = SkillCatalog(ROOT / "skills", extra_dirs=extra_dirs)
            show(
                {
                    "ok": True,
                    "roots": [str(r) for r in catalog.roots],
                    **catalog.catalog_status(),
                }
            )
            return
        target_root = args.target if args.target is not None else (args.state / "skills")
        if args.skill_command == "install":
            try:
                installed = install_skill_from_dir(args.source_dir, target_root)
            except ValueError as exc:
                parser.error(str(exc))
            show(installed)
            return
        if args.skill_command == "remove":
            try:
                removed = remove_skill(args.name, target_root)
            except ValueError as exc:
                parser.error(str(exc))
            show(removed)
            return
    if args.command == "eval":
        if args.harvest and args.grade_run:
            parser.error("--harvest 与 --grade-run 不能同时使用")
        if args.grade_run:
            storage = Storage(args.state / "state.db")
            try:
                target = _resolve_run_by_id_or_prefix(storage, args.grade_run)
            except (LookupError, ValueError) as exc:
                parser.error(
                    str(exc) if isinstance(exc, ValueError) else f"任务不存在：{args.grade_run}"
                )
            run_events = storage.events(target["id"])
            hydrated = dict(target)
            pending_steering = storage.pending_steering_messages(target["id"])
            if pending_steering:
                hydrated["pending_steering"] = pending_steering
            grade = grade_trajectory(hydrated, run_events)
            show(grade)
            return
        if args.harvest:
            if args.limit < 1:
                parser.error("--limit 必须是正整数")
            if args.min_friction < 0:
                parser.error("--min-friction 必须是非负整数")
            storage = Storage(args.state / "state.db")
            out_dir = (
                args.output_dir.expanduser()
                if args.output_dir is not None
                else (args.state / "harvested_evals")
            )
            try:
                report = harvest_failure_traces(
                    storage,
                    out_dir,
                    limit=args.limit,
                    min_friction=args.min_friction,
                )
            except (ValueError, OSError) as exc:
                parser.error(f"轨迹采集失败：{exc}")
            show(report)
            return

        from .evals import run_all_eval_suites

        report = asyncio.run(
            run_all_eval_suites(args.suite, report_path=args.report, trials=args.trials)
        )
        show(report)
        if report.get("failed", 0) or report.get("summary", {}).get("failed", 0):
            raise SystemExit(1)
        return
    if args.command == "model-eval":
        from .model_evals import run_model_evals

        report = asyncio.run(
            run_model_evals(
                provider=args.provider,
                report_path=args.report,
                trials=args.trials,
                task_ids=args.task,
                max_repair_rounds=args.max_repair_rounds,
                verify_baseline=not args.skip_baseline_check,
            )
        )
        show(report)
        summary = report.get("summary", {})
        if summary.get("model_failures", 0) or summary.get("infrastructure_failures", 0):
            raise SystemExit(1)
        return
    storage = Storage(args.state / "state.db")
    if args.command == "trigger":
        _run_trigger_command(args, parser, storage)
        return
    if args.command == "worker":
        tenant_id = _cli_gateway_tenant_id(args.state)
        gw_config = getattr(args, "gateway_config", None)
        gw_profile = getattr(args, "gateway", None)
        if gw_config is not None:
            os.environ["AGENT_LAB_GATEWAYS_FILE"] = str(gw_config.expanduser().resolve())
        env_gw = os.environ.get("AGENT_GATEWAY_PROFILE", "").strip()
        if gw_profile or gw_config is not None or env_gw:
            gw_spec: str | None = None
            if gw_profile:
                raw_gw = gw_profile.strip()
                gw_spec = raw_gw if raw_gw.startswith("gateway:") else f"gateway:{raw_gw}"
            try:
                probe_prov, gw_info = resolve_provider_or_gateway(
                    gw_spec,
                    state_dir=args.state,
                    explicit_config_path=gw_config,
                    tenant_id=tenant_id,
                )
            except (ValueError, RuntimeError, FileNotFoundError, NotImplementedError) as exc:
                parser.error(f"Worker 解析模型网关失败：{exc}")
            else:
                _close_provider_sync(probe_prov)
                if gw_info.get("profile"):
                    os.environ["AGENT_GATEWAY_PROFILE"] = str(gw_info["profile"])
        found_worker_cfg = discover_gateways_config_path(
            explicit_path=gw_config,
            state_dir=args.state,
            tenant_id=tenant_id,
        )
        if found_worker_cfg is not None:
            os.environ.setdefault("AGENT_LAB_GATEWAYS_FILE", str(found_worker_cfg))

        base_worker_factory = build_worker_runtime_factory(ROOT, storage)

        def _worker_runtime_factory(run_doc: dict[str, Any], epoch: int) -> AgentRuntime:
            run_meta = run_doc.get("metadata") or {}
            run_prov = str(run_doc.get("provider") or "").strip()
            explicit_run_cfg = gw_config or run_meta.get("gateway_config")
            needs_gateway = bool(
                gw_profile
                or run_prov.startswith("gateway:")
                or not run_prov
                or run_prov.lower() == "auto"
            )
            if not needs_gateway:
                return base_worker_factory(run_doc, epoch)
            if gw_profile:
                raw_gw = gw_profile.strip()
                target_spec: str | None = raw_gw if raw_gw.startswith("gateway:") else f"gateway:{raw_gw}"
            elif run_prov.startswith("gateway:"):
                target_spec = run_prov
            else:
                target_spec = None
            ws_path = Path(run_meta["workspace"])
            trust_ws = is_workspace_trusted(ws_path, state_dir=args.state)
            resolved_prov, gw_info = resolve_provider_or_gateway(
                target_spec,
                state_dir=args.state,
                workspace=ws_path,
                explicit_config_path=explicit_run_cfg,
                trust_workspace=trust_ws,
                tenant_id=tenant_id,
            )
            rt = AgentRuntime(
                storage=storage,
                workspace=ws_path,
                knowledge_dir=ROOT / "data/knowledge",
                skills_dir=ROOT / "skills",
                provider=resolved_prov,
                max_steps=run_meta.get("max_steps", 12),
                max_tokens=run_meta.get("max_tokens", 30000),
                context_chars=run_meta.get("context_chars", 24000),
                max_output_tokens=run_meta.get("max_output_tokens", 4096),
                expected_epoch=epoch,
                max_repair_rounds=run_meta.get("max_repair_rounds", 2),
                commit_on_pass=bool(run_meta.get("commit_on_pass", False)),
                auto_approve_proposals=bool(run_meta.get("auto_approve_proposals", False)),
                execution_profile=run_meta.get("execution_profile", "sandbox"),
                permission_mode=run_meta.get("permission_mode"),
                enable_gui=bool(run_meta.get("enable_gui", False)),
                mcp_config=run_meta.get("mcp_config"),
                enable_agent_memory=run_meta.get("enable_agent_memory"),
                auto_dream=bool(run_meta.get("auto_dream", True)),
                defer_auto_dream=True,
            )
            prof_label = gw_info.get("profile")
            rt.provider_name = (
                f"gateway:{prof_label}"
                if prof_label
                else str(gw_info.get("protocol") or run_prov or "demo")
            )
            return rt

        try:
            daemon = LeaseWorker(
                storage=storage,
                runtime_factory=_worker_runtime_factory,
                worker_id=args.worker_id,
                ttl_seconds=args.lease_ttl,
                metrics_port=args.metrics_port,
                metrics_host=args.metrics_host,
            )
        except ValueError as exc:
            parser.error(str(exc))
        if args.once:
            outcome = asyncio.run(daemon.process_one())
            show(outcome or {"ok": True, "idle": True})
        else:
            asyncio.run(_serve_worker_gracefully(daemon, poll_interval=args.poll_interval))
        return
    if args.command == "cancel":
        try:
            target = _resolve_run_by_id_or_prefix(storage, args.run_id)
            cancelled = storage.request_cancel_run(target["id"], actor_id="cli", reason=args.reason)
        except (KeyError, LookupError, ValueError) as exc:
            parser.error(str(exc) if isinstance(exc, ValueError) else "任务不存在")
        show(cancelled)
        return
    if args.command == "gc":
        storage_gc = storage.gc_expired_resources(
            workspaces_dir=args.state / "workspaces",
            git_origins_dir=args.state / "git_origins",
            retention_seconds=args.retention_seconds,
        )
        containers_gc = reap_orphan_containers()
        show({"ok": True, "storage_gc": storage_gc, "containers_gc": containers_gc})
        return
    if args.command == "list":
        if args.json and args.table:
            parser.error("--json 与 --table 不能同时使用")
        if not 1 <= args.limit <= 1000:
            parser.error("--limit 必须在 1 到 1000 之间")
        runs = storage.list_runs(limit=args.limit, include_archived=args.include_archived)
        if args.json:
            show(runs)
        else:
            print(_render_runs_table(runs, storage))
        return
    if args.command == "trace":
        trace_id = args.run_id
        if storage.get_run(trace_id) is None:
            with contextlib.suppress(LookupError, ValueError):
                trace_id = _resolve_run_by_id_or_prefix(storage, trace_id)["id"]
        show(storage.events(trace_id))
        return
    if args.command == "calls":
        try:
            target = _resolve_run_by_id_or_prefix(storage, args.run_id)
        except (LookupError, ValueError) as exc:
            parser.error(str(exc) if isinstance(exc, ValueError) else "任务不存在")
        show(storage.call_summaries(target["id"]))
        return
    if args.command == "result":
        try:
            res_run_id = args.run_id
            if storage.get_run(res_run_id) is None:
                with contextlib.suppress(LookupError, ValueError):
                    res_run_id = _resolve_run_by_id_or_prefix(storage, res_run_id)["id"]
            show(storage.get_call_result(res_run_id, args.call_id, args.offset, args.limit))
        except (ValueError, KeyError) as exc:
            parser.error(str(exc))
        return
    if args.command == "dream":
        from .memory import DreamEngine, MemoryError as AgentMemoryError

        if args.dry_run and args.rollback is not None:
            parser.error("--dry-run 与 --rollback 不能同时使用")
        # 若指定 --run-id，优先继承该会话关联的项目工作区与执行档位，确保归入同一 project_slug 记忆库。
        target_run = None
        resolved_dream_run_id: str | None = None
        if args.run_id:
            try:
                target_run = _resolve_run_by_id_or_prefix(storage, args.run_id)
                resolved_dream_run_id = str(target_run["id"])
            except (LookupError, ValueError) as exc:
                parser.error(str(exc) if isinstance(exc, ValueError) else "任务不存在")
        try:
            if target_run is not None:
                ws_raw = (target_run.get("metadata") or {}).get("workspace", "")
                target_ws = Path(ws_raw).resolve() if ws_raw else validate_direct_workspace_path(args.cwd)
                eff_prof = (target_run.get("metadata") or {}).get("execution_profile", "host")
            else:
                target_ws = validate_direct_workspace_path(args.cwd)
                eff_prof = "host"
        except (KeyError, ValueError) as exc:
            parser.error(str(exc))
        if args.rollback is not None:
            provider: Any = "demo"
            provider_name = "demo"
        else:
            provider, provider_name, _dream_gw = _resolve_cli_provider_or_gateway(
                explicit_provider=args.provider,
                explicit_gateway=getattr(args, "gateway", None),
                gateway_config=getattr(args, "gateway_config", None),
                state_dir=args.state,
                workspace=target_ws if target_ws.is_dir() else ROOT,
                parser=parser,
                emit_demo_tip=False,
            )
        runtime = AgentRuntime(
            storage,
            target_ws if target_ws.is_dir() else ROOT,
            ROOT / "data/knowledge",
            ROOT / "skills",
            provider=provider,
            execution_profile=eff_prof,
            enable_agent_memory=True,
        )
        if not isinstance(provider, str):
            runtime.provider_name = provider_name
        if args.rollback is not None:
            store = runtime._memory_store(target_run)
            engine = DreamEngine(store)
            snapshot_id = args.rollback.strip() or None

            async def _run_rollback() -> dict:
                return engine.rollback_last_dream(snapshot_id=snapshot_id)

            try:
                rollback_report = asyncio.run(with_runtime(runtime, _run_rollback()))
            except AgentMemoryError as exc:
                parser.error(str(exc))
            show(rollback_report)
            return
        if args.dry_run:
            store = runtime._memory_store(target_run)
            engine = DreamEngine(store, llm_provider=runtime.provider)
            report = asyncio.run(
                with_runtime(
                    runtime,
                    engine.dream(
                        run_ids=[resolved_dream_run_id] if resolved_dream_run_id else None,
                        focus_goal=args.goal,
                        trigger="cli",
                        dry_run=True,
                    ),
                )
            )
            planned_diffs = report.get("planned_diffs") or []
            report = {
                **report,
                "planned_diffs_count": len(planned_diffs),
                "diff_summary": [
                    {
                        "layer": d.get("layer"),
                        "path": d.get("path"),
                        "action": d.get("action"),
                        "status": d.get("new_status") or d.get("status"),
                        **({"reason": d["reason"]} if d.get("reason") else {}),
                    }
                    for d in planned_diffs
                ],
            }
            show(report)
            return
        report = asyncio.run(
            with_runtime(
                runtime,
                runtime.dream(run_id=resolved_dream_run_id, focus_goal=args.goal, trigger="cli"),
            )
        )
        show(report)
        return
    if args.command in {"code", "chat"}:
        # 交互式多轮工作台：默认直接挂载本地目录（受 GC 豁免保护），传 --worktree 时基于本地 Git 仓创建独立并发 worktree。
        # 支持 -c/--continue 自动恢复当前目录最近未归档会话，以及 --resume <short_prefix> 自动对齐原工作区。
        from .git_repo import is_git_worktree
        from .memory import compute_project_slug

        if args.continue_latest and args.resume:
            parser.error("-c/--continue 与 --resume 不能同时使用")

        target_run: dict[str, Any] | None = None
        fallback_from_other_ws = False
        requested_cwd: Path | None = None

        if args.resume:
            try:
                target_run = _resolve_run_by_id_or_prefix(storage, args.resume)
            except (LookupError, ValueError) as exc:
                parser.error(str(exc))
            with contextlib.suppress(OSError):
                requested_cwd = args.cwd.resolve()
        elif args.continue_latest:
            try:
                base_ws = validate_direct_workspace_path(args.cwd)
            except ValueError as exc:
                parser.error(str(exc))
            requested_cwd = base_ws
            base_slug = compute_project_slug(base_ws, direct_workspace=True, state_dir=args.state)
            target_run, matched_current = _find_continue_run(storage, base_ws, base_slug)
            if target_run is None:
                print("未找到可继续的历史会话；请直接运行 `agent-lab code` 开启新会话。")
                return
            fallback_from_other_ws = not matched_current
        else:
            try:
                base_ws = validate_direct_workspace_path(args.cwd)
            except ValueError as exc:
                parser.error(str(exc))
            requested_cwd = base_ws

        runtime_extra_kwargs: dict[str, Any] = {}
        if target_run is not None:
            target_meta = target_run.get("metadata") if isinstance(target_run.get("metadata"), dict) else {}
            stored_ws_raw = target_meta.get("workspace")
            if not isinstance(stored_ws_raw, str) or not stored_ws_raw.strip():
                parser.error(f"会话 {target_run['id'][:8]} 缺少 workspace 元数据，无法恢复")
            direct_ws = Path(stored_ws_raw).resolve()
            if not direct_ws.is_dir():
                parser.error(f"会话 {target_run['id'][:8]} 绑定的工作区目录已不存在：{direct_ws}")
            is_direct_flag = bool(target_meta.get("direct_workspace", True))
            git_meta = target_meta.get("git") if isinstance(target_meta.get("git"), dict) else None
            proj_slug = str(
                target_meta.get("project_slug")
                or compute_project_slug(direct_ws, direct_workspace=is_direct_flag, state_dir=args.state)
            )
            if fallback_from_other_ws:
                print(
                    f"[提示] 当前工作区未找到历史会话，已自动回退到最近一次会话 "
                    f"{target_run['id'][:8]}（工作区: {direct_ws}）。",
                    file=sys.stderr,
                )
            elif requested_cwd is not None and requested_cwd != direct_ws:
                ws_kind = "直连工作区" if is_direct_flag else "隔离工作区"
                print(
                    f"[提示] 会话 {target_run['id'][:8]} 绑定的{ws_kind}为 {direct_ws}"
                    f"（与当前目录 {requested_cwd} 不同），已自动对齐工作区。",
                    file=sys.stderr,
                )
            resume_id: str | None = str(target_run["id"])
            effective_gui = bool(args.gui or target_meta.get("enable_gui", False))
            profile = "computer" if effective_gui else str(target_meta.get("execution_profile") or "host")
            effective_perm = str(args.permission_mode or target_meta.get("permission_mode") or "auto-edit")
            resumed_provider = str(target_run.get("provider") or "").strip()
            effective_gw_config = args.gateway_config or (
                Path(str(target_meta["gateway_config"])) if target_meta.get("gateway_config") else None
            )
            provider, provider_name, gateway_info = _resolve_cli_provider_or_gateway(
                explicit_provider=args.provider,
                explicit_gateway=args.gateway,
                gateway_config=effective_gw_config,
                state_dir=args.state,
                workspace=direct_ws,
                parser=parser,
                resumed_provider=resumed_provider,
                emit_demo_tip=False,
            )
            for quota_key in ("max_steps", "max_tokens", "context_chars"):
                if target_meta.get(quota_key) is not None:
                    runtime_extra_kwargs[quota_key] = target_meta[quota_key]
            effective_mcp = args.mcp_config if args.mcp_config is not None else target_meta.get("mcp_config")
            resumed_label = _truncate_display(
                str(target_run.get("title") or target_meta.get("title") or target_run.get("prompt") or ""),
                60,
            )
            print(
                f"[恢复会话] {resume_id[:8]} | status={target_run.get('status')} | "
                f"workspace={direct_ws} | {resumed_label}",
                file=sys.stderr,
            )
        else:
            proj_slug = compute_project_slug(base_ws, direct_workspace=True, state_dir=args.state)
            if getattr(args, "worktree", False) and is_git_worktree(base_ws):
                direct_ws = args.state / "workspaces" / uuid.uuid4().hex
                git_meta = create_task_worktree(base_ws, direct_ws, ref="HEAD")
                is_direct_flag = False
            else:
                direct_ws = base_ws
                git_meta = None
                is_direct_flag = True
            resume_id = None
            effective_gui = bool(args.gui)
            profile = "computer" if effective_gui else "host"
            effective_perm = args.permission_mode or "auto-edit"
            effective_gw_config = args.gateway_config
            provider, provider_name, gateway_info = _resolve_cli_provider_or_gateway(
                explicit_provider=args.provider,
                explicit_gateway=args.gateway,
                gateway_config=effective_gw_config,
                state_dir=args.state,
                workspace=direct_ws,
                parser=parser,
            )
            effective_mcp = args.mcp_config

        if effective_gui:
            _warn_macos_gui_tcc_if_needed()

        stream_broker = RunStreamBroker()
        runtime = AgentRuntime(
            storage,
            direct_ws,
            ROOT / "data/knowledge",
            ROOT / "skills",
            provider=provider,
            execution_profile=profile,
            permission_mode=effective_perm,
            enable_gui=effective_gui,
            mcp_config=effective_mcp,
            auto_dream=not bool(getattr(args, "no_auto_dream", False)),
            stream_broker=stream_broker,
            **runtime_extra_kwargs,
        )
        if not isinstance(provider, str):
            runtime.provider_name = provider_name
        resolved_gw_cfg_str = (
            str(effective_gw_config.expanduser().resolve()) if effective_gw_config is not None else None
        )
        if resolved_gw_cfg_str:
            runtime.gateway_config_path = resolved_gw_cfg_str  # type: ignore[attr-defined]
        extra_meta = {
            "template": "local_cwd",
            "direct_workspace": is_direct_flag,
            "project_slug": proj_slug,
            "execution_profile": profile,
            "permission_mode": effective_perm,
            "enable_gui": effective_gui,
            **({"git": git_meta, "isolated_local_worktree": True} if git_meta else {}),
            **({"gateway": gateway_info} if gateway_info else {}),
            **({"gateway_config": resolved_gw_cfg_str} if resolved_gw_cfg_str else {}),
        }
        asyncio.run(
            with_runtime(
                runtime,
                run_interactive_repl(
                    runtime,
                    initial_prompt=args.prompt,
                    resume_run_id=resume_id,
                    mode=args.mode,
                    extra_metadata=extra_meta,
                ),
            )
        )
        return
    if args.command in {"run", "demo"}:
        cwd_arg = getattr(args, "cwd", None)
        repo_arg = getattr(args, "repo", None)
        gui_flag = bool(getattr(args, "gui", False))
        explicit_profile = getattr(args, "profile", None)
        perm_arg = getattr(args, "permission_mode", None)
        mcp_cfg_arg = getattr(args, "mcp_config", None)

        extra_meta: dict = {"template": args.template}
        commit_on_pass = False
        if cwd_arg is not None:
            # 显式 --cwd：宿主直连模式（或 --worktree 独立并发分支模式），共享源项目记忆库 project_slug。
            from .git_repo import is_git_worktree
            from .memory import compute_project_slug

            try:
                base_ws = validate_direct_workspace_path(cwd_arg)
            except ValueError as exc:
                parser.error(str(exc))
            eff_profile = explicit_profile or ("computer" if gui_flag else "host")
            proj_slug = compute_project_slug(base_ws, direct_workspace=True, state_dir=args.state)
            if getattr(args, "worktree", False) and is_git_worktree(base_ws):
                workspace = args.state / "workspaces" / uuid.uuid4().hex
                git_meta = create_task_worktree(base_ws, workspace, ref="HEAD")
                extra_meta.update(
                    template="local_cwd",
                    direct_workspace=False,
                    isolated_local_worktree=True,
                    project_slug=proj_slug,
                    git=git_meta,
                )
            else:
                workspace = base_ws
                extra_meta.update(template="local_cwd", direct_workspace=True, project_slug=proj_slug)
        else:
            # 默认沙箱隔离模式：创建独立副本或分离 HEAD 的 Git worktree，并锁定测试真值哈希（test_oracles）。
            workspace = args.state / "workspaces" / uuid.uuid4().hex
            eff_profile = explicit_profile or ("computer" if gui_flag else "sandbox")
            if repo_arg is not None:
                git_meta = create_task_worktree(repo_arg.resolve(), workspace, ref=args.ref)
                oracles, expected_tests = discover_test_oracles(workspace)
                extra_meta.update(
                    template="custom_git", git=git_meta, test_oracles=oracles, expected_tests=expected_tests
                )
                commit_on_pass = True
            elif args.template == "multi_file":
                origin = args.state / "git_origins" / "multi_file_repo"
                ensure_git_origin_atomic(ROOT / "data/multi_file_repo", origin)
                git_meta = create_task_worktree(origin, workspace, ref="HEAD")
                oracles, expected_tests = discover_test_oracles(workspace)
                extra_meta.update(git=git_meta, test_oracles=oracles, expected_tests=expected_tests)
                commit_on_pass = True
            else:
                shutil.copytree(ROOT / "data/sample_repo", workspace)
        run_gw_info: dict[str, Any] | None = None
        run_gw_cfg: Path | None = None
        if args.command == "run":
            run_gw_cfg = getattr(args, "gateway_config", None)
            provider, provider_name, run_gw_info = _resolve_cli_provider_or_gateway(
                explicit_provider=args.provider,
                explicit_gateway=getattr(args, "gateway", None),
                gateway_config=run_gw_cfg,
                state_dir=args.state,
                workspace=workspace,
                parser=parser,
            )
            if gui_flag:
                _warn_macos_gui_tcc_if_needed()
        else:
            provider = "demo"
            provider_name = "demo"
        if run_gw_info:
            extra_meta["gateway"] = run_gw_info
        if run_gw_cfg is not None:
            extra_meta["gateway_config"] = str(run_gw_cfg.expanduser().resolve())
        repair_rounds = getattr(args, "max_repair_rounds", 2)
        runtime = AgentRuntime(
            storage,
            workspace,
            ROOT / "data/knowledge",
            ROOT / "skills",
            provider,
            max_repair_rounds=repair_rounds,
            commit_on_pass=commit_on_pass,
            execution_profile=eff_profile,
            permission_mode=perm_arg,
            enable_gui=gui_flag,
            mcp_config=mcp_cfg_arg,
            auto_dream=not bool(getattr(args, "no_auto_dream", False)),
        )
        if not isinstance(provider, str):
            runtime.provider_name = provider_name
        if args.command == "run":
            prompt = args.prompt
        elif args.template == "multi_file":
            prompt = "请检查 checkout.py 与 discounts.py 的多文件结算逻辑，修复优惠封顶与应付总额缺陷并在容器中验证。"
        else:
            prompt = "修复 pricing.py 的百分比折扣 bug，并验证修改。"
        created = runtime.create(prompt, args.mode, extra_metadata=extra_meta)
        result = asyncio.run(with_runtime(runtime, runtime.execute(created["id"])))
        proposals = storage.proposals(result["id"])
        if args.command == "demo" and args.auto_approve and proposals:
            result = asyncio.run(
                with_runtime(runtime, runtime.approve(result["id"], proposals[0]["id"], True))
            )
        elif args.command == "run" and result["status"] == "waiting_approval" and os.isatty(0):
            # 交互式终端下触发待审动作时，自动拉起内联 [y/n/a/f] 审批排空循环；非 TTY 管道则直接输出待审回执。
            result = asyncio.run(
                with_runtime(runtime, drain_approvals_interactively(runtime, result))
            )
        show({"run": result, "proposals": storage.proposals(result["id"])})
        if result["status"] in {"failed", "budget_exceeded"}:
            raise SystemExit(1)
        return
    # approve / resume 分支：严格按持久化 metadata 重建 Runtime，保持历史累计预算与执行档位连续。
    try:
        run = _resolve_run_by_id_or_prefix(storage, args.run_id)
    except (LookupError, ValueError) as exc:
        parser.error(str(exc) if isinstance(exc, ValueError) else "任务不存在")
    resolved_run_id = str(run["id"])
    config = run["metadata"]
    resume_ws = Path(config["workspace"])
    stored_provider = str(run.get("provider") or "demo").strip()
    resume_provider_arg: Any = stored_provider
    if stored_provider.startswith("gateway:"):
        try:
            resume_provider_arg, _ = resolve_provider_or_gateway(
                stored_provider,
                state_dir=args.state,
                workspace=resume_ws,
                explicit_config_path=config.get("gateway_config"),
                trust_workspace=is_workspace_trusted(resume_ws, state_dir=args.state),
                tenant_id=_cli_gateway_tenant_id(args.state),
            )
        except (ValueError, RuntimeError, FileNotFoundError, NotImplementedError) as exc:
            parser.error(f"重建任务网关失败：{exc}")
    runtime = AgentRuntime(
        storage,
        resume_ws,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=resume_provider_arg,
        max_steps=config["max_steps"],
        max_tokens=config["max_tokens"],
        context_chars=config["context_chars"],
        max_repair_rounds=config.get("max_repair_rounds", 2),
        commit_on_pass=bool(config.get("commit_on_pass", False)),
        auto_approve_proposals=bool(config.get("auto_approve_proposals", False)),
        execution_profile=config.get("execution_profile", "sandbox"),
        permission_mode=config.get("permission_mode"),
        enable_gui=bool(config.get("enable_gui", False)),
        mcp_config=config.get("mcp_config"),
    )
    if not isinstance(resume_provider_arg, str):
        runtime.provider_name = stored_provider
    if args.command == "approve":
        result = asyncio.run(
            with_runtime(runtime, runtime.approve(resolved_run_id, args.proposal_id, not args.reject))
        )
    else:
        result = asyncio.run(with_runtime(runtime, runtime.resume(resolved_run_id)))
    show(result)
    if result["status"] in {"failed", "budget_exceeded"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
