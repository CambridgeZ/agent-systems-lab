"""本地直连工作区工具集（Claude Code / Codex 风格）：全功能文件读写编辑、Glob/Grep 检索与终端命令执行。

================================================================================
模块导读（Module Guide）
================================================================================
1. 模块角色（Role）
   本模块是 Agent 在 `host` / `computer` 档位下直接操作本地真实代码仓库的策略执行点（PEP,
   Policy Enforcement Point）。相比传统仅支持小文件与只读容器的 `sandbox` 模式，本模块赋予
   Agent 对标 Claude Code 与 OpenAI Codex CLI 的完整本地工程闭环能力：分页读取大文件
   （`fs_read`）、原子全量写入（`fs_write`）、上下文锚定精确编辑（`fs_edit`）、感知
   `.gitignore` 的文件树模式检索（`fs_glob`）、跨文件正则搜索（`fs_grep`）以及进程组级隔离的
   终端命令执行（`bash_exec`）。

2. 推荐阅读顺序（Reading Order）
   - 第一步：精读 `is_host_secret_segment` 与 `resolve_host_workspace_path`，掌握“先纯词法
     审计、再逐段 `lstat` 拒软链、后 `resolve()` 验边界与硬链接计数”的五阶路径防御模型。
   - 第二步：阅读 `HOST_TOOL_SPECS` 与 `_walk_workspace_files`，理解工具输入契约与遵循
     `.gitignore` 的原地目录剪枝遍历机制。
   - 第三步：精读 `_atomic_write_host_file`、`execute_fs_write` 与 `execute_fs_edit`，理解
     `ask` 提案生成路径与直写路径下“跨进程 `flock` + Epoch 租约栅栏 + `mkstemp/fsync/replace`
     原子替换”的事务一致性保证。
   - 第四步：阅读 `_glob_matches`、`execute_fs_glob` 与 `execute_fs_grep`，了解双星号通配符
     零目录折叠与上下文预算截断设计。
   - 第五步：精读 `_drain_stream`、`_terminate_process_group`、`run_host_bash_command` 与
     `execute_bash_tool`，彻底吃透后台排水线程防 64KB 管道死锁与进程组（PGID）孤儿进程清场机制。

3. 核心安全与工程不变量（Core Security & Engineering Invariants）
   - 词法优先于 `resolve()` 不变量：绝不在逐段敏感文件名与 `is_symlink()` 检查前对工作区内部
     路径直接调用 `Path.resolve()`，彻底阻断借软链接隐藏 `.env` 名字或跳出工作区边界的逃逸；
     且对已存在文件强制要求硬链接数 `st_nlink == 1` 且为常规文件（拒绝硬链接越权与 FIFO 挂起）。
   - 跨进程串行与崩溃原子写不变量：一切文件直接落盘必须在工作区级跨进程 `flock`（`cross_lock_cm`）
     临界区内先执行 `storage.verify_epoch` 校验运行代数，再通过同父目录临时文件 + `os.fsync` +
     `os.replace` 原子切换目录项，消除并发读写竞态（TOCTOU）、断电半截损坏与僵尸 Worker 覆写。
   - 管道永不反压死锁不变量：子进程的 `stdout`/`stderr` 由独立守护线程持续循环 `read(4096)` 直至
     EOF；超出 32 KiB 预算后仅停止写入内存缓冲而绝不停止抽干管道，彻底杜绝 OS 管道写满死锁。
   - 进程组无孤儿残留不变量：`bash_exec` 通过 `start_new_session=True` 建立独立进程组，超时或
     异常时对整个 `pgid` 执行 `SIGTERM -> 轮询进程组存活 -> SIGKILL`，并在 `finally` 中兜底补杀，
     确保后台派生的孙进程既不泄露系统资源，也不会占住管道写端阻塞排水线程 `join()`。

4. 技术面试高频考点速览（Interview Q&A Takeaways）
   - Q1：校验工作区路径时，为什么“先 `p = (ws / raw).resolve()` 再检查 `p.is_relative_to(ws)`
     和 `.env` 黑名单”是经典的致命漏洞？
     A：`Path.resolve()` 会跟随沿途全部符号链接直达最终目标：① 若仓库内存在 `.env.link -> a.cfg`，
     先 `resolve()` 会让路径名坍缩为 `a.cfg`，绕过敏感文件名拦截；② 若先对含 `..` 的路径做词法或
     物理折叠，可能跳过中间符号链接目录的逃逸审计；③ 若攻击者在同文件系统下创建指向外部密钥的
     硬链接（Hardlink），连 `is_symlink()` 都无法检出！因此必须依次执行：纯词法禁 `..`/敏感名 ->
     逐级拼接并用 `is_symlink()`（底层 `lstat`）拦截软链接 -> `resolve()` 验边界 -> `st_nlink == 1`
     拦截硬链接与特殊设备文件。
   - Q2：文件落盘为什么要组合使用跨进程 `flock` + `mkstemp(dir=target.parent)` + `fsync` +
     `os.replace`，直接 `target.write_text(content)` 会出什么事故？
     A：直接 `write_text` 底层以 `O_TRUNC` 打开原文件，瞬间将其清空为 0 字节：若写入中途进程崩溃、
     断电或被超时杀死，源码文件将永久损坏为空文件或半截残片；且在 `fs_edit` 中，“读取旧内容 ->
     校验唯一匹配 -> 写回新内容”若无跨进程 `flock` 与 `verify_epoch` 保护，多进程或旧租约僵尸任务
     并发写入会产生丢失更新（Lost Update）与 TOCTOU 覆盖。在 `target.parent` 同目录创建临时文件保
     证与目标位于同一文件系统挂载点（避免跨分区 `rename` 报 `EXDEV`），`fsync` 确保数据落入物理介质
     后再由 POSIX 原子原语 `os.replace` 瞬间切换 inode 指向，任何时刻读取者看到的要么是完整旧文件，
     要么是完整新文件。
   - Q3：用 `subprocess.Popen` 跑命令时，为什么 `proc.wait(timeout)` 后再读 `stdout` 会死锁？
     为什么超时后调 `proc.kill()` 依然会导致主线程卡死或残留孤儿进程？
     A：① 操作系统匿名管道内核缓冲区极其有限（通常 16KB~64KB）。当子进程输出超过 64KB 填满内核缓
     冲区时，子进程会阻塞在 `write()` 系统调用等待父进程消费；若父进程正阻塞在 `proc.wait()` 等子
     进程退出才去读，双方形成死锁直至超时。② `bash -c` 运行的测试脚本常在后台派生子孙进程并继承同
     一个管道写端 FD；`proc.kill()` 只杀顶层 `bash`，孙进程被 PID 1 收养成为孤儿继续存活并持有管道
     写端，导致后台读线程迟迟收不到 EOF 而卡住 `join()`。必须配合 `start_new_session=True` 与
     `os.killpg(pgid, ...)` 按进程组整树收割。
================================================================================
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
import contextlib
import difflib
import fnmatch
from html.parser import HTMLParser
import ipaddress
import os
from pathlib import Path, PurePosixPath
import pty
import pwd
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from urllib.parse import urljoin, urlsplit
import uuid

from .observability import redact_secrets
from .permissions import (
    PermissionRuleEngine,
    build_scrubbed_env,
    classify_bash_command,
    evaluate_tool_permission,
)
from .repomap import _load_gitignore_patterns, _matches_gitignore
from .storage import Storage, digest

# 单文件最大读写体积（512 KiB）：相比经典 sandbox 模式的 64 KiB 提升了 8 倍，足以覆盖绝大多数真实工程源码、
# 复杂配置文件与测试套件，同时硬性拦截数 MB 级的压缩产物/二进制包撑爆进程内存与模型上下文窗口。
_HOST_MAX_FILE_BYTES = 512 * 1024  # 512 KiB，覆盖绝大多数源码、配置与脚本文件
# Glob 与 Grep 返回条数硬上限：防止在大型单体仓库（Monorepo）中一次宽泛匹配刷屏挤占历史对话上下文。
_HOST_MAX_GLOB_RESULTS = 200
_HOST_MAX_GREP_MATCHES = 60
# 终端单流（stdout/stderr）原始字节捕获上限（32 KiB）：防止无限循环打印引发 OOM，超出部分在后台
# 排水线程中静默丢弃并置位 truncated 标志，后续由 Runtime 上下文层进一步做分页引用投影。
_HOST_MAX_STREAM_BYTES = 32 * 1024  # 32 KiB 原始输出上限（超出交由 Runtime 分页投影）

# 文件树遍历（fs_glob / fs_grep）始终原地剪枝跳过的版本控制、虚拟环境、依赖包与框架内部目录。
_ALWAYS_SKIP_DIRS = frozenset(
    {".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".agent-lab"}
)
# 严禁通过 fs_* 工具直接读写的隐藏敏感目录：
# - 禁止读写 .git：防止模型直接篡改 .git/config 或 .git/hooks/pre-commit 植入持久化后门代码；
# - 禁止读写 .agent-lab：保护 Agent 自身运行态账本、锁文件与长效记忆索引不被工具层自指篡改；
# - 禁止读写 .ssh / .gnupg / .aws / .kube：即使这些目录意外出现在工作区内也绝不允许触碰。
_FORBIDDEN_DOT_PARTS = frozenset({".git", ".ssh", ".gnupg", ".aws", ".kube", ".agent-lab"})
# 敏感凭证文件词干、完整文件名与私钥证书后缀黑名单。
_SENSITIVE_STEMS = frozenset(
    {"credentials", "secrets", "secret", "private_key", "tokens", "api_key", "apikey"}
)
_SECRET_NAMES = frozenset({"credentials.py", "secrets.py", "secret.py", "private_key.py", "tokens.py"})
_SECRET_SUFFIXES = frozenset({".pem", ".key", ".p12", ".pfx", ".env"})


class HostToolError(ValueError):
    """宿主工具层结构化业务异常：携带机器可读的错误码 `code` 与面向模型的诊断修复提示。

    为什么使用带 `code` 的结构化异常而非直接抛裸 `OSError`？
    外层 `ToolRegistry` 捕获 `HostToolError` 后会将其序列化为 `{"ok": False, "code": ..., "error": ...}`
    回传给大模型，使模型能精确区分 `old_text_not_found`（需重新 `fs_read`）、`ambiguous_match`
    （需补充上下文行）与 `secret_access_forbidden`（安全红线不可重试），驱动确定性的自修复闭环。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def is_host_secret_segment(name: str) -> bool:
    """判定单个路径段（目录名或文件名）是否属于敏感密钥、环境变量文件或内部原子写入临时文件。

    实现细节：
    - 同时提取 `Path(name).stem` 与首个点号前的 `first_stem`（兼容隐藏文件如 `.secrets.yaml.bak`
      以及多级后缀文件如 `credentials.prod.json`），防止通过叠加多重扩展名绕过词干检测；
    - 拦截 `.env*` 全家族（如 `.env`、`.env.local`、`.env.production`、`.env.link`）与 SSH 私钥；
    - 额外拦截 `.agent-apply-` 与 `.agent-patch-` 前缀：防止并发运行的工具读取或覆盖 `_atomic_write`
      正在写入半途的临时交换文件。
    """
    lower = name.casefold()
    stem = Path(name).stem.casefold()
    first_stem = lower.split(".", 1)[0] if not lower.startswith(".") else lower[1:].split(".", 1)[0]
    if (
        lower in _SECRET_NAMES
        or stem in _SENSITIVE_STEMS
        or first_stem in _SENSITIVE_STEMS
        or lower.startswith((".env", "id_rsa", "id_ed25519", ".agent-apply-", ".agent-patch-"))
        or any(lower.endswith(sfx) for sfx in _SECRET_SUFFIXES)
    ):
        return True
    return False


def resolve_host_workspace_path(
    workspace: Path,
    raw_path: str,
    *,
    allow_missing: bool = False,
    allow_directory: bool = False,
) -> tuple[str, Path]:
    """将相对路径（或位于工作区内的绝对路径）解析为安全的 (rel_posix, resolved_path)。

    核心安全设计：五阶纵深路径校验流水线（Lexical Check -> Iterative lstat -> Resolve -> Inode Guard）
    为什么绝不能写出 `target = (workspace / raw_path).resolve()` 然后再检查黑名单？（面试必考）
    1. 软链接改名逃逸漏洞：假设工作区内存在符号链接 `.env.link -> harmless.txt` 或 `sub_link -> /etc`。
       如果先对输入路径调用 `resolve()`，`.env.link` 会先被操作系统解析为 `harmless.txt`，导致随后的
       `is_host_secret_segment` 与 `is_symlink()` 双双漏检！
    2. 绝对路径双前缀纯词法剥离：模型有时会拼出带工作区绝对前缀的路径（在 macOS 上可能是 `/var/folders/...`
       软链接前缀，也可能是 `resolve()` 后的 `/private/var/folders/...` 物理前缀）。此处仅对 `workspace`
       根本身取 `ws_lex` 与 `ws_raw_lex` 两种纯词法 `PurePosixPath` 前缀做 `relative_to` 剥离，全程不触碰
       工作区内部任何子目录的文件系统 `resolve()`，且严禁绝对路径夹带 `.` 或 `..` 段。
    3. 逐段 `is_symlink()`（底层 `lstat`）：拿到干净的相对路径 `pure` 后，从 `ws_root` 出发一次只拼接
       一层目录/文件名并立即检查 `candidate.is_symlink()`，确保从根到叶的每一级祖先与终点本身均非软链接。
    4. `resolve()` 环路与越界兜底：逐段通过后再调 `candidate.resolve()` 捕获罕见的并发符号链接环路并二次
       确认物理路径严格受限于 `ws_root` 内部。
    5. Inode 级硬链接与特殊文件防御：即便不是符号链接，攻击者仍可能在同分区创建指向外部敏感文件的硬链接
       （Hardlink，`is_symlink()` 为 `False` 但共享同一 inode 且 `st_nlink > 1`），或放置命名管道（FIFO）/
       设备节点导致 `read_text()` 永久阻塞挂起。因此对已存在文件强制要求 `is_file()` 且 `st.st_nlink == 1`。
    """
    # 第 1 阶：基础输入净化（拒绝空串、C 字符串截断符 \x00 以及在 POSIX/Windows 间语义分裂的反斜杠 \）
    if not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path or "\\" in raw_path:
        raise HostToolError("unsafe_path", "路径必须是非空的 POSIX 工作区路径。")
    ws_root = Path(workspace).resolve()
    cleaned = raw_path.strip()

    if cleaned in {".", "./"}:
        if not allow_directory:
            raise HostToolError("unsafe_path", "目标需要是具体文件路径而非工作区根目录。")
        return ".", ws_root

    # 第 2 阶：将工作区内绝对路径纯词法折算为工作区相对路径，拒绝跨工作区绝对路径与 ":" 协议伪装
    raw_p = Path(cleaned)
    if raw_p.is_absolute():
        # 严禁在逐段校验前对工作区内部调用 resolve()（否则会跟随软链接绕过 .env 与 symlink 拦截）！
        raw_pure = PurePosixPath(cleaned)
        if any(part in ("", ".", "..") for part in raw_pure.parts[1:]):
            raise HostToolError("unsafe_path", "绝对路径中禁止包含 '..' 或 '.' 跳转段。")
        ws_lex = PurePosixPath(ws_root.as_posix())
        ws_raw_lex = PurePosixPath(Path(workspace).expanduser().absolute().as_posix())
        rel_pure: PurePosixPath | None = None
        for prefix in (ws_lex, ws_raw_lex):
            try:
                rel_pure = raw_pure.relative_to(prefix)
                break
            except ValueError:
                continue
        if rel_pure is None:
            raise HostToolError("unsafe_path", "绝对路径越过了当前工作区边界。")
        if str(rel_pure) == ".":
            if not allow_directory:
                raise HostToolError("unsafe_path", "目标需要是具体文件路径而非工作区根目录。")
            return ".", ws_root
        pure = rel_pure
    else:
        if ":" in cleaned:
            raise HostToolError("unsafe_path", "路径不能包含冒号或 URL 协议。")
        pure = PurePosixPath(cleaned)

    # 第 3 阶：纯词法级目录穿越拦截、禁区隐藏目录（.git/.ssh/.agent-lab）拦截与敏感凭证文件名拦截
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise HostToolError("unsafe_path", "拒绝上级目录跳转或非法相对路径。")
    if any(part in _FORBIDDEN_DOT_PARTS for part in pure.parts):
        raise HostToolError("unsafe_path", "禁止读写 .git 或系统密钥隐藏目录。")
    if any(is_host_secret_segment(part) for part in pure.parts):
        raise HostToolError("secret_access_forbidden", "禁止读取或修改敏感凭证与密钥文件。")

    # 第 4 阶：自顶向下逐段 lstat 校验（拒绝任意层级的符号链接与非目录父节点），随后物理路径兜底验界
    candidate = ws_root
    for idx, part in enumerate(pure.parts):
        candidate = candidate / part
        if candidate.is_symlink():
            raise HostToolError("unsafe_path", "工作区内拒绝操作符号链接路径。")
        is_last = idx == len(pure.parts) - 1
        if not is_last and candidate.exists() and not candidate.is_dir():
            raise HostToolError("unsafe_path", "父级路径段不是有效目录。")

    try:
        resolved = candidate.resolve()
    except RuntimeError as exc:
        raise HostToolError("unsafe_path", "检测到符号链接循环。") from exc
    if resolved != ws_root and not resolved.is_relative_to(ws_root):
        raise HostToolError("unsafe_path", "路径解析越过了工作区边界。")

    # 第 5 阶：目标 Inode 级属性核验（目录开关、拒绝多重硬链接文件 st_nlink != 1、拒绝 FIFO/Socket/设备文件）
    if candidate.exists():
        st = candidate.stat()
        if candidate.is_dir():
            if not allow_directory:
                raise HostToolError("unsafe_path", "目标路径是目录而非普通文件。")
        elif candidate.is_file():
            if st.st_nlink != 1:
                raise HostToolError("unsafe_path", "拒绝操作多重硬链接文件。")
        else:
            raise HostToolError("unsafe_path", "拒绝操作非常规特殊文件。")
    elif not allow_missing:
        raise HostToolError("not_found", f"路径不存在：{pure.as_posix()}")

    return pure.as_posix(), candidate


# 面向大模型的宿主工作区工具 JSON Schema 契约定义（仅在 execution_profile 为 host / computer 时挂载）。
# 设计要点：
# 1. 默认 sandbox 档位不加载此列表，保证轻量沙盒场景的 System/Tools 前缀紧凑并维持 KV-Cache 稳定；
# 2. 统一开启 `additionalProperties: False` 与字段长度上限（Draft 2020-12），在进入业务函数前即由
#    JSON Schema 校验器拦截模型幻觉拼出的未知参数或超大载荷。
HOST_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "fs_read",
        "description": "读取工作区内任意代码、配置、脚本或测试文件（最大 512 KiB），支持按行号分页读取并带行号返回。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 320, "description": "工作区相对路径。"},
                "offset_line": {"type": "integer", "minimum": 1, "description": "起始行号（从 1 开始，默认 1）。"},
                "limit_lines": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 500,
                    "description": "最大读取行数（默认 200）。",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_write",
        "description": "创建或完整覆写工作区内的文本、代码、脚本或测试文件（最大 512 KiB）；ask 模式下生成待审批提案。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 320, "description": "工作区相对路径。"},
                "content": {"type": "string", "maxLength": 524288, "description": "完整 UTF-8 文件内容。"},
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_edit",
        "description": "精确替换工作区文件中的文本片段（默认要求唯一匹配，支持 replace_all）；ask 模式下生成待审批提案。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 320, "description": "工作区相对路径。"},
                "old_text": {"type": "string", "minLength": 1, "maxLength": 65536, "description": "待替换原文片段。"},
                "new_text": {"type": "string", "maxLength": 65536, "description": "替换后的新文本。"},
                "replace_all": {"type": "boolean", "description": "是否替换全部出现位置（默认 false）。"},
            },
            "required": ["path", "old_text", "new_text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_sketch_edit",
        "description": "Fast-Apply 草图编辑：用 `// ... existing code ...` 或 `# ... existing code ...` 省略未修改片段，基于有序锚点确定性拼接大文件修改，减少完整重写与空白错位。",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1, "maxLength": 240, "description": "工作区相对路径。"},
                "sketch": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 262144,
                    "description": "含省略标记（如 `# ... existing code ...` 或 `// ... existing code ...`）与清晰前后锚点行的编辑草图。",
                },
            },
            "required": ["path", "sketch"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_glob",
        "description": "按通配符模式（如 **/*.py、src/**/*.ts）快速匹配工作区文件列表，自动遵循 .gitignore 并过滤敏感文件。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "minLength": 1, "maxLength": 200, "description": "Glob 匹配模式。"},
                "path": {"type": "string", "maxLength": 240, "description": "可选子目录起点，默认 '.'。"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "fs_grep",
        "description": "在工作区文本文件中使用正则表达式搜索匹配行，返回文件路径、行号与代码片段。",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "minLength": 1, "maxLength": 240, "description": "正则表达式。"},
                "path": {"type": "string", "maxLength": 240, "description": "可选子目录或单文件路径，默认 '.'。"},
                "glob": {"type": "string", "maxLength": 120, "description": "可选文件名过滤（如 *.py）。"},
                "case_sensitive": {"type": "boolean", "description": "是否区分大小写（默认 true）。"},
            },
            "required": ["pattern"],
            "additionalProperties": False,
        },
    },
    {
        "name": "bash_exec",
        "description": "在本机工作区目录执行终端命令或运行脚本（自动剥离服务端密钥环境变量、启用 OS 内核沙箱与进程组回收；支持后台常驻与 PTY 交互模式）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "minLength": 1, "maxLength": 8000, "description": "要执行的终端命令。"},
                "cwd": {"type": "string", "maxLength": 240, "description": "工作区内的相对子目录，默认 '.'。"},
                "timeout_seconds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 120,
                    "description": "命令超时秒数（默认 30，最大 120）。",
                },
                "run_in_background": {
                    "type": "boolean",
                    "description": "是否在后台异步启动常驻进程并返回 proc_id（默认 false）。",
                },
                "use_pty": {
                    "type": "boolean",
                    "description": "后台启动时是否分配伪终端 PTY 以支持交互式 REPL/CLI（默认 false）。",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
]

BACKGROUND_PROCESS_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "bash_output",
        "description": "增量读取后台进程（run_in_background 启动）的环形缓冲日志输出与最新运行状态。",
        "input_schema": {
            "type": "object",
            "properties": {
                "proc_id": {"type": "string", "minLength": 1, "maxLength": 80, "description": "后台进程 ID。"},
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "单调字符偏移量（默认 0，可传入上次返回的 next_offset 持续追踪）。",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 64000,
                    "description": "本次最多读取的字符数（默认 8000）。",
                },
            },
            "required": ["proc_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "bash_write",
        "description": "向正在运行的后台进程 stdin（或 PTY）写入输入文本（如交互式命令与换行符）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "proc_id": {"type": "string", "minLength": 1, "maxLength": 80, "description": "后台进程 ID。"},
                "data": {"type": "string", "maxLength": 16000, "description": "要写入进程标准输入的文本数据。"},
            },
            "required": ["proc_id", "data"],
            "additionalProperties": False,
        },
    },
    {
        "name": "bash_kill",
        "description": "强制终止指定后台进程及其整个子进程组（SIGTERM -> SIGKILL）并释放句柄。",
        "input_schema": {
            "type": "object",
            "properties": {
                "proc_id": {"type": "string", "minLength": 1, "maxLength": 80, "description": "要终止的后台进程 ID。"},
            },
            "required": ["proc_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "bash_list",
        "description": "列出当前运行会话内全部后台进程的 ID、PID、命令、工作目录与存活状态。",
        "input_schema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
]

HOST_TOOL_SPECS.extend(BACKGROUND_PROCESS_TOOL_SPECS)

WEB_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "web_fetch",
        "description": "SSRF 防护安全抓取指定 HTTP(S) 网页并提取轻量可读 Markdown（拒绝私网/回环/元数据 IP，内容包裹在 untrusted_web_content 中）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 1, "maxLength": 2048, "description": "目标 http:// 或 https:// URL。"},
                "max_chars": {
                    "type": "integer",
                    "minimum": 200,
                    "maximum": 64000,
                    "description": "返回正文最大字符数（默认 16000）。",
                },
                "timeout": {
                    "type": "number",
                    "minimum": 1.0,
                    "maximum": 60.0,
                    "description": "单次请求超时秒数（默认 15.0）。",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "web_search",
        "description": "通过配置的搜索后端（AGENT_LAB_WEB_SEARCH_URL / Brave / Tavily / Serper）检索外部网页摘要。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 500, "description": "搜索关键词。"},
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "最大返回结果条数（默认 5）。",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
]

LSP_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "lsp_query",
        "description": "通过本地 Language Server Protocol (LSP) 执行语义代码导航与静态诊断（跳转定义、查找引用、悬停类型、文档/工作区符号与实时诊断）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": [
                        "goToDefinition",
                        "findReferences",
                        "hover",
                        "documentSymbol",
                        "workspaceSymbol",
                        "diagnostics",
                    ],
                    "description": "LSP 查询操作类型。",
                },
                "file_path": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 320,
                    "description": "工作区内相对文件路径。",
                },
                "line": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-based 行号（goToDefinition / findReferences / hover 必填，默认 1）。",
                },
                "character": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "1-based 列号（goToDefinition / findReferences / hover 必填，默认 1）。",
                },
                "query_text": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "workspaceSymbol 时的符号搜索词。",
                },
            },
            "required": ["operation", "file_path"],
            "additionalProperties": False,
        },
    },
]


def _walk_workspace_files(workspace: Path, start_dir: Path) -> list[tuple[str, Path]]:
    """自顶向下安全遍历工作区文本候选文件，原生感知 `.gitignore` 并原地剪枝高噪/敏感子树。

    工程与性能关键点（Why In-Place Subtree Pruning）：
    1. `os.walk(..., followlinks=False)` 禁止跟随符号链接目录，杜绝软链接环路死循环与越界扫描；
    2. `subdirs[:] = kept_dirs` 必须采用切片原地修改（In-place Mutation）：这会直接通知 `os.walk`
       生成器在下一轮迭代前裁剪掉 `.git`、`node_modules`、`.venv`、敏感目录及命中 `.gitignore` 的
       构建产物目录，将原本需要扫描数万个依赖文件的 $O(N)$ I/O 开销降为目录入口级的 $O(1)$ 跳过；
    3. 全程对 `subdirs` 与 `names` 调用 `sorted()`，确保跨平台文件系统返回顺序确定一致，避免工具输出
       顺序抖动破坏外层对话历史的增量推理确定性；
    4. 对叶子文件同步执行软链接跳过、`is_host_secret_segment` 过滤、`st_nlink == 1` 硬链接拦截与
       512 KiB 体积上限过滤，使上层 `fs_glob` 与 `fs_grep` 天然继承相同的安全基线。
    """
    ws_root = Path(workspace).resolve()
    patterns = _load_gitignore_patterns(ws_root)
    results: list[tuple[str, Path]] = []
    for root_str, subdirs, names in os.walk(start_dir, followlinks=False):
        cur_dir = Path(root_str)
        kept_dirs: list[str] = []
        for d in sorted(subdirs):
            if d in _ALWAYS_SKIP_DIRS or d in _FORBIDDEN_DOT_PARTS or is_host_secret_segment(d):
                continue
            sub_p = cur_dir / d
            if sub_p.is_symlink():
                continue
            sub_rel = sub_p.relative_to(ws_root).as_posix()
            if _matches_gitignore(sub_rel, is_dir=True, patterns=patterns):
                continue
            kept_dirs.append(d)
        # 原地覆写 subdirs 列表引用内容，驱动 os.walk 跳过已被过滤的整棵子树
        subdirs[:] = kept_dirs

        for fname in sorted(names):
            if is_host_secret_segment(fname):
                continue
            fpath = cur_dir / fname
            if fpath.is_symlink():
                continue
            rel = fpath.relative_to(ws_root).as_posix()
            if _matches_gitignore(rel, is_dir=False, patterns=patterns):
                continue
            try:
                st = fpath.stat()
            except OSError:
                # 容忍遍历期间并发删除（如临时编译产物消失）引发的瞬态 stat 失败
                continue
            if st.st_nlink == 1 and st.st_size <= _HOST_MAX_FILE_BYTES and fpath.is_file():
                results.append((rel, fpath))
    return results


def execute_fs_read(
    workspace: Path,
    *,
    path: str,
    offset_line: int = 1,
    limit_lines: int = 200,
) -> dict[str, Any]:
    """按行号窗口分页读取工作区内 UTF-8 文本文件，并返回带 `1-based` 行号前缀的内容与 SHA-256 摘要。

    为什么默认限制 200 行并附带 `行号\\t内容` 格式？
    大模型若一次性读入数千行源码会迅速耗尽上下文预算并引发“中间迷失（Lost in the Middle）”注意力衰减；
    分页窗口配合行号前缀既方便模型按需翻页浏览大文件，又能为其后续调用 `fs_edit` 提供精确的上下文定位参考。
    """
    rel, target = resolve_host_workspace_path(workspace, path)
    st = target.stat()
    if st.st_size > _HOST_MAX_FILE_BYTES:
        raise HostToolError("file_too_large", f"文件超过 {_HOST_MAX_FILE_BYTES // 1024} KiB 上限。")
    try:
        raw_text = target.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise HostToolError("binary_file", "目标文件不是 UTF-8 文本文件。") from exc

    all_lines = raw_text.splitlines()
    total_lines = len(all_lines)
    start_idx = max(0, int(offset_line) - 1)
    end_idx = min(total_lines, start_idx + max(1, min(500, int(limit_lines))))
    selected = all_lines[start_idx:end_idx]
    numbered = "\n".join(f"{start_idx + i + 1}\t{line}" for i, line in enumerate(selected))
    return {
        "ok": True,
        "path": rel,
        "sha256": digest(raw_text),
        "total_lines": total_lines,
        "start_line": start_idx + 1 if total_lines else 0,
        "end_line": end_idx,
        "truncated": end_idx < total_lines or start_idx > 0,
        "content": numbered,
    }


def _atomic_write_host_file(target: Path, content: str) -> None:
    """使用 POSIX `mkstemp + fsync + chmod + os.replace` 四步协议原子且持久化地写入目标文件。

    为什么不能直接 `target.write_text(content, encoding="utf-8")`？（面试高频系统编程考点）
    1. 防崩溃半截文件（Crash Consistency）：直接 `open(target, "w")` 会在写入前先执行 `O_TRUNC` 将原文件
       截断为 0 字节；若此刻发生进程崩溃、超时强杀或宿主机断电，原文件内容将彻底丢失。
    2. 同文件系统原子替换（Same-FS Atomic Rename）：`tempfile.mkstemp(dir=target.parent)` 强制在目标文件
       同一父目录下以 `O_CREAT | O_EXCL | 0600` 原子创建唯一临时文件，保证临时文件与目标文件必定处于同一
       挂载分区（避免 `os.replace` 跨文件系统触发 `EXDEV` 失败），且 `.agent-patch-` 前缀会被
       `is_host_secret_segment` 自动对其他并发工具隐藏。
    3. 双层刷盘保证（Durability）：`handle.flush()` 将 Python 用户态缓冲区推入内核 Page Cache，紧接着
       `os.fsync(handle.fileno())` 强制内核脏页落入物理存储介质，避免“先改目录项但新文件数据尚未落盘”导致
       掉电后读出全零坏文件。
    4. 权限继承与原子切换：继承已存在文件的权限位（新建 `.sh` 默认赋予 `0o755` 可执行位，其余默认 `0o644`），
       最后调用 `os.replace`（底层 `rename(2)`）原子更新目录项指针；异常分支由 `finally` 兜底清理残留临时文件。
    """
    encoded = content.encode("utf-8")
    if len(encoded) > _HOST_MAX_FILE_BYTES:
        raise HostToolError("file_too_large", f"写入内容超过 {_HOST_MAX_FILE_BYTES // 1024} KiB 上限。")
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = (target.stat().st_mode & 0o777) if target.exists() else (
        0o755 if target.suffix == ".sh" else 0o644
    )
    fd, tmp_name = tempfile.mkstemp(prefix=".agent-patch-", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, existing_mode)
        os.replace(tmp_name, target)
    finally:
        if os.path.exists(tmp_name):
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)


def execute_fs_write(
    workspace: Path,
    storage: Storage,
    run_id: str,
    *,
    path: str,
    content: str,
    permission_mode: str,
    cross_lock_cm: Any,
    call_id: str | None = None,
    expected_epoch: int | None = None,
    on_before_mutate: Callable[[Path, bytes | None, bool], None] | None = None,
    rule_engine: PermissionRuleEngine | None = None,
    session_grants: Sequence[str] | None = None,
) -> dict[str, Any]:
    """创建或全量覆写工作区文件：在 `ask` 模式下生成可审计补丁提案，在自动模式下持跨进程锁原子落盘。

    并发与租约防护设计（Concurrency & Epoch Fencing）：
    - `ask` 分支：仅在内存中计算 `unified_diff`，并将完整新旧快照与幂等键 `operation_id=call_id`、
      租约代数 `expected_epoch` 写入持久化账本（`create_changeset_proposal`），磁盘原文件纹丝不动。
    - 直写分支（`auto-edit` / `full-auto`）：必须先进入工作区级跨进程文件锁 `with cross_lock_cm:`（底层
      `fcntl.flock`），随后第一件事调用 `storage.verify_epoch(run_id, expected_epoch)`——若当前 Worker
      曾因网络卡顿或 GC 停顿导致运行租约过期并被新 Worker 接管（epoch 已递增），旧 Worker 会在落盘前被
      立即栅栏拦截，彻底杜绝分布式/多进程场景下的脑裂脏写（Stale Worker Split-Brain）。且路径重解析与
      旧文件读取均置于同一把锁内，消除检查与写入之间的 TOCTOU 竞态窗口。
    - 检查点钩子：若传入 `on_before_mutate(target, pre_bytes, existed)`，会在跨进程锁临界区内、
      原子替换文件前一瞬被调用，确保检查点精确记录修改前的原始字节与存在状态。
    """
    if len(content.encode("utf-8")) > _HOST_MAX_FILE_BYTES:
        raise HostToolError("file_too_large", f"写入内容超过 {_HOST_MAX_FILE_BYTES // 1024} KiB 上限。")

    perm = evaluate_tool_permission(
        "fs_write",
        {"path": path},
        permission_mode=permission_mode,
        rule_engine=rule_engine,
        session_grants=session_grants,
    )
    if perm["decision"] == "deny":
        return {
            "ok": False,
            "code": "permission_denied",
            "path": path,
            "error": perm["reason"],
        }
    if perm["decision"] == "ask":
        rel, target = resolve_host_workspace_path(workspace, path, allow_missing=True)
        existed = target.exists()
        old_content = ""
        if existed:
            if target.stat().st_size > _HOST_MAX_FILE_BYTES:
                raise HostToolError("file_too_large", "原文件超过 512 KiB，拒绝直接覆盖。")
            try:
                old_content = target.read_text(encoding="utf-8")
            except UnicodeError as exc:
                raise HostToolError("binary_file", "拒绝覆盖非 UTF-8 二进制文件。") from exc
        action = "modify" if existed else "create"
        diff_text = "".join(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{rel}" if existed else "/dev/null",
                tofile=f"b/{rel}",
            )
        )
        proposal = storage.create_changeset_proposal(
            run_id,
            [{"path": rel, "action": action, "old_content": old_content, "new_content": content}],
            summary=f"fs_write ({action}) {rel}",
            operation_id=call_id,
            expected_epoch=expected_epoch,
            tool_name="fs_write",
        )
        storage.add_event(
            run_id,
            "patch_proposed",
            {
                "proposal_id": proposal["id"],
                "kind": proposal["kind"],
                "path": rel,
                "paths": [rel],
                "diff": diff_text[:6000],
            },
        )
        return {
            "ok": True,
            "requires_approval": True,
            "proposal_id": proposal["id"],
            "path": rel,
            "action": action,
            "diff": diff_text[:4000],
        }

    # 进入工作区级跨进程排他锁（flock），将“Epoch 租约栅栏核验 -> 路径安全校验 -> 原子写盘”串行化
    with cross_lock_cm:
        storage.verify_epoch(run_id, expected_epoch)
        rel, target = resolve_host_workspace_path(workspace, path, allow_missing=True)
        existed = target.exists()
        old_content = ""
        pre_bytes: bytes | None = None
        if existed:
            if target.stat().st_size > _HOST_MAX_FILE_BYTES:
                raise HostToolError("file_too_large", "原文件超过 512 KiB，拒绝直接覆盖。")
            pre_bytes = target.read_bytes()
            try:
                old_content = pre_bytes.decode("utf-8")
            except UnicodeError as exc:
                raise HostToolError("binary_file", "拒绝覆盖非 UTF-8 二进制文件。") from exc
        action = "modify" if existed else "create"
        diff_text = "".join(
            difflib.unified_diff(
                old_content.splitlines(keepends=True),
                content.splitlines(keepends=True),
                fromfile=f"a/{rel}" if existed else "/dev/null",
                tofile=f"b/{rel}",
            )
        )
        if on_before_mutate is not None:
            on_before_mutate(target, pre_bytes, existed)
        _atomic_write_host_file(target, content)
    new_sha = digest(content)
    storage.add_event(
        run_id,
        "file_written",
        {"tool": "fs_write", "path": rel, "action": action, "sha256": new_sha, "bytes": len(content.encode("utf-8"))},
    )
    return {
        "ok": True,
        "path": rel,
        "action": action,
        "sha256": new_sha,
        "bytes": len(content.encode("utf-8")),
        "diff": diff_text[:3000],
    }


_SKETCH_MARKER_RE = re.compile(
    r"^[ \t]*(?://|#|/\*|<!--)?[ \t]*\.\.\.[ \t]*(?:existing code|unchanged|omitted|rest of file)?[ \t]*\.\.\.[ \t]*(?:\*/|-->)?[ \t]*$",
    re.IGNORECASE,
)


def _common_indent(lines: list[str]) -> str:
    indents = [len(ln) - len(ln.lstrip(" \t")) for ln in lines if ln.strip()]
    if not indents:
        return ""
    prefix_len = min(indents)
    for ln in lines:
        if ln.strip():
            return ln[:prefix_len]
    return ""


def _dedent_lines(lines: list[str]) -> tuple[list[str], str]:
    base = _common_indent(lines)
    if not base:
        return list(lines), ""
    out: list[str] = []
    for ln in lines:
        if ln.startswith(base):
            out.append(ln[len(base) :])
        elif not ln.strip():
            out.append("")
        else:
            out.append(ln.lstrip(" \t"))
    return out, base


def match_and_replace_tiered(
    original: str,
    old_text: str,
    new_text: str,
    *,
    replace_all: bool = False,
) -> tuple[str | None, str, int, str | None]:
    """确定性四级局部编辑匹配器（严格拒绝多处歧义匹配，绝不静默改错位置）。

    层级顺序：
    - Tier 1 (`exact`)：原始精确子串匹配；
    - Tier 2 (`crlf_normalized`)：统一 `\\r\\n -> \\n` 换行符匹配并保留文件原有主换行格式；
    - Tier 3 (`trailing_ws_trimmed`)：按连续行窗口剥离行尾空白匹配；
    - Tier 4 (`indent_relative`)：按相对缩进结构唯一匹配，并以目标窗口基准缩进重缩进 `new_text`。
    """
    if not old_text:
        return None, "none", 0, "old_text_not_found"

    # Tier 1: exact verbatim substring match
    cnt = original.count(old_text)
    if cnt == 1 or (cnt > 1 and replace_all):
        updated = original.replace(old_text, new_text) if replace_all else original.replace(old_text, new_text, 1)
        return updated, "exact", (cnt if replace_all else 1), None
    if cnt > 1:
        return None, "ambiguous", cnt, "ambiguous_match"

    # Tier 2: CRLF/LF normalized match
    dominant_nl = "\r\n" if "\r\n" in original else "\n"
    norm_orig = original.replace("\r\n", "\n")
    norm_old = old_text.replace("\r\n", "\n")
    norm_new = new_text.replace("\r\n", "\n")
    cnt2 = norm_orig.count(norm_old)
    if cnt2 == 1 or (cnt2 > 1 and replace_all):
        updated_norm = norm_orig.replace(norm_old, norm_new) if replace_all else norm_orig.replace(norm_old, norm_new, 1)
        updated = updated_norm.replace("\n", dominant_nl) if dominant_nl == "\r\n" else updated_norm
        return updated, "crlf_normalized", (cnt2 if replace_all else 1), None
    if cnt2 > 1:
        return None, "ambiguous", cnt2, "ambiguous_match"

    if replace_all:
        # 多处批量替换要求明确字面量或换行一致，避免模糊层级大范围误伤
        return None, "none", 0, "old_text_not_found"

    orig_lines = norm_orig.splitlines(keepends=True)
    old_lines = norm_old.splitlines(keepends=False)
    if not old_lines or len(old_lines) > len(orig_lines):
        return None, "none", 0, "old_text_not_found"
    win_len = len(old_lines)

    # Tier 3: trailing-whitespace-trimmed contiguous line window
    old_rstripped = [ln.rstrip(" \t") for ln in old_lines]
    t3_hits: list[int] = []
    for idx in range(len(orig_lines) - win_len + 1):
        cand = [orig_lines[idx + j].rstrip("\r\n").rstrip(" \t") for j in range(win_len)]
        if cand == old_rstripped:
            t3_hits.append(idx)
    if len(t3_hits) == 1:
        hit = t3_hits[0]
        had_trailing_nl = orig_lines[hit + win_len - 1].endswith("\n")
        rep_str = norm_new
        if had_trailing_nl and rep_str and not rep_str.endswith("\n"):
            rep_str += "\n"
        rebuilt = "".join(orig_lines[:hit]) + rep_str + "".join(orig_lines[hit + win_len :])
        if dominant_nl == "\r\n":
            rebuilt = rebuilt.replace("\n", "\r\n")
        return rebuilt, "trailing_ws_trimmed", 1, None
    if len(t3_hits) > 1:
        return None, "ambiguous", len(t3_hits), "ambiguous_match"

    # Tier 4: relative-indentation-preserving unique window match
    old_dedented, _ = _dedent_lines([ln.rstrip(" \t") for ln in old_lines])
    if not any(ln.strip() for ln in old_dedented):
        return None, "none", 0, "old_text_not_found"
    t4_hits: list[tuple[int, str]] = []
    for idx in range(len(orig_lines) - win_len + 1):
        raw_win = [orig_lines[idx + j].rstrip("\r\n").rstrip(" \t") for j in range(win_len)]
        cand_dedented, base_ind = _dedent_lines(raw_win)
        if cand_dedented == old_dedented:
            t4_hits.append((idx, base_ind))
    if len(t4_hits) == 1:
        hit, base_ind = t4_hits[0]
        new_dedented, _ = _dedent_lines(norm_new.splitlines(keepends=False))
        reindented_lines = [(base_ind + ln) if ln.strip() else "" for ln in new_dedented]
        had_trailing_nl = orig_lines[hit + win_len - 1].endswith("\n")
        rep_str = "\n".join(reindented_lines) + ("\n" if had_trailing_nl else "")
        rebuilt = "".join(orig_lines[:hit]) + rep_str + "".join(orig_lines[hit + win_len :])
        if dominant_nl == "\r\n":
            rebuilt = rebuilt.replace("\n", "\r\n")
        return rebuilt, "indent_relative", 1, None
    if len(t4_hits) > 1:
        return None, "ambiguous", len(t4_hits), "ambiguous_match"
    return None, "none", 0, "old_text_not_found"


def apply_sketch_edit(original: str, sketch: str) -> tuple[str, dict[str, Any]]:
    """Fast-Apply 风格草图拼接器：用 `... existing code ...` 省略未改代码并按前后锚点确定性回填。"""
    dominant_nl = "\r\n" if "\r\n" in original else "\n"
    orig_norm = original.replace("\r\n", "\n")
    sketch_norm = sketch.replace("\r\n", "\n")
    orig_lines = orig_norm.splitlines()
    sketch_lines = sketch_norm.splitlines()

    # 将草图按省略标记切分为有序块；若不含任何省略标记则视为全文件替换
    segments: list[list[str]] = []
    cur: list[str] = []
    marker_count = 0
    for ln in sketch_lines:
        if _SKETCH_MARKER_RE.match(ln):
            marker_count += 1
            segments.append(cur)
            cur = []
        else:
            cur.append(ln)
    segments.append(cur)

    if marker_count == 0:
        rebuilt = sketch_norm
        if original.endswith("\n") and not rebuilt.endswith("\n"):
            rebuilt += "\n"
        if dominant_nl == "\r\n":
            rebuilt = rebuilt.replace("\n", "\r\n")
        return rebuilt, {"mode": "full_replace", "markers": 0, "hunks_applied": 1}

    def _find_line_anchor(needle: str, start_idx: int) -> int:
        n_trim = needle.rstrip(" \t")
        if not n_trim.strip():
            return -1
        for i in range(start_idx, len(orig_lines)):
            if orig_lines[i].rstrip(" \t") == n_trim:
                return i
        return -1

    result_lines: list[str] = []
    orig_pos = 0
    hunks_applied = 0

    for seg_idx, seg in enumerate(segments):
        is_first = seg_idx == 0
        is_last = seg_idx == len(segments) - 1
        non_empty_indices = [i for i, ln in enumerate(seg) if ln.strip()]
        if not non_empty_indices:
            if is_last and orig_pos < len(orig_lines):
                result_lines.extend(orig_lines[orig_pos:])
                orig_pos = len(orig_lines)
            continue

        first_anchor_pos = non_empty_indices[0]
        last_anchor_pos = non_empty_indices[-1]
        if is_first:
            start_orig = 0
        else:
            start_orig = _find_line_anchor(seg[first_anchor_pos], orig_pos)
            if start_orig < 0:
                raise HostToolError(
                    "sketch_anchor_not_found",
                    f"草图第 {seg_idx + 1} 段的起始锚点行未在原文件中找到：{seg[first_anchor_pos][:80]!r}",
                )
            # 保留上一个结束位置到当前起始锚点之前的原有代码
            result_lines.extend(orig_lines[orig_pos:start_orig])

        if is_last:
            result_lines.extend(seg)
            orig_pos = len(orig_lines)
            hunks_applied += 1
        else:
            end_search_from = start_orig + (1 if last_anchor_pos > first_anchor_pos else 0)
            end_orig = _find_line_anchor(seg[last_anchor_pos], end_search_from)
            if end_orig < 0:
                raise HostToolError(
                    "sketch_anchor_not_found",
                    f"草图第 {seg_idx + 1} 段的结束锚点行未在原文件中找到：{seg[last_anchor_pos][:80]!r}",
                )
            result_lines.extend(seg)
            orig_pos = end_orig + 1
            hunks_applied += 1

    rebuilt = "\n".join(result_lines)
    if original.endswith("\n") and not rebuilt.endswith("\n"):
        rebuilt += "\n"
    if dominant_nl == "\r\n":
        rebuilt = rebuilt.replace("\n", "\r\n")
    return rebuilt, {"mode": "sketch_merge", "markers": marker_count, "hunks_applied": hunks_applied}


def execute_fs_edit(
    workspace: Path,
    storage: Storage,
    run_id: str,
    *,
    path: str,
    old_text: str,
    new_text: str,
    replace_all: bool = False,
    permission_mode: str,
    cross_lock_cm: Any,
    call_id: str | None = None,
    expected_epoch: int | None = None,
    on_before_mutate: Callable[[Path, bytes | None, bool], None] | None = None,
    rule_engine: PermissionRuleEngine | None = None,
    session_grants: Sequence[str] | None = None,
) -> dict[str, Any]:
    """基于四级确定性锚点（exact -> crlf -> trailing_ws -> indent_relative）的局部代码编辑工具。"""
    if old_text == new_text:
        raise HostToolError("empty_patch", "old_text 与 new_text 完全相同，未产生修改。")

    def _compute_edit() -> tuple[str, Path, bytes, str, str, int, str, str]:
        rel_p, tgt = resolve_host_workspace_path(workspace, path, allow_missing=False)
        if tgt.stat().st_size > _HOST_MAX_FILE_BYTES:
            raise HostToolError("file_too_large", "目标文件超过 512 KiB 上限。")
        raw_bytes = tgt.read_bytes()
        try:
            old_c = raw_bytes.decode("utf-8")
        except UnicodeError as exc:
            raise HostToolError("binary_file", "目标文件不是 UTF-8 文本文件。") from exc
        new_c, match_tier, cnt, err_code = match_and_replace_tiered(
            old_c, old_text, new_text, replace_all=replace_all
        )
        if err_code == "ambiguous_match":
            raise HostToolError(
                "ambiguous_match",
                f"old_text 在文件中出现了 {cnt} 次；请提供更多上下文使其唯一，或显式传 replace_all=true。",
            )
        if err_code == "old_text_not_found" or new_c is None:
            raise HostToolError("old_text_not_found", "old_text 在目标文件中未找到，请先用 fs_read 核对最新内容。")
        if len(new_c.encode("utf-8")) > _HOST_MAX_FILE_BYTES:
            raise HostToolError("file_too_large", "编辑后文件超过 512 KiB 上限。")
        diff_t = "".join(
            difflib.unified_diff(
                old_c.splitlines(keepends=True),
                new_c.splitlines(keepends=True),
                fromfile=f"a/{rel_p}",
                tofile=f"b/{rel_p}",
            )
        )
        return rel_p, tgt, raw_bytes, old_c, new_c, cnt, diff_t, match_tier

    perm = evaluate_tool_permission(
        "fs_edit",
        {"path": path},
        permission_mode=permission_mode,
        rule_engine=rule_engine,
        session_grants=session_grants,
    )
    if perm["decision"] == "deny":
        return {
            "ok": False,
            "code": perm.get("code") or "permission_denied",
            "path": path,
            "error": perm["reason"],
        }
    if perm["decision"] == "ask":
        rel, _, _, old_content, new_content, rep_count, diff_text, match_tier = _compute_edit()
        proposal = storage.create_changeset_proposal(
            run_id,
            [{"path": rel, "action": "modify", "old_content": old_content, "new_content": new_content}],
            summary=f"fs_edit {rel} ({rep_count} replacement, tier={match_tier})",
            operation_id=call_id,
            expected_epoch=expected_epoch,
            tool_name="fs_edit",
        )
        storage.add_event(
            run_id,
            "patch_proposed",
            {
                "proposal_id": proposal["id"],
                "kind": proposal["kind"],
                "path": rel,
                "paths": [rel],
                "diff": diff_text[:6000],
            },
        )
        return {
            "ok": True,
            "requires_approval": True,
            "proposal_id": proposal["id"],
            "path": rel,
            "replacements": rep_count,
            "match_tier": match_tier,
            "diff": diff_text[:4000],
        }

    # 持跨进程排他锁并核验 Epoch 后再执行 Read-Modify-Write，严防并发编辑丢失更新（Lost Update）
    with cross_lock_cm:
        storage.verify_epoch(run_id, expected_epoch)
        rel, target, pre_bytes, _, new_content, rep_count, diff_text, match_tier = _compute_edit()
        if on_before_mutate is not None:
            on_before_mutate(target, pre_bytes, True)
        _atomic_write_host_file(target, new_content)
    new_sha = digest(new_content)
    storage.add_event(
        run_id,
        "file_written",
        {
            "tool": "fs_edit",
            "path": rel,
            "action": "modify",
            "sha256": new_sha,
            "replacements": rep_count,
            "match_tier": match_tier,
        },
    )
    return {
        "ok": True,
        "path": rel,
        "replacements": rep_count,
        "match_tier": match_tier,
        "sha256": new_sha,
        "diff": diff_text[:3000],
    }


def execute_fs_sketch_edit(
    workspace: Path,
    storage: Storage,
    run_id: str,
    *,
    path: str,
    sketch: str,
    permission_mode: str,
    cross_lock_cm: Any,
    call_id: str | None = None,
    expected_epoch: int | None = None,
    on_before_mutate: Callable[[Path, bytes | None, bool], None] | None = None,
    rule_engine: PermissionRuleEngine | None = None,
    session_grants: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Fast-Apply 风格草图编辑执行器：支持 `... existing code ...` 省略标记与完整审批/检查点闭环。"""
    def _compute_sketch() -> tuple[str, Path, bytes, str, str, dict[str, Any], str]:
        rel_p, tgt = resolve_host_workspace_path(workspace, path, allow_missing=False)
        if tgt.stat().st_size > _HOST_MAX_FILE_BYTES:
            raise HostToolError("file_too_large", "目标文件超过 512 KiB 上限。")
        raw_bytes = tgt.read_bytes()
        try:
            old_c = raw_bytes.decode("utf-8")
        except UnicodeError as exc:
            raise HostToolError("binary_file", "目标文件不是 UTF-8 文本文件。") from exc
        new_c, merge_meta = apply_sketch_edit(old_c, sketch)
        if new_c == old_c:
            raise HostToolError("empty_patch", "草图合并结果与原文件完全一致，未产生修改。")
        if len(new_c.encode("utf-8")) > _HOST_MAX_FILE_BYTES:
            raise HostToolError("file_too_large", "编辑后文件超过 512 KiB 上限。")
        diff_t = "".join(
            difflib.unified_diff(
                old_c.splitlines(keepends=True),
                new_c.splitlines(keepends=True),
                fromfile=f"a/{rel_p}",
                tofile=f"b/{rel_p}",
            )
        )
        return rel_p, tgt, raw_bytes, old_c, new_c, merge_meta, diff_t

    perm = evaluate_tool_permission(
        "fs_sketch_edit",
        {"path": path},
        permission_mode=permission_mode,
        rule_engine=rule_engine,
        session_grants=session_grants,
    )
    if perm["decision"] == "deny":
        return {
            "ok": False,
            "code": perm.get("code") or "permission_denied",
            "path": path,
            "error": perm["reason"],
        }
    if perm["decision"] == "ask":
        rel, _, _, old_content, new_content, merge_meta, diff_text = _compute_sketch()
        proposal = storage.create_changeset_proposal(
            run_id,
            [{"path": rel, "action": "modify", "old_content": old_content, "new_content": new_content}],
            summary=f"fs_sketch_edit {rel} ({merge_meta['mode']})",
            operation_id=call_id,
            expected_epoch=expected_epoch,
            tool_name="fs_sketch_edit",
        )
        storage.add_event(
            run_id,
            "patch_proposed",
            {
                "proposal_id": proposal["id"],
                "kind": proposal["kind"],
                "path": rel,
                "paths": [rel],
                "diff": diff_text[:6000],
            },
        )
        return {
            "ok": True,
            "requires_approval": True,
            "proposal_id": proposal["id"],
            "path": rel,
            "merge_meta": merge_meta,
            "diff": diff_text[:4000],
        }

    with cross_lock_cm:
        storage.verify_epoch(run_id, expected_epoch)
        rel, target, pre_bytes, _, new_content, merge_meta, diff_text = _compute_sketch()
        if on_before_mutate is not None:
            on_before_mutate(target, pre_bytes, True)
        _atomic_write_host_file(target, new_content)
    new_sha = digest(new_content)
    storage.add_event(
        run_id,
        "file_written",
        {"tool": "fs_sketch_edit", "path": rel, "action": "modify", "sha256": new_sha, **merge_meta},
    )
    return {
        "ok": True,
        "path": rel,
        "merge_meta": merge_meta,
        "sha256": new_sha,
        "diff": diff_text[:3000],
    }


def _glob_matches(rel_posix: str, pattern: str) -> bool:
    """对齐现代 CLI（如 ripgrep / gitignore）直觉的增强型 Glob 匹配器。

    为什么不能只调标准库 `fnmatch.fnmatchcase`？
    标准 `fnmatch` 将 `**` 当作普通单层通配符看待，导致 `**/*.py` 无法匹配仓库根目录下的 `main.py`
    （因为它强行要求至少存在一个 `/`），也会导致 `src/**/foo.py` 无法匹配零层子目录的 `src/foo.py`。
    本函数补充了前导 `**/` 剥离、无斜杠纯文件名匹配以及中间 `/**/` 零层目录折叠三条语义规则。
    """
    norm_pat = pattern.strip().lstrip("./")
    if fnmatch.fnmatchcase(rel_posix, norm_pat):
        return True
    if norm_pat.startswith("**/") and fnmatch.fnmatchcase(rel_posix, norm_pat[3:]):
        return True
    if "/" not in norm_pat and fnmatch.fnmatchcase(PurePosixPath(rel_posix).name, norm_pat):
        return True
    # 支持 src/**/foo.py 在零层子目录 src/foo.py 的匹配
    if "/**/" in norm_pat:
        collapsed = norm_pat.replace("/**/", "/")
        if fnmatch.fnmatchcase(rel_posix, collapsed):
            return True
    return False


def execute_fs_glob(workspace: Path, *, pattern: str, path: str = ".") -> dict[str, Any]:
    """在安全与 `.gitignore` 过滤后的工作区文件集上执行通配符匹配，最多返回前 200 条有序相对路径。"""
    rel_base, start_dir = resolve_host_workspace_path(workspace, path or ".", allow_directory=True)
    if not start_dir.is_dir():
        raise HostToolError("unsafe_path", "fs_glob 的起点必须是目录。")
    candidates = _walk_workspace_files(workspace, start_dir)
    matched: list[str] = []
    for rel, _ in candidates:
        sub_rel = (
            PurePosixPath(rel).relative_to(PurePosixPath(rel_base)).as_posix()
            if rel_base != "."
            else rel
        )
        if _glob_matches(sub_rel, pattern) or _glob_matches(rel, pattern):
            matched.append(rel)
    matched.sort()
    truncated = len(matched) > _HOST_MAX_GLOB_RESULTS
    return {
        "ok": True,
        "pattern": pattern,
        "base": rel_base,
        "total_matches": len(matched),
        "truncated": truncated,
        "files": matched[:_HOST_MAX_GLOB_RESULTS],
    }


def execute_fs_grep(
    workspace: Path,
    *,
    pattern: str,
    path: str = ".",
    glob: str | None = None,
    case_sensitive: bool = True,
) -> dict[str, Any]:
    """在工作区目录或单文件上执行按行正则搜索，带总条数（60 条）与单行长度（400 字符）双重保护。

    为什么必须截断 `line[:400]`？
    真实前端或构建仓库中偶尔存在未进 `.gitignore` 的单行数万字符压缩代码（Minified JS/SVG/JSON）；
    若原样拼入命中结果，单条 Grep 命中即可撑爆上万 Token 上下文预算。
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise HostToolError("invalid_regex", f"无效的正则表达式：{exc}") from exc

    rel_base, target = resolve_host_workspace_path(workspace, path or ".", allow_directory=True)
    if target.is_file():
        files_to_scan = [(rel_base, target)]
    else:
        files_to_scan = _walk_workspace_files(workspace, target)

    matches: list[dict[str, Any]] = []
    truncated = False
    for rel, fpath in files_to_scan:
        if glob and not _glob_matches(rel, glob):
            continue
        try:
            text = fpath.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                matches.append(
                    {
                        "path": rel,
                        "line": lineno,
                        "text": line[:400],
                    }
                )
                if len(matches) >= _HOST_MAX_GREP_MATCHES:
                    truncated = True
                    break
        if truncated:
            break

    return {
        "ok": True,
        "pattern": pattern,
        "matches_count": len(matches),
        "truncated": truncated,
        "matches": matches,
    }


# ---------------------------------------------------------------------------
# OS 内核级沙箱（HostKernelSandbox：macOS Seatbelt SBPL / Linux bubblewrap）
# ---------------------------------------------------------------------------

_SANDBOX_MODES = frozenset({"auto", "strict", "off"})
_SANDBOX_PROBE_LOCK = threading.Lock()
_SANDBOX_PROBE_CACHE: dict[str, bool] = {}
_MACOS_SANDBOX_EXEC = "/usr/bin/sandbox-exec"
_SENSITIVE_HOME_DIRS = (".ssh", ".aws", ".gnupg", ".config/gcloud", ".kube")
_SENSITIVE_HOME_FILES = (".netrc",)


def _sbpl_escape(path_str: str) -> str:
    """转义 macOS SBPL 字符串字面量中的反斜杠与双引号，防路径注入破坏策略语法。"""
    return path_str.replace("\\", "\\\\").replace('"', '\\"')


def _collect_sensitive_home_roots() -> list[Path]:
    """收集当前进程可见的家目录真实物理路径（同时覆盖当前 $HOME 与系统 passwd 主目录）。"""
    roots: list[Path] = []
    seen: set[str] = set()
    candidates: list[Path] = []
    with contextlib.suppress(RuntimeError, OSError):
        candidates.append(Path.home())
    with contextlib.suppress(KeyError, OSError):
        candidates.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    for p in candidates:
        with contextlib.suppress(OSError):
            rp = p.resolve()
            key = str(rp)
            if key not in seen:
                seen.add(key)
                roots.append(rp)
    return roots


def _collect_writable_tmp_roots() -> list[Path]:
    """收集当前进程合法临时目录物理路径（$TMPDIR、tempfile.gettempdir() 与 /private/tmp）。"""
    roots: list[Path] = []
    seen: set[str] = set()
    raw_candidates: list[str] = []
    env_tmp = os.environ.get("TMPDIR", "").strip()
    if env_tmp:
        raw_candidates.append(env_tmp)
    raw_candidates.append(tempfile.gettempdir())
    raw_candidates.append("/private/tmp")
    for raw in raw_candidates:
        with contextlib.suppress(OSError):
            rp = Path(raw).resolve()
            key = str(rp)
            if key not in seen:
                seen.add(key)
                roots.append(rp)
    return roots


class HostKernelSandbox:
    """跨平台 OS 内核级子进程沙箱构建器（macOS `sandbox-exec` SBPL / Linux `bwrap`）。

    策略档位（由环境变量 `AGENT_LAB_HOST_SANDBOX` 或显式 `mode` 控制）：
    - `auto`（默认）：探测本机 OS 沙箱并通过快速自检时自动启用，不可用时安全降级直跑；
    - `strict`：强一致失败闭合（Fail-Closed），若本机无可用 OS 沙箱则直接抛出 `sandbox_unavailable`；
    - `off`：显式关闭 OS 沙箱包装。
    """

    @staticmethod
    def normalize_mode(mode: str | None = None) -> str:
        raw = os.environ.get("AGENT_LAB_HOST_SANDBOX", "auto") if mode is None else mode
        clean = str(raw or "auto").strip().lower()
        if clean not in _SANDBOX_MODES:
            raise HostToolError(
                "invalid_sandbox_mode",
                f"AGENT_LAB_HOST_SANDBOX 必须是 {sorted(_SANDBOX_MODES)} 之一，收到：{clean!r}",
            )
        return clean

    @staticmethod
    def reset_probe_cache() -> None:
        with _SANDBOX_PROBE_LOCK:
            _SANDBOX_PROBE_CACHE.clear()

    @classmethod
    def _probe_macos(cls) -> bool:
        if sys.platform != "darwin" or not os.path.exists(_MACOS_SANDBOX_EXEC):
            return False
        with _SANDBOX_PROBE_LOCK:
            if _MACOS_SANDBOX_EXEC in _SANDBOX_PROBE_CACHE:
                return _SANDBOX_PROBE_CACHE[_MACOS_SANDBOX_EXEC]
        try:
            true_bin = "/usr/bin/true" if os.path.exists("/usr/bin/true") else "/bin/true"
            res = subprocess.run(
                [_MACOS_SANDBOX_EXEC, "-p", "(version 1)(allow default)", true_bin],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
                check=False,
            )
            ok = res.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        with _SANDBOX_PROBE_LOCK:
            _SANDBOX_PROBE_CACHE[_MACOS_SANDBOX_EXEC] = ok
        return ok

    @classmethod
    def _probe_linux_bwrap(cls) -> str | None:
        if not sys.platform.startswith("linux"):
            return None
        bwrap_bin = shutil.which("bwrap")
        if not bwrap_bin:
            return None
        with _SANDBOX_PROBE_LOCK:
            if bwrap_bin in _SANDBOX_PROBE_CACHE:
                return bwrap_bin if _SANDBOX_PROBE_CACHE[bwrap_bin] else None
        try:
            true_bin = "/bin/true" if os.path.exists("/bin/true") else "/usr/bin/true"
            res = subprocess.run(
                [bwrap_bin, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--", true_bin],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
                check=False,
            )
            ok = res.returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        with _SANDBOX_PROBE_LOCK:
            _SANDBOX_PROBE_CACHE[bwrap_bin] = ok
        return bwrap_bin if ok else None

    @classmethod
    def detect_backend(cls) -> str | None:
        """探测当前平台可用的 OS 内核沙箱后端（`macos-sandbox-exec` / `linux-bwrap` / `None`）。"""
        if cls._probe_macos():
            return "macos-sandbox-exec"
        if cls._probe_linux_bwrap():
            return "linux-bwrap"
        return None

    @classmethod
    def is_available(cls) -> bool:
        return cls.detect_backend() is not None

    @staticmethod
    def build_macos_sbpl(workspace: Path, *, allow_network: bool = False) -> str:
        """构建 macOS Seatbelt SBPL 策略：限制读写凭证、收敛写范围至工作区与临时区、保护 .git/.agent-lab、默认断出站网络。"""
        ws_resolved = Path(workspace).resolve()
        home_roots = _collect_sensitive_home_roots()
        tmp_roots = _collect_writable_tmp_roots()

        deny_read_clauses: list[str] = []
        for home in home_roots:
            for rel_dir in _SENSITIVE_HOME_DIRS:
                deny_read_clauses.append(f'    (subpath "{_sbpl_escape(str(home / rel_dir))}")')
            for rel_file in _SENSITIVE_HOME_FILES:
                deny_read_clauses.append(f'    (literal "{_sbpl_escape(str(home / rel_file))}")')

        allow_write_clauses: list[str] = [f'    (subpath "{_sbpl_escape(str(ws_resolved))}")']
        for tmp_root in tmp_roots:
            allow_write_clauses.append(f'    (subpath "{_sbpl_escape(str(tmp_root))}")')
        allow_write_clauses.extend(
            [
                '    (literal "/dev/null")',
                '    (literal "/dev/zero")',
                '    (literal "/dev/dtracehelper")',
                '    (literal "/dev/tty")',
                '    (literal "/dev/ptmx")',
                '    (regex #"^/dev/ttys[0-9]+$")',
            ]
        )

        # macOS Seatbelt SBPL 采用“最后匹配规则胜出（Last-Match-Wins）”语义：
        # 必须在 (allow file-write* (subpath "<ws_resolved>")) 之后追加显式 (deny file-write* ...)，
        # 防止沙箱内命令向 .git/hooks 或 .agent-lab/settings.json 写入持久化后门，并额外保护宿主凭证目录/文件。
        deny_write_clauses: list[str] = [
            f'    (subpath "{_sbpl_escape(str(ws_resolved / ".git"))}")',
            f'    (subpath "{_sbpl_escape(str(ws_resolved / ".agent-lab"))}")',
            *deny_read_clauses,
        ]

        lines = [
            "(version 1)",
            "(allow default)",
            "(deny file-read*",
            *deny_read_clauses,
            ")",
            "(deny file-write*)",
            "(allow file-write*",
            *allow_write_clauses,
            ")",
            "(deny file-write*",
            *deny_write_clauses,
            ")",
        ]
        if not allow_network:
            lines.append("(deny network-outbound)")
        return "\n".join(lines) + "\n"

    @staticmethod
    def build_linux_bwrap_argv(
        bwrap_bin: str,
        shell_bin: str,
        command: str,
        workspace: Path,
        *,
        allow_network: bool = False,
    ) -> list[str]:
        """构建 Linux bubblewrap (`bwrap`) 挂载与网络隔离命令行参数。"""
        ws_resolved = Path(workspace).resolve()
        argv: list[str] = [
            bwrap_bin,
            "--die-with-parent",
            "--ro-bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--bind",
            str(ws_resolved),
            str(ws_resolved),
        ]
        env_tmp = os.environ.get("TMPDIR", "").strip()
        if env_tmp:
            with contextlib.suppress(OSError):
                tmp_resolved = Path(env_tmp).resolve()
                if tmp_resolved != Path("/tmp") and tmp_resolved.is_dir():
                    argv.extend(["--bind", str(tmp_resolved), str(tmp_resolved)])
        # 在工作区与临时目录可写挂载之后，以只读 --ro-bind 覆写保护 .git 与 .agent-lab，防篡改 Git Hooks 与策略配置
        for protected_name in (".git", ".agent-lab"):
            protected_path = ws_resolved / protected_name
            if protected_path.exists() and not protected_path.is_symlink():
                argv.extend(["--ro-bind", str(protected_path), str(protected_path)])
        for home in _collect_sensitive_home_roots():
            for rel_dir in _SENSITIVE_HOME_DIRS:
                target_dir = home / rel_dir
                if target_dir.exists() and target_dir.is_dir():
                    argv.extend(["--tmpfs", str(target_dir)])
            for rel_file in _SENSITIVE_HOME_FILES:
                target_file = home / rel_file
                if target_file.exists() and target_file.is_file():
                    argv.extend(["--ro-bind", "/dev/null", str(target_file)])
        if not allow_network:
            argv.append("--unshare-net")
        argv.extend(["--", shell_bin, "-c", command])
        return argv

    @classmethod
    def build_sandboxed_shell_argv(
        cls,
        shell_bin: str,
        command: str,
        workspace: Path,
        *,
        allow_network: bool = False,
        mode: str | None = None,
    ) -> list[str]:
        """按当前沙箱策略返回可直接喂给 `subprocess.Popen` 的完整 argv。"""
        eff_mode = cls.normalize_mode(mode)
        if eff_mode == "off":
            return [shell_bin, "-c", command]

        if cls._probe_macos():
            sbpl = cls.build_macos_sbpl(workspace, allow_network=allow_network)
            return [_MACOS_SANDBOX_EXEC, "-p", sbpl, shell_bin, "-c", command]

        bwrap_bin = cls._probe_linux_bwrap()
        if bwrap_bin:
            return cls.build_linux_bwrap_argv(
                bwrap_bin,
                shell_bin,
                command,
                workspace,
                allow_network=allow_network,
            )

        if eff_mode == "strict":
            raise HostToolError(
                "sandbox_unavailable",
                "AGENT_LAB_HOST_SANDBOX=strict 要求启用 OS 内核沙箱，但当前平台未检测到可用的 sandbox-exec 或 bwrap。",
            )
        return [shell_bin, "-c", command]


def build_sandboxed_shell_argv(
    shell_bin: str,
    command: str,
    workspace: Path,
    *,
    allow_network: bool = False,
    mode: str | None = None,
) -> list[str]:
    """构建受 OS 内核沙箱保护的 Shell 执行 argv（便捷顶层函数入口）。"""
    return HostKernelSandbox.build_sandboxed_shell_argv(
        shell_bin,
        command,
        workspace,
        allow_network=allow_network,
        mode=mode,
    )


def _sandbox_backend_from_argv(argv: list[str]) -> str:
    if not argv:
        return "off"
    exe = Path(argv[0]).name
    if exe == "sandbox-exec":
        return "macos-sandbox-exec"
    if exe == "bwrap":
        return "linux-bwrap"
    return "off"


def _drain_stream(
    stream: Any,
    bucket: bytearray,
    limit: int,
    overflow_flag: list[bool],
    *,
    stream_name: str = "stdout",
    on_output: Callable[[str, str], None] | None = None,
) -> None:
    """后台管道排水线程入口：持续抽干子进程输出流以消除 64KB 内核管道死锁，并支持逐行实时流式回调。

    核心系统编程不变量（Why Continuous Drain Prevents 64KB Pipe Deadlock）：
    1. 使用 `read1(4096)`（底层单次 `read(2)`）而非阻塞攒满 4096 字节的 `BufferedReader.read(4096)`，
       确保子进程每吐出一行日志都能毫秒级送达 `on_output(stream_name, line)` 回调；
    2. 超出 `limit`（32 KiB）后绝不提前退出循环或关闭管道，继续抽干直至 EOF，防止内核管道写满死锁或 SIGPIPE；
    3. 所有流式推送行均经 `redact_secrets` 脱敏，且回调异常被隔离吞并，绝不影响管道排水线程。
    """
    pending = bytearray()
    read_fn = getattr(stream, "read1", None) or stream.read
    try:
        while True:
            chunk = read_fn(4096)
            if not chunk:
                break
            if len(bucket) < limit:
                take = min(len(chunk), limit - len(bucket))
                bucket.extend(chunk[:take])
                if take < len(chunk):
                    overflow_flag[0] = True
            else:
                overflow_flag[0] = True

            if on_output is not None:
                pending.extend(chunk)
                while True:
                    nl_idx = pending.find(b"\n")
                    if nl_idx == -1:
                        break
                    raw_line = bytes(pending[: nl_idx + 1])
                    del pending[: nl_idx + 1]
                    text_line = redact_secrets(raw_line.decode("utf-8", errors="replace"))
                    with contextlib.suppress(Exception):
                        on_output(stream_name, text_line)
                # 若单行超长且长时间无换行，避免 pending 无限膨胀
                if len(pending) >= 4096:
                    raw_part = bytes(pending)
                    pending.clear()
                    text_part = redact_secrets(raw_part.decode("utf-8", errors="replace"))
                    with contextlib.suppress(Exception):
                        on_output(stream_name, text_part)
    except OSError:
        pass
    finally:
        if on_output is not None and pending:
            text_tail = redact_secrets(bytes(pending).decode("utf-8", errors="replace"))
            with contextlib.suppress(Exception):
                on_output(stream_name, text_tail)
        with contextlib.suppress(OSError):
            stream.close()


def _terminate_process_group(proc: subprocess.Popen[bytes], pgid: int) -> None:
    """对整个子进程组执行“两阶段优雅终止 + 全组存活探测 + 强制清场”孤儿进程回收协议。"""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGTERM)
    if proc.poll() is None:
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=0.4)
    # 即使主 shell 已退出，若进程组内仍有后台孙进程存活，也必须等待短暂宽限后 SIGKILL 清场
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
            time.sleep(0.03)
        except ProcessLookupError:
            return
        except (PermissionError, OSError):
            break
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGKILL)
    if proc.poll() is None:
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=1.0)


def run_host_bash_command(
    workspace: Path,
    command: str,
    *,
    cwd: str = ".",
    timeout_seconds: int = 30,
    cancel_event: threading.Event | None = None,
    on_output: Callable[[str, str], None] | None = None,
    allow_network: bool = False,
    sandbox_mode: str | None = None,
) -> dict[str, Any]:
    """在 OS 内核沙箱、脱敏环境变量与独立进程组中执行终端命令，支持协作式取消与实时流式输出。"""
    rel_cwd, resolved_cwd = resolve_host_workspace_path(workspace, cwd or ".", allow_directory=True)
    if not resolved_cwd.is_dir():
        raise HostToolError("unsafe_path", f"命令工作目录不是有效目录：{rel_cwd}")

    timeout = max(1, min(120, int(timeout_seconds)))
    shell_bin = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
    argv = build_sandboxed_shell_argv(
        shell_bin,
        command,
        workspace,
        allow_network=allow_network,
        mode=sandbox_mode,
    )
    sandbox_backend = _sandbox_backend_from_argv(argv)

    # 启动前快速检查是否已被外部协作取消
    if cancel_event is not None and cancel_event.is_set():
        return {
            "ok": False,
            "code": "cancelled",
            "command": command,
            "cwd": rel_cwd,
            "exit_code": -15,
            "timed_out": False,
            "cancelled": True,
            "duration_ms": 0.0,
            "stdout": "",
            "stderr": "",
            "stdout_truncated": False,
            "stderr_truncated": False,
            "sandbox": sandbox_backend,
        }

    env = build_scrubbed_env()
    t0 = time.monotonic()

    proc = subprocess.Popen(
        argv,
        cwd=str(resolved_cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    # setsid() 保证新启动的子进程自身就是新进程组的 Leader，故进程组 ID (pgid) 恒等于 proc.pid
    pgid = proc.pid
    out_buf = bytearray()
    err_buf = bytearray()
    out_trunc = [False]
    err_trunc = [False]

    t_out = threading.Thread(
        target=_drain_stream,
        args=(proc.stdout, out_buf, _HOST_MAX_STREAM_BYTES, out_trunc),
        kwargs={"stream_name": "stdout", "on_output": on_output},
        daemon=True,
    )
    t_err = threading.Thread(
        target=_drain_stream,
        args=(proc.stderr, err_buf, _HOST_MAX_STREAM_BYTES, err_trunc),
        kwargs={"stream_name": "stderr", "on_output": on_output},
        daemon=True,
    )
    t_out.start()
    t_err.start()

    timed_out = False
    cancelled = False
    try:
        if cancel_event is None:
            proc.wait(timeout=timeout)
        else:
            deadline = t0 + timeout
            while True:
                if cancel_event.is_set():
                    cancelled = True
                    _terminate_process_group(proc, pgid)
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    _terminate_process_group(proc, pgid)
                    break
                try:
                    proc.wait(timeout=min(0.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate_process_group(proc, pgid)
    except BaseException:
        # 捕获包括 KeyboardInterrupt / asyncio.CancelledError 在内的一切基础异常，严防取消任务时遗留孤儿进程
        _terminate_process_group(proc, pgid)
        raise
    finally:
        # 清理可能残留并占用管道 FD 的后台子孙进程，防止 t_out.join 阻塞
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, signal.SIGKILL)
        t_out.join(timeout=1.5)
        t_err.join(timeout=1.5)

    duration_ms = round((time.monotonic() - t0) * 1000.0, 2)
    exit_code = proc.returncode if proc.returncode is not None else -9
    stdout_text = redact_secrets(out_buf.decode("utf-8", errors="replace"))
    stderr_text = redact_secrets(err_buf.decode("utf-8", errors="replace"))

    if cancelled:
        code = "cancelled"
        ok = False
    elif timed_out:
        code = "timeout"
        ok = False
    elif exit_code == 0:
        code = "ok"
        ok = True
    else:
        code = "nonzero_exit"
        ok = False

    return {
        "ok": ok,
        "code": code,
        "command": command,
        "cwd": rel_cwd,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "cancelled": cancelled,
        "duration_ms": duration_ms,
        "stdout": stdout_text,
        "stderr": stderr_text,
        "stdout_truncated": out_trunc[0],
        "stderr_truncated": err_trunc[0],
        "sandbox": sandbox_backend,
    }


# ---------------------------------------------------------------------------
# 持久化 PTY / 后台进程管理器（BackgroundProcessManager）
# ---------------------------------------------------------------------------


class _ManagedBackgroundProcess:
    """单个受管后台进程状态机：维护环形日志缓冲、单调偏移量、PTY/管道句柄与超时看门狗。"""

    def __init__(
        self,
        *,
        proc_id: str,
        command: str,
        cwd: str,
        proc: subprocess.Popen[bytes],
        use_pty: bool,
        master_fd: int | None,
        sandbox_backend: str,
        max_buffer_chars: int,
    ) -> None:
        self.proc_id = proc_id
        self.command = command
        self.cwd = cwd
        self.proc = proc
        self.pgid = proc.pid
        self.use_pty = use_pty
        self.master_fd = master_fd
        self.master_closed = False
        self.sandbox_backend = sandbox_backend
        self.max_buffer_chars = max_buffer_chars
        self.started_at = time.time()
        self.ended_at: float | None = None
        self.status = "running"  # running | exited | killed | timed_out
        self.exit_code: int | None = None

        self.lock = threading.RLock()
        self.reap_lock = threading.Lock()
        self.pgid_reaped = False
        self.stop_event = threading.Event()
        self.buffer = ""
        self.base_offset = 0
        self.total_chars = 0
        self.reader_thread: threading.Thread | None = None
        self.timer: threading.Timer | None = None

    def append_output(self, raw_chunk: bytes) -> None:
        text = redact_secrets(raw_chunk.decode("utf-8", errors="replace"))
        if not text:
            return
        with self.lock:
            self.buffer += text
            self.total_chars += len(text)
            if len(self.buffer) > self.max_buffer_chars:
                drop = len(self.buffer) - self.max_buffer_chars
                self.buffer = self.buffer[drop:]
                self.base_offset += drop

    def refresh_status(self) -> str:
        with self.lock:
            rc = self.proc.poll()
            if rc is not None:
                self.exit_code = rc
                if self.status == "running":
                    self.status = "exited"
                if self.ended_at is None:
                    self.ended_at = time.time()
                if self.timer is not None:
                    self.timer.cancel()
                    self.timer = None
            return self.status

    def close_fds(self, *, close_master: bool = False) -> None:
        """关闭管道 stdin/stdout FD；PTY `master_fd` 由 `_pty_reader` 独占关闭（避免关闭后 FD 被内核复用导致竞态读写）。"""
        with self.lock:
            can_close_master = close_master or (
                self.reader_thread is None or not self.reader_thread.is_alive()
            )
            if can_close_master and self.master_fd is not None and not self.master_closed:
                self.master_closed = True
                fd_to_close = self.master_fd
                self.master_fd = None
                with contextlib.suppress(OSError):
                    os.close(fd_to_close)
            if self.proc.stdin is not None:
                with contextlib.suppress(OSError):
                    self.proc.stdin.close()
            if self.proc.stdout is not None:
                with contextlib.suppress(OSError):
                    self.proc.stdout.close()


def _reap_managed_process_group(item: _ManagedBackgroundProcess) -> None:
    """无条件对受管进程组执行 SIGTERM -> SIGKILL 回收（即使顶层 Shell 已退出也要收割残留孤儿孙进程，幂等防 PID 复用）。"""
    with item.reap_lock:
        with item.lock:
            if item.pgid_reaped:
                return
        _terminate_process_group(item.proc, item.pgid)
        with item.lock:
            if item.proc.poll() is not None:
                try:
                    os.killpg(item.pgid, 0)
                except ProcessLookupError:
                    item.pgid_reaped = True
                except OSError:
                    pass


class BackgroundProcessManager:
    """运行级持久化后台进程与伪终端（PTY）管理器。

    设计不变量：
    - 有界进程数上限（默认 6 个并发运行进程），防止模型无限派生后台服务耗尽宿主机资源；
    - 有界环形日志缓冲（默认 128 KiB）配合单调递增字符偏移量 `offset` / `next_offset`，
      支持增量轮询追踪且绝不 OOM；
    - 同时支持 PTY（`pty.openpty()`）交互式终端与标准 Pipe 模式；
    - `_pty_reader` 对 PTY `master_fd` 拥有独占关闭权，杜绝并发关闭与 FD 复用读写竞态；
    - 退出、驱逐或 `close_all()` 时无条件对每个进程组执行 `SIGTERM -> SIGKILL` 回收孤儿孙进程并关闭全部 FD。
    """

    def __init__(
        self,
        workspace: Path,
        *,
        max_processes: int = 6,
        max_buffer_chars: int = 128 * 1024,
        sandbox_mode: str | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.max_processes = max(1, int(max_processes))
        self.max_buffer_chars = max(4096, int(max_buffer_chars))
        self.sandbox_mode = sandbox_mode
        self._lock = threading.RLock()
        self._procs: dict[str, _ManagedBackgroundProcess] = {}

    def _active_count_locked(self) -> int:
        active = 0
        for item in self._procs.values():
            if item.refresh_status() == "running":
                active += 1
        return active

    def _cleanup_exited_item(self, item: _ManagedBackgroundProcess) -> None:
        """对顶层已退出的进程项执行彻底清场：无条件收割进程组孤儿孙进程、等待读线程收敛并关闭管道 FD。"""
        item.stop_event.set()
        with item.lock:
            if item.timer is not None:
                item.timer.cancel()
                item.timer = None
        _reap_managed_process_group(item)
        if item.reader_thread is not None and item.reader_thread.is_alive():
            if threading.current_thread() is not item.reader_thread:
                item.reader_thread.join(timeout=1.0)
        item.close_fds()
        item.refresh_status()

    def _evict_stale_locked(self) -> None:
        max_records = self.max_processes * 4
        if len(self._procs) < max_records:
            return
        exited = [
            (p.ended_at or p.started_at, pid)
            for pid, p in self._procs.items()
            if p.refresh_status() != "running"
        ]
        exited.sort()
        while len(self._procs) >= max_records and exited:
            _, oldest_id = exited.pop(0)
            evicted = self._procs.pop(oldest_id, None)
            if evicted is not None:
                self._cleanup_exited_item(evicted)

    @staticmethod
    def _pipe_reader(item: _ManagedBackgroundProcess) -> None:
        stream = item.proc.stdout
        if stream is None:
            _reap_managed_process_group(item)
            item.refresh_status()
            item.close_fds()
            return
        read_fn = getattr(stream, "read1", None) or stream.read
        try:
            while True:
                chunk = read_fn(4096)
                if not chunk:
                    break
                item.append_output(chunk)
        except OSError:
            pass
        finally:
            if item.proc.poll() is None:
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    item.proc.wait(timeout=0.05)
            item.refresh_status()
            _reap_managed_process_group(item)
            item.refresh_status()
            item.close_fds()

    @staticmethod
    def _pty_reader(item: _ManagedBackgroundProcess) -> None:
        fd = item.master_fd
        if fd is None:
            _reap_managed_process_group(item)
            item.refresh_status()
            item.close_fds(close_master=True)
            return
        try:
            while True:
                if item.stop_event.is_set():
                    break
                try:
                    readable, _, _ = select.select([fd], [], [], 0.1)
                except (OSError, ValueError):
                    break
                if readable:
                    try:
                        chunk = os.read(fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    item.append_output(chunk)
                    continue
                if item.stop_event.is_set() or item.proc.poll() is not None:
                    # 进程已结束且无残留可读字节时退出读取循环
                    break
        finally:
            if item.proc.poll() is None:
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    item.proc.wait(timeout=0.05)
            item.refresh_status()
            _reap_managed_process_group(item)
            item.refresh_status()
            # _pty_reader 独占关闭 master_fd，确保 select/os.read 完全退出后才释放 FD
            item.close_fds(close_master=True)

    def _terminate_item(self, item: _ManagedBackgroundProcess, *, reason_status: str) -> None:
        item.stop_event.set()
        with item.lock:
            if item.timer is not None:
                item.timer.cancel()
                item.timer = None
            if item.status == "running":
                item.status = reason_status
        _reap_managed_process_group(item)
        # 必须先等待读线程（尤其是独占 master_fd 的 _pty_reader）退出，再做兜底 close_fds
        if item.reader_thread is not None and item.reader_thread.is_alive():
            if threading.current_thread() is not item.reader_thread:
                item.reader_thread.join(timeout=1.5)
        item.close_fds()
        with item.lock:
            if item.proc.returncode is not None:
                item.exit_code = item.proc.returncode
            elif item.exit_code is None:
                item.exit_code = -9
            if item.ended_at is None:
                item.ended_at = time.time()

    def start(
        self,
        command: str,
        cwd: str | Path = ".",
        use_pty: bool = False,
        timeout_seconds: float | None = None,
        allow_network: bool = False,
    ) -> dict[str, Any]:
        """在工作区内启动受控后台进程（支持 PTY 或 Pipe 模式及可选超时回收）。"""
        verdict = classify_bash_command(command)
        if verdict["classification"] == "blocked":
            raise HostToolError("command_blocked", f"安全策略拒绝启动高危命令：{verdict['reason']}")

        cwd_str = str(cwd) if cwd is not None else "."
        rel_cwd, resolved_cwd = resolve_host_workspace_path(self.workspace, cwd_str or ".", allow_directory=True)
        if not resolved_cwd.is_dir():
            raise HostToolError("unsafe_path", f"命令工作目录不是有效目录：{rel_cwd}")

        with self._lock:
            if self._active_count_locked() >= self.max_processes:
                raise HostToolError(
                    "process_limit_exceeded",
                    f"当前运行已达后台进程上限（{self.max_processes} 个），请先使用 bash_kill 结束不再需要的进程。",
                )
            self._evict_stale_locked()

        shell_bin = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
        argv = build_sandboxed_shell_argv(
            shell_bin,
            command,
            self.workspace,
            allow_network=allow_network,
            mode=self.sandbox_mode,
        )
        sandbox_backend = _sandbox_backend_from_argv(argv)
        extra_env = {"TERM": "xterm-256color"} if use_pty else None
        env = build_scrubbed_env(extra_env)

        master_fd: int | None = None
        slave_fd: int | None = None
        try:
            if use_pty:
                master_fd, slave_fd = pty.openpty()
                proc = subprocess.Popen(
                    argv,
                    cwd=str(resolved_cwd),
                    env=env,
                    stdin=slave_fd,
                    stdout=slave_fd,
                    stderr=slave_fd,
                    start_new_session=True,
                    close_fds=True,
                )
                os.close(slave_fd)
                slave_fd = None
            else:
                proc = subprocess.Popen(
                    argv,
                    cwd=str(resolved_cwd),
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
        except Exception:
            if slave_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(slave_fd)
            if master_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(master_fd)
            raise

        proc_id = "proc_" + uuid.uuid4().hex[:10]
        item = _ManagedBackgroundProcess(
            proc_id=proc_id,
            command=command,
            cwd=rel_cwd,
            proc=proc,
            use_pty=bool(use_pty),
            master_fd=master_fd,
            sandbox_backend=sandbox_backend,
            max_buffer_chars=self.max_buffer_chars,
        )
        reader_target = self._pty_reader if use_pty else self._pipe_reader
        item.reader_thread = threading.Thread(target=reader_target, args=(item,), daemon=True)
        try:
            item.reader_thread.start()
        except Exception:
            item.reader_thread = None
            _reap_managed_process_group(item)
            item.close_fds(close_master=True)
            raise

        if timeout_seconds is not None:
            t_sec = max(0.2, float(timeout_seconds))
            timer = threading.Timer(t_sec, self._terminate_item, args=(item,), kwargs={"reason_status": "timed_out"})
            timer.daemon = True
            item.timer = timer
            timer.start()

        with self._lock:
            self._procs[proc_id] = item

        return {
            "ok": True,
            "proc_id": proc_id,
            "pid": proc.pid,
            "command": command,
            "cwd": rel_cwd,
            "use_pty": bool(use_pty),
            "status": item.refresh_status(),
            "sandbox": sandbox_backend,
        }

    def _get_item(self, proc_id: str) -> _ManagedBackgroundProcess:
        with self._lock:
            item = self._procs.get(proc_id)
        if item is None:
            raise HostToolError("process_not_found", f"未找到后台进程：{proc_id}")
        return item

    def read_output(self, proc_id: str, offset: int = 0, max_chars: int = 8000) -> dict[str, Any]:
        """从单调字符偏移量读取后台进程的环形缓冲输出。"""
        item = self._get_item(proc_id)
        status = item.refresh_status()
        req_offset = max(0, int(offset))
        limit = max(1, min(64000, int(max_chars)))
        with item.lock:
            dropped = req_offset < item.base_offset
            eff_offset = max(req_offset, item.base_offset)
            rel_start = eff_offset - item.base_offset
            chunk = item.buffer[rel_start : rel_start + limit]
            next_offset = eff_offset + len(chunk)
            total_chars = item.total_chars
            exit_code = item.exit_code
        return {
            "ok": True,
            "proc_id": proc_id,
            "pid": item.proc.pid,
            "status": status,
            "running": status == "running",
            "exit_code": exit_code,
            "output": chunk,
            "offset": eff_offset,
            "next_offset": next_offset,
            "total_chars": total_chars,
            "truncated_from_ring": dropped,
            "has_more": next_offset < total_chars,
        }

    def write_stdin(self, proc_id: str, data: str) -> dict[str, Any]:
        """向运行中的后台进程标准输入（Pipe 或 PTY 主端）写入数据。"""
        item = self._get_item(proc_id)
        if item.refresh_status() != "running":
            raise HostToolError("process_not_running", f"后台进程 {proc_id} 已结束，无法继续写入输入。")
        encoded = str(data).encode("utf-8")
        try:
            if item.use_pty:
                with item.lock:
                    if item.stop_event.is_set() or item.master_fd is None or item.master_closed:
                        raise HostToolError("process_not_running", f"后台进程 {proc_id} PTY 已关闭。")
                    written = os.write(item.master_fd, encoded)
            else:
                with item.lock:
                    if item.stop_event.is_set() or item.proc.stdin is None or item.proc.stdin.closed:
                        raise HostToolError("process_not_running", f"后台进程 {proc_id} stdin 已关闭。")
                    item.proc.stdin.write(encoded)
                    item.proc.stdin.flush()
                    written = len(encoded)
        except OSError as exc:
            item.refresh_status()
            raise HostToolError("stdin_write_failed", f"写入后台进程 {proc_id} 失败：{exc}") from exc
        return {
            "ok": True,
            "proc_id": proc_id,
            "bytes_written": written,
            "status": item.refresh_status(),
        }

    def kill(self, proc_id: str) -> dict[str, Any]:
        """终止指定后台进程及其整个子进程组并回收资源。"""
        item = self._get_item(proc_id)
        self._terminate_item(item, reason_status="killed")
        return {
            "ok": True,
            "proc_id": proc_id,
            "pid": item.proc.pid,
            "status": item.status,
            "exit_code": item.exit_code,
        }

    def list_processes(self) -> list[dict[str, Any]]:
        """返回当前管理器追踪的全部后台进程状态摘要列表。"""
        with self._lock:
            items = list(self._procs.values())
        summaries: list[dict[str, Any]] = []
        for item in items:
            status = item.refresh_status()
            with item.lock:
                summaries.append(
                    {
                        "proc_id": item.proc_id,
                        "pid": item.proc.pid,
                        "command": item.command,
                        "cwd": item.cwd,
                        "use_pty": item.use_pty,
                        "status": status,
                        "running": status == "running",
                        "exit_code": item.exit_code,
                        "total_chars": item.total_chars,
                        "sandbox": item.sandbox_backend,
                        "started_at": round(item.started_at, 3),
                        "ended_at": round(item.ended_at, 3) if item.ended_at is not None else None,
                    }
                )
        return summaries

    def close_all(self) -> int:
        """终止并清理全部后台进程组（含顶层已退出但仍有孤儿孙进程的进程组，幂等）。返回清理的活跃进程数量。"""
        with self._lock:
            items = list(self._procs.values())
        cleaned = 0
        for item in items:
            if item.refresh_status() == "running":
                self._terminate_item(item, reason_status="killed")
                cleaned += 1
            else:
                self._cleanup_exited_item(item)
        return cleaned


def execute_bash_output(
    process_manager: BackgroundProcessManager,
    *,
    proc_id: str,
    offset: int = 0,
    max_chars: int = 8000,
) -> dict[str, Any]:
    return process_manager.read_output(proc_id, offset=offset, max_chars=max_chars)


def execute_bash_write(
    process_manager: BackgroundProcessManager,
    *,
    proc_id: str,
    data: str,
) -> dict[str, Any]:
    return process_manager.write_stdin(proc_id, data)


def execute_bash_kill(
    process_manager: BackgroundProcessManager,
    *,
    proc_id: str,
) -> dict[str, Any]:
    return process_manager.kill(proc_id)


def execute_bash_list(process_manager: BackgroundProcessManager) -> dict[str, Any]:
    procs = process_manager.list_processes()
    return {"ok": True, "count": len(procs), "processes": procs}


def execute_bash_tool(
    workspace: Path,
    storage: Storage,
    run_id: str,
    *,
    command: str,
    cwd: str = ".",
    timeout_seconds: int = 30,
    permission_mode: str,
    call_id: str | None = None,
    expected_epoch: int | None = None,
    run_in_background: bool = False,
    use_pty: bool = False,
    process_manager: BackgroundProcessManager | None = None,
    cancel_event: threading.Event | None = None,
    on_output: Callable[[str, str], None] | None = None,
    rule_engine: PermissionRuleEngine | None = None,
    session_grants: Sequence[str] | None = None,
    allow_network: bool = False,
) -> dict[str, Any]:
    """`bash_exec` 工具顶层编排入口：串联目录校验、规则与三级权限裁决、提案挂起、Epoch 栅栏与后台/前台执行。"""
    rel_cwd, resolved_cwd = resolve_host_workspace_path(workspace, cwd or ".", allow_directory=True)
    if not resolved_cwd.is_dir():
        raise HostToolError("unsafe_path", f"命令工作目录不是有效目录：{rel_cwd}")

    perm = evaluate_tool_permission(
        "bash_exec",
        {
            "command": command,
            "cwd": rel_cwd,
            "timeout_seconds": timeout_seconds,
            "run_in_background": bool(run_in_background),
            "use_pty": bool(use_pty),
        },
        permission_mode=permission_mode,
        rule_engine=rule_engine,
        session_grants=session_grants,
    )
    if perm["decision"] == "deny":
        return {
            "ok": False,
            "code": "command_blocked",
            "command": command,
            "error": f"安全策略拒绝执行该命令：{perm['reason']}",
        }
    if perm["decision"] == "ask":
        args_payload: dict[str, Any] = {
            "command": command,
            "cwd": rel_cwd,
            "timeout_seconds": int(timeout_seconds),
        }
        if run_in_background:
            args_payload["run_in_background"] = True
        if use_pty:
            args_payload["use_pty"] = True
        proposal = storage.create_action_proposal(
            run_id,
            kind="command",
            tool_name="bash_exec",
            arguments=args_payload,
            summary=f"[{perm['risk']}] ({rel_cwd}) $ {command[:200]}",
            risk_level=perm["risk"],
            operation_id=call_id,
            expected_epoch=expected_epoch,
        )
        storage.add_event(
            run_id,
            "action_proposed",
            {
                "proposal_id": proposal["id"],
                "kind": "command",
                "tool": "bash_exec",
                "command": command[:500],
                "cwd": rel_cwd,
                "risk_level": perm["risk"],
                "reason": perm["reason"],
            },
        )
        return {
            "ok": True,
            "requires_approval": True,
            "proposal_id": proposal["id"],
            "kind": "command",
            "command": command,
            "cwd": rel_cwd,
            "risk_level": perm["risk"],
            "reason": perm["reason"],
        }

    storage.verify_epoch(run_id, expected_epoch)
    if run_in_background:
        if process_manager is None:
            raise HostToolError("background_not_configured", "当前运行未挂载 BackgroundProcessManager，无法启动后台进程。")
        bg_res = process_manager.start(
            command,
            cwd=rel_cwd,
            use_pty=bool(use_pty),
            timeout_seconds=timeout_seconds,
            allow_network=allow_network,
        )
        storage.add_event(
            run_id,
            "command_started_background",
            {
                "proc_id": bg_res["proc_id"],
                "pid": bg_res["pid"],
                "command": command[:300],
                "cwd": rel_cwd,
                "use_pty": bool(use_pty),
            },
        )
        return bg_res

    result = run_host_bash_command(
        workspace,
        command,
        cwd=rel_cwd,
        timeout_seconds=timeout_seconds,
        cancel_event=cancel_event,
        on_output=on_output,
        allow_network=allow_network,
    )
    storage.add_event(
        run_id,
        "command_executed",
        {
            "command": command[:300],
            "cwd": rel_cwd,
            "exit_code": result["exit_code"],
            "timed_out": result["timed_out"],
            "cancelled": result.get("cancelled", False),
            "duration_ms": result["duration_ms"],
        },
    )
    return result


# ---------------------------------------------------------------------------
# SSRF 防护网页抓取（execute_web_fetch）与可配置网页搜索（execute_web_search）
# ---------------------------------------------------------------------------

_WEB_FETCH_MAX_BYTES = 2 * 1024 * 1024  # 单响应体硬上限 2 MiB
_WEB_FETCH_MAX_REDIRECTS = 5
_CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")
_NAT64_WELL_KNOWN_NET = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL_NET = ipaddress.ip_network("64:ff9b:1::/48")
_BLOCKED_HOSTNAMES = frozenset({"localhost", "metadata.google.internal", "metadata.goog"})
_BLOCKED_HOST_SUFFIXES = (".localhost", ".local", ".internal")


def _is_blocked_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """判定目标 IP 是否属于禁止访问的内网/回环/链路本地/云元数据地址（含 IPv4-mapped、6to4、Teredo、NAT64 递归解包）。"""
    if isinstance(ip_obj, ipaddress.IPv6Address):
        if ip_obj.ipv4_mapped is not None:
            return _is_blocked_ip(ip_obj.ipv4_mapped)
        if ip_obj.sixtofour is not None:
            inner_reason = _is_blocked_ip(ip_obj.sixtofour)
            if inner_reason is not None:
                return f"6to4 embedded {inner_reason}"
        if ip_obj.teredo is not None:
            server_ip, client_ip = ip_obj.teredo
            for role, embedded in (("server", server_ip), ("client", client_ip)):
                inner_reason = _is_blocked_ip(embedded)
                if inner_reason is not None:
                    return f"teredo {role} {inner_reason}"
        if ip_obj in _NAT64_WELL_KNOWN_NET:
            embedded_v4 = ipaddress.IPv4Address(int(ip_obj) & 0xFFFFFFFF)
            inner_reason = _is_blocked_ip(embedded_v4)
            if inner_reason is not None:
                return f"nat64 embedded {inner_reason}"
        if ip_obj in _NAT64_LOCAL_NET:
            return "nat64 local-use address"
    if ip_obj.is_loopback:
        return "loopback address"
    if ip_obj.is_link_local:
        return "link-local / cloud metadata address"
    if ip_obj.is_private:
        return "private / ULA address"
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj in _CGNAT_NET:
        return "carrier-grade NAT private address"
    if ip_obj.is_multicast:
        return "multicast address"
    if ip_obj.is_unspecified:
        return "unspecified address"
    if ip_obj.is_reserved or not ip_obj.is_global:
        return "non-global / reserved address"
    return None


def _validate_fetch_url_policy(
    url: str,
    *,
    allowed_domains: Sequence[str] | None = None,
    url_validator: Callable[[str], Any] | None = None,
) -> None:
    """校验可选的域名白名单与自定义 URL 策略回调（同时覆盖首跳与所有重定向跳转目标）。"""
    if allowed_domains is not None:
        host = (urlsplit(url).hostname or "").strip().lower().rstrip(".")
        allowed = False
        for entry in allowed_domains:
            pat = str(entry or "").strip().lower().rstrip(".")
            if not pat:
                continue
            if pat.startswith("domain:"):
                pat = pat[len("domain:") :].strip()
            if pat == "*" or host == pat:
                allowed = True
                break
            if any(ch in pat for ch in ("*", "?", "[")):
                if fnmatch.fnmatchcase(host, pat):
                    allowed = True
                    break
            elif pat.startswith(".") and host.endswith(pat):
                allowed = True
                break
        if not allowed:
            raise HostToolError("domain_not_allowed", f"目标域名不在允许列表中：{host}")
    if url_validator is not None:
        try:
            verdict = url_validator(url)
        except HostToolError:
            raise
        except ValueError as exc:
            raise HostToolError("url_policy_denied", str(exc)) from exc
        if verdict is False:
            raise HostToolError("url_policy_denied", f"URL 策略校验拒绝访问：{url}")


def _validate_fetch_url_and_resolve(
    url: str,
    *,
    allowed_domains: Sequence[str] | None = None,
    url_validator: Callable[[str], Any] | None = None,
) -> tuple[str, list[str]]:
    """严格校验抓取 URL 协议、主机名、可选域名/策略回调并执行 DNS 解析，阻断任何指向非公网 IP 的 SSRF 请求。"""
    if not isinstance(url, str) or not url.strip():
        raise HostToolError("invalid_url", "URL 不能为空。")
    clean = url.strip()
    if any(ch in clean for ch in ("\r", "\n", "\x00")):
        raise HostToolError("invalid_url", "URL 包含非法控制字符。")
    parsed = urlsplit(clean)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise HostToolError("ssrf_blocked", f"仅允许 http/https 协议，拒绝：{scheme or '(empty)'}")
    if parsed.username or parsed.password:
        raise HostToolError("ssrf_blocked", "拒绝包含内嵌凭据的 URL。")

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise HostToolError("invalid_url", "URL 缺少有效主机名。")
    if host in _BLOCKED_HOSTNAMES or any(host.endswith(sfx) for sfx in _BLOCKED_HOST_SUFFIXES):
        raise HostToolError("ssrf_blocked", f"SSRF 防护拦截受限主机名：{host}")

    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise HostToolError("invalid_url", f"URL 端口无效：{exc}") from exc
    if not (1 <= port <= 65535):
        raise HostToolError("invalid_url", "URL 端口超出有效范围。")

    # 若主机本身是 IP 字面量，先直接校验（离线场景也可立即拦截）
    try:
        lit_ip = ipaddress.ip_address(host)
    except ValueError:
        lit_ip = None
    if lit_ip is not None:
        reason = _is_blocked_ip(lit_ip)
        if reason is not None:
            raise HostToolError("ssrf_blocked", f"SSRF 防护拦截受限 IP ({lit_ip}): {reason}")

    # 在发起 DNS 解析前执行域名白名单与策略回调校验，阻断向受禁域名的 DNS 外泄
    _validate_fetch_url_policy(clean, allowed_domains=allowed_domains, url_validator=url_validator)

    if lit_ip is not None:
        return clean, [str(lit_ip)]

    # 对域名执行 DNS 解析并审查全部 A / AAAA 记录
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise HostToolError("dns_resolution_failed", f"DNS 解析失败（{host}）：{exc}") from exc
    if not infos:
        raise HostToolError("dns_resolution_failed", f"DNS 未返回任何地址：{host}")

    resolved_ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip_raw = str(sockaddr[0]).split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(ip_raw)
        except ValueError as exc:
            raise HostToolError("ssrf_blocked", f"无法解析的 DNS 目标 IP：{ip_raw}") from exc
        reason = _is_blocked_ip(ip_obj)
        if reason is not None:
            raise HostToolError("ssrf_blocked", f"SSRF 防护拦截：{host} 解析到受限 IP {ip_raw} ({reason})")
        if ip_raw not in resolved_ips:
            resolved_ips.append(ip_raw)

    if not resolved_ips:
        raise HostToolError("dns_resolution_failed", f"DNS 未解析出可用 IP：{host}")
    return clean, resolved_ips


def _build_pinned_http_transport(httpx_mod: Any, pinned_hosts: dict[str, list[str]]) -> Any:
    """构造固定预校验公网 IP 的 httpx/httpcore 传输层：直连已校验 IP 而不触发二次 DNS 解析，同时保留原始 Host 与 TLS SNI。"""
    import httpcore

    try:
        from httpcore._backends.sync import SyncStream as _HttpcoreSyncStream
    except ImportError:
        _HttpcoreSyncStream = None

    class _PinnedSyncBackend(httpcore.SyncBackend):
        def __init__(self, host_ip_map: dict[str, list[str]]) -> None:
            super().__init__()
            self._host_ip_map = host_ip_map

        def connect_tcp(
            self,
            host: str,
            port: int,
            timeout: float | None = None,
            local_address: str | None = None,
            socket_options: Any = None,
        ) -> Any:
            norm_host = host.strip().lower().strip("[]").rstrip(".")
            ips = self._host_ip_map.get(norm_host)
            if not ips:
                raise httpcore.ConnectError(f"SSRF 防护拒绝未固定 DNS 的主机直连：{host}")
            opts = list(socket_options) if socket_options is not None else []
            last_exc: BaseException | None = None
            for ip_str in ips:
                try:
                    ip_obj = ipaddress.ip_address(ip_str)
                except ValueError as exc:
                    last_exc = exc
                    continue
                blocked_reason = _is_blocked_ip(ip_obj)
                if blocked_reason is not None:
                    raise httpcore.ConnectError(f"SSRF 防护在建连阶段拦截受限 IP ({ip_str}): {blocked_reason}")
                if _HttpcoreSyncStream is None:
                    try:
                        return super().connect_tcp(
                            host=ip_str,
                            port=port,
                            timeout=timeout,
                            local_address=local_address,
                            socket_options=opts,
                        )
                    except (httpcore.ConnectTimeout, httpcore.ConnectError) as exc:
                        last_exc = exc
                        continue
                family = socket.AF_INET6 if isinstance(ip_obj, ipaddress.IPv6Address) else socket.AF_INET
                sockaddr: tuple[Any, ...] = (ip_str, port, 0, 0) if family == socket.AF_INET6 else (ip_str, port)
                sock: socket.socket | None = None
                try:
                    sock = socket.socket(family, socket.SOCK_STREAM)
                    if timeout is not None:
                        sock.settimeout(timeout)
                    if local_address is not None:
                        sock.bind((local_address, 0))
                    sock.connect(sockaddr)
                    for option in opts:
                        sock.setsockopt(*option)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    return _HttpcoreSyncStream(sock)
                except TimeoutError as exc:
                    if sock is not None:
                        with contextlib.suppress(OSError):
                            sock.close()
                    last_exc = httpcore.ConnectTimeout(str(exc))
                except OSError as exc:
                    if sock is not None:
                        with contextlib.suppress(OSError):
                            sock.close()
                    last_exc = httpcore.ConnectError(str(exc))
            if isinstance(last_exc, (httpcore.ConnectTimeout, httpcore.ConnectError)):
                raise last_exc
            raise httpcore.ConnectError(f"连接固定公网 IP 失败（{host}）：{last_exc}")

    class _PinnedHTTPTransport(httpx_mod.HTTPTransport):
        def __init__(self, host_ip_map: dict[str, list[str]]) -> None:
            super().__init__(
                verify=True,
                trust_env=False,
                limits=httpx_mod.Limits(max_connections=10, max_keepalive_connections=0),
            )
            ssl_context = getattr(self._pool, "_ssl_context", None)
            self._pool.close()
            self._pool = httpcore.ConnectionPool(
                ssl_context=ssl_context,
                max_connections=10,
                max_keepalive_connections=0,
                http1=True,
                http2=False,
                retries=0,
                network_backend=_PinnedSyncBackend(host_ip_map),
            )

    return _PinnedHTTPTransport(pinned_hosts)


class _ReadableMarkdownHTMLParser(HTMLParser):
    """基于 Python 标准库 `HTMLParser` 的轻量 HTML -> 可读 Markdown 提取器。"""

    _SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "svg", "head"})
    _BLOCK_TAGS = frozenset({"p", "div", "section", "article", "main", "header", "blockquote", "tr", "hr"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._in_title = False
        self._in_pre = False
        self._in_code = False
        self._link_href: str | None = None
        self._link_buf: list[str] = []
        self.title_parts: list[str] = []
        self._out: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t == "title":
            self._in_title = True
            return
        if t in self._SKIP_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return
        if t in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(t[1])
            self._out.append(f"\n\n{'#' * level} ")
        elif t == "br":
            self._out.append("\n")
        elif t == "li":
            self._out.append("\n- ")
        elif t == "blockquote":
            self._out.append("\n> ")
        elif t == "pre":
            self._in_pre = True
            self._out.append("\n```\n")
        elif t == "code" and not self._in_pre:
            self._in_code = True
            self._out.append("`")
        elif t == "a":
            attr_map = {k.lower(): (v or "") for k, v in attrs}
            href = attr_map.get("href", "").strip()
            if href.startswith(("http://", "https://", "/")):
                self._link_href = href
                self._link_buf = []
        elif t in self._BLOCK_TAGS:
            self._out.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "title":
            self._in_title = False
            return
        if t in self._SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth > 0:
            return
        if t in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._out.append("\n")
        elif t == "pre":
            self._in_pre = False
            self._out.append("\n```\n")
        elif t == "code" and self._in_code:
            self._in_code = False
            self._out.append("`")
        elif t == "a" and self._link_href is not None:
            label = " ".join("".join(self._link_buf).split()).strip()
            if label:
                self._out.append(f"[{label}]({self._link_href})")
            self._link_href = None
            self._link_buf = []
        elif t in self._BLOCK_TAGS:
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
            return
        if self._skip_depth > 0:
            return
        if self._link_href is not None:
            self._link_buf.append(data)
            return
        if self._in_pre:
            self._out.append(data)
        else:
            collapsed = re.sub(r"[ \t\r\f\v]+", " ", data)
            self._out.append(collapsed)

    def render(self) -> tuple[str, str]:
        title = " ".join("".join(self.title_parts).split()).strip()
        raw = "".join(self._out)
        lines = [ln.rstrip() for ln in raw.splitlines()]
        cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
        return title, cleaned


def _html_to_markdown(html_text: str) -> tuple[str, str]:
    parser = _ReadableMarkdownHTMLParser()
    parser.feed(html_text)
    parser.close()
    return parser.render()


def execute_web_fetch(
    url: str,
    max_chars: int = 16000,
    timeout: float = 15.0,
    *,
    client: Any | None = None,
    transport: Any | None = None,
    allowed_domains: Sequence[str] | None = None,
    url_validator: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """SSRF 防护网页抓取工具：严格校验 DNS/IP、固定已校验 IP 防 DNS Rebinding、逐跳复验重定向与策略并包裹 `untrusted_web_content`。"""
    try:
        import httpx
    except ImportError as exc:
        raise HostToolError("dependency_missing", "web_fetch 需要安装 httpx 依赖。") from exc

    char_limit = max(200, min(64000, int(max_chars)))
    req_timeout = max(1.0, min(60.0, float(timeout)))

    pinned_hosts: dict[str, list[str]] = {}

    def _record_pinned_host(target_url: str, ips: list[str]) -> None:
        host = (urlsplit(target_url).hostname or "").strip().lower().rstrip(".")
        if not host:
            return
        pinned_hosts[host] = list(ips)
        with contextlib.suppress(UnicodeError):
            ascii_host = host.encode("idna").decode("ascii")
            pinned_hosts[ascii_host] = list(ips)

    current_url, initial_ips = _validate_fetch_url_and_resolve(
        url,
        allowed_domains=allowed_domains,
        url_validator=url_validator,
    )
    _record_pinned_host(current_url, initial_ips)

    def _build_stream_kwargs(target_url: str, *, use_extensions: bool) -> dict[str, Any]:
        if not use_extensions:
            return {}
        parsed_hop = urlsplit(target_url)
        if (parsed_hop.scheme or "").lower() == "https" and parsed_hop.hostname:
            raw_host = parsed_hop.hostname.strip().rstrip(".")
            try:
                ipaddress.ip_address(raw_host)
            except ValueError:
                sni_host = raw_host
                with contextlib.suppress(UnicodeError):
                    sni_host = raw_host.encode("idna").decode("ascii")
                return {"extensions": {"sni_hostname": sni_host}}
        return {}

    def _fetch_with_client(http_client: Any, *, use_extensions: bool) -> dict[str, Any]:
        nonlocal current_url
        redirects_followed = 0
        while True:
            try:
                stream_kwargs = _build_stream_kwargs(current_url, use_extensions=use_extensions)
                with http_client.stream("GET", current_url, **stream_kwargs) as resp:
                    status_code = int(resp.status_code)
                    if status_code in {301, 302, 303, 307, 308}:
                        location = resp.headers.get("location")
                        if not location:
                            raise HostToolError("redirect_error", f"HTTP {status_code} 响应缺少 Location 头。")
                        redirects_followed += 1
                        if redirects_followed > _WEB_FETCH_MAX_REDIRECTS:
                            raise HostToolError("too_many_redirects", f"重定向超过 {_WEB_FETCH_MAX_REDIRECTS} 次上限。")
                        next_url = urljoin(current_url, location.strip())
                        # 关键 SSRF 防线：每一跳重定向目标均必须重新经过协议、策略回调与 DNS/IP 阻断校验并刷新固定 IP！
                        current_url, hop_ips = _validate_fetch_url_and_resolve(
                            next_url,
                            allowed_domains=allowed_domains,
                            url_validator=url_validator,
                        )
                        _record_pinned_host(current_url, hop_ips)
                        continue

                    content_type = str(resp.headers.get("content-type", "")).strip()
                    ct_low = content_type.lower()
                    if ct_low and not any(
                        tok in ct_low
                        for tok in ("text/", "application/json", "application/xml", "application/xhtml", "+json", "+xml")
                    ):
                        raise HostToolError("unsupported_content_type", f"不支持抓取二进制内容类型：{content_type}")

                    buf = bytearray()
                    byte_truncated = False
                    for chunk in resp.iter_bytes(chunk_size=16384):
                        if not chunk:
                            continue
                        if len(buf) + len(chunk) > _WEB_FETCH_MAX_BYTES:
                            remain = _WEB_FETCH_MAX_BYTES - len(buf)
                            if remain > 0:
                                buf.extend(chunk[:remain])
                            byte_truncated = True
                            break
                        buf.extend(chunk)
                    encoding = resp.encoding or "utf-8"
                    raw_text = bytes(buf).decode(encoding, errors="replace")
                    break
            except HostToolError:
                raise
            except httpx.TimeoutException as exc:
                raise HostToolError("fetch_timeout", f"抓取网页超时（{req_timeout}s）：{current_url}") from exc
            except httpx.HTTPError as exc:
                raise HostToolError("fetch_error", f"抓取网页网络请求失败：{exc}") from exc

        title = ""
        if "html" in ct_low or (not ct_low and "<html" in raw_text[:1024].lower()):
            title, extracted = _html_to_markdown(raw_text)
        else:
            extracted = raw_text.strip()

        truncated = byte_truncated or len(extracted) > char_limit
        extracted = extracted[:char_limit]
        safe_url_attr = current_url.replace('"', "&quot;")
        wrapped = f'<untrusted_web_content url="{safe_url_attr}">\n{extracted}\n</untrusted_web_content>'
        return {
            "ok": 200 <= status_code < 400,
            "url": current_url,
            "status_code": status_code,
            "content_type": content_type,
            "title": title,
            "trust": "untrusted_web_content",
            "truncated": truncated,
            "chars": len(extracted),
            "content": wrapped,
        }

    if client is not None:
        return _fetch_with_client(client, use_extensions=False)

    eff_transport = transport if transport is not None else _build_pinned_http_transport(httpx, pinned_hosts)
    with httpx.Client(
        transport=eff_transport,
        follow_redirects=False,
        timeout=req_timeout,
        trust_env=False,
        headers={"User-Agent": "agent-systems-lab/0.1 (+web_fetch)"},
    ) as managed_client:
        return _fetch_with_client(managed_client, use_extensions=(transport is None))


def _normalize_search_items(raw_items: Any, limit: int) -> list[dict[str, str]]:
    """将各搜索后端（自定义/SearXNG/Brave/Tavily/Serper）返回条目统一归一化为 `{title, url, snippet}`。"""
    if not isinstance(raw_items, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "").strip()
        url = str(item.get("url") or item.get("link") or item.get("href") or "").strip()
        snippet = str(
            item.get("snippet") or item.get("description") or item.get("content") or item.get("body") or ""
        ).strip()
        if not (title or url or snippet):
            continue
        out.append({"title": title[:300], "url": url[:1000], "snippet": snippet[:1000]})
        if len(out) >= limit:
            break
    return out


def execute_web_search(
    query: str,
    limit: int = 5,
    *,
    timeout: float = 15.0,
    client: Any | None = None,
) -> dict[str, Any]:
    """可配置外部网页搜索工具（支持 AGENT_LAB_WEB_SEARCH_URL / Brave / Tavily / Serper）。"""
    if not isinstance(query, str) or not query.strip():
        raise HostToolError("invalid_query", "搜索关键词不能为空。")
    clean_query = query.strip()
    max_results = max(1, min(20, int(limit)))
    req_timeout = max(1.0, min(60.0, float(timeout)))

    custom_url = os.environ.get("AGENT_LAB_WEB_SEARCH_URL", "").strip()
    custom_key = os.environ.get("AGENT_LAB_WEB_SEARCH_API_KEY", "").strip()
    brave_key = (os.environ.get("BRAVE_SEARCH_API_KEY") or os.environ.get("BRAVE_API_KEY") or "").strip()
    tavily_key = os.environ.get("TAVILY_API_KEY", "").strip()
    serper_key = os.environ.get("SERPER_API_KEY", "").strip()

    if not any((custom_url, brave_key, tavily_key, serper_key)):
        return {
            "ok": False,
            "code": "web_search_unconfigured",
            "query": clean_query,
            "error": (
                "未配置 Web 搜索后端。请设置以下任一环境变量："
                "AGENT_LAB_WEB_SEARCH_URL、BRAVE_SEARCH_API_KEY（或 BRAVE_API_KEY）、"
                "TAVILY_API_KEY 或 SERPER_API_KEY。"
            ),
            "supported_env_vars": [
                "AGENT_LAB_WEB_SEARCH_URL",
                "BRAVE_SEARCH_API_KEY",
                "TAVILY_API_KEY",
                "SERPER_API_KEY",
            ],
            "results": [],
        }

    try:
        import httpx
    except ImportError as exc:
        raise HostToolError("dependency_missing", "web_search 需要安装 httpx 依赖。") from exc

    def _run_search(http_client: Any) -> dict[str, Any]:
        provider_name = ""
        try:
            if custom_url:
                scheme = (urlsplit(custom_url).scheme or "").lower()
                if scheme not in {"http", "https"}:
                    raise HostToolError("invalid_url", "AGENT_LAB_WEB_SEARCH_URL 必须是 http:// 或 https:// 地址。")
                provider_name = "custom"
                headers = {"Accept": "application/json"}
                if custom_key:
                    headers["Authorization"] = f"Bearer {custom_key}"
                resp = http_client.get(
                    custom_url,
                    params={"q": clean_query, "query": clean_query, "limit": max_results, "format": "json"},
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                if isinstance(data, list):
                    raw_items = data
                elif isinstance(data, dict):
                    raw_items = (
                        data.get("results")
                        or data.get("items")
                        or data.get("organic")
                        or (data.get("web") or {}).get("results")
                        or []
                    )
                else:
                    raw_items = []
            elif brave_key:
                provider_name = "brave"
                resp = http_client.get(
                    "https://api.search.brave.com/res/v1/web/search",
                    params={"q": clean_query, "count": max_results},
                    headers={"Accept": "application/json", "X-Subscription-Token": brave_key},
                )
                resp.raise_for_status()
                data = resp.json()
                raw_items = (data.get("web") or {}).get("results", []) if isinstance(data, dict) else []
            elif tavily_key:
                provider_name = "tavily"
                resp = http_client.post(
                    "https://api.tavily.com/search",
                    json={"api_key": tavily_key, "query": clean_query, "max_results": max_results},
                    headers={"Accept": "application/json"},
                )
                resp.raise_for_status()
                data = resp.json()
                raw_items = data.get("results", []) if isinstance(data, dict) else []
            else:
                provider_name = "serper"
                resp = http_client.post(
                    "https://google.serper.dev/search",
                    json={"q": clean_query, "num": max_results},
                    headers={"Accept": "application/json", "X-API-KEY": serper_key},
                )
                resp.raise_for_status()
                data = resp.json()
                raw_items = data.get("organic", []) if isinstance(data, dict) else []
        except HostToolError:
            raise
        except httpx.TimeoutException as exc:
            raise HostToolError("search_timeout", f"搜索请求超时（{provider_name}, {req_timeout}s）。") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise HostToolError("search_error", f"搜索请求失败（{provider_name}）：{exc}") from exc

        normalized = _normalize_search_items(raw_items, max_results)
        return {
            "ok": True,
            "provider": provider_name,
            "query": clean_query,
            "trust": "untrusted_web_content",
            "count": len(normalized),
            "results": normalized,
        }

    if client is not None:
        return _run_search(client)

    with httpx.Client(
        timeout=req_timeout,
        headers={"User-Agent": "agent-systems-lab/0.1 (+web_search)"},
    ) as managed_client:
        return _run_search(managed_client)
