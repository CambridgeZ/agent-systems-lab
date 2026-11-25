"""权限策略与宿主执行边界：四级权限档位（ask / auto-edit / full-auto / plan）、命令风险分级与环境变量脱敏。

================================================================================
模块导读（Module Guide）
================================================================================
1. 模块角色（Role）
   本模块是 Agent 宿主直连执行（Host / Computer Profile）的核心策略决策点（PDP, Policy
   Decision Point）。在具备真实文件写入、终端命令执行、桌面 GUI 操控与外部 MCP 调用能力的
   Coding Agent 架构中，提示词（System Prompt）里的“请勿越权”不构成任何安全边界——一旦
   模型遭遇间接提示词注入（Indirect Prompt Injection，例如读取了恶意仓库 README、报错日志
   或网页内容），纯靠提示词约束的防线会瞬间失效。因此，必须在工具执行入口之前，由确定性的
   Python 代码完成静态命令风险分级、三级权限裁决、子进程敏感凭证剥离与工作区目录边界校验。

2. 推荐阅读顺序（Reading Order）
   - 第一步：阅读 `EXECUTION_PROFILES`、`PERMISSION_MODES` 与两个 `normalize_*` 归一化函数，
     理解“运行环境能力面（Profile）”与“人工审批自动化程度（Permission Mode）”正交解耦的设计。
   - 第二步：精读 `_CATASTROPHIC_PATTERNS`、`_has_catastrophic_rm_tokens` 与
     `classify_bash_command`，掌握终端命令如何从不可信字符串被确定性地划分为
     `blocked`（硬阻断）/ `safe_readonly`（安全只读）/ `mutating`（潜在副作用）。
   - 第三步：阅读 `READONLY_HOST_TOOLS` 与 `evaluate_tool_permission`，理解三级权限矩阵
     如何将工具类别与风险等级映射为 `allow` / `ask` / `deny` 三态决策。
   - 第四步：阅读 `build_scrubbed_env` 与 `validate_direct_workspace_path`，了解子进程凭据
     隔离、非交互防挂起环境变量注入，以及直连工作区根目录的反符号链接与系统禁区保护。

3. 核心安全不变量（Core Security Invariants）
   - 毁机熔断不变量：无论处于何种权限模式（包括 `full-auto`），命中毁机特征（递归删根目录或
     主目录、`mkfs` 格式化、`dd` 写块设备、Fork Bomb、关机/擦盘）的命令永远返回 `deny`。
   - 失败闭合（Fail-Closed）分类不变量：`classify_bash_command` 采用“严格白名单证明只读，
     否则一律视为 `mutating`”原则；凡含管道/重定向/多命令连接符、引号语法异常、非显示类内联
     环境变量、带 `/` 路径前缀的可执行文件或危险 CLI 参数，绝不进入 `safe_readonly` 通道。
   - 宿主凭证零泄露不变量：通过 `build_scrubbed_env` 派生的终端子进程，绝不继承宿主的 LLM
     API Key、平台鉴权 Token、数据库连接串或云厂商密钥（精确黑名单 + 正则启发式双重剔除）。
   - 工作区根边界不变量：直连工作区根目录本身不得为符号链接，且 `resolve()` 后的真实物理路径
     严禁指向 `/`、操作系统核心目录（如 `/etc`、`/System`）或用户密钥目录（如 `~/.ssh`）。

4. 技术面试高频考点速览（Interview Q&A Takeaways）
   - Q1：为什么要借鉴 Claude Code / Codex 设计 `ask`、`auto-edit`、`full-auto` 三级权限？
     A：若只有“全拦截”和“全放行”，开发者要么因每次改一行代码、跑一次 `git diff` 都要点审批
     而产生“审批疲劳（Approval Fatigue）”继而盲目全开，要么完全失去自动化研发效率。
     `auto-edit`（对标 Claude Code Accept Edits / Codex `auto-edit`）精准切中软件工程甜点：
     放行受工作区沙箱约束的文件编辑（可由 Git 轻松回滚）与静态证明无副作用的只读测试/检查命令，
     而将真正可能篡改系统状态、联网外发数据或执行任意脚本的 `mutating` 命令与 GUI 点击拦截为
     `ask` 提案审批，兼顾研发流吞吐与系统安全边界。
   - Q2：拦截 `rm -rf /` 为什么既用正则 `_CATASTROPHIC_PATTERNS`，又用词法分词检查操作数？
     A：纯正则难以穷举参数拆分与长选项变形（如 `rm -r -f -- /`、`rm --recursive ~`、
     `/bin/rm -rf /Users/alice`）；纯 `shlex.split` 则在遇到复杂语法（如 Fork Bomb
     `:(){ :|:& };:` 或未闭合引号）时难以直接匹配原始结构。两者结合形成纵深防御（Defense in
     Depth）：表层正则秒拒典型高危字面模式，词法 Token 分析穿透 `ENV=1`、`sudo`、长短选项与
     `--` 终止符精准核验 `rm` 的递归标志与高危目标操作数。
   - Q3：为什么 `GIT_EXTERNAL_DIFF=./pwn.sh git diff` 不能当作只读的 `git diff` 放行？
     A：POSIX Shell 允许在命令前通过 `KEY=VALUE` 注入单次环境变量。许多看似只读的程序会读取
     特定环境变量执行外部二进制或加载共享库（如 `GIT_EXTERNAL_DIFF`、`GIT_SSH_COMMAND`、
     Linux `LD_PRELOAD`、macOS `DYLD_INSERT_LIBRARIES`、`PYTHONPATH`）。因此分类器仅对
     `LC_ALL`、`LANG`、`TZ`、`NO_COLOR` 等纯显示/区域设置变量放行，其余内联变量一律按
     `mutating` 拦截审查。
   - Q4：为什么 `argv[0]` 只要包含 `/`（例如 `./pytest` 或 `/tmp/ls`）就拒绝判为只读？
     A：防止在 `auto-edit` 模式下发生跨工具链权限提升（Privilege Escalation）：在 `auto-edit`
     下 `fs_write` 是自动放行的；如果分类器只看文件名 `Path(argv[0]).name == "pytest"`，被劫持
     的模型就能先用 `fs_write` 在工作区写一个恶意脚本 `./pytest`，再调 `bash_exec("./pytest")`
     免审批执行任意代码！强制禁止 `argv[0]` 含 `/`，确保只读命令只能经由受信任的 `$PATH` 解析。
================================================================================
"""

from __future__ import annotations

from collections.abc import Sequence
import contextlib
import fnmatch
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import re
import shlex
import tempfile
import time
from typing import Any
from urllib.parse import urlparse

# 执行环境能力档位（Execution Profile）决定暴露给模型的工具集边界（Capability Surface）：
# - "sandbox"  : 经典高隔离沙盒模式，仅暴露受限的只读检索、显式补丁提案与容器化验收工具（无任意 bash/GUI）。
# - "host"     : 本地直连工作区模式（Claude Code / Codex CLI 形态），挂载 fs_* 与 bash_exec 工具链。
# - "computer" : 在 host 基础上额外挂载桌面原生 GUI 视觉与键鼠操作工具（computer_screen/mouse/keyboard/open）。
EXECUTION_PROFILES = frozenset({"sandbox", "host", "computer"})

# 四级权限模式（Permission Mode）控制副作用工具的人工介入水位（与 Claude Code / Codex 设计对齐）：
# - "ask"       : 保守模式（对标 Claude Code 默认模式 / Codex suggest）。只读检索直接放行，但一切
#                 文件写入（fs_write/fs_edit/fs_sketch_edit）、所有终端命令（含 ls/git status 等只读命令）、
#                 GUI 动作与非只读 MCP 工具均生成待审批提案，必须经人工确认后方可执行。
# - "auto-edit" : 自动编辑模式（对标 Claude Code Accept Edits / Codex auto-edit）。放行受工作区边界
#                 保护的文件写入/编辑，以及经静态证明无副作用的只读命令（git diff、ruff check、pytest）；
#                 可能产生系统级副作用的 mutating 终端命令、GUI 操控与外部非只读 MCP 调用仍需人工确认。
# - "full-auto" : 全自动执行模式（对标受护栏保护的自动化流水线模式）。自动放行工作区编辑、常规终端命令、
#                 GUI 操作与 MCP 工具，但仍受工作区路径禁区、子进程凭证脱敏与毁机级命令硬阻断（blocked）保护。
# - "plan"      : 严格只读规划模式（对标 Claude Code Plan Mode）。仅允许只读检索工具、交互式规划工具
#                 （ask_user_question / enter_plan_mode / exit_plan_mode）、静态安全只读命令（safe_readonly）、
#                 只读外部 MCP 调用，以及向 `.agent-lab/plans/` 写入计划产物；一切其他变更型工具均硬阻断
#                 （decision="deny", code="plan_mode_readonly"），不可被 allow/session_grants 绕过。
PERMISSION_MODES = frozenset({"ask", "auto-edit", "full-auto", "plan"})
_PERMISSION_MODE_ALIASES: dict[str, str] = {
    "plan-mode": "plan",
    "plan_mode": "plan",
}
PLAN_ARTIFACT_DIR = ".agent-lab/plans"

# 精确匹配的宿主敏感环境变量黑名单：涵盖主流大模型密钥、本系统 API/多租户/沙盒鉴权令牌、
# 数据库连接串（可能内含明文账密）以及云厂商与代码托管平台高权限凭据。
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
# 启发式正则黑名单：作为精确列表的第二道防线，大小写不敏感地拦截诸如 STRIPE_API_KEY、
# DB_PASSWORD、MY_CUSTOM_SECRET_KEY 等用户自定义或第三方 SDK 注入的敏感环境变量。
_SECRET_ENV_RE = re.compile(
    r"(?:API_KEY|AUTH_TOKEN|SECRET|PASSWORD|PRIVATE_KEY|CREDENTIAL|ACCESS_KEY|SESSION_TOKEN)",
    re.IGNORECASE,
)

# 在任何权限模式（包括 full-auto）下都硬阻断的毁机级破坏命令模式。
# 设计原因（Why）：正则层负责在分词之前直接捕获原始命令字符串中的毁灭性操作（尤其是像
# Fork Bomb 这类重度依赖特殊标点符号、普通词法分析难以直观建模的载荷，以及 dd 写裸块设备、
# mkfs 格式化、shutdown/diskutil eraseDisk 等不可逆物理破坏命令）。
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

# Shell 元字符检测正则：涵盖命令分隔/后台/逻辑连接（; & |）、命令替换（` $）、重定向（> <）与换行。
# 注意：单个管道符 `|` 的安全过滤链由 `_split_safe_pipe_segments` 在严格引号状态机下切分校验，
# 其余元字符（重定向、子壳、命令替换、多命令连接符、逻辑 || 与换行）一律按 mutating 拦截审查。
_SHELL_META_RE = re.compile(r"[;&|`$><\n\r]")
# 多子句切分正则：专供 _has_catastrophic_rm_tokens 与 deny/ask 规则在复合命令（含子壳括号与反引号）中逐段审计。
_CMD_SPLIT_RE = re.compile(r"[;&|()`\n\r]+")
_INLINE_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# 透明命令包装器及其带值标志表（用于 deny/ask 规则与毁机 rm 归一化，防止通过 wrapper 绕过拦截）。
_TRANSPARENT_WRAPPERS = frozenset({"sudo", "env", "command", "exec", "nohup", "nice", "time"})
_WRAPPER_SHORT_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "sudo": frozenset({"u", "g", "C", "D", "h", "p", "r", "t", "T", "U"}),
    "env": frozenset({"u", "C", "S", "P"}),
    "command": frozenset(),
    "exec": frozenset({"a"}),
    "nohup": frozenset(),
    "nice": frozenset({"n"}),
    "time": frozenset({"f", "o"}),
}
_WRAPPER_LONG_VALUE_FLAGS: dict[str, frozenset[str]] = {
    "sudo": frozenset(
        {
            "--user",
            "--group",
            "--close-from",
            "--chdir",
            "--host",
            "--prompt",
            "--role",
            "--type",
            "--command-timeout",
            "--other-user",
        }
    ),
    "env": frozenset(
        {
            "--unset",
            "--chdir",
            "--split-string",
            "--block-signal",
            "--default-signal",
            "--ignore-signal",
        }
    ),
    "command": frozenset(),
    "exec": frozenset(),
    "nohup": frozenset(),
    "nice": frozenset({"--adjustment"}),
    "time": frozenset({"--format", "--output"}),
}

# 纯只读管道右端过滤器白名单（仅允许作为安全管道链 RHS 从 stdin 过滤到 stdout，禁止文件覆写/任意读/外部执行）。
_SAFE_PIPE_FILTERS = frozenset({"head", "tail", "wc", "grep", "rg", "sort", "uniq", "cut", "tr", "cat"})

# 每个纯管道过滤器的严格精确白名单选项表（严禁黑名单或前缀模糊匹配，彻底杜绝 getopt_long 缩写绕过）。
_PIPE_FILTER_ALLOWLISTS: dict[str, dict[str, Any]] = {
    "head": {
        "short_bool": frozenset({"q", "v", "z"}),
        "short_value": frozenset({"n", "c"}),
        "long_bool": frozenset({"--quiet", "--silent", "--verbose", "--zero-terminated"}),
        "long_value": frozenset({"--lines", "--bytes"}),
        "long_eq_only": {},
        "allow_numeric_short": True,
        "max_positionals": 0,
    },
    "tail": {
        "short_bool": frozenset({"q", "v", "z", "r"}),
        "short_value": frozenset({"n", "c"}),
        "long_bool": frozenset({"--quiet", "--silent", "--verbose", "--zero-terminated"}),
        "long_value": frozenset({"--lines", "--bytes"}),
        "long_eq_only": {},
        "allow_numeric_short": True,
        "max_positionals": 0,
    },
    "wc": {
        "short_bool": frozenset({"l", "w", "c", "m", "L"}),
        "short_value": frozenset(),
        "long_bool": frozenset({"--lines", "--words", "--bytes", "--chars", "--max-line-length"}),
        "long_value": frozenset(),
        "long_eq_only": {},
        "allow_numeric_short": False,
        "max_positionals": 0,
    },
    "cat": {
        "short_bool": frozenset({"A", "b", "e", "E", "n", "s", "t", "T", "u", "v"}),
        "short_value": frozenset(),
        "long_bool": frozenset(
            {
                "--show-all",
                "--number-nonblank",
                "--show-ends",
                "--number",
                "--squeeze-blank",
                "--show-tabs",
                "--show-nonprinting",
            }
        ),
        "long_value": frozenset(),
        "long_eq_only": {},
        "allow_numeric_short": False,
        "max_positionals": 0,
    },
    "sort": {
        "short_bool": frozenset({"b", "c", "C", "d", "f", "g", "h", "i", "M", "n", "r", "R", "s", "u", "V", "z"}),
        "short_value": frozenset({"k", "t", "S"}),
        "long_bool": frozenset(
            {
                "--ignore-leading-blanks",
                "--check",
                "--dictionary-order",
                "--ignore-case",
                "--general-numeric-sort",
                "--human-numeric-sort",
                "--ignore-nonprinting",
                "--month-sort",
                "--numeric-sort",
                "--reverse",
                "--random-sort",
                "--stable",
                "--unique",
                "--version-sort",
                "--zero-terminated",
            }
        ),
        "long_value": frozenset({"--key", "--field-separator", "--buffer-size", "--sort", "--parallel"}),
        "long_eq_only": {},
        "allow_numeric_short": False,
        "max_positionals": 0,
    },
    "uniq": {
        "short_bool": frozenset({"c", "d", "D", "i", "u", "z"}),
        "short_value": frozenset({"f", "s", "w"}),
        "long_bool": frozenset({"--count", "--repeated", "--ignore-case", "--unique", "--zero-terminated"}),
        "long_value": frozenset({"--skip-fields", "--skip-chars", "--check-chars"}),
        "long_eq_only": {
            "--group": frozenset({"separate", "prepend", "append", "both"}),
            "--all-repeated": frozenset({"none", "prepend", "separate"}),
        },
        "allow_numeric_short": False,
        "max_positionals": 0,
    },
    "cut": {
        "short_bool": frozenset({"n", "s", "z"}),
        "short_value": frozenset({"b", "c", "d", "f"}),
        "long_bool": frozenset({"--only-delimited", "--complement", "--zero-terminated"}),
        "long_value": frozenset({"--bytes", "--characters", "--delimiter", "--fields", "--output-delimiter"}),
        "long_eq_only": {},
        "allow_numeric_short": False,
        "max_positionals": 0,
    },
    "tr": {
        "short_bool": frozenset({"c", "C", "d", "s", "t"}),
        "short_value": frozenset(),
        "long_bool": frozenset({"--complement", "--delete", "--squeeze-repeats", "--truncate-set1"}),
        "long_value": frozenset(),
        "long_eq_only": {},
        "allow_numeric_short": False,
        "max_positionals": 2,
    },
    "grep": {
        "short_bool": frozenset(
            {"E", "F", "G", "P", "i", "v", "w", "x", "c", "l", "L", "n", "H", "h", "o", "q", "s", "a", "I", "U", "z", "Z"}
        ),
        "short_value": frozenset({"e", "m", "A", "B", "C"}),
        "long_bool": frozenset(
            {
                "--extended-regexp",
                "--fixed-strings",
                "--basic-regexp",
                "--perl-regexp",
                "--ignore-case",
                "--invert-match",
                "--word-regexp",
                "--line-regexp",
                "--count",
                "--line-number",
                "--with-filename",
                "--no-filename",
                "--only-matching",
                "--quiet",
                "--silent",
                "--no-messages",
                "--text",
                "--null-data",
                "--null",
                "--initial-tab",
            }
        ),
        "long_value": frozenset(
            {"--regexp", "--max-count", "--after-context", "--before-context", "--context", "--label", "--binary-files"}
        ),
        "long_eq_only": {
            "--color": frozenset({"never", "auto", "always"}),
            "--colour": frozenset({"never", "auto", "always"}),
        },
        "allow_numeric_short": False,
        "max_positionals": 1,  # 若使用 -e/--regexp 显式模式则动态收紧为 0
    },
    "rg": {
        "short_bool": frozenset(
            {"s", "F", "i", "v", "x", "U", "P", "S", "a", "w", "b", "n", "N", "o", "p", "q", "H", "I", "c", "l"}
        ),
        "short_value": frozenset({"e", "m", "A", "B", "C", "r", "M"}),
        "long_bool": frozenset(
            {
                "--case-sensitive",
                "--fixed-strings",
                "--ignore-case",
                "--invert-match",
                "--line-regexp",
                "--multiline",
                "--pcre2",
                "--smart-case",
                "--text",
                "--word-regexp",
                "--byte-offset",
                "--line-number",
                "--no-line-number",
                "--only-matching",
                "--pretty",
                "--quiet",
                "--with-filename",
                "--no-filename",
                "--count",
                "--files-with-matches",
                "--no-heading",
                "--heading",
                "--vimgrep",
                "--trim",
                "--passthru",
                "--passthrough",
                "--include-zero",
            }
        ),
        "long_value": frozenset(
            {
                "--regexp",
                "--max-count",
                "--after-context",
                "--before-context",
                "--context",
                "--replace",
                "--max-columns",
                "--colors",
            }
        ),
        "long_eq_only": {
            "--color": frozenset({"never", "auto", "always"}),
        },
        "allow_numeric_short": False,
        "max_positionals": 1,  # 若使用 -e/--regexp 显式模式则动态收紧为 0
    },
}

# 高危非 Python 测试/检查命令的危险选项黑名单：
_UNSAFE_JS_PM_PREFIXES = (
    "--script-shell",
    "--userconfig",
    "--globalconfig",
    "--registry",
    "--cache",
    "--prefix",
    "--dir",
    "--cwd",
    "--config",
    "--shell-emulator",
    "--use-yarnrc",
)
_UNSAFE_TSC_FLAGS = frozenset(
    {
        "--outFile",
        "--outDir",
        "--declarationDir",
        "--tsBuildInfoFile",
        "--emitDeclarationOnly",
        "--watch",
        "-w",
        "--build",
        "-b",
        "--init",
        "--generateTrace",
        "--diagnostics",
    }
)
_UNSAFE_TSC_PREFIXES = (
    "--outFile=",
    "--outDir=",
    "--declarationDir=",
    "--tsBuildInfoFile=",
    "--generateTrace=",
)
_UNSAFE_CARGO_PREFIXES = ("--config", "-Z", "--fix", "--allow-dirty", "--allow-staged")
_UNSAFE_GO_TEST_PREFIXES = (
    "-exec",
    "--exec",
    "-toolexec",
    "--toolexec",
    "-o",
    "--o",
    "-c",
    "-cpuprofile",
    "-memprofile",
    "-blockprofile",
    "-mutexprofile",
    "-trace",
    "-coverprofile",
    "-outputdir",
    "-overlay",
    "-modfile",
)
_UNSAFE_GO_VET_PREFIXES = (
    "-vettool",
    "--vettool",
    "-toolexec",
    "--toolexec",
    "-overlay",
    "-modfile",
)
# settings.json 与 trusted_workspaces.json 体积上限（防止超大畸形文件拖垮启动）
_MAX_SETTINGS_BYTES = 256 * 1024

# 内联环境变量（KEY=VALUE cmd）严格白名单：
# 仅允许影响本地化语言、时区、终端颜色与字节码缓存的无害配置项。
# 严禁放行如下高危环境变量（它们可让只读命令瞬间蜕变为任意代码执行 RCE）：
# - GIT_EXTERNAL_DIFF / GIT_SSH_COMMAND / GIT_ASKPASS / PAGER : 劫持 git 只读子命令执行恶意脚本
# - LD_PRELOAD / LD_LIBRARY_PATH (Linux)                      : 劫持动态链接器注入恶意 .so 共享库
# - DYLD_INSERT_LIBRARIES / DYLD_LIBRARY_PATH (macOS)         : 在 Darwin 上注入恶意 .dylib 动态库
# - PYTHONPATH / PYTHONSTARTUP / PYTHONHOME                   : 劫持 python -m pytest/ruff 导入恶意包
_BENIGN_INLINE_ENV_KEYS = frozenset(
    {"LC_ALL", "LANG", "TZ", "TERM", "NO_COLOR", "FORCE_COLOR", "PYTHONDONTWRITEBYTECODE"}
)
# 单命令零副作用系统只读工具白名单（注意：刻意不包含 find / sed / awk / xargs / env 等可通过
# -exec、-i、system() 或子命令派生任意执行与文件覆写的“伪只读”工具）。
_SAFE_SINGLE_BINARIES = frozenset(
    {"ls", "pwd", "which", "uname", "whoami", "wc", "date", "true", "false"}
)
# 只读 Git 子命令白名单（配合下方标志过滤，确保不会修改引用、写任意文件或触发外部钩子）。
_SAFE_GIT_SUBCOMMANDS = frozenset({"status", "diff", "log", "branch", "show", "rev-parse"})
# git branch 默认不带参数时为只读列举，但一旦传入分支名或 -d/-D/-m/-M/-f 就会创建、删除或重置分支；
# 因此当 git branch 带有附加参数时，必须全部落在只读查询标志集合内才允许视为 safe_readonly。
_SAFE_GIT_BRANCH_FLAGS = frozenset(
    {"--show-current", "-a", "--all", "-r", "--remotes", "-v", "-vv", "--list"}
)
# 高危 Git 选项前缀黑名单（即便子命令是 diff / log / status 也必须阻断免审放行）：
# - --output=<file>         : 可将 diff/log 输出直接覆写到任意文件路径（任意文件篡改）
# - --ext-diff / --textconv : 触发外部差异/文本转换器执行任意二进制脚本
# - --no-index              : 脱离 Git 追踪索引对比任意两个宿主文件（如 git diff --no-index .env /dev/null 窃密）
# - --git-dir / --work-tree : 越过当前工作区边界窥探宿主机其他仓库
# - --exec / -c             : 注入临时配置（如 -c core.pager=evil.sh 或 -c diff.external=evil.sh）执行代码
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
# 高危 Pytest 选项前缀黑名单：
# - --basetemp              : Pytest 启动时会递归清空 basetemp 目录！若指向 "." 或重要目录会导致删库灾难
# - --junitxml / --log-file : 向指定路径写入文件（任意文件写入/覆盖）
# - --override-ini          : 篡改测试运行配置
# - --rootdir / --confcutdir: 诱导 Pytest 跨出当前项目加载外部恶意 conftest.py 执行任意代码
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

# 严禁作为本地直连工作区（Direct Workspace）挂载的操作系统敏感根目录前缀。
# 注意：macOS 下 /etc 与 /var 实际是指向 /private/etc 与 /private/var 的符号链接，
# 因此必须同时列出标准 POSIX 路径与 /private/* 物理路径，确保 Path.resolve() 后仍能精准命中。
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
# 严禁作为工作区挂载的用户主目录高危凭证子目录（SSH 私钥、GPG 密钥环、AWS 与 Kubernetes 集群凭据）。
_FORBIDDEN_HOME_SUBDIRS = frozenset({".ssh", ".gnupg", ".aws", ".kube"})


def normalize_execution_profile(
    profile: str | None, *, enable_gui: bool = False
) -> str:
    """归一化并校验运行环境档位（sandbox / host / computer）。

    当调用方显式请求开启桌面 GUI（enable_gui=True）且基础档位为 "host" 时，自动平滑升级为
    "computer" 档位；但若当前为严格受限的 "sandbox" 档位，则绝不因 enable_gui 而静默逃逸出沙盒。
    """
    clean = str(profile or "sandbox").strip().lower()
    if clean not in EXECUTION_PROFILES:
        raise ValueError(f"execution_profile 必须是 {sorted(EXECUTION_PROFILES)} 之一")
    if enable_gui and clean == "host":
        return "computer"
    return clean


def normalize_permission_mode(mode: str | None, *, default: str = "ask") -> str:
    """归一化并校验权限模式（ask / auto-edit / full-auto / plan），支持 plan-mode 别名，缺省回退至 ask。"""
    clean = str(mode or default).strip().lower()
    clean = _PERMISSION_MODE_ALIASES.get(clean, clean)
    if clean not in PERMISSION_MODES:
        raise ValueError(f"permission_mode 必须是 {sorted(PERMISSION_MODES)} 之一")
    return clean


def _safe_shlex_tokens(segment: str) -> list[str]:
    """解析命令子句为词法 Token；若引号未闭合则降级按空白切分并剥除引号与反斜杠，防畸形语法绕过安全拦截。"""
    try:
        return shlex.split(segment)
    except ValueError:
        out: list[str] = []
        for raw in segment.split():
            cleaned = re.sub(r"""["'\\]""", "", raw).strip()
            if cleaned:
                out.append(cleaned)
        return out


def _advance_past_wrapper(tokens: list[str]) -> list[str] | None:
    """若 `tokens[0]` 为内联环境变量赋值或透明命令包装器（sudo/env/command/exec/nohup/nice/time），返回剥离后的剩余 Token；否则返回 None。"""
    if not tokens:
        return None
    head = tokens[0]
    if _INLINE_ENV_ASSIGN_RE.match(head):
        return tokens[1:]
    wrapper = Path(head).name
    if wrapper not in _TRANSPARENT_WRAPPERS:
        return None

    short_val = _WRAPPER_SHORT_VALUE_FLAGS[wrapper]
    long_val = _WRAPPER_LONG_VALUE_FLAGS[wrapper]
    idx = 1
    n = len(tokens)
    while idx < n:
        tok = tokens[idx]
        if tok == "--":
            idx += 1
            break
        if wrapper == "env" and tok == "-":
            idx += 1
            continue
        if wrapper in {"sudo", "env"} and _INLINE_ENV_ASSIGN_RE.match(tok):
            idx += 1
            continue
        if not tok.startswith("-") or tok == "-":
            break
        # 特殊处理 env -S "..." / --split-string="..."：将拆出的命令拼接到后续参数之前
        if wrapper == "env" and (tok in {"-S", "--split-string"} or tok.startswith(("-S", "--split-string="))):
            if tok in {"-S", "--split-string"}:
                if idx + 1 < n:
                    split_toks = _safe_shlex_tokens(tokens[idx + 1])
                    return split_toks + tokens[idx + 2 :]
                return []
            if tok.startswith("--split-string="):
                split_toks = _safe_shlex_tokens(tok.split("=", 1)[1])
                return split_toks + tokens[idx + 1 :]
            # -S<attached>
            split_toks = _safe_shlex_tokens(tok[2:])
            return split_toks + tokens[idx + 1 :]
        if tok.startswith("--"):
            if "=" not in tok and tok in long_val:
                idx += 2
            else:
                idx += 1
            continue
        # 短选项：检查末尾是否需要独立参数值（例如 sudo -u root、nice -n 10、env -u HOME、exec -a fake）
        expects_next = False
        for j, ch in enumerate(tok[1:]):
            if ch in short_val:
                if j == len(tok[1:]) - 1:
                    expects_next = True
                break
        idx += 2 if expects_next else 1
    return tokens[idx:]


def _iter_normalized_bash_candidates(segment: str) -> list[str]:
    """为 deny/ask 规则生成单个子句的原始与多层归一化候选命令形式（穿透内联变量、透明包装器、引号转义与绝对路径）。"""
    seen: set[str] = set()
    candidates: list[str] = []

    def _add(text: str) -> None:
        clean = text.strip()
        if clean and clean not in seen:
            seen.add(clean)
            candidates.append(clean)

    _add(segment)
    cur = _safe_shlex_tokens(segment)
    steps = 0
    max_steps = max(8, len(cur) + 4)
    while cur and steps < max_steps:
        steps += 1
        _add(" ".join(cur))
        base = Path(cur[0]).name
        if "/" in cur[0] and base:
            _add(" ".join([base, *cur[1:]]))
        nxt = _advance_past_wrapper(cur)
        if nxt is None:
            break
        cur = nxt
    return candidates


def _normalize_rm_operand(operand: str, *, home_resolved: str) -> str:
    """展开 `~` / `$HOME` / `${HOME}` 并用 `posixpath.normpath` 规范化路径（保留尾部 `/*` 语义并折叠 POSIX `//`）。"""
    raw = operand.strip()
    if not raw:
        return ""
    if raw == "~" or raw.startswith("~/"):
        expanded = home_resolved + raw[1:]
    elif raw == "$HOME" or raw.startswith("$HOME/"):
        expanded = home_resolved + raw[len("$HOME") :]
    elif raw == "${HOME}" or raw.startswith("${HOME}/"):
        expanded = home_resolved + raw[len("${HOME}") :]
    else:
        expanded = raw

    norm = posixpath.normpath(expanded)
    # POSIX 规范下 posixpath.normpath("//") 会保留双斜杠 "//"，必须折叠为单斜杠 "/"，防止 `rm -rf //` 绕过！
    if norm.startswith("//"):
        norm = "/" + norm.lstrip("/")
    return norm


def _has_catastrophic_rm_tokens(command: str) -> str | None:
    """对命令做分段词法与操作数级审计，拦截正则难以穷举的变形递归删根/删主目录命令。

    为什么单靠 `_CATASTROPHIC_PATTERNS` 正则不够？（Why Both Regex & Token Inspection）
    攻击者或模型可能写出如下合法 POSIX/GNU 变形：
    1. 拆分短参数或夹带其他参数：`rm -r -f /`、`rm -v -r ~`
    2. 使用 GNU 长选项或缩写：`rm --recursive --no-preserve-root /`
    3. 使用 `--` 终止选项解析后跟危险路径：`rm -rf -- /`
    4. 使用绝对路径、引号转义或包装器：`/bin/rm -rf /`、`env sudo -u root rm -rf ${HOME}/.`
    5. 路径变形绕过：`rm -rf //`、`rm -rf /.`、`rm -rf /tmp/../..`、`rm -rf ${HOME}/.`

    本函数切分子句后还原真实参数列表，剥离前置 `KEY=VAL` 与透明命令包装器，识别任意路径下的 `rm`，
    并对每个操作数展开 `~`/`$HOME`/`${HOME}` 与 `posixpath.normpath` 归一化后比对 `/` 与用户主目录。
    """
    home_resolved = posixpath.normpath(str(Path.home().resolve()))
    if home_resolved.startswith("//"):
        home_resolved = "/" + home_resolved.lstrip("/")
    home_raw = posixpath.normpath(str(Path.home()))
    if home_raw.startswith("//"):
        home_raw = "/" + home_raw.lstrip("/")
    home_targets = {home_resolved, home_raw}
    dangerous_targets = {"/", "/*"} | home_targets | {f"{h}/*" for h in home_targets if h != "/"}

    for segment in _CMD_SPLIT_RE.split(command):
        seg = segment.strip()
        if not seg:
            continue
        cur = _safe_shlex_tokens(seg)
        steps = 0
        max_steps = max(8, len(cur) + 4)
        while cur and steps < max_steps:
            steps += 1
            nxt = _advance_past_wrapper(cur)
            if nxt is None:
                break
            cur = nxt
        if not cur:
            continue
        # 使用 Path(...).name 同时捕获 rm、/bin/rm、/usr/bin/rm 等调用形式
        if Path(cur[0]).name != "rm":
            continue
        rm_args = cur[1:]
        has_recursive = False
        operands: list[str] = []
        after_ddash = False
        for arg in rm_args:
            # POSIX 规范：遇到 "--" 后，后续所有参数无论是否以 "-" 开头均视为路径操作数
            if after_ddash:
                operands.append(arg)
            elif arg == "--":
                after_ddash = True
            elif arg in {"--recursive", "--no-preserve-root"} or (
                len(arg) >= 4 and arg.startswith("--rec") and "--recursive".startswith(arg)
            ):
                has_recursive = True
            elif arg.startswith("-") and not arg.startswith("--"):
                # 合并短选项（如 -rf、-frv、-R）只要包含 r/R 即具备目录树递归删除能力
                if "r" in arg.lower():
                    has_recursive = True
            elif not arg.startswith("-"):
                operands.append(arg)
        if has_recursive:
            for op in operands:
                norm_op = _normalize_rm_operand(op, home_resolved=home_resolved)
                if norm_op in dangerous_targets:
                    return "禁止递归删除根目录或用户主目录"
    return None


def _split_safe_pipe_segments(command: str) -> list[str] | None:
    """在严格 POSIX 引号状态机下将命令按单管道符 `|` 切分为子句，同时拒绝重定向、子壳、未加引号 `#` 注释与命令替换。

    返回 `None` 表示命令包含任何非单管道的 Shell 元字符（`; & ` $ > < ( ) # \n \r`、逻辑 `||`、
    空管道段或未闭合引号/悬空转义），调用方按 Fail-Closed 原则直接判定为 `mutating`。
    """
    segments: list[str] = []
    buf: list[str] = []
    in_single = False
    in_double = False
    escaped = False
    i = 0
    n = len(command)

    while i < n:
        ch = command[i]
        # 无论是否位于引号内，换行符一律拒绝，防多行命令偷渡
        if ch in ("\n", "\r"):
            return None
        if escaped:
            buf.append(ch)
            escaped = False
            i += 1
            continue
        if in_single:
            if ch == "'":
                in_single = False
            buf.append(ch)
            i += 1
            continue
        if in_double:
            if ch == "\\":
                escaped = True
                buf.append(ch)
                i += 1
                continue
            # 双引号内 `$` 与反引号仍会触发 Bash 变量/命令替换，必须阻断
            if ch in ("$", "`"):
                return None
            if ch == '"':
                in_double = False
            buf.append(ch)
            i += 1
            continue

        # 未在引号内
        if ch == "\\":
            escaped = True
            buf.append(ch)
            i += 1
            continue
        if ch == "'":
            in_single = True
            buf.append(ch)
            i += 1
            continue
        if ch == '"':
            in_double = True
            buf.append(ch)
            i += 1
            continue
        # 严禁未加引号的 Shell 元字符及 `#` 注释符（防止 `tsc # --noEmit` 注释绕过安全标志校验！）
        if ch in (";", "&", "`", "$", ">", "<", "(", ")", "#"):
            return None
        if ch == "|":
            # 拒绝 `||` 逻辑或与 `|&` 合并管道
            if i + 1 < n and command[i + 1] in ("|", "&"):
                return None
            seg = "".join(buf).strip()
            if not seg:
                return None
            segments.append(seg)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1

    if in_single or in_double or escaped:
        return None
    tail = "".join(buf).strip()
    if not tail:
        return None
    segments.append(tail)
    return segments


def _strip_benign_inline_env(tokens: list[str]) -> tuple[list[str] | None, str | None]:
    """剥离前置纯显示/区域设置内联环境变量；命中非白名单环境变量时返回拒绝原因。"""
    idx = 0
    while idx < len(tokens) and "=" in tokens[idx] and not tokens[idx].startswith("-"):
        env_key = tokens[idx].split("=", 1)[0]
        if env_key.upper() not in _BENIGN_INLINE_ENV_KEYS:
            return None, f"包含非只读白名单环境变量 {env_key}"
        idx += 1
    return tokens[idx:], None


def _validate_pipe_filter_args(
    prog: str,
    args: list[str],
    spec: dict[str, Any],
) -> tuple[bool, str]:
    """按每个管道过滤器的严格精确白名单校验全部短/长选项与位置参数个数，彻底消除黑名单与 getopt_long 缩写绕过。"""
    short_bool: frozenset[str] = spec["short_bool"]
    short_val: frozenset[str] = spec["short_value"]
    long_bool: frozenset[str] = spec["long_bool"]
    long_val: frozenset[str] = spec["long_value"]
    long_eq_only: dict[str, frozenset[str] | None] = spec["long_eq_only"]
    allow_numeric_short: bool = bool(spec["allow_numeric_short"])
    max_pos: int = int(spec["max_positionals"])

    positionals = 0
    expect_val = False
    after_ddash = False
    seen_short: set[str] = set()
    seen_long: set[str] = set()

    for arg in args:
        if expect_val:
            expect_val = False
            continue
        if after_ddash:
            positionals += 1
            continue
        if arg == "--":
            after_ddash = True
            continue
        if arg.startswith("--"):
            if "=" in arg:
                opt, val = arg.split("=", 1)
                if opt in long_val:
                    seen_long.add(opt)
                    continue
                if opt in long_eq_only:
                    allowed_vals = long_eq_only[opt]
                    if allowed_vals is not None and val not in allowed_vals:
                        return False, f"{prog} 选项 {opt} 的取值 {val!r} 不在白名单内"
                    seen_long.add(opt)
                    continue
                return False, f"{prog} 选项 {opt} 不在纯管道过滤器精确白名单内"
            if arg in long_bool:
                seen_long.add(arg)
                continue
            if arg in long_val:
                seen_long.add(arg)
                expect_val = True
                continue
            return False, f"{prog} 选项 {arg} 不在纯管道过滤器精确白名单内"
        if arg.startswith("-") and len(arg) > 1:
            if allow_numeric_short and arg[1:].isdigit():
                continue
            chars = arg[1:]
            for j, ch in enumerate(chars):
                if ch in short_bool:
                    seen_short.add(ch)
                    continue
                if ch in short_val:
                    seen_short.add(ch)
                    if j == len(chars) - 1:
                        expect_val = True
                    # 否则同 token 剩余字符即该短选项的紧贴值（例如 -n20、-iefoo）
                    break
                return False, f"{prog} 短选项 -{ch} 不在纯管道过滤器白名单内"
            continue
        positionals += 1

    if expect_val:
        return False, f"{prog} 选项缺少必需参数值"

    if prog in {"grep", "rg"}:
        has_explicit_pattern = ("e" in seen_short) or ("--regexp" in seen_long)
        eff_max_pos = 0 if has_explicit_pattern else 1
        if positionals > eff_max_pos:
            return False, f"{prog} 管道过滤器仅允许匹配 stdin，禁止指定文件路径位置参数"
        return True, f"{prog} 纯 stdin 管道过滤器"

    if positionals > max_pos:
        return False, f"{prog} 仅允许作为纯 stdin 管道过滤器，禁止指定文件位置参数"
    return True, f"{prog} 纯 stdin 管道过滤器"


def _is_safe_pipe_filter_segment(segment: str) -> tuple[bool, str]:
    """校验管道右端（RHS）单个过滤段是否为严格的纯 stdin->stdout 无副作用过滤器。"""
    if _split_safe_pipe_segments(segment) != [segment.strip()]:
        return False, "管道过滤器包含非法元字符或注释"
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return False, "管道过滤器引号不闭合或语法异常"
    if not tokens:
        return False, "空管道过滤段"

    argv, env_err = _strip_benign_inline_env(tokens)
    if env_err is not None or argv is None:
        return False, env_err or "管道过滤器包含受限环境变量"
    if not argv:
        return False, "空管道过滤段"
    if "/" in argv[0]:
        return False, "管道过滤器禁止使用显式路径可执行文件"

    prog = argv[0]
    rest = argv[1:]
    if prog not in _SAFE_PIPE_FILTERS:
        return False, f"管道右端命令 {prog} 不在纯过滤器白名单内"
    if any(is_token_secret(a) for a in rest):
        return False, "管道过滤器参数涉及敏感文件"

    spec = _PIPE_FILTER_ALLOWLISTS.get(prog)
    if spec is None:
        return False, f"未识别的管道过滤器 {prog}"
    return _validate_pipe_filter_args(prog, rest, spec)


def _classify_single_segment(segment: str) -> dict[str, str]:
    """对不含管道和 Shell 元字符的单条命令段执行白名单只读分类。"""
    if _split_safe_pipe_segments(segment) != [segment.strip()]:
        return {"classification": "mutating", "reason": "包含重定向、子壳、命令替换、注释或多命令连接符"}
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return {"classification": "mutating", "reason": "引号不闭合或包含复杂 shell 语法"}
    if not tokens:
        return {"classification": "blocked", "reason": "命令为空"}

    # 仅放行极少数无执行副作用的显示/区域设置内联环境变量；其余内联变量（如 GIT_EXTERNAL_DIFF、
    # LD_PRELOAD、DYLD_INSERT_LIBRARIES、PYTHONPATH）一律视为 mutating，封堵环境变量劫持 RCE。
    argv, env_err = _strip_benign_inline_env(tokens)
    if env_err is not None or argv is None:
        return {"classification": "mutating", "reason": env_err or "包含受限环境变量"}
    if not argv:
        return {"classification": "safe_readonly", "reason": "纯显示变量赋值空命令"}

    # 拒绝 ./ls 或 /tmp/git 等带路径分隔符的可执行文件伪装成系统只读命令：
    # 关键安全原因：在 auto-edit 模式下 fs_write 免审，若允许 ./pytest 按名称命中只读白名单，
    # 攻击者即可通过“先写 ./pytest 恶意脚本 -> 再免审运行 ./pytest”完成零交互任意代码执行！
    if "/" in argv[0]:
        return {"classification": "mutating", "reason": "显式路径可执行文件需按脚本审查"}

    # 剥离 uv run [--no-sync] 前缀以便识别只读测试/静态检查：
    # 注意仅允许剥离无副作用的锁定/静默标志（拒绝 --with / --script 等可拉取并运行外部包的参数），
    # 且剥离后必须二次检查目标命令是否含 "/"，防止 `uv run ./evil.py` 绕过路径检查。
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
        # 拦截会直接原地改写源码（--fix / --add-noqa）或覆写任意输出文件（-o / --output-file）的标志
        return not any(
            a.startswith(("--fix", "--unsafe-fixes", "--add-noqa", "-o", "--output-file"))
            for a in r_args
        )

    def _is_safe_pytest_args(p_args: list[str]) -> bool:
        # 拦截 -c（加载任意配置文件）、-o（覆盖 ini 选项）、-p（导入任意 Python 模块插件实现代码执行），
        # 同时拦截紧贴形式（-pfoo / -cfoo.ini / -ofoo=bar）与组合短选项（-qpfoo）以及 _UNSAFE_PYTEST_PREFIXES
        for a in p_args:
            if a.startswith(("-c", "-o", "-p")) or a.startswith(_UNSAFE_PYTEST_PREFIXES):
                return False
            if a.startswith("-") and not a.startswith("--") and len(a) > 2:
                for ch in a[1:]:
                    if ch in {"k", "m", "r", "W"}:
                        break
                    if ch in {"c", "o", "p"}:
                        return False
        return True

    def _is_safe_tsc_args(t_args: list[str]) -> bool:
        if "--noEmit" not in t_args:
            return False
        return not any(
            a in _UNSAFE_TSC_FLAGS or a.startswith(_UNSAFE_TSC_PREFIXES) or is_token_secret(a)
            for a in t_args
        )

    if prog in _SAFE_SINGLE_BINARIES:
        # 防止借基础命令探测敏感凭证文件存在性或统计内容（如 `wc -l .env`、`ls id_rsa`）
        if any(is_token_secret(a) for a in rest):
            return {"classification": "mutating", "reason": "命令参数涉及敏感文件"}
        return {"classification": "safe_readonly", "reason": f"白名单只读基础命令 ({prog})"}

    if prog == "git" and rest:
        # 拒绝 `git -c diff.external=... diff` 或 `git -C /etc status` 等前置全局标志调用
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

    # 仅放行显式 `-m` 标准测试与语法编译模块，拒绝 `python -c "..."` 或 `python script.py` 任意执行
    if prog in {"python", "python3"} and len(rest) >= 2 and rest[0] == "-m":
        mod = rest[1]
        mod_args = rest[2:]
        if mod in {"unittest", "py_compile"}:
            return {"classification": "safe_readonly", "reason": f"标准测试或语法检查 (python -m {mod})"}
        if mod == "pytest" and _is_safe_pytest_args(mod_args):
            return {"classification": "safe_readonly", "reason": "标准测试运行 (python -m pytest)"}
        if mod == "ruff" and mod_args and mod_args[0] == "check" and _is_safe_ruff_args(mod_args[1:]):
            return {"classification": "safe_readonly", "reason": "只读静态检查 (python -m ruff check)"}

    # 标准非 Python 测试 / 类型检查 / 静态 Lint 命令白名单：
    # 1. Node/JS 包管理器 test 子命令（npm test / pnpm test / yarn test）
    if prog in {"npm", "pnpm", "yarn"} and rest and rest[0] == "test":
        test_args = rest[1:]
        if not any(
            a in {"-C"} or a.startswith(_UNSAFE_JS_PM_PREFIXES) or is_token_secret(a)
            for a in test_args
        ):
            return {"classification": "safe_readonly", "reason": f"运行测试套件 ({prog} test)"}

    # 2. TypeScript 仅类型检查（tsc --noEmit / npx [--no-install] tsc --noEmit）
    if prog == "tsc" and _is_safe_tsc_args(rest):
        return {"classification": "safe_readonly", "reason": "只读类型检查 (tsc --noEmit)"}
    if prog == "npx" and rest:
        npx_idx = 0
        while npx_idx < len(rest) and rest[npx_idx] in {"--no-install", "--no"}:
            npx_idx += 1
        if npx_idx < len(rest) and rest[npx_idx] == "tsc" and _is_safe_tsc_args(rest[npx_idx + 1 :]):
            return {"classification": "safe_readonly", "reason": "只读类型检查 (npx tsc --noEmit)"}

    # 3. Rust Cargo 只读检查/测试/Clippy（禁止 --fix 与 --config 覆写 rustc-wrapper）
    if prog == "cargo" and rest and rest[0] in {"check", "test", "clippy"}:
        c_sub = rest[0]
        c_args = rest[1:]
        if not any(a.startswith(_UNSAFE_CARGO_PREFIXES) or is_token_secret(a) for a in c_args):
            return {"classification": "safe_readonly", "reason": f"只读 Cargo 检查/测试 (cargo {c_sub})"}

    # 4. Go test / vet（禁止 -exec / -toolexec / -vettool 外部执行以及 -o / -coverprofile 等写文件标志）
    if prog == "go" and rest and rest[0] == "test":
        g_args = rest[1:]
        if not any(
            a in _UNSAFE_GO_TEST_PREFIXES
            or any(a.startswith(f"{pfx}=") for pfx in _UNSAFE_GO_TEST_PREFIXES)
            or is_token_secret(a)
            for a in g_args
        ):
            return {"classification": "safe_readonly", "reason": "运行 Go 测试套件 (go test)"}
    if prog == "go" and rest and rest[0] == "vet":
        g_args = rest[1:]
        if not any(
            a in _UNSAFE_GO_VET_PREFIXES
            or any(a.startswith(f"{pfx}=") for pfx in _UNSAFE_GO_VET_PREFIXES)
            or is_token_secret(a)
            for a in g_args
        ):
            return {"classification": "safe_readonly", "reason": "只读 Go 静态检查 (go vet)"}

    return {"classification": "mutating", "reason": f"命令 {prog} 可能修改状态或执行任意脚本"}


def classify_bash_command(command: str) -> dict[str, str]:
    """对终端命令做确定性静态风险分类：blocked / safe_readonly / mutating。

    分类器遵循零信任“漏斗式白名单（Funnel Allowlisting）”架构：
    1. 黑名单熔断层（blocked）：空命令、空字节注入（`\\x00` 会导致 Python 字符串与底层 C `execve`
       发生截断语义不一致）、毁机正则与毁机 `rm` 操作数分析；命中则在一切权限模式下硬拒绝。
    2. 复合语法与安全管道切分层：凡含重定向 `><`、子壳 `()`、命令替换 `` ` `` / `$()`、多命令连接
       `; & ||` 或引号未闭合，一律降级为 `mutating`；若为合法单管道链 `LHS | RHS1 | RHS2`，仅当
       最左端命令属于 `safe_readonly` 且所有右端阶段均为严格的纯 stdin 过滤器（head/tail/wc/grep/
       rg/sort/uniq/cut/tr/cat，无写文件/外部执行/文件路径操作数）时，整体方可判为 `safe_readonly`。
    3. 细粒度工具与参数白名单层：涵盖基础只读命令、`git`、`ruff`、`pytest`、`python -m` 以及
       `npm/pnpm/yarn test`、`[npx] tsc --noEmit`、`cargo check/test/clippy`、`go test/vet`。
    """
    if not isinstance(command, str) or not command.strip():
        return {"classification": "blocked", "reason": "命令不能为空"}
    stripped = command.strip()
    # 防御 Null Byte 注入：底层 OS 系统调用以 \0 结尾，若放行可能造成审计看到的字符串与实际执行不一致
    if "\x00" in stripped:
        return {"classification": "blocked", "reason": "命令包含非法空字节"}
    for pattern, reason in _CATASTROPHIC_PATTERNS:
        if pattern.search(stripped):
            return {"classification": "blocked", "reason": reason}
    rm_block = _has_catastrophic_rm_tokens(stripped)
    if rm_block:
        return {"classification": "blocked", "reason": rm_block}

    segments = _split_safe_pipe_segments(stripped)
    if segments is None:
        return {"classification": "mutating", "reason": "包含重定向、子壳、命令替换、注释或多命令连接符"}

    lhs_verdict = _classify_single_segment(segments[0])
    if lhs_verdict["classification"] != "safe_readonly":
        return lhs_verdict
    if len(segments) == 1:
        return lhs_verdict

    for rhs in segments[1:]:
        ok_filter, filter_reason = _is_safe_pipe_filter_segment(rhs)
        if not ok_filter:
            return {"classification": "mutating", "reason": filter_reason}
    return {
        "classification": "safe_readonly",
        "reason": f"{lhs_verdict['reason']}（附纯只读管道过滤链）",
    }


def is_token_secret(token: str) -> bool:
    """检查单个命令行参数的文件名部分是否命中高危密钥/环境变量凭证命名特征。"""
    name = Path(token).name.casefold()
    return name.startswith((".env", "id_rsa", "id_ed25519")) or name.endswith((".pem", ".key", ".p12", ".pfx"))


# 交互式协作与规划模式状态机控制工具：不直接修改代码仓，在所有权限档位（含 ask / plan）下均默认放行。
INTERACTIVE_HOST_TOOLS = frozenset(
    {
        "ask_user_question",
        "enter_plan_mode",
        "exit_plan_mode",
    }
)

# 在所有权限档位（包括最严格的 ask 与 plan 模式）下均默认免审放行的纯只读检索、截屏观测与受控上下文管理工具集合。
# 说明：
# - `computer_screen` 仅截取屏幕状态而不发出键鼠副作用；
# - `memory_*` / `working_memory_*` / `skill_*` 仅在 Agent 自身隔离的状态数据库或托管记忆目录内读写，
#   不会直接篡改用户代码仓或宿主机系统状态；
# - `subagent_run` 派生的子智能体会完整继承（或收敛）父级权限策略，因此派发动作本身无需阻塞；
# - `ask_user_question` / `enter_plan_mode` / `exit_plan_mode` 为交互澄清与规划状态切换控制工具；
# - `ptc_exec` 仅在无网隔离沙箱中编排白名单只读工具，`shadow_verify_edits` 仅在隔离影子副本中试跑校验，
#   两者均不修改真实工作区，且依然服从显式 `deny`/`ask` 声明式规则。
READONLY_HOST_TOOLS = frozenset(
    {
        "repo_list",
        "repo_map",
        "repomap",
        "symbol_search",
        "repo_git_status",
        "repo_read",
        "repo_search",
        "knowledge_search",
        "verify_python",
        "fs_read",
        "fs_glob",
        "fs_grep",
        "lsp_query",
        "bash_output",
        "bash_list",
        "web_search",
        "mcp_list_resources",
        "mcp_read_resource",
        "a2a_list_agents",
        "a2a_discover_agent",
        "a2a_get_task",
        "computer_screen",
        "skill_search",
        "skill_inspect",
        "skill_load",
        "skill_unload",
        "mcp_tool_search",
        "memory_get",
        "memory_read",
        "memory_write",
        "memory_search",
        "memory_tag",
        "working_memory_read",
        "working_memory_write",
        "tool_result_get",
        "subagent_run",
        "ask_user_question",
        "enter_plan_mode",
        "exit_plan_mode",
        "ptc_exec",
        "shadow_verify_edits",
        "codebase_semantic_search",
        "browser_snapshot",
        "browser_console_logs",
        "browser_screenshot",
    }
)

# 工作区文件局部编辑类变更工具集合（统一适用 Edit(...) 声明式规则与 auto-edit/full-auto 放行、ask 审批、plan 硬拒策略）。
MUTATING_FILE_EDIT_TOOLS = frozenset({"fs_edit", "fs_sketch_edit"})

# 具有远端副作用的 A2A（Agent-to-Agent）写/控制工具：ask / auto-edit 下需人工确认，full-auto 或命中 A2A 规则放行，plan 硬阻断。
A2A_MUTATING_TOOLS = frozenset({"a2a_send_task", "a2a_cancel_task"})

# 具有外部网页导航或页面交互副作用的 Headless Browser 工具：
# plan 硬阻断（plan_mode_readonly），ask / auto-edit 需人工确认（category="browser", risk="browser"），full-auto 自动放行，并服从声明式 deny/ask/allow 规则。
BROWSER_INTERACTIVE_TOOLS = frozenset({"browser_navigate", "browser_click", "browser_type"})


# ---------------------------------------------------------------------------
# 工作区信任校验与分层 settings.json 安全加载（供 PermissionRuleEngine 与 HookManager 复用）
# ---------------------------------------------------------------------------


def _read_safe_json_dict(path: Path) -> dict[str, Any]:
    """安全读取受体积限制的非符号链接 JSON 字典文件；缺失、非法或越界一律返回空字典。"""
    try:
        if path.is_symlink() or not path.exists() or not path.is_file():
            return {}
        if path.parent.is_symlink():
            return {}
        st = path.stat()
        if st.st_size > _MAX_SETTINGS_BYTES:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        return {}


def is_workspace_trusted(
    workspace: Path,
    *,
    state_dir: Path | None = None,
) -> bool:
    """校验项目工作区是否已经过显式信任登记，防止克隆未知仓库时自动加载恶意项目规则或命令钩子。

    信任来源（任意一项精确匹配 resolved 工作区物理路径即视为已信任）：
    1. 环境变量 `AGENT_LAB_TRUSTED_WORKSPACES`（`os.pathsep` 分隔的目录路径列表）；
    2. `<state_dir>/trusted_workspaces.json`（格式为 `{"trusted": [...]}` 或 JSON 字符串列表）；
    3. `<state_dir>/settings.json` 中的 `"trusted_workspaces": [...]` 列表。
    """
    try:
        ws_expanded = Path(workspace).expanduser().absolute()
        if ws_expanded.is_symlink() or not ws_expanded.is_dir():
            return False
        ws_resolved = str(ws_expanded.resolve())
    except OSError:
        return False

    env_raw = os.environ.get("AGENT_LAB_TRUSTED_WORKSPACES", "").strip()
    if env_raw:
        for entry in env_raw.split(os.pathsep):
            clean = entry.strip()
            if not clean:
                continue
            with contextlib.suppress(OSError):
                if str(Path(clean).expanduser().resolve()) == ws_resolved:
                    return True

    if state_dir is None:
        return False
    st_dir = Path(state_dir)
    if st_dir.is_symlink() or not st_dir.is_dir():
        return False
    st_resolved = st_dir.resolve()

    # 1. 检查 <state_dir>/trusted_workspaces.json
    trust_file = st_resolved / "trusted_workspaces.json"
    try:
        if not trust_file.is_symlink() and trust_file.is_file() and trust_file.stat().st_size <= _MAX_SETTINGS_BYTES:
            raw = json.loads(trust_file.read_text(encoding="utf-8"))
            entries = raw.get("trusted", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
            for item in entries:
                path_str = item.get("path") if isinstance(item, dict) else item
                if isinstance(path_str, str) and path_str.strip():
                    with contextlib.suppress(OSError):
                        if str(Path(path_str).expanduser().resolve()) == ws_resolved:
                            return True
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        pass

    # 2. 检查 <state_dir>/settings.json 中的 trusted_workspaces 列表
    global_cfg = _read_safe_json_dict(st_resolved / "settings.json")
    raw_list = global_cfg.get("trusted_workspaces")
    if isinstance(raw_list, list):
        for item in raw_list:
            if isinstance(item, str) and item.strip():
                with contextlib.suppress(OSError):
                    if str(Path(item).expanduser().resolve()) == ws_resolved:
                        return True
    return False


def trust_workspace(workspace: Path, *, state_dir: Path) -> Path:
    """将指定工作区的物理路径原子写入 `<state_dir>/trusted_workspaces.json` 信任登记表。"""
    ws_resolved = validate_direct_workspace_path(workspace)
    st_root = Path(state_dir).expanduser().resolve()
    st_root.mkdir(parents=True, exist_ok=True)
    trust_file = st_root / "trusted_workspaces.json"
    if trust_file.is_symlink():
        raise ValueError("trusted_workspaces.json 不能是符号链接")

    trusted_paths: list[str] = []
    if trust_file.is_file():
        with contextlib.suppress(OSError, UnicodeError, json.JSONDecodeError):
            existing = json.loads(trust_file.read_text(encoding="utf-8"))
            raw_items = (
                existing.get("trusted", [])
                if isinstance(existing, dict)
                else (existing if isinstance(existing, list) else [])
            )
            for item in raw_items:
                p = item.get("path") if isinstance(item, dict) else item
                if isinstance(p, str) and p.strip():
                    trusted_paths.append(p.strip())

    ws_str = str(ws_resolved)
    if ws_str not in trusted_paths:
        trusted_paths.append(ws_str)

    payload = json.dumps(
        {"trusted": trusted_paths, "updated_at": round(time.time(), 3)},
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".agent-trust-", dir=st_root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, trust_file)
    finally:
        if os.path.exists(tmp_name):
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
    return trust_file


def untrust_workspace(workspace: Path, *, state_dir: Path) -> bool:
    """从 `<state_dir>/trusted_workspaces.json` 移除指定工作区的信任状态。"""
    ws_str = str(Path(workspace).expanduser().resolve())
    st_root = Path(state_dir).expanduser().resolve()
    trust_file = st_root / "trusted_workspaces.json"
    if trust_file.is_symlink() or not trust_file.is_file():
        return False
    try:
        existing = json.loads(trust_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    raw_items = (
        existing.get("trusted", [])
        if isinstance(existing, dict)
        else (existing if isinstance(existing, list) else [])
    )
    kept: list[str] = []
    removed = False
    for item in raw_items:
        p = item.get("path") if isinstance(item, dict) else item
        if isinstance(p, str) and p.strip():
            with contextlib.suppress(OSError):
                if str(Path(p).expanduser().resolve()) == ws_str:
                    removed = True
                    continue
            kept.append(p.strip())
    if not removed:
        return False
    payload = json.dumps(
        {"trusted": kept, "updated_at": round(time.time(), 3)},
        ensure_ascii=False,
        indent=2,
    ) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".agent-trust-", dir=st_root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, trust_file)
    finally:
        if os.path.exists(tmp_name):
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
    return True


def load_settings_layers(
    state_dir: Path | None = None,
    workspace: Path | None = None,
    *,
    trust_project_settings: bool | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """加载全局 `<state_dir>/settings.json` 与经信任校验的项目 `<workspace>/.agent-lab/settings.json`。

    安全边界：
    - 若项目工作区未经信任校验（`trust_project_settings` 为 `False`，或为 `None` 且未在信任表登记），
      一律拒绝读取 `<workspace>/.agent-lab/settings.json`，返回 `{}`；
    - 若 `state_dir` 与 `<workspace>/.agent-lab` 指向同一物理目录，避免重复加载同一份配置两次；
    - 拒绝任何符号链接目录或符号链接 `settings.json`。
    """
    global_settings: dict[str, Any] = {}
    project_settings: dict[str, Any] = {}
    global_path: Path | None = None

    if state_dir is not None:
        st_dir = Path(state_dir)
        if not st_dir.is_symlink() and st_dir.is_dir():
            global_path = st_dir.resolve() / "settings.json"
            global_settings = _read_safe_json_dict(global_path)

    if workspace is not None:
        ws_dir = Path(workspace)
        if not ws_dir.is_symlink() and ws_dir.is_dir():
            proj_dir = ws_dir.resolve() / ".agent-lab"
            proj_path = proj_dir / "settings.json"
            same_as_global = global_path is not None and proj_path.resolve() == global_path.resolve()
            if not same_as_global:
                trusted = (
                    bool(trust_project_settings)
                    if trust_project_settings is not None
                    else is_workspace_trusted(ws_dir, state_dir=state_dir)
                )
                if trusted and not proj_dir.is_symlink():
                    project_settings = _read_safe_json_dict(proj_path)

    return global_settings, project_settings


# ---------------------------------------------------------------------------
# 声明式权限规则引擎（PermissionRuleEngine）与会话级授权建议
# ---------------------------------------------------------------------------

_RULE_SYNTAX_RE = re.compile(r"^\s*([A-Za-z0-9_*.-]+)\s*(?:\((.*)\))?\s*$")

# 规则前缀别名到具体工具名的映射表（键全部小写）
_RULE_TOOL_ALIASES: dict[str, frozenset[str]] = {
    "bash": frozenset({"bash_exec"}),
    "bash_exec": frozenset({"bash_exec"}),
    "bashwrite": frozenset({"bash_write"}),
    "bash_write": frozenset({"bash_write"}),
    "bashkill": frozenset({"bash_kill"}),
    "bash_kill": frozenset({"bash_kill"}),
    "edit": frozenset({"fs_edit", "fs_sketch_edit"}),
    "fs_edit": frozenset({"fs_edit"}),
    "fs_sketch_edit": frozenset({"fs_sketch_edit"}),
    "sketchedit": frozenset({"fs_sketch_edit"}),
    "write": frozenset({"fs_write"}),
    "fs_write": frozenset({"fs_write"}),
    "read": frozenset({"fs_read"}),
    "fs_read": frozenset({"fs_read"}),
    "glob": frozenset({"fs_glob"}),
    "fs_glob": frozenset({"fs_glob"}),
    "grep": frozenset({"fs_grep"}),
    "fs_grep": frozenset({"fs_grep"}),
    "webfetch": frozenset({"web_fetch"}),
    "web_fetch": frozenset({"web_fetch"}),
    "websearch": frozenset({"web_search"}),
    "web_search": frozenset({"web_search"}),
    "lsp": frozenset({"lsp_query"}),
    "lsp_query": frozenset({"lsp_query"}),
    "computer": frozenset({"computer_screen", "computer_mouse", "computer_keyboard", "computer_open"}),
    "askuserquestion": frozenset({"ask_user_question"}),
    "ask_user_question": frozenset({"ask_user_question"}),
    "enterplanmode": frozenset({"enter_plan_mode"}),
    "enter_plan_mode": frozenset({"enter_plan_mode"}),
    "exitplanmode": frozenset({"exit_plan_mode"}),
    "exit_plan_mode": frozenset({"exit_plan_mode"}),
    "ptc": frozenset({"ptc_exec"}),
    "ptc_exec": frozenset({"ptc_exec"}),
    "shadow": frozenset({"shadow_verify_edits"}),
    "shadow_verify_edits": frozenset({"shadow_verify_edits"}),
    "codeindex": frozenset({"codebase_semantic_search"}),
    "codebase_semantic_search": frozenset({"codebase_semantic_search"}),
    "browser": frozenset(
        {
            "browser_navigate",
            "browser_snapshot",
            "browser_click",
            "browser_type",
            "browser_console_logs",
            "browser_screenshot",
        }
    ),
    "browser_navigate": frozenset({"browser_navigate"}),
    "browser_snapshot": frozenset({"browser_snapshot"}),
    "browser_click": frozenset({"browser_click"}),
    "browser_type": frozenset({"browser_type"}),
    "browser_console_logs": frozenset({"browser_console_logs"}),
    "browser_screenshot": frozenset({"browser_screenshot"}),
}


def _match_path_glob(rel_path: str, pattern: str) -> bool:
    """支持 `**` 多级匹配与零层目录折叠的路径 Glob 匹配器。"""
    norm_pat = pattern.strip().lstrip("./")
    norm_path = rel_path.strip().lstrip("./")
    if not norm_pat or not norm_path:
        return False
    if fnmatch.fnmatchcase(norm_path, norm_pat):
        return True
    if norm_pat.startswith("**/") and fnmatch.fnmatchcase(norm_path, norm_pat[3:]):
        return True
    if "/**/" in norm_pat and fnmatch.fnmatchcase(norm_path, norm_pat.replace("/**/", "/")):
        return True
    if norm_pat.endswith("/**"):
        prefix = norm_pat[:-3].rstrip("/")
        if norm_path == prefix or norm_path.startswith(prefix + "/"):
            return True
    return False


def _normalize_rule_path(raw_path: str, workspace: Path | None = None) -> str | None:
    """将工具参数中的路径归一化为 POSIX 相对路径；含空字节或 `..` 目录穿越时返回 None。"""
    if not isinstance(raw_path, str):
        return None
    cleaned = raw_path.strip().replace("\\", "/")
    if not cleaned or "\x00" in cleaned:
        return None
    if cleaned.startswith("/") and workspace is not None:
        ws_root = workspace.resolve()
        try:
            rel = PurePosixPath(cleaned).relative_to(PurePosixPath(ws_root.as_posix()))
            cleaned = rel.as_posix()
        except ValueError:
            try:
                rel = PurePosixPath(cleaned).relative_to(PurePosixPath(workspace.expanduser().absolute().as_posix()))
                cleaned = rel.as_posix()
            except ValueError:
                pass
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    parts = PurePosixPath(cleaned).parts
    if any(p == ".." for p in parts):
        return None
    return cleaned


def _extract_mcp_canonical(
    tool_name: str,
    arguments: dict[str, Any],
    mcp_routes: dict[str, Any] | None = None,
) -> tuple[str | None, str | None]:
    """将 `mcp__<server>__<tool>` 或 `mcp_call_tool` 还原为 `(full_ns_name, "server:tool")`。"""
    target = tool_name
    if tool_name == "mcp_call_tool":
        raw = arguments.get("tool_name") if isinstance(arguments, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            return None, None
        target = raw.strip()
    if not target.startswith("mcp__"):
        return None, None

    if isinstance(mcp_routes, dict) and target in mcp_routes and isinstance(mcp_routes[target], dict):
        route = mcp_routes[target]
        srv = str(route.get("server") or "").strip()
        remote = str(route.get("remote_name") or route.get("tool") or route.get("name") or "").strip()
        if srv and remote:
            return target, f"{srv}:{remote}"

    body = target[len("mcp__") :]
    if "__" in body:
        srv, remote = body.split("__", 1)
        return target, f"{srv}:{remote}"
    return target, body


def _extract_a2a_peer(arguments: dict[str, Any]) -> str:
    """从 A2A 工具调用参数中提取目标远端智能体标识（优先 `peer`，兼容发现/查询场景的 `url` 字段）。"""
    if not isinstance(arguments, dict):
        return ""
    for key in ("peer", "peer_name", "agent", "url", "agent_url"):
        raw = arguments.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def _split_a2a_rule_pattern(pattern: str) -> tuple[str, str] | None:
    """将 `A2A(...)` 括号内规则解析为 `(peer_glob, tool_glob)`；格式非法时 Fail-Closed 返回 None。

    支持：
    - `A2A(<peer_glob>)` -> `(peer_glob, "*")`，例如 `A2A(local-reviewer)`、`A2A(https://agent.example.com:8443)`
    - `A2A(<peer_glob>:<tool_glob>)` -> `(peer_glob, tool_glob)`，例如 `A2A(trusted-*:*)`、`A2A(*:a2a_send_task)`
    """
    pat = pattern.strip()
    if not pat:
        return None
    if pat == "*":
        return "*", "*"

    if "://" in pat:
        scheme_end = pat.index("://") + 3
        colon_idx = pat.rfind(":", scheme_end)
        if colon_idx == -1:
            return pat, "*"
        left = pat[:colon_idx].strip()
        right = pat[colon_idx + 1 :].strip()
        if not left or not right:
            return None
        # 若冒号右侧为纯数字端口或包含路径斜杠（如 host:8443 或 host:8443/a2a），整体视为 URL peer_glob
        if "/" in right or right.isdigit():
            return pat, "*"
        return left, right

    if ":" in pat:
        left, right = pat.rsplit(":", 1)
        peer_pat = left.strip()
        tool_pat = right.strip()
        if not peer_pat or not tool_pat:
            return None
        return peer_pat, tool_pat

    return pat, "*"


def _match_a2a_pattern(
    tool_name: str,
    arguments: dict[str, Any],
    pattern: str | None,
    *,
    allow_mode: bool,
) -> bool:
    """匹配 `A2A`、`A2A(<peer_glob>)` 与 `A2A(<peer_glob>:<tool_glob>)` 规则。"""
    if not tool_name.startswith("a2a_"):
        return False
    if pattern is None:
        return True
    parsed = _split_a2a_rule_pattern(pattern)
    if parsed is None:
        return False
    peer_pat, tool_pat = parsed
    if not fnmatch.fnmatchcase(tool_name, tool_pat):
        return False
    peer = _extract_a2a_peer(arguments)
    if allow_mode and any(ord(ch) < 32 or ord(ch) == 127 for ch in peer):
        return False
    if peer_pat == "*":
        return True
    if not peer:
        return False
    return fnmatch.fnmatchcase(peer, peer_pat)


def _match_bash_pattern(command: str, pattern: str, *, allow_mode: bool) -> bool:
    """匹配 `Bash(...)` 规则模式，支持 `prefix:*` 前缀通配、精确匹配与常规 Glob。

    安全不变量：
    - 当用于 `allow` / `session_grants` 放行规则（`allow_mode=True`）时，严格禁止通过 `&&` / `;` /
      重定向 / 子壳 / 命令替换绕过前缀授权（例如 `Bash(cargo test:*)` 绝不放行 `cargo test && curl evil`），
      但允许最左段命中前缀且右端全为纯过滤器的安全管道链（如 `cargo test | tail -n 20`）；
    - 当用于 `deny` / `ask` 规则（`allow_mode=False`）时，会对复合命令的每一子句逐一检测，确保
      `deny: ["Bash(curl:*)"]` 同样能阻断 `echo ok && curl evil.com`！
    """
    pat = pattern.strip()
    cmd = command.strip()
    if not pat or not cmd:
        return False
    if pat == "*":
        return True

    def _single_matches(candidate: str) -> bool:
        c = candidate.strip()
        if not c:
            return False
        if pat.endswith(":*"):
            prefix = pat[:-2].strip()
            if not prefix:
                return True
            if c == prefix:
                return True
            if c.startswith(prefix) and len(c) > len(prefix) and c[len(prefix)].isspace():
                return True
            return False
        if any(ch in pat for ch in ("*", "?", "[")):
            return fnmatch.fnmatchcase(c, pat)
        return c == pat

    if allow_mode:
        # 若规则本身未显式包含危险 Shell 元字符，则对命令执行严格防串联校验
        if not any(ch in pat for ch in (";", "&", "`", "$", ">", "<")):
            segs = _split_safe_pipe_segments(cmd)
            if segs is None:
                return False
            if len(segs) > 1:
                for rhs in segs[1:]:
                    ok_filter, _ = _is_safe_pipe_filter_segment(rhs)
                    if not ok_filter:
                        return False
                return _single_matches(segs[0])
            return _single_matches(segs[0])
        return _single_matches(cmd)

    # deny / ask 模式：全串及每个复合子句均同时匹配原始形式与归一化形式（剥离内联环境变量、
    # 透明命令包装器 sudo/env/command/exec/nohup/nice/time、引号转义与绝对路径前缀），
    # 防止 `/usr/bin/curl`、`FOO=1 curl`、`\curl`、`env curl`、`'c'url` 绕过 deny/ask 规则。
    for cand in _iter_normalized_bash_candidates(cmd):
        if _single_matches(cand):
            return True
    for seg in _CMD_SPLIT_RE.split(cmd):
        if not seg.strip():
            continue
        for cand in _iter_normalized_bash_candidates(seg):
            if _single_matches(cand):
                return True
    return False


def _match_webfetch_pattern(url: str, pattern: str) -> bool:
    """匹配 `WebFetch(...)` 规则：支持精确域名、通配子域名、域名+路径前缀与完整 URL Glob。"""
    pat = pattern.strip()
    if not pat or not isinstance(url, str) or not url.strip():
        return False
    if pat == "*":
        return True
    if pat.lower().startswith("domain:"):
        pat = pat[len("domain:") :].strip()
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False

    if "://" in pat:
        return fnmatch.fnmatchcase(url.strip().lower(), pat.lower())
    if "/" in pat:
        host_and_path = f"{host}{parsed.path or '/'}"
        return fnmatch.fnmatchcase(host_and_path.lower(), pat.lower())
    # 纯域名或域名通配（如 docs.python.org / *.python.org）
    pat_lower = pat.lower()
    if any(ch in pat_lower for ch in ("*", "?", "[")):
        return fnmatch.fnmatchcase(host, pat_lower)
    return host == pat_lower


def match_permission_rule(
    rule: str,
    tool_name: str,
    arguments: dict[str, Any],
    *,
    mode: str = "allow",
    workspace: Path | None = None,
    mcp_routes: dict[str, Any] | None = None,
) -> bool:
    """判断单条声明式权限规则是否命中指定工具调用。"""
    if not isinstance(rule, str) or not rule.strip():
        return False
    m = _RULE_SYNTAX_RE.match(rule)
    if not m:
        return False
    selector_raw = m.group(1).strip()
    arg_pat = m.group(2)
    selector_low = selector_raw.lower()
    args = arguments if isinstance(arguments, dict) else {}
    allow_mode = mode == "allow"

    # 1. MCP 命名空间规则：MCP(github:list_*) / MCP(mcp__github__*) / 裸 mcp__srv__tool
    if selector_low == "mcp" or selector_raw.startswith("mcp__"):
        if not (tool_name.startswith("mcp__") or tool_name == "mcp_call_tool"):
            return False
        full_ns, canonical = _extract_mcp_canonical(tool_name, args, mcp_routes=mcp_routes)
        if full_ns is None or canonical is None:
            return False
        if selector_raw.startswith("mcp__"):
            return fnmatch.fnmatchcase(full_ns, selector_raw)
        if arg_pat is None or arg_pat.strip() in {"", "*"}:
            return True
        pat = arg_pat.strip()
        if pat.startswith("mcp__"):
            return fnmatch.fnmatchcase(full_ns, pat)
        return fnmatch.fnmatchcase(canonical, pat)

    # 1b. A2A 远端智能体规则：A2A(<peer_glob>) / A2A(<peer_glob>:<tool_glob>) / 裸 A2A
    if selector_low == "a2a":
        return _match_a2a_pattern(tool_name, args, arg_pat, allow_mode=allow_mode)

    # 2. 选择器匹配工具名（支持标准别名、精确工具名与通配选择器）
    if selector_low in _RULE_TOOL_ALIASES:
        if tool_name not in _RULE_TOOL_ALIASES[selector_low]:
            return False
    elif selector_raw == "*":
        pass
    elif any(ch in selector_raw for ch in ("*", "?", "[")):
        if not fnmatch.fnmatchcase(tool_name, selector_raw):
            return False
    elif tool_name != selector_raw:
        return False

    # 无括号参数过滤 -> 匹配该工具的任意调用
    if arg_pat is None:
        return True
    pat = arg_pat.strip()
    if pat == "*":
        return True

    # 3. 按工具类别对括号内模式进行语义匹配
    if tool_name == "bash_exec":
        return _match_bash_pattern(str(args.get("command", "")), pat, allow_mode=allow_mode)

    if tool_name in {"fs_edit", "fs_sketch_edit", "fs_write", "fs_read", "fs_glob", "fs_grep", "lsp_query"}:
        raw_p = str(args.get("path") or args.get("file_path") or "")
        norm_p = _normalize_rule_path(raw_p, workspace=workspace)
        if norm_p is None:
            # 含 `..` 路径穿越时，放行规则绝不放行；拒绝规则仍可用原始串兜底拦截
            if allow_mode:
                return False
            return fnmatch.fnmatchcase(raw_p.strip(), pat)
        return _match_path_glob(norm_p, pat)

    if tool_name in {"web_fetch", "browser_navigate"}:
        return _match_webfetch_pattern(str(args.get("url", "")), pat)

    if tool_name == "web_search":
        return fnmatch.fnmatchcase(str(args.get("query", "")).strip(), pat)

    if tool_name in {"bash_write", "bash_kill"}:
        proc_id = str(args.get("proc_id", "")).strip()
        return fnmatch.fnmatchcase(proc_id, pat)

    if tool_name.startswith("a2a_"):
        return _match_a2a_pattern(tool_name, args, pat, allow_mode=allow_mode)

    # 其他自定义、浏览器或 GUI 工具：匹配常见关键参数或参数序列化表示
    for key in ("peer", "target", "ref_or_selector", "path", "path_prefix", "command", "url", "query", "proc_id"):
        if key in args and isinstance(args[key], str):
            if fnmatch.fnmatchcase(args[key].strip(), pat):
                return True
    return False


class PermissionRuleEngine:
    """声明式权限规则引擎：合并全局与受信任项目 `settings.json` 中的 deny / ask / allow 规则。

    优先级不变量：
    - 引擎内部严格按照 `deny` -> `ask` -> `allow` / `session_grants` 顺序裁决；
    - 合并全局配置与项目配置时采用并集策略，确保项目级 `allow` 永远无法覆盖全局 `deny` 或全局 `ask`。
    """

    def __init__(
        self,
        *,
        deny: Sequence[str] = (),
        ask: Sequence[str] = (),
        allow: Sequence[str] = (),
        workspace: Path | None = None,
        sources: Sequence[str] = (),
    ) -> None:
        self.deny: tuple[str, ...] = tuple(str(r).strip() for r in deny if isinstance(r, str) and r.strip())
        self.ask: tuple[str, ...] = tuple(str(r).strip() for r in ask if isinstance(r, str) and r.strip())
        self.allow: tuple[str, ...] = tuple(str(r).strip() for r in allow if isinstance(r, str) and r.strip())
        self.workspace: Path | None = Path(workspace).resolve() if workspace is not None else None
        self.sources: tuple[str, ...] = tuple(sources)

    @property
    def is_empty(self) -> bool:
        return not (self.deny or self.ask or self.allow)

    @classmethod
    def from_dict(
        cls,
        config: dict[str, Any],
        *,
        workspace: Path | None = None,
        source: str = "inline",
    ) -> PermissionRuleEngine:
        perms = config.get("permissions") if isinstance(config, dict) else None
        if not isinstance(perms, dict):
            perms = config if isinstance(config, dict) and any(k in config for k in ("deny", "ask", "allow")) else {}
        return cls(
            deny=perms.get("deny") if isinstance(perms.get("deny"), list) else (),
            ask=perms.get("ask") if isinstance(perms.get("ask"), list) else (),
            allow=perms.get("allow") if isinstance(perms.get("allow"), list) else (),
            workspace=workspace,
            sources=(source,) if perms else (),
        )

    @classmethod
    def from_settings(
        cls,
        state_dir: Path | None = None,
        workspace: Path | None = None,
        *,
        trust_project_settings: bool | None = None,
    ) -> PermissionRuleEngine:
        """从全局 `<state_dir>/settings.json` 与经信任验证的项目 `<workspace>/.agent-lab/settings.json` 加载规则。"""
        global_cfg, project_cfg = load_settings_layers(
            state_dir=state_dir,
            workspace=workspace,
            trust_project_settings=trust_project_settings,
        )
        deny_rules: list[str] = []
        ask_rules: list[str] = []
        allow_rules: list[str] = []
        sources: list[str] = []

        for label, cfg in (("global", global_cfg), ("project", project_cfg)):
            perms = cfg.get("permissions") if isinstance(cfg, dict) else None
            if not isinstance(perms, dict):
                continue
            added = False
            for bucket, target_list in (("deny", deny_rules), ("ask", ask_rules), ("allow", allow_rules)):
                items = perms.get(bucket)
                if isinstance(items, list):
                    for r in items:
                        if isinstance(r, str) and r.strip() and r.strip() not in target_list:
                            target_list.append(r.strip())
                            added = True
            if added:
                sources.append(label)

        return cls(
            deny=deny_rules,
            ask=ask_rules,
            allow=allow_rules,
            workspace=workspace,
            sources=sources,
        )

    def match_rule(
        self,
        rule: str,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        mode: str = "allow",
        mcp_routes: dict[str, Any] | None = None,
    ) -> bool:
        return match_permission_rule(
            rule,
            tool_name,
            arguments,
            mode=mode,
            workspace=self.workspace,
            mcp_routes=mcp_routes,
        )

    def evaluate(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        session_grants: Sequence[str] | None = None,
        mcp_routes: dict[str, Any] | None = None,
    ) -> dict[str, str] | None:
        """按 deny -> ask -> allow -> session_grants 优先级求值；未命中任何规则返回 None。"""
        is_a2a = tool_name in A2A_MUTATING_TOOLS or tool_name.startswith("a2a_")
        is_browser = tool_name in BROWSER_INTERACTIVE_TOOLS or tool_name.startswith("browser_")

        def _pack(decision: str, risk: str, reason: str, matched_rule: str) -> dict[str, str]:
            verdict = {
                "decision": decision,
                "risk": risk,
                "reason": reason,
                "matched_rule": matched_rule,
            }
            if is_a2a:
                verdict["category"] = "a2a"
            elif is_browser:
                verdict["category"] = "browser"
            return verdict

        for rule in self.deny:
            if self.match_rule(rule, tool_name, arguments, mode="deny", mcp_routes=mcp_routes):
                return _pack("deny", "policy_deny", f"命中显式拒绝规则：{rule}", rule)
        for rule in self.ask:
            if self.match_rule(rule, tool_name, arguments, mode="ask", mcp_routes=mcp_routes):
                return _pack("ask", _default_tool_risk(tool_name, arguments), f"命中显式审批规则：{rule}", rule)
        for rule in self.allow:
            if self.match_rule(rule, tool_name, arguments, mode="allow", mcp_routes=mcp_routes):
                return _pack("allow", _default_tool_risk(tool_name, arguments), f"命中显式放行规则：{rule}", rule)
        if session_grants:
            for grant in session_grants:
                if self.match_rule(grant, tool_name, arguments, mode="allow", mcp_routes=mcp_routes):
                    return _pack(
                        "allow",
                        _default_tool_risk(tool_name, arguments),
                        f"命中会话授权规则：{grant}",
                        grant,
                    )
        return None


_SUBCOMMAND_BINARIES = frozenset(
    {
        "cargo",
        "npm",
        "pnpm",
        "yarn",
        "npx",
        "go",
        "git",
        "docker",
        "kubectl",
        "poetry",
        "pip",
        "pip3",
        "make",
        "just",
    }
)


def suggest_session_grant_pattern(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    mcp_routes: dict[str, Any] | None = None,
) -> str | None:
    """为审批通过的工具调用生成安全的会话级授权模式建议（如 `Bash(cargo test:*)`、`Edit(src/**)`、`MCP(server:tool)`、`A2A(peer)`）。

    对高危硬阻断命令、含多命令串联的 Shell 语句或含 `..` 穿越的路径返回 `None`，拒绝生成宽泛授权建议。
    """
    args = arguments if isinstance(arguments, dict) else {}

    if tool_name == "bash_exec":
        cmd = str(args.get("command", "")).strip()
        if not cmd:
            return None
        if classify_bash_command(cmd)["classification"] == "blocked":
            return None
        segs = _split_safe_pipe_segments(cmd)
        if segs is None:
            return None
        # 仅取主命令段；若含管道但右端不安全则不建议授权
        if len(segs) > 1:
            for rhs in segs[1:]:
                ok_f, _ = _is_safe_pipe_filter_segment(rhs)
                if not ok_f:
                    return None
        try:
            tokens = shlex.split(segs[0])
        except ValueError:
            return None
        argv, env_err = _strip_benign_inline_env(tokens)
        if env_err is not None or not argv:
            return None
        prog = argv[0]
        rest = argv[1:]
        if prog == "npm" and len(rest) >= 2 and rest[0] == "run" and not rest[1].startswith("-"):
            return f"Bash(npm run {rest[1]}:*)"
        if prog == "uv" and len(rest) >= 2 and rest[0] == "run":
            idx = 1
            while idx < len(rest) and rest[idx] in {"--no-sync", "--locked", "-q", "--quiet"}:
                idx += 1
            if idx < len(rest) and not rest[idx].startswith("-"):
                return f"Bash({' '.join([prog] + rest[: idx + 1])}:*)"
        if prog in {"python", "python3"} and len(rest) >= 2 and rest[0] == "-m" and not rest[1].startswith("-"):
            return f"Bash({prog} -m {rest[1]}:*)"
        if prog in _SUBCOMMAND_BINARIES and rest and not rest[0].startswith("-") and "/" not in rest[0]:
            return f"Bash({prog} {rest[0]}:*)"
        return f"Bash({prog}:*)"

    if tool_name in {"fs_edit", "fs_sketch_edit", "fs_write", "fs_read"}:
        prefix_map = {
            "fs_edit": "Edit",
            "fs_sketch_edit": "Edit",
            "fs_write": "Write",
            "fs_read": "Read",
        }
        norm = _normalize_rule_path(str(args.get("path", "")))
        if not norm:
            return None
        parts = PurePosixPath(norm).parts
        if len(parts) >= 2:
            return f"{prefix_map[tool_name]}({parts[0]}/**)"
        return f"{prefix_map[tool_name]}({norm})"

    if tool_name.startswith("mcp__") or tool_name == "mcp_call_tool":
        _, canonical = _extract_mcp_canonical(tool_name, args, mcp_routes=mcp_routes)
        if canonical:
            return f"MCP({canonical})"
        return None

    if tool_name == "web_fetch":
        raw_url = str(args.get("url", "")).strip()
        if not raw_url:
            return None
        try:
            host = (urlparse(raw_url).hostname or "").lower()
        except ValueError:
            return None
        return f"WebFetch({host})" if host else None

    if tool_name in {"bash_write", "bash_kill"}:
        return f"{tool_name}(*)"

    if tool_name == "browser_navigate":
        raw_url = str(args.get("url", "")).strip()
        if not raw_url:
            return None
        try:
            host = (urlparse(raw_url).hostname or "").lower()
        except ValueError:
            return None
        return f"browser_navigate({host})" if host else None

    if tool_name in {"browser_click", "browser_type"}:
        return f"{tool_name}(*)"

    if tool_name in A2A_MUTATING_TOOLS:
        raw_peer = args.get("peer")
        if not isinstance(raw_peer, str):
            return None
        peer = raw_peer.strip()
        if (
            not peer
            or any(ch in peer for ch in ("*", "?", "[", "]", "(", ")"))
            or any(ord(ch) < 32 or ord(ch) == 127 for ch in peer)
        ):
            return None
        if ":" in peer and "://" not in peer:
            return None
        pattern = f"A2A({peer})"
        if not match_permission_rule(pattern, tool_name, {"peer": peer}, mode="allow"):
            return None
        return pattern

    return None


def _default_tool_risk(tool_name: str, arguments: dict[str, Any]) -> str:
    """根据工具名称推断统一的风险分类标签，供规则命中时保持返回结构一致。"""
    if tool_name in READONLY_HOST_TOOLS:
        return "readonly"
    if tool_name == "fs_write" or tool_name in MUTATING_FILE_EDIT_TOOLS:
        return "workspace_edit"
    if tool_name == "bash_exec":
        return classify_bash_command(str(arguments.get("command", "")))["classification"]
    if tool_name in {"bash_write", "bash_kill"}:
        return "process_control"
    if tool_name == "web_fetch":
        return "network_readonly"
    if tool_name in {"computer_mouse", "computer_keyboard", "computer_open"}:
        return "gui_action"
    if tool_name in BROWSER_INTERACTIVE_TOOLS:
        return "browser"
    if tool_name.startswith("mcp__") or tool_name == "mcp_call_tool":
        return "mcp_call"
    if tool_name in A2A_MUTATING_TOOLS:
        return "a2a"
    return "unknown"


def is_plan_artifact_path(raw_path: Any, *, workspace: Path | None = None) -> bool:
    """校验目标路径是否严格位于工作区 `.agent-lab/plans/` 计划产物目录内（防 `..` 穿越、前缀混淆与密钥文件名）。"""
    if not isinstance(raw_path, str):
        return False
    cleaned = raw_path.strip().replace("\\", "/")
    if not cleaned or "\x00" in cleaned or cleaned.endswith("/"):
        return False
    if any(seg == ".." for seg in cleaned.split("/")):
        return False
    norm = _normalize_rule_path(cleaned, workspace=workspace)
    if not norm or norm.startswith("/"):
        return False
    parts = PurePosixPath(norm).parts
    if len(parts) < 3 or parts[0] != ".agent-lab" or parts[1] != "plans":
        return False
    if any(is_token_secret(part) for part in parts[2:]):
        return False
    return True


def _is_mcp_call_read_only(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    mcp_read_only: bool = False,
    mcp_routes: dict[str, Any] | None = None,
) -> bool:
    """判定 MCP 外部工具调用是否被明确标注为只读（read_only）。"""
    if mcp_read_only:
        return True
    target_mcp_name = tool_name
    if tool_name == "mcp_call_tool":
        raw_target = arguments.get("tool_name")
        if raw_target is not None and str(raw_target).strip():
            target_mcp_name = str(raw_target).strip()
        else:
            return False
    if (
        isinstance(mcp_routes, dict)
        and target_mcp_name in mcp_routes
        and isinstance(mcp_routes[target_mcp_name], dict)
    ):
        return bool(mcp_routes[target_mcp_name].get("read_only", False))
    return False


def _evaluate_plan_mode_deny(
    tool_name: str,
    args: dict[str, Any],
    *,
    bash_verdict: dict[str, str] | None = None,
    mcp_read_only: bool = False,
    mcp_routes: dict[str, Any] | None = None,
    workspace: Path | None = None,
) -> dict[str, str] | None:
    """在 `plan` 只读规划模式下对一切非计划产物的变更型工具执行不可绕过的硬阻断（plan_mode_readonly）。"""

    def _deny(risk: str, reason: str, *, category: str | None = None) -> dict[str, str]:
        verdict: dict[str, str] = {
            "decision": "deny",
            "code": "plan_mode_readonly",
            "risk": risk,
            "reason": reason,
        }
        if category is not None:
            verdict["category"] = category
        return verdict

    if tool_name in READONLY_HOST_TOOLS:
        return None
    if tool_name in {"web_fetch", "sandbox_test"}:
        return None
    if tool_name == "fs_write":
        raw_p = args.get("path") if args.get("path") is not None else args.get("file_path")
        if is_plan_artifact_path(raw_p, workspace=workspace):
            return None
        return _deny(
            "workspace_edit",
            "plan 模式为只读规划模式，fs_write 仅允许写入 .agent-lab/plans/ 目录下的计划产物",
        )
    if tool_name in MUTATING_FILE_EDIT_TOOLS:
        return _deny(
            "workspace_edit",
            f"plan 模式为只读规划模式，禁止调用 {tool_name} 修改工作区文件",
        )
    if tool_name == "bash_exec":
        verdict = bash_verdict or classify_bash_command(str(args.get("command", "")))
        if verdict["classification"] == "safe_readonly":
            return None
        return _deny(
            "mutating",
            f"plan 模式为只读规划模式，禁止执行变更型终端命令（{verdict['reason']}）",
        )
    if tool_name in {"bash_write", "bash_kill"}:
        return _deny(
            "process_control",
            f"plan 模式为只读规划模式，禁止执行后台进程控制工具 {tool_name}",
        )
    if tool_name in {"computer_mouse", "computer_keyboard", "computer_open"}:
        return _deny(
            "gui_action",
            f"plan 模式为只读规划模式，禁止执行桌面 GUI 操作工具 {tool_name}",
        )
    if tool_name in BROWSER_INTERACTIVE_TOOLS:
        return _deny(
            "browser",
            f"plan 模式为只读规划模式，禁止执行交互式浏览器操作工具 {tool_name}",
            category="browser",
        )
    if tool_name in A2A_MUTATING_TOOLS:
        return _deny(
            "a2a",
            f"plan 模式为只读规划模式，禁止向远端 A2A 智能体执行 {tool_name}",
            category="a2a",
        )
    if tool_name in {"propose_patch", "propose_changeset"}:
        return _deny(
            "proposal",
            f"plan 模式为只读规划模式，禁止提交代码变更提案工具 {tool_name}",
        )
    if tool_name.startswith("mcp__") or tool_name == "mcp_call_tool":
        if _is_mcp_call_read_only(
            tool_name,
            args,
            mcp_read_only=mcp_read_only,
            mcp_routes=mcp_routes,
        ):
            return None
        return _deny(
            "mcp_call",
            "plan 模式为只读规划模式，禁止调用未标注只读（read_only）的外部 MCP 工具",
        )
    return _deny(
        "unknown",
        f"plan 模式为只读规划模式，拒绝执行未分类的潜在变更工具 {tool_name}",
    )


def evaluate_tool_permission(
    tool_name: str,
    arguments: dict[str, Any],
    *,
    permission_mode: str,
    mcp_read_only: bool = False,
    mcp_routes: dict[str, Any] | None = None,
    rule_engine: PermissionRuleEngine | None = None,
    session_grants: Sequence[str] | None = None,
    workspace: Path | None = None,
) -> dict[str, str]:
    """根据分层规则与四级权限策略（ask / auto-edit / full-auto / plan）裁决工具调用。

    评估优先级（Evaluation Precedence）：
    1. 硬阻断安全熔断（hard-deny）：毁机级 `bash_exec` 命令与越界 `mcp_call_tool` 命名空间，
       任何规则、会话授权或 `full-auto` 均不可绕过；
    1b. `plan` 严格只读规划硬阻断（plan_mode_readonly）：当处于 `plan` 模式时，除只读检索工具、
        交互规划工具（ask_user_question / enter_plan_mode / exit_plan_mode）、安全只读 bash_exec、
        只读外部 MCP 与写入 `.agent-lab/plans/` 计划产物外，所有变更型工具均硬拒绝，且不可被
        `allow` 规则或 `session_grants` 绕过；
    2. 声明式显式 `deny` 规则（`rule_engine.deny`）；
    3. 声明式显式 `ask` 规则（`rule_engine.ask`）；
    4. 声明式显式 `allow` 规则（`rule_engine.allow`）与当前会话临时授权（`session_grants`）；
    5. 四级权限矩阵（ask / auto-edit / full-auto / plan）：
       - READONLY（含交互规划工具 ask_user_question / enter_plan_mode / exit_plan_mode、lsp_query、
         bash_output / bash_list / web_search / mcp_*_resource / a2a_list_agents / a2a_discover_agent / a2a_get_task）：四档均放行；
       - fs_write：ask 审批，auto-edit / full-auto 放行，plan 仅放行 `.agent-lab/plans/` 内写入；
       - fs_edit / fs_sketch_edit：ask 审批，auto-edit / full-auto 放行，plan 硬阻断；
       - bash_exec (safe_readonly)：ask 审批，auto-edit / full-auto / plan 放行；
       - bash_exec (mutating) / bash_write / bash_kill：ask / auto-edit 审批，full-auto 放行，plan 硬阻断；
       - web_fetch (network_readonly)：ask / auto-edit / plan 审批（命中 WebFetch 规则除外），full-auto 放行；
       - computer_mouse/key/open：ask / auto-edit 审批，full-auto 放行，plan 硬阻断；
       - a2a_send_task / a2a_cancel_task：ask / auto-edit 审批（命中 A2A 规则除外），full-auto 放行，plan 硬阻断；
       - mcp__* / mcp_call_tool：标只读则四档均放行，未标只读则在 full-auto 放行、plan 硬阻断、其余审批；
       - propose_patch / propose_changeset：plan 硬阻断，其余放行；
       - 未知工具：plan 硬阻断，其余 Fail-Safe 一律审批。
    """
    mode = normalize_permission_mode(permission_mode)
    args = arguments if isinstance(arguments, dict) else {}
    effective_workspace = (
        workspace if workspace is not None else (rule_engine.workspace if rule_engine is not None else None)
    )

    # 第 1 层：硬阻断安全熔断（不可被任何规则或 full-auto 覆盖）
    bash_verdict: dict[str, str] | None = None
    if tool_name == "bash_exec":
        cmd = str(args.get("command", ""))
        bash_verdict = classify_bash_command(cmd)
        if bash_verdict["classification"] == "blocked":
            return {"decision": "deny", "risk": "blocked", "reason": bash_verdict["reason"]}

    if tool_name == "mcp_call_tool":
        raw_target = args.get("tool_name")
        if raw_target is not None and str(raw_target).strip():
            target_check = str(raw_target).strip()
            if not target_check.startswith("mcp__"):
                return {
                    "decision": "deny",
                    "risk": "blocked",
                    "reason": "mcp_call_tool 仅允许代理调用 mcp__* 外部命名空间工具",
                }

    # 第 1b 层：plan 模式只读规划硬边界（不可被 allow 规则或 session_grants 穿透）
    if mode == "plan":
        plan_deny = _evaluate_plan_mode_deny(
            tool_name,
            args,
            bash_verdict=bash_verdict,
            mcp_read_only=mcp_read_only,
            mcp_routes=mcp_routes,
            workspace=effective_workspace,
        )
        if plan_deny is not None:
            return plan_deny

    # 第 2~4 层：显式 deny -> ask -> allow / session_grants
    if rule_engine is not None or session_grants:
        engine = rule_engine if rule_engine is not None else PermissionRuleEngine()
        rule_verdict = engine.evaluate(
            tool_name,
            args,
            session_grants=session_grants,
            mcp_routes=mcp_routes,
        )
        if rule_verdict is not None:
            return rule_verdict

    # 第 5 层：四级权限矩阵
    if tool_name in READONLY_HOST_TOOLS:
        reason = (
            "交互式协作与规划状态控制工具默认放行"
            if tool_name in INTERACTIVE_HOST_TOOLS
            else "只读或上下文化工具默认放行"
        )
        verdict = {"decision": "allow", "risk": "readonly", "reason": reason}
        if tool_name.startswith("a2a_"):
            verdict["category"] = "a2a"
        elif tool_name.startswith("browser_"):
            verdict["category"] = "browser"
        return verdict

    if tool_name in {"propose_patch", "propose_changeset"}:
        # 传统提案工具自身就是生成提案（或由 auto_approve_proposals 控制），调用阶段不直接改盘
        return {"decision": "allow", "risk": "proposal", "reason": "显式补丁提案工具"}

    if tool_name == "sandbox_test":
        # sandbox_test 运行在受限临时副本或无网 Docker 容器中，不触碰宿主真实工作区原文件
        return {"decision": "allow", "risk": "sandbox", "reason": "受控隔离容器验收测试"}

    if tool_name == "fs_write":
        if mode == "plan":
            return {
                "decision": "allow",
                "risk": "workspace_edit",
                "reason": "plan 模式允许向 .agent-lab/plans/ 写入计划产物",
            }
        if mode in {"auto-edit", "full-auto"}:
            return {"decision": "allow", "risk": "workspace_edit", "reason": f"{mode} 模式允许直接编辑工作区文件"}
        return {"decision": "ask", "risk": "workspace_edit", "reason": "ask 模式写入文件前需要人工确认"}

    if tool_name in MUTATING_FILE_EDIT_TOOLS:
        if mode in {"auto-edit", "full-auto"}:
            return {"decision": "allow", "risk": "workspace_edit", "reason": f"{mode} 模式允许直接编辑工作区文件"}
        return {"decision": "ask", "risk": "workspace_edit", "reason": "ask 模式写入文件前需要人工确认"}

    if tool_name == "bash_exec":
        verdict = bash_verdict or classify_bash_command(str(args.get("command", "")))
        cls_name = verdict["classification"]
        if cls_name == "blocked":
            return {"decision": "deny", "risk": "blocked", "reason": verdict["reason"]}
        if cls_name == "safe_readonly":
            if mode in {"auto-edit", "full-auto", "plan"}:
                return {"decision": "allow", "risk": "safe_readonly", "reason": verdict["reason"]}
            return {"decision": "ask", "risk": "safe_readonly", "reason": f"ask 模式执行命令需人工确认（{verdict['reason']}）"}
        if mode == "full-auto":
            return {"decision": "allow", "risk": "mutating", "reason": "full-auto 模式允许自动运行终端命令"}
        return {"decision": "ask", "risk": "mutating", "reason": verdict["reason"]}

    if tool_name in {"bash_write", "bash_kill"}:
        if mode == "full-auto":
            return {"decision": "allow", "risk": "process_control", "reason": "full-auto 模式允许自动控制后台进程"}
        return {"decision": "ask", "risk": "process_control", "reason": f"{mode} 模式执行后台进程控制（{tool_name}）前需人工确认"}

    if tool_name == "web_fetch":
        if mode == "full-auto":
            return {"decision": "allow", "risk": "network_readonly", "reason": "full-auto 模式允许自动抓取外部网页内容"}
        return {"decision": "ask", "risk": "network_readonly", "reason": f"{mode} 模式抓取外部网页前需人工确认（可通过 WebFetch 规则放行指定域名）"}

    if tool_name in {"computer_mouse", "computer_keyboard", "computer_open"}:
        # 桌面 GUI 动作可跨出当前代码工作区点击浏览器、邮件客户端或系统设置，风险等同于 mutating 命令
        if mode == "full-auto":
            return {"decision": "allow", "risk": "gui_action", "reason": "full-auto 模式允许自动执行桌面 GUI 操作"}
        return {"decision": "ask", "risk": "gui_action", "reason": f"{mode} 模式执行桌面 GUI 键鼠/应用操作前需人工确认"}

    if tool_name in BROWSER_INTERACTIVE_TOOLS:
        if mode == "full-auto":
            return {
                "decision": "allow",
                "risk": "browser",
                "category": "browser",
                "reason": "full-auto 模式允许自动执行交互式浏览器操作",
            }
        return {
            "decision": "ask",
            "risk": "browser",
            "category": "browser",
            "reason": f"{mode} 模式执行交互式浏览器操作（{tool_name}）前需人工确认",
        }

    if tool_name in A2A_MUTATING_TOOLS:
        if mode == "full-auto":
            return {
                "decision": "allow",
                "risk": "a2a",
                "category": "a2a",
                "reason": "full-auto 模式允许自动向远端 A2A 智能体发送/取消任务",
            }
        return {
            "decision": "ask",
            "risk": "a2a",
            "category": "a2a",
            "reason": "向远端 A2A 智能体发送/取消任务需要人工确认",
        }

    if tool_name.startswith("mcp__") or tool_name == "mcp_call_tool":
        if _is_mcp_call_read_only(
            tool_name,
            args,
            mcp_read_only=mcp_read_only,
            mcp_routes=mcp_routes,
        ):
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
    """构造脱敏子进程环境变量：保留本地开发工具链环境，严格剔除 API 密钥与服务端令牌。

    设计考量（Why Scrub & Normalize Subprocess Env）：
    1. 防凭证窃取（Anti-Exfiltration）：Agent 运行所在的宿主服务进程通常持有 `ANTHROPIC_API_KEY`、
       `AGENT_LAB_TOKEN`（管理员/租户令牌）、`AGENT_LAB_STORAGE_DSN`（生产库连接串）以及开发者本机的
       `GITHUB_TOKEN`/`AWS_SECRET_ACCESS_KEY`。若直接透传 `os.environ`，一旦被执行的测试套件含恶意
       依赖或模型遭遇提示词注入执行 `env` / `curl`，宿主全部核心密钥将瞬间失守。
    2. 保留研发工具链可用性：不同于完全清空环境的 `env -i`（会导致 `pyenv`/`nvm`/`uv`/`cargo`/
       `HOME`/`USER` 丢失而无法编译测试），此处采用“全量保留普通开发变量 + 双层黑名单精准手术刀剥离”。
    3. 强制非交互式与实时刷新（Anti-Hang）：
       - `GIT_TERMINAL_PROMPT=0`：禁止 Git 在遇到私有仓库鉴权缺失时弹出终端密码提示死等输入；
       - `PAGER=cat` & `GIT_PAGER=cat`：禁止 `git log`/`git diff` 拉起 `less` 分页器挂起后台进程；
       - `PYTHONUNBUFFERED=1`：强制 Python 子进程禁用块缓冲，确保在超时被杀前最后时刻的崩溃栈与打印
         日志能实时刷入管道供 Agent 诊断修复。
    """
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
            # 过滤包含空字节的异常键值，防止底层 C putenv/execve 抛出 ValueError 或发生截断异常
            if key_str and "\x00" not in key_str and "\x00" not in str(val):
                cleaned[key_str] = str(val)
    return cleaned


# 语义等价别名，供 hooks.py 与外部调用方使用
scrub_subprocess_env = build_scrubbed_env


def validate_direct_workspace_path(
    raw_path: str | Path,
    *,
    allowed_roots: list[Path] | None = None,
) -> Path:
    """校验本地直连工作区目录：禁止符号链接根、系统关键目录与密钥配置目录。

    安全校验时序不变量（Why Order Matters）：
    1. 必须先在未 `resolve()` 的词法绝对路径（`expanded`）上调用 `expanded.is_symlink()`：
       如果先调用 `resolve()`，符号链接会被静默跟随为目标目录，`is_symlink()` 将永远返回 `False`，
       攻击者便可通过软链接把工作区挂载到受限区域或制造路径认知偏差。
    2. 确认词法根不是符号链接且确实是存在的目录后，再调用 `expanded.resolve()` 解出中间祖先路径中
       可能存在的系统级合法链接（如 macOS 下 `/var/folders/...` -> `/private/var/folders/...`），
       并在统一物理路径坐标系下核对系统禁区（`_FORBIDDEN_DIRECT_ROOTS`）、用户主目录密钥区
       （`_FORBIDDEN_HOME_SUBDIRS`）以及多租户部署可选配置的白名单根（`allowed_roots`）。
    """
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
