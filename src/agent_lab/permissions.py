"""权限策略与宿主执行边界：三级权限档位（ask / auto-edit / full-auto）、命令风险分级与环境变量脱敏。"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shlex
from typing import Any

EXECUTION_PROFILES = frozenset({"sandbox", "host", "computer"})
PERMISSION_MODES = frozenset({"ask", "auto-edit", "full-auto"})

_SECRET_ENV_EXACT = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "OPENAI_API_KEY",
        "AGENT_LAB_TOKEN",
        "AGENT_LAB_TOKENS_JSON",
        "AGENT_LAB_SANDBOX_TOKEN",
        "AGENT_LAB_STORAGE_DSN",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "GITLAB_TOKEN",
    }
)
_SECRET_ENV_RE = re.compile(
    r"(?:API_KEY|AUTH_TOKEN|SECRET|PASSWORD|PRIVATE_KEY|CREDENTIAL|ACCESS_KEY|SESSION_TOKEN)",
    re.IGNORECASE,
)

# 在任何权限模式（包括 full-auto）下都硬阻断的毁机级破坏命令模式。
_CATASTROPHIC_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?:^|[;&|\s])(?:sudo\s+)?rm\s+(?:-[A-Za-z]*[rf][A-Za-z]*\s+)+"
            r"(?:/(?:\s|$|\*)|~(?:\s|/|$)|\$HOME(?:\s|/|$))",
            re.IGNORECASE,
        ),
        "禁止递归删除根目录或用户主目录",
    ),
    (
        re.compile(r"(?:^|[;&|\s])(?:sudo\s+)?mkfs(?:\.[A-Za-z0-9_+-]+)?\b", re.IGNORECASE),
        "禁止格式化磁盘文件系统",
    ),
    (
        re.compile(r"\bdd\b[^\n;|&]*\bof=/dev/", re.IGNORECASE),
        "禁止向原始块设备写入数据",
    ),
    (
        re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:"),
        "禁止执行 Fork Bomb",
    ),
    (
        re.compile(
            r"(?:^|[;&|\s])(?:sudo\s+)?(?:shutdown|reboot|halt|poweroff|diskutil\s+(?:eraseDisk|partitionDisk))\b",
            re.IGNORECASE,
        ),
        "禁止关机、重启或擦除磁盘",
    ),
)

_SHELL_META_RE = re.compile(r"[;&|`$><\n\r]")
_CMD_SPLIT_RE = re.compile(r"[;&|\n\r]+")

_BENIGN_INLINE_ENV_KEYS = frozenset(
    {"LC_ALL", "LANG", "TZ", "TERM", "NO_COLOR", "FORCE_COLOR", "PYTHONDONTWRITEBYTECODE"}
)
_SAFE_SINGLE_BINARIES = frozenset(
    {"ls", "pwd", "which", "uname", "whoami", "wc", "date", "true", "false"}
)
_SAFE_GIT_SUBCOMMANDS = frozenset({"status", "diff", "log", "branch", "show", "rev-parse"})
_SAFE_GIT_BRANCH_FLAGS = frozenset(
    {"--show-current", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--list"}
)
_UNSAFE_GIT_FLAGS_PREFIX = (
    "--output",
    "--ext-diff",
    "--no-index",
    "--textconv",
    "--git-dir",
    "--work-tree",
    "--exec",
    "-c",
)
_UNSAFE_PYTEST_PREFIXES = (
    "--basetemp",
    "--junitxml",
    "--junit-xml",
    "--log-file",
    "--result-log",
    "--override-ini",
    "--rootdir",
    "--confcutdir",
)

_FORBIDDEN_DIRECT_ROOTS = (
    "/System",
    "/Library",
    "/bin",
    "/sbin",
    "/usr",
    "/etc",
    "/private/etc",
    "/var/root",
    "/private/var/root",
    "/dev",
    "/proc",
)
_FORBIDDEN_HOME_SUBDIRS = frozenset({".ssh", ".gnupg", ".aws", ".kube"})


def normalize_execution_profile(
    profile: str | None, *, enable_gui: bool = False
) -> str:
    clean = str(profile or "sandbox").strip().lower()
    if clean not in EXECUTION_PROFILES:
        raise ValueError(f"execution_profile 必须是 {sorted(EXECUTION_PROFILES)} 之一")
    if enable_gui and clean == "host":
        return "computer"
    return clean


def normalize_permission_mode(mode: str | None, *, default: str = "ask") -> str:
    clean = str(mode or default).strip().lower()
    if clean not in PERMISSION_MODES:
        raise ValueError(f"permission_mode 必须是 {sorted(PERMISSION_MODES)} 之一")
    return clean


def _has_catastrophic_rm_tokens(command: str) -> str | None:
    home_resolved = str(Path.home().resolve()).rstrip("/")
    dangerous_targets = {"/", "/*", "~", "~/*", "$HOME", "${HOME}", home_resolved, f"{home_resolved}/*"}
    for segment in _CMD_SPLIT_RE.split(command):
        seg = segment.strip()
        if not seg:
            continue
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks = [t.strip("\"'") for t in seg.split() if t.strip("\"'")]
        idx = 0
        while idx < len(toks) and ("=" in toks[idx] and not toks[idx].startswith("-")):
            idx += 1
        if idx < len(toks) and toks[idx] == "sudo":
            idx += 1
            while idx < len(toks) and toks[idx].startswith("-"):
                idx += 1
        if idx >= len(toks):
            continue
        if Path(toks[idx]).name != "rm":
            continue
        rm_args = toks[idx + 1 :]
        has_recursive = False
        operands: list[str] = []
        after_ddash = False
        for arg in rm_args:
            if after_ddash:
                operands.append(arg)
            elif arg == "--":
                after_ddash = True
            elif arg in {"--recursive", "--no-preserve-root"}:
                has_recursive = True
            elif arg.startswith("-") and not arg.startswith("--"):
                if "r" in arg.lower():
                    has_recursive = True
            elif not arg.startswith("-"):
                operands.append(arg)
        if has_recursive:
            for op in operands:
                norm_op = op.strip()
                if norm_op != "/" and norm_op.endswith("/") and not norm_op.endswith("/*"):
                    norm_op = norm_op.rstrip("/")
                if norm_op in dangerous_targets:
                    return "禁止递归删除根目录或用户主目录"
    return None


def classify_bash_command(command: str) -> dict[str, str]:
    """对终端命令做确定性静态风险分类：blocked / safe_readonly / mutating。"""
    if not isinstance(command, str) or not command.strip():
        return {"classification": "blocked", "reason": "命令不能为空"}
    stripped = command.strip()
    if "\x00" in stripped:
        return {"classification": "blocked", "reason": "命令包含非法空字节"}
    for pattern, reason in _CATASTROPHIC_PATTERNS:
        if pattern.search(stripped):
            return {"classification": "blocked", "reason": reason}
    rm_block = _has_catastrophic_rm_tokens(stripped)
    if rm_block:
        return {"classification": "blocked", "reason": rm_block}

    if _SHELL_META_RE.search(stripped):
        return {"classification": "mutating", "reason": "包含管道、重定向或多命令连接符"}

    try:
        tokens = shlex.split(stripped)
    except ValueError:
        return {"classification": "mutating", "reason": "引号不闭合或包含复杂 shell 语法"}
    if not tokens:
        return {"classification": "blocked", "reason": "命令为空"}

    # 仅放行极少数无执行副作用的显示/区域设置内联环境变量；其余内联变量（如 GIT_EXTERNAL_DIFF）一律视为 mutating
    idx = 0
    while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-"):
        env_key = tokens[idx].split("=", 1)[0]
        if env_key.upper() not in _BENIGN_INLINE_ENV_KEYS:
            return {"classification": "mutating", "reason": f"包含非只读白名单环境变量 {env_key}"}
        idx += 1
    argv = tokens[idx:]
    if not argv:
        return {"classification": "safe_readonly", "reason": "纯显示变量赋值空命令"}

    # 拒绝 ./ls 或 /tmp/git 等带路径分隔符的可执行文件伪装成系统只读命令
    if "/" in argv[0]:
        return {"classification": "mutating", "reason": "显式路径可执行文件需按脚本审查"}

    # 剥离 uv run [--no-sync] 前缀以便识别只读测试/静态检查
    if argv[0] == "uv" and len(argv) >= 3 and argv[1] == "run":
        sub_idx = 2
        while sub_idx < len(argv) and argv[sub_idx] in {"--no-sync", "--locked", "-q", "--quiet"}:
            sub_idx += 1
        argv = argv[sub_idx:]
        if not argv or "/" in argv[0]:
            return {"classification": "mutating", "reason": "uv run 目标不是标准只读命令"}

    prog = argv[0]
    rest = argv[1:]

    def _is_safe_ruff_args(r_args: list[str]) -> bool:
        return not any(
            a.startswith(("--fix", "--unsafe-fixes", "--add-noqa", "-o", "--output-file"))
            for a in r_args
        )

    def _is_safe_pytest_args(p_args: list[str]) -> bool:
        return not any(
            a in {"-c", "-o", "-p"} or a.startswith(_UNSAFE_PYTEST_PREFIXES)
            for a in p_args
        )

    if prog in _SAFE_SINGLE_BINARIES:
        if any(is_token_secret(a) for a in rest):
            return {"classification": "mutating", "reason": "命令参数涉及敏感文件"}
        return {"classification": "safe_readonly", "reason": f"白名单只读基础命令 ({prog})"}

    if prog == "git" and rest:
        if rest[0].startswith("-"):
            return {"classification": "mutating", "reason": "带全局选项的 git 调用需确认"}
        sub = rest[0]
        sub_args = rest[1:]
        if sub not in _SAFE_GIT_SUBCOMMANDS:
            return {"classification": "mutating", "reason": f"修改型 Git 子命令 ({sub})"}
        if any(a.startswith(_UNSAFE_GIT_FLAGS_PREFIX) or is_token_secret(a) for a in sub_args):
            return {"classification": "mutating", "reason": "Git 参数包含外部执行、跨目录或敏感路径选项"}
        if sub == "branch" and any(a not in _SAFE_GIT_BRANCH_FLAGS for a in sub_args):
            return {"classification": "mutating", "reason": "git branch 包含创建、重命名或删除分支参数"}
        return {"classification": "safe_readonly", "reason": f"只读 Git 查询 (git {sub})"}

    if prog == "ruff" and rest and rest[0] in {"check", "version", "--version"}:
        if _is_safe_ruff_args(rest[1:]):
            return {"classification": "safe_readonly", "reason": "只读静态检查 (ruff check)"}

    if prog in {"pytest", "py.test"} and _is_safe_pytest_args(rest):
        return {"classification": "safe_readonly", "reason": "运行测试套件 (pytest)"}

    if prog in {"python", "python3"} and len(rest) >= 2 and rest[0] == "-m":
        mod = rest[1]
        mod_args = rest[2:]
        if mod in {"unittest", "py_compile"}:
            return {"classification": "safe_readonly", "reason": f"标准测试或语法检查 (python -m {mod})"}
        if mod == "pytest" and _is_safe_pytest_args(mod_args):
            return {"classification": "safe_readonly", "reason": "标准测试运行 (python -m pytest)"}
        if mod == "ruff" and mod_args and mod_args[0] == "check" and _is_safe_ruff_args(mod_args[1:]):
            return {"classification": "safe_readonly", "reason": "只读静态检查 (python -m ruff check)"}

    return {"classification": "mutating", "reason": f"命令 {prog} 可能修改状态或执行任意脚本"}


def is_token_secret(token: str) -> bool:
    name = Path(token).name.casefold()
    return name.startswith((".env", "id_rsa", "id_ed25519")) or name.endswith((".pem", ".key", ".p12", ".pfx"))


READONLY_HOST_TOOLS = frozenset(
    {
        "repo_list",
        "repo_map",
        "symbol_search",
        "repo_git_status",
        "repo_read",
        "repo_search",
        "knowledge_search",
        "verify_python",
        "fs_read",
        "fs_glob",
        "fs_grep",
        "computer_screen",
        "skill_search",
        "skill_inspect",
        "skill_load",
        "skill_unload",
        "memory_get",
        "memory_read",
        "memory_write",
        "memory_search",
        "memory_tag",
        "working_memory_read",
        "working_memory_write",
        "tool_result_get",
        "subagent_run",
    }
)


def evaluate_tool_permission(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    permission_mode: str,
    mcp_read_only: bool = False,
) -> dict[str, str]:
    """根据三级权限策略（ask / auto-edit / full-auto）裁决工具调用是直接放行、请求审批还是硬拒绝。"""
    mode = normalize_permission_mode(permission_mode)

    if tool_name in READONLY_HOST_TOOLS:
        return {"decision": "allow", "risk": "readonly", "reason": "只读或上下文化工具默认放行"}

    if tool_name in {"propose_patch", "propose_changeset"}:
        # 传统提案工具自身就是生成提案（或由 auto_approve_proposals 控制）
        return {"decision": "allow", "risk": "proposal", "reason": "显式补丁提案工具"}

    if tool_name == "sandbox_test":
        return {"decision": "allow", "risk": "sandbox", "reason": "受控隔离容器验收测试"}

    if tool_name in {"fs_write", "fs_edit"}:
        if mode in {"auto-edit", "full-auto"}:
            return {"decision": "allow", "risk": "workspace_edit", "reason": f"{mode} 模式允许直接编辑工作区文件"}
        return {"decision": "ask", "risk": "workspace_edit", "reason": "ask 模式写入文件前需要人工确认"}

    if tool_name == "bash_exec":
        cmd = str(arguments.get("command", ""))
        verdict = classify_bash_command(cmd)
        cls_name = verdict["classification"]
        if cls_name == "blocked":
            return {"decision": "deny", "risk": "blocked", "reason": verdict["reason"]}
        if cls_name == "safe_readonly":
            if mode in {"auto-edit", "full-auto"}:
                return {"decision": "allow", "risk": "safe_readonly", "reason": verdict["reason"]}
            return {"decision": "ask", "risk": "safe_readonly", "reason": f"ask 模式执行命令需人工确认（{verdict['reason']}）"}
        if mode == "full-auto":
            return {"decision": "allow", "risk": "mutating", "reason": "full-auto 模式允许自动运行终端命令"}
        return {"decision": "ask", "risk": "mutating", "reason": verdict["reason"]}

    if tool_name in {"computer_mouse", "computer_keyboard", "computer_open"}:
        if mode == "full-auto":
            return {"decision": "allow", "risk": "gui_action", "reason": "full-auto 模式允许自动执行桌面 GUI 操作"}
        return {"decision": "ask", "risk": "gui_action", "reason": f"{mode} 模式执行桌面 GUI 键鼠/应用操作前需人工确认"}

    if tool_name.startswith("mcp__"):
        if mcp_read_only:
            return {"decision": "allow", "risk": "mcp_readonly", "reason": "只读外部 MCP 工具"}
        if mode == "full-auto":
            return {"decision": "allow", "risk": "mcp_call", "reason": "full-auto 模式允许自动调用外部 MCP 工具"}
        return {"decision": "ask", "risk": "mcp_call", "reason": f"{mode} 模式调用未标只读的外部 MCP 工具需人工确认"}

    return {"decision": "ask", "risk": "unknown", "reason": "未分类副作用工具需人工确认"}


def build_scrubbed_env(
    extra_env: dict[str, str] | None = None,
    *,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """构造脱敏子进程环境变量：保留本地开发工具链环境，严格剔除 API 密钥与服务端令牌。"""
    source = os.environ if base_env is None else base_env
    cleaned: dict[str, str] = {}
    for key, val in source.items():
        if key in _SECRET_ENV_EXACT or _SECRET_ENV_RE.search(key):
            continue
        cleaned[key] = str(val)
    cleaned.setdefault("PATH", os.defpath)
    cleaned["GIT_TERMINAL_PROMPT"] = "0"
    cleaned["PAGER"] = "cat"
    cleaned["GIT_PAGER"] = "cat"
    cleaned["PYTHONUNBUFFERED"] = "1"
    if extra_env:
        for key, val in extra_env.items():
            key_str = str(key).strip()
            if key_str and "\x00" not in key_str and "\x00" not in str(val):
                cleaned[key_str] = str(val)
    return cleaned


def validate_direct_workspace_path(
    raw_path: str | Path,
    *,
    allowed_roots: list[Path] | None = None,
) -> Path:
    """校验本地直连工作区目录：禁止符号链接根、系统关键目录与密钥配置目录。"""
    if raw_path is None or not str(raw_path).strip():
        raise ValueError("工作区目录路径不能为空")
    raw_str = str(raw_path).strip()
    if "\x00" in raw_str:
        raise ValueError("工作区路径包含非法字符")
    expanded = Path(raw_str).expanduser().absolute()
    if expanded.is_symlink():
        raise ValueError("直连工作区根目录不能是符号链接")
    if not expanded.exists() or not expanded.is_dir():
        raise ValueError(f"直连工作区目录不存在或不是目录：{raw_str}")
    resolved = expanded.resolve()
    if str(resolved) == "/":
        raise ValueError("禁止将系统根目录 / 作为工作区")
    for sys_prefix in _FORBIDDEN_DIRECT_ROOTS:
        sys_path = Path(sys_prefix)
        if resolved == sys_path or resolved.is_relative_to(sys_path):
            raise ValueError(f"禁止将系统敏感目录作为工作区：{sys_prefix}")
    with_home = Path.home().resolve()
    for sub in _FORBIDDEN_HOME_SUBDIRS:
        secret_dir = with_home / sub
        if resolved == secret_dir or resolved.is_relative_to(secret_dir):
            raise ValueError(f"禁止将用户密钥目录作为工作区：~/{sub}")
    if allowed_roots:
        norm_roots = [Path(r).expanduser().resolve() for r in allowed_roots]
        if not any(resolved == root or resolved.is_relative_to(root) for root in norm_roots):
            raise ValueError("目标目录不在 AGENT_LAB_ALLOWED_WORKSPACES 允许范围内")
    return resolved
