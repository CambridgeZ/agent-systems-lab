"""CaMeL 启发式数据血缘污点追踪（Provenance Taint Tracking）与统一出站密文防泄漏（Outbound Egress DLP）。

================================================================================
模块导读（Module Guide）
================================================================================
1. 模块角色（Role）
   本模块实现受 CaMeL（Defeating Prompt Injections by Design）架构启发的两道运行时防线，
   与 `permissions.py` 的权限裁决点协同工作，阻断间接提示词注入（Indirect Prompt Injection）
   触发的凭证外传与非预期控制流劫持：
   - 数据血缘与污点追踪（`DataProvenance` + `TaintRegistry`）：按运行实例（per-run）轻量追踪
     本轮是否摄入过不可信外部数据（网页、外部 MCP、远端 A2A 智能体），并以“SHA-256 + 截断
     前/后缀窗口指纹”形式登记敏感密钥——内存快照、事件日志与返回结果中绝不保存或打印原始密钥明文。
   - 统一出站密文防泄漏检查器（`EgressDlpInspector`）：在所有具备网络或外部外发能力的工具
     （`web_fetch`、`web_search`、`mcp_call_tool`、`mcp__*`、`a2a_send_task`、`a2a_discover_agent`、
     含 `curl`/`wget`/`nc`/`http`/`urllib`/`requests` 等出网命令的 `bash_exec`）执行前，递归扫描
     全部参数字符串（含原始文本、URL 解码视图与 Base64 片段解码视图），识别主流云/模型/代码平台密钥、
     Bearer JWT、PEM 私钥头、含明文密码的数据库 DSN、活跃环境变量密钥（原样 / Base64 / URL 编码）
     及已登记密钥指纹；命中即硬拒绝（`decision="deny", code="egress_dlp_blocked"`）并彻底脱敏。
   - 严格数据流控制流门控（`strict_dataflow=True` / CaMeL 模式）：当 `taint_registry.has_untrusted_taint`
     为真时，若工具尝试向未经显式批准的外部主机外发数据或执行未经批准的变更型命令，返回
     `decision="ask", code="tainted_control_flow"` 强制人工确认。
   - 交互式规划工具契约（`INTERACTIVE_TOOL_SPECS`）：导出 `ask_user_question`、`enter_plan_mode`、
     `exit_plan_mode` 三个标准 JSON Schema Draft 2020-12 工具规范供 `runtime.py` 挂载。

2. 核心安全不变量（Core Security Invariants）
   - 零明文留痕不变量：`TaintRegistry` 与 `EgressDlpInspector` 绝不在指纹对象、`reason`、`findings`、
     `matched_rules` 或 `to_dict()` 快照中输出任何原始密钥或可逆密钥片段。
   - DLP 优先于 CaMeL 审批不变量：出站请求一旦命中密钥泄漏特征，永远返回硬拒绝 `deny`
     （`egress_dlp_blocked`），绝不会因 `strict_dataflow=True` 或 `explicitly_approved=True` 降级为 `ask`/`allow`。
   - 编码抗绕过不变量：对出站参数同时检查原始视图、URL 解码视图（最多两轮解套）与 Base64/Base64URL
     片段解码视图，阻断 `base64(secret)`、`base64("k=" + secret)` 与 `%XX` 百分号编码绕过。
================================================================================
"""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
import fnmatch
import hashlib
import os
import re
import threading
import time
from typing import Any
from urllib.parse import quote, quote_plus, unquote, unquote_plus, urlparse

from .permissions import (
    A2A_MUTATING_TOOLS,
    MUTATING_FILE_EDIT_TOOLS,
    _SECRET_ENV_EXACT,
    _SECRET_ENV_RE,
    classify_bash_command,
)


# ---------------------------------------------------------------------------
# 1. 数据血缘信任域（DataProvenance）与轻量记录结构
# ---------------------------------------------------------------------------


class DataProvenance(StrEnum):
    """CaMeL 启发式数据来源信任域枚举（兼容 JSON 序列化与字符串比较）。"""

    TRUSTED_USER = "trusted_user"
    WORKSPACE_FILE = "workspace_file"
    UNTRUSTED_WEB = "untrusted_web"
    UNTRUSTED_MCP = "untrusted_mcp"
    UNTRUSTED_A2A = "untrusted_a2a"
    SENSITIVE_SECRET = "sensitive_secret"

    @property
    def is_untrusted(self) -> bool:
        return self in UNTRUSTED_PROVENANCES

    @property
    def is_sensitive(self) -> bool:
        return self is DataProvenance.SENSITIVE_SECRET


TRUSTED_USER = DataProvenance.TRUSTED_USER
WORKSPACE_FILE = DataProvenance.WORKSPACE_FILE
UNTRUSTED_WEB = DataProvenance.UNTRUSTED_WEB
UNTRUSTED_MCP = DataProvenance.UNTRUSTED_MCP
UNTRUSTED_A2A = DataProvenance.UNTRUSTED_A2A
SENSITIVE_SECRET = DataProvenance.SENSITIVE_SECRET

UNTRUSTED_PROVENANCES = frozenset(
    {
        DataProvenance.UNTRUSTED_WEB,
        DataProvenance.UNTRUSTED_MCP,
        DataProvenance.UNTRUSTED_A2A,
    }
)


def _normalize_provenance(value: DataProvenance | str) -> DataProvenance:
    if isinstance(value, DataProvenance):
        return value
    clean = str(value or "").strip().lower()
    for member in DataProvenance:
        if clean == member.value or clean == member.name.lower():
            return member
    raise ValueError(f"未知的 DataProvenance：{value!r}")


@dataclass(frozen=True)
class TaintEvent:
    """单次不可信或敏感数据摄入记录（摘要已脱敏，绝不含原始密钥）。"""

    provenance: DataProvenance
    source_tool: str
    call_id: str = ""
    timestamp: float = 0.0
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "provenance": str(self.provenance),
            "source_tool": self.source_tool,
            "call_id": self.call_id,
            "timestamp": self.timestamp,
            "summary": self.summary,
        }


@dataclass(frozen=True)
class _WindowMatcher:
    """无明文滑窗指纹匹配器：仅保存长度、极短前后缀锚点与完整 SHA-256，用于 O(N) 精准子串检出。"""

    variant: str
    length: int
    prefix: str
    suffix: str
    sha256: str


@dataclass(frozen=True)
class SecretFingerprint:
    """已登记敏感密钥的安全指纹元数据（仅保留 SHA-256 与极短脱敏提示，绝不存储密钥明文）。"""

    label: str
    sha256: str
    length: int
    prefix_token: str
    suffix_token: str
    provenance: DataProvenance = DataProvenance.SENSITIVE_SECRET
    registered_at: float = 0.0

    @property
    def masked_hint(self) -> str:
        if self.prefix_token and self.suffix_token:
            return f"{self.prefix_token}***{self.suffix_token}"
        return f"***sha256:{self.sha256[:10]}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "sha256": self.sha256,
            "length": self.length,
            "prefix": self.prefix_token,
            "suffix": self.suffix_token,
            "masked_hint": self.masked_hint,
            "provenance": str(self.provenance),
            "registered_at": self.registered_at,
        }


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="surrogatepass")).hexdigest()


def _build_window_matcher(variant: str, text: str) -> _WindowMatcher | None:
    """将待保护变体转换为无明文滑窗匹配器；短于 8 字符的值跳过以防碰撞与短锚点泄露。"""
    n = len(text)
    if n < 8:
        return None
    anchor_len = 4 if n >= 12 else 2
    return _WindowMatcher(
        variant=variant,
        length=n,
        prefix=text[:anchor_len],
        suffix=text[-anchor_len:],
        sha256=_sha256_hex(text),
    )


def _window_matcher_hits(haystack: str, matcher: _WindowMatcher) -> bool:
    """在任意文本中基于前后缀锚点与 SHA-256 精确检验是否存在已登记密钥子串。"""
    if len(haystack) < matcher.length:
        return False
    start = 0
    prefix = matcher.prefix
    suffix = matcher.suffix
    sfx_len = len(suffix)
    length = matcher.length
    limit = len(haystack) - length
    while start <= limit:
        idx = haystack.find(prefix, start)
        if idx == -1 or idx > limit:
            return False
        end = idx + length
        if haystack[end - sfx_len : end] == suffix:
            if _sha256_hex(haystack[idx:end]) == matcher.sha256:
                return True
        start = idx + 1
    return False


def _make_safe_display_tokens(raw_secret: str) -> tuple[str, str]:
    """生成用于指纹审计的极短脱敏前后缀（仅当长度 >= 16 时保留至多 2 字符，否则置空）。"""
    if len(raw_secret) < 16:
        return "", ""
    return raw_secret[:2], raw_secret[-2:]


def _iter_env_secret_items(
    env: Mapping[str, str] | None = None,
    *,
    min_length: int = 16,
) -> list[tuple[str, str]]:
    """读取活跃环境中命中 `_SECRET_ENV_EXACT` 或 `_SECRET_ENV_RE` 且长度达标的敏感变量键值。"""
    source = os.environ if env is None else env
    items: list[tuple[str, str]] = []
    for key, raw_val in source.items():
        if not isinstance(key, str):
            continue
        if key not in _SECRET_ENV_EXACT and not _SECRET_ENV_RE.search(key):
            continue
        val = str(raw_val or "")
        if len(val) >= min_length:
            items.append((key, val))
    return items


# ---------------------------------------------------------------------------
# 2. 已知密文正则规则与无明文脱敏函数
# ---------------------------------------------------------------------------

# 规则顺序重要：更具体的模式（如 anthropic_api_key、bearer_jwt）排在通用模式（openai_api_key、bearer_token）之前，
# 以便按 span 覆盖关系抑制冗余低精度重复命中，同时避免漏报。
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "anthropic_api_key",
        re.compile(r"(?<![A-Za-z0-9_-])sk-ant-[A-Za-z0-9_-]{8,}"),
        "Anthropic API 密钥",
    ),
    (
        "openai_project_key",
        re.compile(r"(?<![A-Za-z0-9_-])sk-(?:proj|svcacct|admin|live|test)-[A-Za-z0-9_-]{12,}"),
        "OpenAI 项目/服务密钥",
    ),
    (
        "openai_api_key",
        re.compile(r"(?<![A-Za-z0-9_-])sk-(?!ant-)[A-Za-z0-9]{20,}(?:T3BlbkFJ[A-Za-z0-9]{20,})?"),
        "OpenAI API 密钥",
    ),
    (
        "github_pat",
        re.compile(r"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{16,}"),
        "GitHub Fine-Grained PAT 令牌",
    ),
    (
        "github_token",
        re.compile(r"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{16,}"),
        "GitHub 访问令牌",
    ),
    (
        "aws_access_key_id",
        re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"),
        "AWS Access Key ID",
    ),
    (
        "google_api_key",
        re.compile(r"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{35}(?![A-Za-z0-9_-])"),
        "Google API 密钥",
    ),
    (
        "slack_token",
        re.compile(r"(?<![A-Za-z0-9_-])(?:xox[baprs]-[0-9A-Za-z-]{10,}|xapp-[0-9]-[A-Za-z0-9-]{10,})"),
        "Slack 令牌",
    ),
    (
        "stripe_key",
        re.compile(
            r"(?<![A-Za-z0-9_])(?:(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}|whsec_[A-Za-z0-9]{16,})"
        ),
        "Stripe 密钥",
    ),
    (
        "pem_private_key",
        re.compile(r"-----BEGIN [A-Z0-9 ]{0,40}PRIVATE KEY(?: BLOCK)?-----"),
        "PEM 私钥头",
    ),
    (
        "database_dsn_password",
        re.compile(
            r"(?i)\b(?:postgres(?:ql)?|mysql|mariadb|mongodb(?:\+srv)?|rediss?|amqps?|mssql|sqlserver|clickhouse|cockroachdb)"
            r"://[^/\s:@\"']*:[^/\s@\"']+@[^\s\"'<>]+"
        ),
        "含明文密码的数据库连接串（DSN）",
    ),
    (
        "bearer_jwt",
        re.compile(r"(?i)\bBearer\s+eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        "Bearer JWT 令牌",
    ),
    (
        "jwt_token",
        re.compile(r"(?<![A-Za-z0-9_.-])eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        "JWT 令牌",
    ),
    (
        "bearer_token",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
        "Bearer 鉴权令牌",
    ),
)

_SUBSUMED_BY_BEARER_JWT = frozenset({"jwt_token", "bearer_token"})
_PERCENT_ENCODED_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_BASE64_CANDIDATE_RE = re.compile(r"[A-Za-z0-9+/_-]{20,}={0,2}")
_URL_HOST_EXTRACT_RE = re.compile(r"https?://[^\s\"'<>`)\]]+", re.IGNORECASE)

# 判定 `bash_exec` 命令是否包含出站网络外发能力的确定性特征集合
_BASH_NETWORK_SINK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\b(?:curl|wget|nc|ncat|netcat|socat|xh|httpie|telnet|scp|sftp|ftp|rsync|dig|nslookup)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[;&|]\s*)(?:(?:sudo|env|command|exec|nohup)\s+)*https?\s+"
        r"(?:GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|https?://|--|[A-Za-z0-9._-]*[:/]|[A-Za-z0-9-]+\.[A-Za-z]{2,})",
        re.IGNORECASE,
    ),
    re.compile(r"https?://", re.IGNORECASE),
    re.compile(
        r"\b(?:urllib(?:\.request|\.parse)?|import\s+requests\b|from\s+requests\b|"
        r"requests\.(?:get|post|put|patch|delete|head|request|Session)\b|"
        r"import\s+httpx\b|from\s+httpx\b|httpx\.(?:get|post|put|patch|delete|request|Client|AsyncClient)\b|"
        r"aiohttp\b|http\.client\b|socket\.(?:create_connection|socket)\b|smtplib\b|ftplib\b|xmlrpc\b|"
        r"axios\b|fetch\s*\()",
        re.IGNORECASE,
    ),
    re.compile(r"/dev/(?:tcp|udp)/", re.IGNORECASE),
)

# 明确属于外部出站汇聚点（Outbound Sink）的工具名集合
OUTBOUND_SINK_TOOLS = frozenset(
    {
        "web_fetch",
        "web_search",
        "mcp_call_tool",
        "a2a_send_task",
        "a2a_discover_agent",
        "a2a_get_task",
        "a2a_cancel_task",
    }
)

# 在 CaMeL 严格数据流模式下被视为需要人工确认的不可信外发汇聚工具
_CAMEL_NETWORK_SINK_TOOLS = frozenset(
    {
        "web_fetch",
        "web_search",
        "mcp_call_tool",
        "a2a_send_task",
        "a2a_discover_agent",
        "a2a_cancel_task",
    }
)

# 在 CaMeL 严格数据流模式下被视为变更型副作用的工具集合
_CAMEL_MUTATING_TOOLS = (
    frozenset(
        {
            "fs_write",
            "bash_write",
            "bash_kill",
            "computer_mouse",
            "computer_keyboard",
            "computer_open",
            "propose_patch",
            "propose_changeset",
        }
    )
    | MUTATING_FILE_EDIT_TOOLS
    | A2A_MUTATING_TOOLS
)


def redact_secrets(
    text: str,
    *,
    env: Mapping[str, str] | None = None,
    min_env_length: int = 16,
) -> str:
    """对任意文本中的已知密钥模式与活跃环境变量密钥执行原地脱敏，确保日志与摘要不泄露明文。"""
    if not isinstance(text, str) or not text:
        return ""
    scrubbed = text
    for rule_name, pattern, _ in _SECRET_PATTERNS:
        scrubbed = pattern.sub(f"[REDACTED:{rule_name}]", scrubbed)
    for env_key, raw_val in _iter_env_secret_items(env, min_length=min_env_length):
        if raw_val in scrubbed:
            scrubbed = scrubbed.replace(raw_val, f"[REDACTED:env:{env_key}]")
        b64_std = base64.b64encode(raw_val.encode("utf-8")).decode("ascii").rstrip("=")
        if len(b64_std) >= 12 and b64_std in scrubbed:
            scrubbed = scrubbed.replace(b64_std, f"[REDACTED:env:{env_key}:base64]")
        b64_url = base64.urlsafe_b64encode(raw_val.encode("utf-8")).decode("ascii").rstrip("=")
        if len(b64_url) >= 12 and b64_url != b64_std and b64_url in scrubbed:
            scrubbed = scrubbed.replace(b64_url, f"[REDACTED:env:{env_key}:base64url]")
        url_q = quote(raw_val, safe="")
        if url_q != raw_val and url_q in scrubbed:
            scrubbed = scrubbed.replace(url_q, f"[REDACTED:env:{env_key}:urlencoded]")
    return scrubbed


# ---------------------------------------------------------------------------
# 3. 每轮次污点与密钥指纹登记表（TaintRegistry）
# ---------------------------------------------------------------------------


def infer_tool_provenance(tool_name: str) -> DataProvenance:
    """根据工具名称推断其返回数据的默认血缘信任域。"""
    if tool_name in {"web_fetch", "web_search"}:
        return DataProvenance.UNTRUSTED_WEB
    if tool_name.startswith("mcp__") or tool_name in {"mcp_call_tool", "mcp_read_resource"}:
        return DataProvenance.UNTRUSTED_MCP
    if tool_name in {"a2a_send_task", "a2a_discover_agent", "a2a_get_task"}:
        return DataProvenance.UNTRUSTED_A2A
    return DataProvenance.WORKSPACE_FILE


class TaintRegistry:
    """按运行实例隔离的轻量污点追踪与无明文密钥指纹登记表（线程安全）。

    职责：
    1. 记录当前 run 是否摄入过不可信外部数据（`UNTRUSTED_WEB` / `UNTRUSTED_MCP` / `UNTRUSTED_A2A`），
       保留 `source_tool`、`call_id`、`timestamp` 与已脱敏的 `summary`；
    2. 以“SHA-256 + 极短前后缀锚点”形式登记来自环境变量或敏感文件的密钥指纹，同时衍生 Base64 /
       Base64URL / URL 编码变体滑窗指纹，内存与快照中绝不保留原始密钥明文。
    """

    def __init__(self, run_id: str | None = None, *, max_events: int = 256) -> None:
        self.run_id: str | None = run_id
        self._max_events: int = max(16, int(max_events))
        self._lock = threading.RLock()
        self._events: list[TaintEvent] = []
        self._has_untrusted: bool = False
        self._has_sensitive: bool = False
        self._fingerprints: dict[str, SecretFingerprint] = {}
        self._matchers: dict[str, tuple[_WindowMatcher, ...]] = {}
        self._approved_hosts: set[str] = set()

    @property
    def has_untrusted_taint(self) -> bool:
        with self._lock:
            return self._has_untrusted

    @property
    def has_sensitive_taint(self) -> bool:
        with self._lock:
            return self._has_sensitive

    @property
    def events(self) -> tuple[TaintEvent, ...]:
        with self._lock:
            return tuple(self._events)

    @property
    def taint_sources(self) -> list[dict[str, Any]]:
        """返回所有不可信外部数据摄入源的已脱敏字典列表。"""
        with self._lock:
            return [ev.to_dict() for ev in self._events if ev.provenance.is_untrusted]

    @property
    def secret_fingerprints(self) -> tuple[SecretFingerprint, ...]:
        with self._lock:
            return tuple(self._fingerprints.values())

    def record_taint(
        self,
        provenance: DataProvenance | str,
        source_tool: str,
        *,
        call_id: str = "",
        summary: str = "",
        timestamp: float | None = None,
    ) -> TaintEvent:
        """记录一次数据血缘事件；摘要自动做密钥脱敏与截断，绝不落盘或打印密钥明文。"""
        prov = _normalize_provenance(provenance)
        ts = round(time.time() if timestamp is None else float(timestamp), 3)
        safe_summary = redact_secrets(str(summary or "").strip())[:240]
        event = TaintEvent(
            provenance=prov,
            source_tool=str(source_tool or "").strip() or "unknown",
            call_id=str(call_id or "").strip(),
            timestamp=ts,
            summary=safe_summary,
        )
        with self._lock:
            self._events.append(event)
            if len(self._events) > self._max_events:
                self._events = self._events[-self._max_events :]
            if prov.is_untrusted:
                self._has_untrusted = True
            if prov.is_sensitive:
                self._has_sensitive = True
        return event

    def record_tool_output(
        self,
        tool_name: str,
        *,
        call_id: str = "",
        summary: str = "",
        provenance: DataProvenance | str | None = None,
        timestamp: float | None = None,
    ) -> TaintEvent:
        """便捷入口：按工具名自动推断血缘并记录本轮数据摄入。"""
        eff_prov = infer_tool_provenance(tool_name) if provenance is None else _normalize_provenance(provenance)
        return self.record_taint(
            eff_prov,
            tool_name,
            call_id=call_id,
            summary=summary,
            timestamp=timestamp,
        )

    def register_secret(
        self,
        raw_secret: str,
        *,
        label: str = "secret",
        provenance: DataProvenance | str = DataProvenance.SENSITIVE_SECRET,
        min_length: int = 8,
    ) -> SecretFingerprint | None:
        """登记敏感密钥指纹（仅保留 SHA-256 与极短锚点，绝不存储原始密钥明文）。"""
        if not isinstance(raw_secret, str) or len(raw_secret) < max(4, int(min_length)):
            return None
        prov = _normalize_provenance(provenance)
        raw_sha = _sha256_hex(raw_secret)
        pfx_tok, sfx_tok = _make_safe_display_tokens(raw_secret)
        fp = SecretFingerprint(
            label=str(label or "secret").strip() or "secret",
            sha256=raw_sha,
            length=len(raw_secret),
            prefix_token=pfx_tok,
            suffix_token=sfx_tok,
            provenance=prov,
            registered_at=round(time.time(), 3),
        )

        # 构建原样、Base64、Base64URL 与 URL 编码变体的无明文滑窗指纹
        raw_bytes = raw_secret.encode("utf-8", errors="surrogatepass")
        b64_std = base64.b64encode(raw_bytes).decode("ascii").rstrip("=")
        b64_url = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")
        url_q = quote(raw_secret, safe="")
        url_qp = quote_plus(raw_secret)

        candidate_variants: list[tuple[str, str]] = [("raw", raw_secret)]
        seen_texts = {raw_secret}
        for variant_name, variant_val in (
            ("base64", b64_std),
            ("base64url", b64_url),
            ("urlencoded", url_q),
            ("urlencoded_plus", url_qp),
        ):
            if variant_val and variant_val not in seen_texts:
                seen_texts.add(variant_val)
                candidate_variants.append((variant_name, variant_val))

        matchers: list[_WindowMatcher] = []
        for variant_name, variant_val in candidate_variants:
            m = _build_window_matcher(variant_name, variant_val)
            if m is not None:
                matchers.append(m)

        with self._lock:
            self._fingerprints[raw_sha] = fp
            self._matchers[raw_sha] = tuple(matchers)
        return fp

    def register_env_secrets(
        self,
        env: Mapping[str, str] | None = None,
        *,
        min_length: int = 16,
    ) -> int:
        """扫描环境变量中命中的敏感密钥（长度 >= min_length）并仅登记其不可逆指纹。"""
        count = 0
        for key, val in _iter_env_secret_items(env, min_length=min_length):
            if self.register_secret(val, label=f"env:{key}", min_length=min_length) is not None:
                count += 1
        return count

    def match_registered_secrets(self, text: str) -> list[dict[str, str]]:
        """检查给定文本是否包含任何已登记密钥指纹（原样或编码变体），返回全脱敏命中列表。"""
        if not isinstance(text, str) or len(text) < 8:
            return []
        with self._lock:
            snapshot_items = list(self._fingerprints.items())
            matcher_map = dict(self._matchers)
        hits: list[dict[str, str]] = []
        for sha, fp in snapshot_items:
            for matcher in matcher_map.get(sha, ()):
                if _window_matcher_hits(text, matcher):
                    hits.append(
                        {
                            "rule": (
                                "registered_secret_fingerprint"
                                if matcher.variant == "raw"
                                else f"registered_secret_{matcher.variant}"
                            ),
                            "label": fp.label,
                            "variant": matcher.variant,
                            "sha256_prefix": fp.sha256[:12],
                            "redacted": f"[REDACTED:{fp.label}]",
                        }
                    )
                    break
        return hits

    def approve_host(self, host_pattern: str) -> None:
        """在当前运行内登记一个经用户显式放行的外发主机名或通配模式（供 CaMeL 严格模式参考）。"""
        clean = str(host_pattern or "").strip().lower()
        if clean.startswith("domain:"):
            clean = clean[len("domain:") :].strip()
        if clean:
            with self._lock:
                self._approved_hosts.add(clean)

    def is_host_approved(self, host: str) -> bool:
        clean = str(host or "").strip().lower()
        if not clean:
            return False
        with self._lock:
            patterns = tuple(self._approved_hosts)
        return _host_matches_any(clean, patterns)

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self._has_untrusted = False
            self._has_sensitive = False
            self._fingerprints.clear()
            self._matchers.clear()
            self._approved_hosts.clear()

    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "run_id": self.run_id,
                "has_untrusted_taint": self._has_untrusted,
                "has_sensitive_taint": self._has_sensitive,
                "taint_sources": [ev.to_dict() for ev in self._events if ev.provenance.is_untrusted],
                "events_count": len(self._events),
                "secret_fingerprints_count": len(self._fingerprints),
                "secret_fingerprints": [fp.to_dict() for fp in self._fingerprints.values()],
                "approved_hosts": sorted(self._approved_hosts),
            }

    snapshot = to_dict


# ---------------------------------------------------------------------------
# 4. 统一出站密文防泄漏与 CaMeL 严格数据流检查器（EgressDlpInspector）
# ---------------------------------------------------------------------------


def _host_matches_any(host: str, patterns: Sequence[str]) -> bool:
    h = host.strip().lower()
    if not h:
        return False
    for raw_pat in patterns:
        pat = str(raw_pat or "").strip().lower()
        if not pat:
            continue
        if pat.startswith("domain:"):
            pat = pat[len("domain:") :].strip()
        if pat == "*" or h == pat:
            return True
        if any(ch in pat for ch in ("*", "?", "[")) and fnmatch.fnmatchcase(h, pat):
            return True
    return False


def _is_bash_network_command(command: str) -> bool:
    """检测 `bash_exec` 命令字符串是否包含出站网络外发工具或 HTTP/Socket 调用。"""
    if not isinstance(command, str) or not command.strip():
        return False
    return any(pat.search(command) for pat in _BASH_NETWORK_SINK_PATTERNS)


def _is_outbound_sink_call(tool_name: str, arguments: Mapping[str, Any]) -> bool:
    """判断一次工具调用是否属于需要执行出站密文 DLP 扫描的外部汇聚点。"""
    if tool_name in OUTBOUND_SINK_TOOLS or tool_name.startswith("mcp__"):
        return True
    if tool_name == "bash_exec":
        return _is_bash_network_command(str(arguments.get("command", "")))
    return False


_SAFE_FIELD_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")


def _safe_field_segment(
    key_str: str,
    idx: int,
    *,
    env_items: Sequence[tuple[str, str]] = (),
    taint_registry: TaintRegistry | None = None,
) -> str:
    """将字典键安全格式化为审计字段路径段，防止把隐藏在键名中的原始密钥泄漏进 findings。"""
    if not _SAFE_FIELD_KEY_RE.match(key_str):
        return f".<key[{idx}]>"
    if any(pat.search(key_str) for _, pat, _ in _SECRET_PATTERNS):
        return f".<key[{idx}]>"
    if any(raw_val in key_str for _, raw_val in env_items):
        return f".<key[{idx}]>"
    if taint_registry is not None and taint_registry.match_registered_secrets(key_str):
        return f".<key[{idx}]>"
    return f".{key_str}"


def _iter_argument_strings(
    value: Any,
    *,
    prefix: str = "arguments",
    max_depth: int = 32,
    env_items: Sequence[tuple[str, str]] = (),
    taint_registry: TaintRegistry | None = None,
) -> list[tuple[str, str]]:
    """递归提取任意嵌套参数结构中的全部字符串键与值（防循环引用与过深嵌套，且字段路径不回显高危键明文）。"""
    collected: list[tuple[str, str]] = []
    seen_containers: set[int] = set()

    def _walk(obj: Any, path: str, depth: int) -> None:
        if depth > max_depth or obj is None:
            return
        if isinstance(obj, str):
            if obj:
                collected.append((path, obj))
            return
        if isinstance(obj, (bytes, bytearray)):
            decoded = bytes(obj).decode("utf-8", errors="ignore")
            if decoded:
                collected.append((path, decoded))
            return
        if isinstance(obj, Mapping):
            obj_id = id(obj)
            if obj_id in seen_containers:
                return
            seen_containers.add(obj_id)
            try:
                for idx, (k, v) in enumerate(obj.items()):
                    key_str = str(k)
                    seg = _safe_field_segment(
                        key_str,
                        idx,
                        env_items=env_items,
                        taint_registry=taint_registry,
                    )
                    if isinstance(k, str) and k:
                        collected.append((f"{path}.<key[{idx}]>", k))
                    _walk(v, f"{path}{seg}", depth + 1)
            finally:
                seen_containers.discard(obj_id)
            return
        if isinstance(obj, (list, tuple, set, frozenset)):
            obj_id = id(obj)
            if obj_id in seen_containers:
                return
            seen_containers.add(obj_id)
            try:
                for idx, item in enumerate(obj):
                    _walk(item, f"{path}[{idx}]", depth + 1)
            finally:
                seen_containers.discard(obj_id)

    _walk(value, prefix, 0)
    return collected


def _extract_decoded_views(raw_text: str, *, max_b64_tokens: int = 64) -> list[tuple[str, str]]:
    """为单个字符串构造检查视图：原始视图 + URL 解码视图（最多 2 层）+ Base64 片段解码视图。"""
    views: list[tuple[str, str]] = [("raw", raw_text)]
    seen_texts: set[str] = {raw_text}

    # 1. URL 百分号解码（支持 1~2 轮解套，拦截 %XX 编码与双重 URL 编码绕过）
    url_views: list[str] = [raw_text]
    cur = raw_text
    for round_idx in range(2):
        if not _PERCENT_ENCODED_RE.search(cur):
            break
        dec = unquote(cur)
        dec_plus = unquote_plus(cur)
        progressed = False
        for label_suffix, candidate in (("urlencoded", dec), ("urlencoded_plus", dec_plus)):
            if candidate and candidate not in seen_texts:
                seen_texts.add(candidate)
                tag = label_suffix if round_idx == 0 else f"{label_suffix}_nested"
                views.append((tag, candidate))
                url_views.append(candidate)
                progressed = True
        if not progressed:
            break
        cur = dec

    # 2. Base64 / Base64URL 候选片段解码（对原始视图与 URL 解码视图均执行，拦截 URL+Base64 叠加绕过）
    decoded_count = 0
    for src in url_views:
        if decoded_count >= max_b64_tokens:
            break
        for m in _BASE64_CANDIDATE_RE.finditer(src):
            if decoded_count >= max_b64_tokens:
                break
            token = m.group(0)
            if len(token) > 65536 or len(token) % 4 == 1:
                continue
            padded = token + ("=" * ((4 - len(token) % 4) % 4))
            decoded_bytes: bytes | None = None
            try:
                if "-" in padded or "_" in padded:
                    decoded_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
                else:
                    decoded_bytes = base64.b64decode(padded.encode("ascii"), validate=True)
            except Exception:
                try:
                    decoded_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
                except Exception:
                    decoded_bytes = None
            if not decoded_bytes:
                continue
            decoded_str = decoded_bytes.decode("utf-8", errors="ignore")
            if len(decoded_str) >= 12 and decoded_str not in seen_texts:
                seen_texts.add(decoded_str)
                views.append(("base64_decoded", decoded_str))
                decoded_count += 1

    return views


def _scan_text_for_secret_patterns(
    text: str,
    *,
    field_path: str,
    view: str,
) -> list[dict[str, str]]:
    """对单个文本视图执行全部已知密文正则扫描；结果完全脱敏，不含任何密钥子串。"""
    findings: list[dict[str, str]] = []
    bearer_jwt_spans: list[tuple[int, int]] = []

    for rule_name, pattern, label in _SECRET_PATTERNS:
        for m in pattern.finditer(text):
            span = (m.start(), m.end())
            if rule_name == "bearer_jwt":
                bearer_jwt_spans.append(span)
            elif rule_name in _SUBSUMED_BY_BEARER_JWT and any(
                bs <= span[0] and span[1] <= be for bs, be in bearer_jwt_spans
            ):
                continue
            findings.append(
                {
                    "rule": rule_name if view == "raw" else f"{rule_name}_{view}",
                    "base_rule": rule_name,
                    "label": label,
                    "field": field_path,
                    "view": view,
                    "redacted": f"[REDACTED:{rule_name}]",
                }
            )
            # 同一字段同一视图同一规则记录一次即可，避免海量重复告警
            break
    return findings


def _scan_text_for_active_env_secrets(
    text: str,
    *,
    field_path: str,
    view: str,
    env_items: Sequence[tuple[str, str]],
) -> list[dict[str, str]]:
    """检查文本是否包含活跃环境变量密钥（原样 / Base64 / Base64URL / URL 编码），绝不暴露密钥值。"""
    if not env_items or not text:
        return []
    findings: list[dict[str, str]] = []
    for env_key, raw_val in env_items:
        matched_encoding: str | None = None
        if raw_val in text:
            matched_encoding = "raw" if view == "raw" else view
        else:
            raw_bytes = raw_val.encode("utf-8", errors="surrogatepass")
            b64_std = base64.b64encode(raw_bytes).decode("ascii").rstrip("=")
            if len(b64_std) >= 12 and b64_std in text:
                matched_encoding = "base64"
            else:
                b64_url = base64.urlsafe_b64encode(raw_bytes).decode("ascii").rstrip("=")
                if len(b64_url) >= 12 and b64_url in text:
                    matched_encoding = "base64url"
                else:
                    url_q = quote(raw_val, safe="")
                    url_qp = quote_plus(raw_val)
                    if (url_q != raw_val and url_q in text) or (url_qp != raw_val and url_qp in text):
                        matched_encoding = "urlencoded"
        if matched_encoding is not None:
            rule_name = (
                "active_env_secret"
                if matched_encoding == "raw"
                else f"active_env_secret_{matched_encoding}"
            )
            findings.append(
                {
                    "rule": rule_name,
                    "base_rule": "active_env_secret",
                    "label": f"活跃环境变量敏感凭据 ({env_key})",
                    "env_key": env_key,
                    "field": field_path,
                    "view": view,
                    "encoding": matched_encoding,
                    "redacted": f"[REDACTED:env:{env_key}]",
                }
            )
    return findings


def _extract_hosts_from_text(text: str) -> list[str]:
    hosts: list[str] = []
    for m in _URL_HOST_EXTRACT_RE.finditer(text):
        try:
            host = (urlparse(m.group(0)).hostname or "").strip().lower()
        except ValueError:
            host = ""
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def _is_target_approved_in_camel(
    tool_name: str,
    arguments: Mapping[str, Any],
    *,
    approved_hosts: Sequence[str],
    taint_registry: TaintRegistry | None,
) -> bool:
    """判断纯外发只读请求（如 web_fetch / a2a_discover_agent）的目标主机是否已在显式批准白名单内。"""

    def _host_ok(host: str) -> bool:
        if not host:
            return False
        if _host_matches_any(host, approved_hosts):
            return True
        if taint_registry is not None and taint_registry.is_host_approved(host):
            return True
        return False

    if tool_name == "web_fetch":
        raw_url = str(arguments.get("url", "")).strip()
        if not raw_url:
            return False
        try:
            host = (urlparse(raw_url).hostname or "").strip().lower()
        except ValueError:
            return False
        return _host_ok(host)

    if tool_name in {"a2a_send_task", "a2a_discover_agent"}:
        peer = ""
        for key in ("peer", "peer_name", "agent", "url", "agent_url"):
            val = arguments.get(key)
            if isinstance(val, str) and val.strip():
                peer = val.strip()
                break
        if not peer:
            return False
        if "://" in peer:
            try:
                host = (urlparse(peer).hostname or "").strip().lower()
            except ValueError:
                host = ""
            return _host_ok(host) or _host_ok(peer)
        return _host_ok(peer)

    return False


class EgressDlpInspector:
    """统一出站密文防泄漏（Outbound Egress DLP）与 CaMeL 严格数据流检查器。"""

    def __init__(
        self,
        *,
        min_env_secret_length: int = 16,
        approved_hosts: Sequence[str] = (),
    ) -> None:
        self.min_env_secret_length: int = max(8, int(min_env_secret_length))
        self.default_approved_hosts: tuple[str, ...] = tuple(
            str(h).strip().lower() for h in approved_hosts if isinstance(h, str) and h.strip()
        )

    def inspect_outbound_call(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None,
        *,
        taint_registry: TaintRegistry | None = None,
        strict_dataflow: bool = False,
        approved_hosts: Sequence[str] | None = None,
        explicitly_approved: bool = False,
        env: Mapping[str, str] | None = None,
    ) -> dict[str, Any]:
        """检查单次工具调用是否存在出站密文泄漏或 CaMeL 污点控制流风险。

        返回结构：
        - 安全放行：`{"allowed": True, "findings": []}`
        - 命中密文防泄漏（硬阻断）：
          `{"allowed": False, "decision": "deny", "code": "egress_dlp_blocked", "reason": ..., "matched_rules": [...], "findings": [...]}`
        - 命中 CaMeL 严格数据流门控（人工确认）：
          `{"allowed": False, "decision": "ask", "code": "tainted_control_flow", "reason": ..., "taint_sources": [...], "findings": []}`
        """
        args: dict[str, Any] = dict(arguments) if isinstance(arguments, Mapping) else {}
        is_sink = _is_outbound_sink_call(tool_name, args)

        # 第一道防线（最高优先级，永远硬拒绝）：出站密文 DLP 扫描
        if is_sink:
            env_items = _iter_env_secret_items(env, min_length=self.min_env_secret_length)
            all_findings: list[dict[str, str]] = []
            seen_finding_keys: set[tuple[str, str, str]] = set()

            for field_path, raw_text in _iter_argument_strings(
                args, env_items=env_items, taint_registry=taint_registry
            ):
                views = _extract_decoded_views(raw_text)
                for view_name, view_text in views:
                    for item in _scan_text_for_secret_patterns(
                        view_text, field_path=field_path, view=view_name
                    ):
                        dedup_key = (item["rule"], item["field"], item["redacted"])
                        if dedup_key not in seen_finding_keys:
                            seen_finding_keys.add(dedup_key)
                            all_findings.append(item)

                    for item in _scan_text_for_active_env_secrets(
                        view_text,
                        field_path=field_path,
                        view=view_name,
                        env_items=env_items,
                    ):
                        dedup_key = (item["rule"], item["field"], item["redacted"])
                        if dedup_key not in seen_finding_keys:
                            seen_finding_keys.add(dedup_key)
                            all_findings.append(item)

                    if taint_registry is not None:
                        for hit in taint_registry.match_registered_secrets(view_text):
                            item = {
                                "rule": hit["rule"],
                                "base_rule": "registered_secret_fingerprint",
                                "label": f"已登记敏感凭据 ({hit['label']})",
                                "field": field_path,
                                "view": view_name,
                                "sha256_prefix": hit["sha256_prefix"],
                                "redacted": hit["redacted"],
                            }
                            dedup_key = (item["rule"], item["field"], item["redacted"])
                            if dedup_key not in seen_finding_keys:
                                seen_finding_keys.add(dedup_key)
                                all_findings.append(item)

            if all_findings:
                matched_rules: list[str] = []
                for f in all_findings:
                    r = f["rule"]
                    if r not in matched_rules:
                        matched_rules.append(r)
                rules_preview = ", ".join(matched_rules[:6])
                return {
                    "allowed": False,
                    "decision": "deny",
                    "code": "egress_dlp_blocked",
                    "reason": (
                        f"出站密文防泄漏（Egress DLP）拦截 {tool_name}：检测到疑似敏感凭据外发"
                        f"（命中规则：{rules_preview}；所有密文片段已脱敏，禁止外传）"
                    ),
                    "matched_rules": matched_rules,
                    "findings": all_findings,
                }

        # 第二道防线：CaMeL 严格数据流控制流门控（strict_dataflow=True）
        if (
            strict_dataflow
            and taint_registry is not None
            and taint_registry.has_untrusted_taint
            and not explicitly_approved
        ):
            merged_approved: list[str] = list(self.default_approved_hosts)
            if approved_hosts:
                merged_approved.extend(str(h).strip().lower() for h in approved_hosts if str(h).strip())

            needs_camel_ask = False
            camel_detail = ""

            if tool_name == "bash_exec":
                cmd = str(args.get("command", ""))
                bash_cls = classify_bash_command(cmd)["classification"]
                if is_sink:
                    hosts = _extract_hosts_from_text(cmd)
                    host_note = f"（目标主机：{', '.join(hosts[:3])}）" if hosts else ""
                    needs_camel_ask = True
                    camel_detail = f"向外部网络发送数据{host_note}"
                elif bash_cls != "safe_readonly":
                    needs_camel_ask = True
                    camel_detail = "执行可能修改系统或工作区状态的变更型终端命令"
            elif tool_name in _CAMEL_NETWORK_SINK_TOOLS or tool_name.startswith("mcp__"):
                if not _is_target_approved_in_camel(
                    tool_name,
                    args,
                    approved_hosts=merged_approved,
                    taint_registry=taint_registry,
                ):
                    needs_camel_ask = True
                    camel_detail = f"经由 {tool_name} 向未经显式批准的外部目标发送数据"
            elif tool_name in _CAMEL_MUTATING_TOOLS:
                needs_camel_ask = True
                camel_detail = f"执行未经显式批准的变更型工具 {tool_name}"

            if needs_camel_ask:
                return {
                    "allowed": False,
                    "decision": "ask",
                    "code": "tainted_control_flow",
                    "reason": (
                        f"CaMeL 严格数据流保护：当前会话已摄入不可信外部数据，"
                        f"{camel_detail}前需要人工显式确认"
                    ),
                    "taint_sources": taint_registry.taint_sources,
                    "findings": [],
                }

        return {"allowed": True, "findings": []}


DEFAULT_EGRESS_DLP_INSPECTOR = EgressDlpInspector()


def inspect_outbound_call(
    tool_name: str,
    arguments: dict[str, Any] | None,
    *,
    taint_registry: TaintRegistry | None = None,
    strict_dataflow: bool = False,
    approved_hosts: Sequence[str] | None = None,
    explicitly_approved: bool = False,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """模块级快捷入口，委托默认 `EgressDlpInspector` 执行出站密文 DLP 与 CaMeL 污点检查。"""
    return DEFAULT_EGRESS_DLP_INSPECTOR.inspect_outbound_call(
        tool_name,
        arguments,
        taint_registry=taint_registry,
        strict_dataflow=strict_dataflow,
        approved_hosts=approved_hosts,
        explicitly_approved=explicitly_approved,
        env=env,
    )


# ---------------------------------------------------------------------------
# 5. 交互式协作与规划模式工具规范（INTERACTIVE_TOOL_SPECS）
# ---------------------------------------------------------------------------

INTERACTIVE_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "ask_user_question",
        "description": (
            "当需求存在歧义、存在多种架构权衡或需要用户确认关键参数时，向用户提出结构化澄清问题"
            "（支持提供可选项列表与推荐默认项）；在 plan / ask / auto-edit / full-auto 模式下均可安全调用。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 2000,
                    "description": "向用户提出的清晰、具体的澄清问题。",
                },
                "options": {
                    "type": "array",
                    "maxItems": 8,
                    "items": {"type": "string", "minLength": 1, "maxLength": 200},
                    "description": "可选：供用户快速选择的候选方案列表。",
                },
                "multi_select": {
                    "type": "boolean",
                    "description": "是否允许用户多选（默认 false）。",
                },
                "context": {
                    "type": "string",
                    "maxLength": 2000,
                    "description": "可选：补充背景说明或各候选方案的核心权衡。",
                },
                "default_option": {
                    "type": "string",
                    "maxLength": 200,
                    "description": "可选：推荐的默认选项文本。",
                },
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "enter_plan_mode",
        "description": (
            "将当前任务切换至严格只读的规划模式（permission_mode='plan'）：允许只读代码探索、静态只读检查与向 "
            "`.agent-lab/plans/` 撰写实施计划文档，同时硬阻断一切源码修改与变更型终端命令。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "maxLength": 1000,
                    "description": "可选：说明为何进入只读规划模式（如跨多文件重构或方案设计）。",
                },
                "plan_file": {
                    "type": "string",
                    "maxLength": 240,
                    "description": "可选：计划产物相对路径（应位于 `.agent-lab/plans/` 目录下，如 `.agent-lab/plans/design.md`）。",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "exit_plan_mode",
        "description": (
            "结束只读规划模式并向用户呈现完整实施计划以请求确认；经用户批准后切换回可执行编辑的权限档位。"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 4000,
                    "description": "可选：实施计划摘要，涵盖改动文件清单、核心设计与验证策略。",
                },
                "plan_file": {
                    "type": "string",
                    "maxLength": 240,
                    "description": "可选：已写入 `.agent-lab/plans/` 目录下的计划 Markdown 文件相对路径。",
                },
                "target_permission_mode": {
                    "type": "string",
                    "enum": ["ask", "auto-edit", "full-auto"],
                    "description": "可选：计划获批后建议切换的目标权限模式（默认回退进入前的档位或 auto-edit）。",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
]

__all__ = [
    "DEFAULT_EGRESS_DLP_INSPECTOR",
    "DataProvenance",
    "EgressDlpInspector",
    "INTERACTIVE_TOOL_SPECS",
    "OUTBOUND_SINK_TOOLS",
    "SENSITIVE_SECRET",
    "SecretFingerprint",
    "TRUSTED_USER",
    "TaintEvent",
    "TaintRegistry",
    "UNTRUSTED_A2A",
    "UNTRUSTED_MCP",
    "UNTRUSTED_PROVENANCES",
    "UNTRUSTED_WEB",
    "WORKSPACE_FILE",
    "infer_tool_provenance",
    "inspect_outbound_call",
    "redact_secrets",
]
