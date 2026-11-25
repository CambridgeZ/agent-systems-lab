"""声明式多协议模型网关配置管理器（Declarative Multi-Protocol Model Gateway Config Manager）。

================================================================================
模块导读（Module Guide）
================================================================================
1. 模块角色（Role）
   本模块为 Agent Systems Lab 提供统一、声明式且默认安全的模型网关（`gateways.json`）管理层，
   屏蔽多厂商底层线协议差异（`anthropic_messages`、`openai_chat`、`openai_responses`、
   `gemini_generate_content` 与离线 `demo`），支持多 Profile 切换、环境变量插值、原子落盘与
   面向 CLI / FastAPI / Web UI 的零泄露脱敏视图。

2. 核心安全不变量（Core Security Invariants）
   - 信任分层发现（Trusted-Only Workspace Discovery）：仅当调用方显式确认 `trust_workspace=True`
     时，才会读取 `<workspace>/.agent-lab/gateways.json`，阻断克隆不可信代码仓时通过项目配置
     劫持 `base_url` 窃取宿主 API Key；多租户或隔离 `state_dir` 下默认不回退至宿主机
     `~/.agent-lab/gateways.json`，防止宿主机个人凭证跨租户泄露。
   - 防符号链接/硬链接与 TOCTOU（`O_NOFOLLOW` + `st_nlink == 1`）：所有读取与落盘均拒绝符号链接、
     目录/FIFO 特殊文件与多重硬链接文件，写路径采用同目录 `mkstemp + fsync + chmod(0o600) + os.replace`
     四步原子协议，保证内联密钥（如用户显式保存）权限最小化且永不产生半截文件。
   - 网关 SSRF 与云元数据阻断（`validate_gateway_url`）：仅允许 `http://` / `https://`，强制拒绝
     URL 内嵌凭据（`user:pass@host`）、查询串/片段及云元数据 IMDS（`169.254.169.254`、link-local、
     `fd00:ec2::254`、`100.100.100.200`、`metadata.google.internal` 及十进制/十六进制/八进制混淆 IP）；
     未显式开启 `allow_custom_base_url=True` 或 `AGENT_ALLOW_CUSTOM_BASE_URL=1` 时，仅放行对应协议的
     官方 HTTPS 端点。
   - 零密钥回显（`redact_gateways_document` & `gateway_info`）：所有面向 API / Web / CLI 的配置视图
     强制将内联 `api_key` 与敏感请求头（`Authorization`、`x-api-key`、`x-goog-api-key` 及匹配
     `key|token|secret` 的键）替换为 `***REDACTED***`，并计算 `credential_configured` 状态供界面展示。
================================================================================
"""

from __future__ import annotations

from collections.abc import Mapping
import contextlib
from copy import deepcopy
from dataclasses import dataclass, field
import errno
import inspect
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import sys
import tempfile
import threading
from typing import TYPE_CHECKING, Any
import urllib.parse

if TYPE_CHECKING:
    from .providers import Provider

GATEWAYS_SCHEMA_VERSION = 1
MAX_GATEWAY_PROFILES = 32
MAX_FALLBACK_BASE_URLS = 8
MAX_GATEWAY_HEADERS = 32
DEFAULT_GATEWAY_TIMEOUT_SECONDS = 120.0
MIN_GATEWAY_TIMEOUT_SECONDS = 0.5
MAX_GATEWAY_TIMEOUT_SECONDS = 3600.0
_MAX_GATEWAYS_FILE_BYTES = 256 * 1024  # 256 KiB 硬上限，与 settings.json 保持一致

REDACTED_SECRET_MARKER = "***REDACTED***"

CANONICAL_PROTOCOLS: tuple[str, ...] = (
    "anthropic_messages",
    "openai_chat",
    "openai_responses",
    "gemini_generate_content",
    "demo",
)

PROTOCOL_ALIASES: dict[str, str] = {
    "anthropic": "anthropic_messages",
    "anthropic_messages": "anthropic_messages",
    "anthropic-messages": "anthropic_messages",
    "claude": "anthropic_messages",
    "messages": "anthropic_messages",
    "openai": "openai_chat",
    "openai_compat": "openai_chat",
    "openai-compat": "openai_chat",
    "openai_chat": "openai_chat",
    "openai-chat": "openai_chat",
    "chat_completions": "openai_chat",
    "chat-completions": "openai_chat",
    "responses": "openai_responses",
    "openai_responses": "openai_responses",
    "openai-responses": "openai_responses",
    "gemini": "gemini_generate_content",
    "google_gemini": "gemini_generate_content",
    "google-gemini": "gemini_generate_content",
    "gemini_native": "gemini_generate_content",
    "gemini-native": "gemini_generate_content",
    "gemini_generate_content": "gemini_generate_content",
    "gemini-generate-content": "gemini_generate_content",
    "google": "gemini_generate_content",
    "demo": "demo",
    "offline_demo": "demo",
    "offline-demo": "demo",
}

DEFAULT_BASE_URLS: dict[str, str] = {
    "anthropic_messages": "https://api.anthropic.com",
    "openai_chat": "https://api.openai.com/v1",
    "openai_responses": "https://api.openai.com/v1",
    "gemini_generate_content": "https://generativelanguage.googleapis.com/v1beta",
    "demo": "",
}

OFFICIAL_PROTOCOL_HOSTS: dict[str, frozenset[str]] = {
    "anthropic_messages": frozenset({"api.anthropic.com"}),
    "openai_chat": frozenset({"api.openai.com"}),
    "openai_responses": frozenset({"api.openai.com"}),
    "gemini_generate_content": frozenset({"generativelanguage.googleapis.com"}),
    "demo": frozenset(),
}

DEFAULT_API_KEY_ENVS: dict[str, str] = {
    "anthropic_messages": "ANTHROPIC_API_KEY",
    "openai_chat": "OPENAI_API_KEY",
    "openai_responses": "OPENAI_API_KEY",
    "gemini_generate_content": "GEMINI_API_KEY",
    "demo": "",
}

DEFAULT_MODEL_ENVS: dict[str, str] = {
    "anthropic_messages": "ANTHROPIC_MODEL",
    "openai_chat": "OPENAI_MODEL",
    "openai_responses": "OPENAI_MODEL",
    "gemini_generate_content": "GEMINI_MODEL",
    "demo": "",
}

PROTOCOL_PROVIDER_CLASSES: dict[str, str] = {
    "anthropic_messages": "AnthropicProvider",
    "openai_chat": "OpenAICompatibleProvider",
    "openai_responses": "OpenAIResponsesProvider",
    "gemini_generate_content": "GeminiNativeProvider",
    "demo": "DemoProvider",
}

_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_ENV_INTERP_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$")
_UNSAFE_CTL_RE = re.compile(r"[\r\n\x00]")
_URL_UNSAFE_RE = re.compile(r"[\r\n\x00\t ]")
_SENSITIVE_HEADER_NAMES = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "x-goog-api-key",
        "api-key",
        "cookie",
        "set-cookie",
    }
)
_SENSITIVE_HEADER_KEY_RE = re.compile(
    r"(?:key|token|secret|authorization|password|credential)", re.IGNORECASE
)
_BEARER_VALUE_RE = re.compile(r"(?i)\b(Bearer\s+)[^\s,;\"']+")
_VALID_REASONING_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max"})

_BLOCKED_METADATA_HOSTS = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "metadata",
        "instance-data",
        "metadata.azure.internal",
        "metadata.aws.internal",
    }
)
_AWS_IPV6_IMDS_NET = ipaddress.ip_network("fd00:ec2::254/128")
_ALIBABA_IMDS_IP = ipaddress.IPv4Address("100.100.100.200")
_NAT64_WELL_KNOWN_NET = ipaddress.ip_network("64:ff9b::/96")

_DARWIN_SYSTEM_SYMLINKS = frozenset({"/tmp", "/var", "/etc"})
_LEGACY_ENV_LOCK = threading.Lock()


@dataclass
class GatewayProfile:
    """单个声明式模型网关 Profile 的规范化内存表示。"""

    name: str
    protocol: str
    base_url: str = ""
    api_key_env: str = ""
    api_key: str | None = None
    model: str = ""
    fast_model: str | None = None
    reasoning_model: str | None = None
    reasoning_effort: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    fallback_base_urls: list[str] = field(default_factory=list)
    timeout_seconds: float = DEFAULT_GATEWAY_TIMEOUT_SECONDS
    context_window_tokens: int | None = None
    allow_custom_base_url: bool = False
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        """导出为可原子落盘的规范 JSON 字典（未设置内联 api_key 时不输出该字段）。"""
        payload: dict[str, Any] = {
            "name": self.name,
            "protocol": self.protocol,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
            "model": self.model,
            "fast_model": self.fast_model,
            "reasoning_model": self.reasoning_model,
            "reasoning_effort": self.reasoning_effort,
            "headers": dict(self.headers),
            "fallback_base_urls": list(self.fallback_base_urls),
            "timeout_seconds": float(self.timeout_seconds),
            "context_window_tokens": self.context_window_tokens,
            "allow_custom_base_url": bool(self.allow_custom_base_url),
            "description": self.description,
        }
        if self.api_key:
            payload["api_key"] = self.api_key
        return payload

    @classmethod
    def from_dict(cls, name: str, data: Mapping[str, Any]) -> GatewayProfile:
        """从原始字典校验并构造规范化的 `GatewayProfile`。"""
        return _validate_profile(name, data)


def normalize_protocol(raw: Any) -> str:
    """将友好协议别名（如 `anthropic` / `openai` / `responses` / `gemini`）归一化为规范协议名。"""
    if not isinstance(raw, str):
        raise ValueError(f"protocol 必须为字符串，收到: {type(raw).__name__}")
    key = raw.strip().lower()
    if not key:
        raise ValueError("protocol 不能为空")
    canonical = PROTOCOL_ALIASES.get(key)
    if canonical is None:
        supported = ", ".join(CANONICAL_PROTOCOLS)
        raise ValueError(f"未知的 gateway 协议 {raw!r}（仅支持: {supported}）")
    return canonical


def _parse_ip_literal(host: str) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address | None, bool]:
    """解析主机名是否为 IP 字面量（含检测非标准整数/八进制/十六进制 IPv4 混淆形式）。

    返回 `(ip_obj, is_obfuscated_ipv4)`。不触发任何 DNS 解析，纯离线确定性校验。
    """
    if "%" in host:
        # 拒绝携带 IPv6 Zone ID（如 fe80::1%eth0）的主机名
        raise ValueError(f"拒绝包含 IPv6 Zone ID 的网关主机: {host!r}")
    try:
        return ipaddress.ip_address(host), False
    except ValueError:
        pass
    # 检测单整数（如 2852039166）、十六进制（0xa9fea9fe）或短点分（169.254.43518）混淆 IPv4
    if host and all(ch in "0123456789abcdefx." for ch in host):
        try:
            packed = socket.inet_aton(host)
            return ipaddress.IPv4Address(packed), True
        except OSError:
            pass
    return None, False


def _check_blocked_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """判定 IP 是否指向云元数据 IMDS、link-local、未指定或组播地址（含 IPv6 嵌入解包）。"""
    if isinstance(ip_obj, ipaddress.IPv6Address):
        if ip_obj.ipv4_mapped is not None:
            return _check_blocked_ip(ip_obj.ipv4_mapped)
        if ip_obj.sixtofour is not None:
            reason = _check_blocked_ip(ip_obj.sixtofour)
            if reason is not None:
                return f"6to4 {reason}"
        if ip_obj.teredo is not None:
            server_ip, client_ip = ip_obj.teredo
            for embedded in (server_ip, client_ip):
                reason = _check_blocked_ip(embedded)
                if reason is not None:
                    return f"teredo {reason}"
        if ip_obj in _NAT64_WELL_KNOWN_NET:
            embedded_v4 = ipaddress.IPv4Address(int(ip_obj) & 0xFFFFFFFF)
            reason = _check_blocked_ip(embedded_v4)
            if reason is not None:
                return f"nat64 {reason}"
        if ip_obj in _AWS_IPV6_IMDS_NET:
            return "AWS IPv6 IMDS (fd00:ec2::254)"
    if isinstance(ip_obj, ipaddress.IPv4Address) and ip_obj == _ALIBABA_IMDS_IP:
        return "Alibaba Cloud IMDS (100.100.100.200)"
    if ip_obj.is_link_local:
        return "link-local / cloud metadata IMDS address"
    if ip_obj.is_unspecified:
        return "unspecified address"
    if ip_obj.is_multicast:
        return "multicast address"
    return None


def _is_loopback_host(host: str, ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address | None) -> bool:
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".localhost"):
        return True
    if ip_obj is not None:
        if isinstance(ip_obj, ipaddress.IPv6Address) and ip_obj.ipv4_mapped is not None:
            return ip_obj.ipv4_mapped.is_loopback
        return ip_obj.is_loopback
    return False


def _is_loopback_url(url: str) -> bool:
    """判断给定 URL 是否指向本机回环地址（用于 Ollama / 本地 vLLM 凭证就绪判定）。"""
    if not isinstance(url, str) or not url.strip():
        return False
    try:
        parsed = urllib.parse.urlsplit(url.strip())
        host = (parsed.hostname or "").strip().lower().rstrip(".")
        if not host:
            return False
        ip_obj, _ = _parse_ip_literal(host)
        return _is_loopback_host(host, ip_obj)
    except ValueError:
        return False


def validate_gateway_url(
    url: str,
    *,
    protocol: str,
    allow_custom_base_url: bool = False,
) -> str:
    """严格校验模型网关 `base_url`：阻断非 http(s)、内嵌凭据、IMDS 元数据与未授权自定义端点。

    安全规则：
    1. 仅允许 `http://` 与 `https://`；拒绝 URL 内嵌凭据（`user:pass@host`）、query 与 fragment；
    2. 无条件拒绝云元数据 IMDS（`169.254.169.254`、link-local、`fd00:ec2::254`、`100.100.100.200`、
       `metadata.google.internal` 等）及混淆 IPv4 编码；
    3. 当 `allow_custom_base_url=False` 且环境变量 `AGENT_ALLOW_CUSTOM_BASE_URL != "1"` 时，
       仅放行该协议的官方 HTTPS 端点；显式开启 `allow_custom_base_url=True` 或
       `AGENT_ALLOW_CUSTOM_BASE_URL=1` 后，允许自定义网关与本机回环地址（`127.0.0.1`/`localhost`/`::1`）。
    """
    norm_protocol = normalize_protocol(protocol)
    if norm_protocol == "demo":
        if url is None or (isinstance(url, str) and not url.strip()):
            return ""
    if not isinstance(url, str) or not url.strip():
        raise ValueError(f"协议 {norm_protocol!r} 的 base_url 不能为空")
    cleaned = url.strip()
    if _URL_UNSAFE_RE.search(cleaned):
        raise ValueError("base_url 包含非法控制字符或空白字符")

    parsed = urllib.parse.urlsplit(cleaned)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"base_url 仅支持 http:// 或 https://，拒绝协议: {scheme or '(empty)'}")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise ValueError("base_url 禁止包含内嵌用户名或密码凭据（user:pass@host）")
    if parsed.query or parsed.fragment:
        raise ValueError("base_url 不允许包含查询参数（?query）或片段（#fragment）")

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"base_url 端口无效: {exc}") from exc
    if port is not None and not (1 <= port <= 65535):
        raise ValueError(f"base_url 端口超出有效范围: {port}")

    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("base_url 缺少有效主机名")

    if host in _BLOCKED_METADATA_HOSTS or host.endswith(".metadata.google.internal"):
        raise ValueError(f"安全防护拒绝访问云元数据主机: {host}")

    ip_obj, is_obfuscated_v4 = _parse_ip_literal(host)
    if is_obfuscated_v4:
        raise ValueError(f"安全防护拒绝非标准混淆 IP 地址: {host}")
    if ip_obj is not None:
        blocked_reason = _check_blocked_ip(ip_obj)
        if blocked_reason is not None:
            raise ValueError(f"安全防护拒绝访问受限地址（{blocked_reason}）: {host}")

    env_allow_custom = os.environ.get("AGENT_ALLOW_CUSTOM_BASE_URL", "").strip() == "1"
    effective_allow_custom = bool(allow_custom_base_url) or env_allow_custom
    official_hosts = OFFICIAL_PROTOCOL_HOSTS.get(norm_protocol, frozenset())

    if host in official_hosts:
        if scheme != "https":
            raise ValueError(f"官方端点 {host} 必须使用 https://，拒绝明文 http://")
        return cleaned.rstrip("/")

    is_loopback = _is_loopback_host(host, ip_obj)
    if not effective_allow_custom:
        if is_loopback:
            raise ValueError(
                f"本地回环网关地址 {host!r} 需显式设置 allow_custom_base_url=True 或 AGENT_ALLOW_CUSTOM_BASE_URL=1"
            )
        allowed_desc = ", ".join(sorted(official_hosts)) or "(none)"
        raise ValueError(
            f"未开启 allow_custom_base_url，协议 {norm_protocol!r} 仅允许官方端点 [{allowed_desc}]，"
            f"拒绝主机: {host!r}"
        )

    return cleaned.rstrip("/")


def _validate_profile_name(name: Any) -> str:
    if not isinstance(name, str) or not _PROFILE_NAME_RE.fullmatch(name) or name in {".", ".."}:
        raise ValueError(
            f"非法 gateway profile 名称 {name!r}（仅允许 1~64 位 [A-Za-z0-9_.-] 且不能为 . 或 ..）"
        )
    return name


def _validate_optional_str(value: Any, *, field_name: str, max_len: int = 256) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} 必须为字符串或 null")
    if _UNSAFE_CTL_RE.search(value):
        raise ValueError(f"{field_name} 包含非法控制字符")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > max_len:
        raise ValueError(f"{field_name} 长度超过上限 {max_len}")
    return cleaned


def _validate_headers(raw_headers: Any) -> dict[str, str]:
    if raw_headers is None:
        return {}
    if not isinstance(raw_headers, Mapping):
        raise ValueError("headers 必须为 JSON 对象（dict）")
    if len(raw_headers) > MAX_GATEWAY_HEADERS:
        raise ValueError(f"headers 数量超过上限 {MAX_GATEWAY_HEADERS}")
    validated: dict[str, str] = {}
    for raw_k, raw_v in raw_headers.items():
        if not isinstance(raw_k, str) or not isinstance(raw_v, str):
            raise ValueError("headers 的键与值均必须为字符串")
        key = raw_k.strip()
        if not _HEADER_NAME_RE.fullmatch(key):
            raise ValueError(f"非法 HTTP 请求头名称: {raw_k!r}")
        if _UNSAFE_CTL_RE.search(raw_v):
            raise ValueError(f"HTTP 请求头 {key!r} 的值包含非法换行或空字节")
        if len(raw_v) > 2048:
            raise ValueError(f"HTTP 请求头 {key!r} 的值超过 2048 字符上限")
        validated[key] = raw_v
    return validated


def _validate_profile(name: str, raw: Any) -> GatewayProfile:
    valid_name = _validate_profile_name(name)
    if isinstance(raw, GatewayProfile):
        raw_dict: Mapping[str, Any] = raw.to_dict()
    elif isinstance(raw, Mapping):
        raw_dict = raw
    else:
        raise ValueError(f"profile {valid_name!r} 必须为 JSON 对象")

    declared_name = raw_dict.get("name")
    if declared_name is not None and str(declared_name).strip():
        if str(declared_name).strip() != valid_name:
            raise ValueError(
                f"profile 键名 {valid_name!r} 与内部 name 字段 {declared_name!r} 不一致"
            )

    protocol = normalize_protocol(raw_dict.get("protocol"))

    raw_allow_custom = raw_dict.get("allow_custom_base_url", False)
    if not isinstance(raw_allow_custom, bool):
        raise ValueError(f"profile {valid_name!r} 的 allow_custom_base_url 必须为布尔值")
    allow_custom = raw_allow_custom

    raw_base_url = raw_dict.get("base_url")
    if raw_base_url is None or (isinstance(raw_base_url, str) and not raw_base_url.strip()):
        base_url = DEFAULT_BASE_URLS[protocol]
    elif isinstance(raw_base_url, str):
        base_url = raw_base_url.strip()
    else:
        raise ValueError(f"profile {valid_name!r} 的 base_url 必须为字符串")
    validated_base_url = validate_gateway_url(
        base_url,
        protocol=protocol,
        allow_custom_base_url=allow_custom,
    )

    raw_fallbacks = raw_dict.get("fallback_base_urls")
    fallback_urls: list[str] = []
    if raw_fallbacks is not None:
        if not isinstance(raw_fallbacks, list):
            raise ValueError(f"profile {valid_name!r} 的 fallback_base_urls 必须为数组")
        if len(raw_fallbacks) > MAX_FALLBACK_BASE_URLS:
            raise ValueError(
                f"profile {valid_name!r} 的 fallback_base_urls 数量超过上限 {MAX_FALLBACK_BASE_URLS}"
            )
        for item in raw_fallbacks:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(f"profile {valid_name!r} 的 fallback_base_urls 包含无效项")
            norm_fb = validate_gateway_url(
                item,
                protocol=protocol,
                allow_custom_base_url=allow_custom,
            )
            if norm_fb and norm_fb not in fallback_urls:
                fallback_urls.append(norm_fb)

    api_key = _validate_optional_str(raw_dict.get("api_key"), field_name="api_key", max_len=1024)

    if "api_key_env" in raw_dict:
        raw_env = raw_dict.get("api_key_env")
        if raw_env is None or (isinstance(raw_env, str) and not raw_env.strip()):
            api_key_env = ""
        elif isinstance(raw_env, str):
            api_key_env = raw_env.strip()
            if not _ENV_NAME_RE.fullmatch(api_key_env):
                raise ValueError(f"profile {valid_name!r} 的 api_key_env 环境变量名非法: {raw_env!r}")
        else:
            raise ValueError(f"profile {valid_name!r} 的 api_key_env 必须为字符串")
    else:
        # 未显式声明 api_key_env 时：demo / 回环本地网关 / 内联密钥保持为空，官方云端点自动补齐默认变量名
        if protocol == "demo" or api_key or _is_loopback_url(validated_base_url):
            api_key_env = ""
        else:
            api_key_env = DEFAULT_API_KEY_ENVS[protocol]

    model = _validate_optional_str(raw_dict.get("model"), field_name="model", max_len=256) or ""
    fast_model = _validate_optional_str(raw_dict.get("fast_model"), field_name="fast_model", max_len=256)
    reasoning_model = _validate_optional_str(
        raw_dict.get("reasoning_model"), field_name="reasoning_model", max_len=256
    )

    raw_effort = _validate_optional_str(
        raw_dict.get("reasoning_effort"), field_name="reasoning_effort", max_len=32
    )
    reasoning_effort: str | None = None
    if raw_effort is not None:
        norm_effort = raw_effort.lower()
        if norm_effort not in _VALID_REASONING_EFFORTS:
            allowed_eff = ", ".join(sorted(_VALID_REASONING_EFFORTS))
            raise ValueError(
                f"profile {valid_name!r} 的 reasoning_effort {raw_effort!r} 无效（允许: {allowed_eff}）"
            )
        reasoning_effort = norm_effort

    headers = _validate_headers(raw_dict.get("headers"))

    raw_timeout = raw_dict.get("timeout_seconds", DEFAULT_GATEWAY_TIMEOUT_SECONDS)
    if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
        raise ValueError(f"profile {valid_name!r} 的 timeout_seconds 必须为数字")
    timeout_val = float(raw_timeout)
    if not math.isfinite(timeout_val) or not (
        MIN_GATEWAY_TIMEOUT_SECONDS <= timeout_val <= MAX_GATEWAY_TIMEOUT_SECONDS
    ):
        raise ValueError(
            f"profile {valid_name!r} 的 timeout_seconds 超出范围 "
            f"[{MIN_GATEWAY_TIMEOUT_SECONDS}, {MAX_GATEWAY_TIMEOUT_SECONDS}]"
        )

    raw_ctx_win = raw_dict.get("context_window_tokens")
    context_window_tokens: int | None = None
    if raw_ctx_win is not None:
        if isinstance(raw_ctx_win, bool) or not isinstance(raw_ctx_win, int):
            raise ValueError(f"profile {valid_name!r} 的 context_window_tokens 必须为整数")
        if not (4096 <= raw_ctx_win <= 10_000_000):
            raise ValueError(
                f"profile {valid_name!r} 的 context_window_tokens 超出范围 [4096, 10000000]"
            )
        context_window_tokens = raw_ctx_win

    raw_desc = raw_dict.get("description", "")
    if raw_desc is None:
        description = ""
    elif isinstance(raw_desc, str):
        if "\x00" in raw_desc:
            raise ValueError(f"profile {valid_name!r} 的 description 包含空字节")
        if len(raw_desc) > 500:
            raise ValueError(f"profile {valid_name!r} 的 description 超过 500 字符上限")
        description = raw_desc.strip()
    else:
        raise ValueError(f"profile {valid_name!r} 的 description 必须为字符串")

    return GatewayProfile(
        name=valid_name,
        protocol=protocol,
        base_url=validated_base_url,
        api_key_env=api_key_env,
        api_key=api_key,
        model=model,
        fast_model=fast_model,
        reasoning_model=reasoning_model,
        reasoning_effort=reasoning_effort,
        headers=headers,
        fallback_base_urls=fallback_urls,
        timeout_seconds=timeout_val,
        context_window_tokens=context_window_tokens,
        allow_custom_base_url=allow_custom,
        description=description,
    )


def validate_gateways_document(doc: Any) -> dict[str, Any]:
    """校验并规范化整个 `gateways.json` 文档。

    校验项：
    - `schema_version`（默认或必须为 `1`）
    - `profiles`（必须为 dict，数量 `<= 32`，每个 profile 名称、协议、URL、请求头、超时全量校验）
    - `active_profile`（可为 `None`/空字符串；若非空则必须存在于 `profiles` 中）
    """
    if not isinstance(doc, Mapping):
        raise ValueError("gateways 配置根对象必须为 JSON 对象（dict）")

    if "schema_version" in doc:
        raw_ver = doc["schema_version"]
        if isinstance(raw_ver, bool) or not isinstance(raw_ver, int) or raw_ver != GATEWAYS_SCHEMA_VERSION:
            raise ValueError(
                f"不支持的 gateways schema_version: {raw_ver!r}（仅支持 {GATEWAYS_SCHEMA_VERSION}）"
            )
    schema_version = GATEWAYS_SCHEMA_VERSION

    if "profiles" not in doc or not isinstance(doc["profiles"], Mapping):
        raise ValueError("gateways 配置必须包含对象类型的 profiles 字段")
    raw_profiles = doc["profiles"]
    if len(raw_profiles) > MAX_GATEWAY_PROFILES:
        raise ValueError(f"gateways profiles 数量超过上限 {MAX_GATEWAY_PROFILES}")

    validated_profiles: dict[str, dict[str, Any]] = {}
    for prof_name, prof_val in raw_profiles.items():
        gp = _validate_profile(prof_name, prof_val)
        validated_profiles[gp.name] = gp.to_dict()

    raw_active = doc.get("active_profile")
    active_profile: str | None = None
    if raw_active is not None:
        if not isinstance(raw_active, str):
            raise ValueError("active_profile 必须为字符串或 null")
        cleaned_active = raw_active.strip()
        if cleaned_active:
            _validate_profile_name(cleaned_active)
            if cleaned_active not in validated_profiles:
                raise ValueError(f"active_profile {cleaned_active!r} 不存在于 profiles 中")
            active_profile = cleaned_active

    return {
        "schema_version": schema_version,
        "active_profile": active_profile,
        "profiles": validated_profiles,
    }


def _is_sensitive_header_name(header_name: str) -> bool:
    low = header_name.strip().lower()
    return low in _SENSITIVE_HEADER_NAMES or _SENSITIVE_HEADER_KEY_RE.search(low) is not None


def _is_profile_credential_configured(
    profile: Mapping[str, Any],
    *,
    environ: Mapping[str, str],
) -> bool:
    raw_proto = profile.get("protocol", "")
    try:
        protocol = normalize_protocol(raw_proto) if raw_proto else ""
    except ValueError:
        protocol = str(raw_proto or "").strip().lower()

    if protocol == "demo":
        return True

    base_url = str(profile.get("base_url") or "").strip()
    if _is_loopback_url(base_url):
        return True

    inline_key = profile.get("api_key")
    if isinstance(inline_key, str) and inline_key.strip():
        return True

    env_name = str(profile.get("api_key_env") or "").strip()
    if env_name and bool(str(environ.get(env_name, "")).strip()):
        return True

    return False


def redact_gateways_document(
    doc: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """生成面向 API / Web UI / CLI 的脱敏配置视图，杜绝任何内联密钥或敏感请求头泄露。

    脱敏策略：
    - 任何非空内联 `api_key` 一律替换为 `***REDACTED***`；
    - 所有敏感请求头（`Authorization`、`x-api-key`、`x-goog-api-key` 或名称匹配 `key|token|secret`）
      值一律替换为 `***REDACTED***`；
    - 为每个 profile 附加 `credential_configured: bool` 指示运行态凭证是否已就绪。
    """
    env_src = os.environ if environ is None else environ
    out: dict[str, Any] = {
        "schema_version": doc.get("schema_version", GATEWAYS_SCHEMA_VERSION),
        "active_profile": doc.get("active_profile"),
        "profiles": {},
    }
    if "config_path" in doc:
        out["config_path"] = doc["config_path"]

    raw_profiles = doc.get("profiles")
    if isinstance(raw_profiles, Mapping):
        redacted_profiles: dict[str, dict[str, Any]] = {}
        for prof_name, raw_prof in raw_profiles.items():
            if not isinstance(raw_prof, Mapping):
                continue
            prof_copy = deepcopy(dict(raw_prof))
            cred_ok = _is_profile_credential_configured(prof_copy, environ=env_src)

            raw_inline = prof_copy.get("api_key")
            if isinstance(raw_inline, str) and raw_inline.strip():
                prof_copy["api_key"] = REDACTED_SECRET_MARKER
            elif "api_key" in prof_copy:
                prof_copy["api_key"] = None

            raw_headers = prof_copy.get("headers")
            if isinstance(raw_headers, Mapping):
                safe_headers: dict[str, str] = {}
                for hk, hv in raw_headers.items():
                    hkey = str(hk)
                    if _is_sensitive_header_name(hkey):
                        safe_headers[hkey] = REDACTED_SECRET_MARKER
                    else:
                        safe_headers[hkey] = _BEARER_VALUE_RE.sub(
                            rf"\1{REDACTED_SECRET_MARKER}", str(hv)
                        )
                prof_copy["headers"] = safe_headers

            prof_copy["credential_configured"] = cred_ok
            redacted_profiles[str(prof_name)] = prof_copy
        out["profiles"] = redacted_profiles

    return out


# ---------------------------------------------------------------------------
# 路径安全校验（O_NOFOLLOW + st_nlink == 1 + 符号链接祖先拦截）与原子持久化
# ---------------------------------------------------------------------------


def _is_system_compat_symlink(path: Path) -> bool:
    """macOS 根目录下 /tmp -> private/tmp、/var -> private/var 等系统兼容链接不视为攻击。"""
    return sys.platform == "darwin" and str(path) in _DARWIN_SYSTEM_SYMLINKS


def _assert_no_symlink_in_path(target: Path, *, check_ancestors: bool = True) -> None:
    """拒绝目标自身、直接父目录或非系统祖先路径中的符号链接。"""
    if target.is_symlink():
        raise ValueError(f"安全防护拒绝符号链接文件: {target}")
    if target.parent.is_symlink() and not _is_system_compat_symlink(target.parent):
        raise ValueError(f"安全防护拒绝符号链接父目录: {target.parent}")
    if check_ancestors:
        for anc in target.parents:
            if anc == anc.parent:
                break
            if anc.is_symlink() and not _is_system_compat_symlink(anc):
                raise ValueError(f"安全防护拒绝路径中包含符号链接目录: {anc}")


def _open_and_read_safe_regular_file(path: Path) -> bytes:
    """使用 `O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC` 原子打开并校验单硬链常规文件后读取内容。"""
    _assert_no_symlink_in_path(path, check_ancestors=True)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    try:
        fd = os.open(str(path), flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, getattr(errno, "EMLINK", errno.ELOOP)}:
            raise ValueError(f"安全防护（O_NOFOLLOW）拒绝打开符号链接: {path}") from exc
        if exc.errno == errno.ENOENT:
            raise FileNotFoundError(f"未找到 gateways 配置文件: {path}") from exc
        raise ValueError(f"无法安全打开 gateways 配置文件 {path}: {exc}") from exc

    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError(f"gateways 配置路径必须是常规文件: {path}")
        if st.st_nlink != 1:
            raise ValueError(f"安全防护拒绝存在多重硬链接（st_nlink={st.st_nlink}）的配置文件: {path}")
        if st.st_size > _MAX_GATEWAYS_FILE_BYTES:
            raise ValueError(
                f"gateways 配置文件体积超过 {_MAX_GATEWAYS_FILE_BYTES} 字节上限: {path}"
            )
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 16384)
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_GATEWAYS_FILE_BYTES:
                raise ValueError(
                    f"gateways 配置文件体积超过 {_MAX_GATEWAYS_FILE_BYTES} 字节上限: {path}"
                )
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _is_safe_existing_config_file(path: Path, *, boundary: Path | None = None) -> bool:
    """静默检查候选配置文件是否存在且满足非符号链接、单硬链常规文件及可选根目录边界约束。"""
    try:
        if boundary is not None:
            if boundary.is_symlink() or not boundary.is_dir():
                return False
            # 逐段检查 boundary 到 path 之间的每一个组件都不是符号链接
            rel_parts = path.relative_to(boundary).parts
            cur = boundary
            for part in rel_parts:
                cur = cur / part
                if cur.is_symlink():
                    return False
            if not path.resolve().is_relative_to(boundary.resolve()):
                return False
        _assert_no_symlink_in_path(path, check_ancestors=True)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(str(path), flags)
    except (OSError, ValueError):
        return False
    try:
        st = os.fstat(fd)
        return (
            stat.S_ISREG(st.st_mode)
            and st.st_nlink == 1
            and st.st_size <= _MAX_GATEWAYS_FILE_BYTES
        )
    except OSError:
        return False
    finally:
        os.close(fd)


def discover_gateways_config_path(
    *,
    explicit_path: Path | str | None = None,
    state_dir: Path | str | None = None,
    workspace: Path | str | None = None,
    trust_workspace: bool = False,
    tenant_id: str | None = None,
) -> Path | None:
    """按严格优先级与安全边界发现生效的 `gateways.json` 配置文件路径。

    查找顺序：
    1. `explicit_path` 或环境变量 `AGENT_LAB_GATEWAYS_FILE`；
    2. `workspace / ".agent-lab" / "gateways.json"`（仅当 `trust_workspace=True` 且通过非软链/单硬链校验）；
    3. `state_dir / "gateways.json"`（若提供 `state_dir`）；
    4. `Path.home() / ".agent-lab" / "gateways.json"`（仅当 `state_dir is None` 或显式 `tenant_id == "local"`）。
    """
    # 1. 显式路径或环境变量 AGENT_LAB_GATEWAYS_FILE
    raw_explicit: Path | str | None = explicit_path
    if raw_explicit is None or (isinstance(raw_explicit, str) and not raw_explicit.strip()):
        env_file = os.environ.get("AGENT_LAB_GATEWAYS_FILE", "").strip()
        raw_explicit = env_file or None
    if raw_explicit is not None:
        exp_path = Path(raw_explicit).expanduser()
        if _is_safe_existing_config_file(exp_path):
            return exp_path.resolve()
        return None

    # 2. 受信任工作区下的 .agent-lab/gateways.json
    if trust_workspace and workspace is not None:
        ws_path = Path(workspace).expanduser()
        ws_candidate = ws_path / ".agent-lab" / "gateways.json"
        if _is_safe_existing_config_file(ws_candidate, boundary=ws_path):
            return ws_candidate.resolve()

    # 3. state_dir 下的 gateways.json
    if state_dir is not None:
        st_path = Path(state_dir).expanduser()
        st_candidate = st_path / "gateways.json"
        if _is_safe_existing_config_file(st_candidate, boundary=st_path):
            return st_candidate.resolve()

    # 4. 用户主目录 ~/.agent-lab/gateways.json（仅在未传 state_dir 或本机 local 租户时回退）
    allow_home_fallback = (state_dir is None and tenant_id in (None, "local")) or (
        tenant_id == "local"
    )
    if allow_home_fallback:
        with contextlib.suppress(RuntimeError, OSError):
            home_dir = Path.home()
            home_candidate = home_dir / ".agent-lab" / "gateways.json"
            if _is_safe_existing_config_file(home_candidate, boundary=home_dir):
                return home_candidate.resolve()

    return None


def _read_and_validate_gateways_file(path: Path) -> dict[str, Any]:
    raw_bytes = _open_and_read_safe_regular_file(path)
    try:
        decoded = raw_bytes.decode("utf-8")
        parsed = json.loads(decoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"gateways 配置文件不是合法 UTF-8 JSON ({path}): {exc}") from exc
    validated = validate_gateways_document(parsed)
    return {
        **validated,
        "config_path": str(path.resolve()),
    }


def _empty_gateways_document(*, config_path: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": GATEWAYS_SCHEMA_VERSION,
        "active_profile": None,
        "profiles": {},
        "config_path": config_path,
    }


def load_gateways_config(
    *,
    explicit_path: Path | str | None = None,
    state_dir: Path | str | None = None,
    workspace: Path | str | None = None,
    trust_workspace: bool = False,
    tenant_id: str | None = None,
) -> dict[str, Any]:
    """发现、安全读取并校验 `gateways.json`；若尚未创建配置文件则返回空默认结构。

    当显式指定路径或 `state_dir/gateways.json` 被篡改为符号链接、多重硬链接或非常规文件时，
    按失败闭合原则直接抛出 `ValueError` 而不是静默忽略。
    """
    raw_explicit: Path | str | None = explicit_path
    if raw_explicit is None or (isinstance(raw_explicit, str) and not raw_explicit.strip()):
        env_file = os.environ.get("AGENT_LAB_GATEWAYS_FILE", "").strip()
        raw_explicit = env_file or None
    if raw_explicit is not None:
        exp_path = Path(raw_explicit).expanduser()
        if exp_path.is_symlink() or exp_path.exists():
            return _read_and_validate_gateways_file(exp_path)
        _assert_no_symlink_in_path(exp_path, check_ancestors=True)
        return _empty_gateways_document(config_path=None)

    found = discover_gateways_config_path(
        explicit_path=None,
        state_dir=state_dir,
        workspace=workspace,
        trust_workspace=trust_workspace,
        tenant_id=tenant_id,
    )
    if found is not None:
        return _read_and_validate_gateways_file(found)

    # 失败闭合校验：若 state_dir/gateways.json 存在但因软链/硬链未通过安全发现，显式报错而非静默回空
    if state_dir is not None:
        st_candidate = Path(state_dir).expanduser() / "gateways.json"
        if st_candidate.is_symlink() or st_candidate.exists():
            return _read_and_validate_gateways_file(st_candidate)

    return _empty_gateways_document(config_path=None)


def save_gateways_config(path: Path | str, doc: Mapping[str, Any]) -> dict[str, Any]:
    """校验并以 `mkstemp + fsync + chmod(0o600) + os.replace` 原子安全写入 `gateways.json`。"""
    validated = validate_gateways_document(doc)
    target = Path(path).expanduser()
    _assert_no_symlink_in_path(target, check_ancestors=True)

    parent = target.parent
    if not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)
        _assert_no_symlink_in_path(target, check_ancestors=True)
    if not parent.is_dir():
        raise ValueError(f"目标父路径不是有效目录: {parent}")

    # 若目标已存在，通过 O_NOFOLLOW 校验确为单硬链常规文件，阻断软链/硬链覆盖宿主机任意文件
    if target.is_symlink() or target.exists():
        check_flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        try:
            existing_fd = os.open(str(target), check_flags)
        except OSError as exc:
            raise ValueError(f"拒绝写入不安全的 gateways 目标文件 {target}: {exc}") from exc
        try:
            st = os.fstat(existing_fd)
            if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                raise ValueError(
                    f"拒绝覆盖非单硬链常规文件（mode={oct(st.st_mode)}, nlink={st.st_nlink}）: {target}"
                )
        finally:
            os.close(existing_fd)

    encoded = json.dumps(validated, ensure_ascii=False, indent=2) + "\n"
    if len(encoded.encode("utf-8")) > _MAX_GATEWAYS_FILE_BYTES:
        raise ValueError(f"gateways 配置序列化后超过 {_MAX_GATEWAYS_FILE_BYTES} 字节上限")

    fd, tmp_name = tempfile.mkstemp(prefix=".gateways-", suffix=".tmp", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(encoded)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        if target.is_symlink():
            raise ValueError(f"写入前检测到目标被替换为符号链接: {target}")
        os.replace(tmp_name, target)
        # 尽力对父目录执行 fsync，保证 rename 目录项持久化
        dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with contextlib.suppress(OSError):
            dir_fd = os.open(str(parent), dir_flags)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        if os.path.exists(tmp_name):
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)

    return {
        **validated,
        "config_path": str(target.resolve()),
    }


def set_active_gateway_profile(path: Path | str, profile_name: str) -> dict[str, Any]:
    """原子切换指定 `gateways.json` 中的 `active_profile`，并校验目标 profile 已定义。"""
    valid_name = _validate_profile_name(profile_name)
    target = Path(path).expanduser()
    doc = _read_and_validate_gateways_file(target)
    if valid_name not in doc["profiles"]:
        raise ValueError(f"无法激活不存在的 gateway profile: {valid_name!r}")
    doc["active_profile"] = valid_name
    return save_gateways_config(target, doc)


def default_gateways_template() -> dict[str, Any]:
    """生成覆盖全部 5 种运行模式的标准起始配置模板（零硬编码密钥，全量使用 `api_key_env`）。"""
    template: dict[str, Any] = {
        "schema_version": GATEWAYS_SCHEMA_VERSION,
        "active_profile": "demo",
        "profiles": {
            "claude-anthropic": {
                "name": "claude-anthropic",
                "protocol": "anthropic_messages",
                "base_url": "https://api.anthropic.com",
                "api_key_env": "ANTHROPIC_API_KEY",
                "model": "claude-sonnet-4-6",
                "fast_model": "claude-haiku-4-5",
                "reasoning_model": "claude-opus-4-6",
                "reasoning_effort": None,
                "headers": {},
                "fallback_base_urls": [],
                "timeout_seconds": 120.0,
                "context_window_tokens": 200_000,
                "allow_custom_base_url": False,
                "description": (
                    "Anthropic Messages API (Claude 4.5 / 4.6 with prompt caching and extended thinking)"
                ),
            },
            "openai-chat": {
                "name": "openai-chat",
                "protocol": "openai_chat",
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY",
                "model": "gpt-4.1",
                "fast_model": "gpt-4.1-mini",
                "reasoning_model": "o4-mini",
                "reasoning_effort": "medium",
                "headers": {},
                "fallback_base_urls": [],
                "timeout_seconds": 120.0,
                "allow_custom_base_url": False,
                "description": (
                    "OpenAI Chat Completions API (also compatible with DeepSeek, vLLM, SGLang, OpenRouter)"
                ),
            },
            "openai-responses": {
                "name": "openai-responses",
                "protocol": "openai_responses",
                "base_url": "https://api.openai.com/v1",
                "api_key_env": "OPENAI_API_KEY",
                "model": "gpt-5",
                "fast_model": "gpt-5-mini",
                "reasoning_model": "o3",
                "reasoning_effort": "medium",
                "headers": {},
                "fallback_base_urls": [],
                "timeout_seconds": 120.0,
                "allow_custom_base_url": False,
                "description": (
                    "OpenAI Responses API (/v1/responses with native reasoning and function_call items)"
                ),
            },
            "gemini-native": {
                "name": "gemini-native",
                "protocol": "gemini_generate_content",
                "base_url": "https://generativelanguage.googleapis.com/v1beta",
                "api_key_env": "GEMINI_API_KEY",
                "model": "gemini-2.5-pro",
                "fast_model": "gemini-2.5-flash",
                "reasoning_model": "gemini-2.5-pro",
                "reasoning_effort": None,
                "headers": {},
                "fallback_base_urls": [],
                "timeout_seconds": 120.0,
                "context_window_tokens": 1_000_000,
                "allow_custom_base_url": False,
                "description": (
                    "Google Gemini Native GenerateContent API (v1beta function calling and streaming)"
                ),
            },
            "ollama-local": {
                "name": "ollama-local",
                "protocol": "openai_chat",
                "base_url": "http://127.0.0.1:11434/v1",
                "api_key_env": "",
                "model": "qwen2.5-coder:14b",
                "fast_model": "qwen2.5-coder:7b",
                "reasoning_model": None,
                "reasoning_effort": None,
                "headers": {},
                "fallback_base_urls": [],
                "timeout_seconds": 180.0,
                "allow_custom_base_url": True,
                "description": (
                    "Local Ollama OpenAI-compatible gateway on loopback (no API key required)"
                ),
            },
            "demo": {
                "name": "demo",
                "protocol": "demo",
                "base_url": "",
                "api_key_env": "",
                "model": "demo",
                "fast_model": None,
                "reasoning_model": None,
                "reasoning_effort": None,
                "headers": {},
                "fallback_base_urls": [],
                "timeout_seconds": 30.0,
                "allow_custom_base_url": False,
                "description": (
                    "Offline deterministic DemoProvider (harness_regression_only; no network or API key)"
                ),
            },
        },
    }
    return validate_gateways_document(template)


# ---------------------------------------------------------------------------
# 请求头环境变量插值与多协议 Provider 工厂
# ---------------------------------------------------------------------------


def interpolate_gateway_headers(
    headers: Mapping[str, str] | None,
    *,
    environ: Mapping[str, str] | None = None,
    profile_name: str = "",
) -> dict[str, str]:
    """展开请求头中的 `${ENV_VAR}` 或 `${ENV_VAR:-default}` 占位符，并阻断 CRLF 注入。"""
    if not headers:
        return {}
    env_src = os.environ if environ is None else environ
    resolved: dict[str, str] = {}
    for raw_key, raw_val in headers.items():
        key = str(raw_key).strip()
        if not _HEADER_NAME_RE.fullmatch(key):
            raise ValueError(f"非法 HTTP 请求头名称: {raw_key!r}")
        missing: list[str] = []

        def _sub(match: re.Match[str]) -> str:
            var_name = match.group(1)
            default_val = match.group(2)
            env_val = env_src.get(var_name)
            if env_val is not None and env_val != "":
                return str(env_val)
            if default_val is not None:
                return default_val
            missing.append(var_name)
            return ""

        expanded = _ENV_INTERP_RE.sub(_sub, str(raw_val))
        if missing:
            label = f"gateway profile {profile_name!r} 的" if profile_name else ""
            vars_desc = ", ".join(sorted(set(missing)))
            raise ValueError(f"{label}请求头 {key!r} 引用了未设置的环境变量: {vars_desc}")
        if _UNSAFE_CTL_RE.search(expanded):
            raise ValueError(f"请求头 {key!r} 展开后的值包含非法控制字符（防 CRLF 注入）")
        resolved[key] = expanded
    return resolved


def _resolve_profile_api_key(
    gp: GatewayProfile,
    *,
    resolved_headers: Mapping[str, str],
    environ: Mapping[str, str],
) -> tuple[str, bool]:
    """解析 profile 运行态 API Key，返回 `(secret_value, use_anthropic_auth_token)`。"""
    use_auth_token = (
        gp.protocol == "anthropic_messages" and gp.api_key_env == "ANTHROPIC_AUTH_TOKEN"
    )
    if gp.api_key_env:
        env_val = str(environ.get(gp.api_key_env, "")).strip()
        if env_val:
            return env_val, use_auth_token
    if gp.api_key and gp.api_key != REDACTED_SECRET_MARKER:
        return gp.api_key.strip(), use_auth_token

    if _is_loopback_url(gp.base_url):
        # 本地回环 Ollama / vLLM 兼容网关通常不校验密钥；OpenAI 协议客户端用占位值避免空鉴权报错
        placeholder = "ollama" if gp.protocol in {"openai_chat", "openai_responses"} else ""
        return placeholder, False

    has_auth_header = any(
        k.lower() in {"authorization", "x-api-key", "x-goog-api-key"}
        for k in resolved_headers
    )
    if has_auth_header:
        return "", False

    if gp.api_key_env:
        raise ValueError(
            f"gateway profile {gp.name!r} 缺少 API 密钥：请设置环境变量 {gp.api_key_env}"
        )
    raise ValueError(
        f"gateway profile {gp.name!r} 未配置可用凭证（请设置 api_key_env、内联 api_key 或认证请求头）"
    )


def _apply_post_init_profile_attrs(
    provider: Any,
    gp: GatewayProfile,
    resolved_headers: dict[str, str],
    *,
    headers_passed_to_init: bool,
) -> None:
    """将 fast_model / reasoning_model / 额外请求头与非敏感来源元数据挂载到 Provider 实例上。"""
    if gp.fast_model is not None and not getattr(provider, "fast_model", None):
        provider.fast_model = gp.fast_model
    if gp.reasoning_model is not None and not getattr(provider, "reasoning_model", None):
        provider.reasoning_model = gp.reasoning_model
    if not getattr(provider, "base_url", None) and gp.base_url:
        provider.base_url = gp.base_url
    if gp.context_window_tokens is not None:
        provider.context_window_tokens = gp.context_window_tokens
    provider.gateway_profile = gp.name
    provider.gateway_protocol = gp.protocol

    if resolved_headers and not headers_passed_to_init:
        client = getattr(provider, "client", None)
        if client is not None:
            if hasattr(client, "with_options") and gp.protocol == "anthropic_messages":
                with contextlib.suppress(TypeError, ValueError):
                    provider.client = client.with_options(default_headers=resolved_headers)
            elif hasattr(client, "headers") and hasattr(client.headers, "update"):
                client.headers.update(resolved_headers)


@contextlib.contextmanager
def _scoped_environ_patch(updates: Mapping[str, str | None]):
    """线程安全地临时覆盖 `os.environ`（仅用于兼容尚未支持显式构造参数的旧版 Provider）。"""
    with _LEGACY_ENV_LOCK:
        previous: dict[str, str | None] = {}
        for key, val in updates.items():
            previous[key] = os.environ.get(key)
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        try:
            yield
        finally:
            for key, old_val in previous.items():
                if old_val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old_val


def build_provider_from_profile(
    profile_name: str,
    profile: Mapping[str, Any] | GatewayProfile,
) -> Provider:
    """根据声明式 gateway profile 解析凭证、插值请求头并懒加载构造对应的 Provider 实例。

    映射关系：
    - `anthropic_messages` -> `AnthropicProvider`
    - `openai_chat` -> `OpenAICompatibleProvider`
    - `openai_responses` -> `OpenAIResponsesProvider`
    - `gemini_generate_content` -> `GeminiNativeProvider`
    - `demo` -> `DemoProvider`
    """
    gp = _validate_profile(profile_name, profile)
    from . import providers as providers_mod

    if gp.protocol == "demo":
        provider = providers_mod.DemoProvider()
        provider.gateway_profile = gp.name
        provider.gateway_protocol = "demo"
        return provider

    cls_name = PROTOCOL_PROVIDER_CLASSES[gp.protocol]
    provider_cls = getattr(providers_mod, cls_name, None)
    if provider_cls is None:
        raise NotImplementedError(
            f"协议 {gp.protocol!r} 需要 agent_lab.providers.{cls_name}，但当前模块尚未实现该适配器"
        )

    resolved_headers = interpolate_gateway_headers(
        gp.headers, environ=os.environ, profile_name=gp.name
    )
    resolved_model = gp.model or os.environ.get(DEFAULT_MODEL_ENVS[gp.protocol], "").strip()
    if not resolved_model:
        raise ValueError(f"gateway profile {gp.name!r} 缺少 model 配置")

    secret_value, use_auth_token = _resolve_profile_api_key(
        gp, resolved_headers=resolved_headers, environ=os.environ
    )

    sig = inspect.signature(provider_cls.__init__)
    params = sig.parameters
    has_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())
    supports_explicit_config = has_var_kw or ("api_key" in params) or ("model" in params)

    def _accepts(param_name: str) -> bool:
        if has_var_kw:
            return True
        p = params.get(param_name)
        return p is not None and p.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )

    if supports_explicit_config:
        init_kwargs: dict[str, Any] = {}
        if _accepts("model"):
            init_kwargs["model"] = resolved_model
        if _accepts("base_url"):
            init_kwargs["base_url"] = gp.base_url
        if use_auth_token and _accepts("auth_token"):
            init_kwargs["auth_token"] = secret_value
        elif _accepts("api_key"):
            init_kwargs["api_key"] = secret_value

        headers_passed = False
        for hdr_param in ("headers", "extra_headers", "default_headers"):
            if (hdr_param in params) or (has_var_kw and hdr_param == "headers"):
                if resolved_headers or hdr_param in params:
                    init_kwargs[hdr_param] = dict(resolved_headers)
                    headers_passed = True
                break

        if _accepts("fallback_base_urls"):
            init_kwargs["fallback_base_urls"] = list(gp.fallback_base_urls)
        if gp.reasoning_effort is not None and _accepts("reasoning_effort"):
            init_kwargs["reasoning_effort"] = gp.reasoning_effort
        if gp.fast_model is not None and _accepts("fast_model"):
            init_kwargs["fast_model"] = gp.fast_model
        if gp.reasoning_model is not None and _accepts("reasoning_model"):
            init_kwargs["reasoning_model"] = gp.reasoning_model
        if _accepts("profile_name"):
            init_kwargs["profile_name"] = gp.name
        if _accepts("allow_custom_base_url"):
            effective_allow_custom = gp.allow_custom_base_url or (
                os.environ.get("AGENT_ALLOW_CUSTOM_BASE_URL", "").strip() == "1"
            )
            init_kwargs["allow_custom_base_url"] = effective_allow_custom

        if "timeout" in params or has_var_kw:
            init_kwargs["timeout"] = gp.timeout_seconds
        elif _accepts("timeout_seconds"):
            init_kwargs["timeout_seconds"] = gp.timeout_seconds

        provider = provider_cls(**init_kwargs)
        _apply_post_init_profile_attrs(
            provider, gp, resolved_headers, headers_passed_to_init=headers_passed
        )
        return provider

    # 向后兼容路径：旧版仅从环境变量读取配置的 AnthropicProvider / OpenAICompatibleProvider
    legacy_kwargs: dict[str, Any] = {}
    if _accepts("timeout"):
        legacy_kwargs["timeout"] = gp.timeout_seconds
    if gp.fallback_base_urls and _accepts("fallback_base_urls"):
        legacy_kwargs["fallback_base_urls"] = list(gp.fallback_base_urls)
    if gp.reasoning_effort is not None and _accepts("reasoning_effort"):
        legacy_kwargs["reasoning_effort"] = gp.reasoning_effort

    env_patch: dict[str, str | None] = {}
    if gp.allow_custom_base_url or os.environ.get("AGENT_ALLOW_CUSTOM_BASE_URL") == "1":
        env_patch["AGENT_ALLOW_CUSTOM_BASE_URL"] = "1"
    else:
        env_patch["AGENT_ALLOW_CUSTOM_BASE_URL"] = "0"

    if gp.protocol == "anthropic_messages":
        env_patch["ANTHROPIC_MODEL"] = resolved_model
        env_patch["ANTHROPIC_BASE_URL"] = gp.base_url
        if use_auth_token:
            env_patch["ANTHROPIC_API_KEY"] = None
            env_patch["ANTHROPIC_AUTH_TOKEN"] = secret_value
        else:
            env_patch["ANTHROPIC_API_KEY"] = secret_value
            env_patch["ANTHROPIC_AUTH_TOKEN"] = None
    elif gp.protocol in {"openai_chat", "openai_responses"}:
        env_patch["OPENAI_MODEL"] = resolved_model
        env_patch["OPENAI_BASE_URL"] = gp.base_url
        env_patch["OPENAI_API_KEY"] = secret_value or "ollama"
    elif gp.protocol == "gemini_generate_content":
        env_patch["GEMINI_MODEL"] = resolved_model
        env_patch["GEMINI_BASE_URL"] = gp.base_url
        env_patch["GEMINI_API_KEY"] = secret_value

    with _scoped_environ_patch(env_patch):
        provider = provider_cls(**legacy_kwargs)
    _apply_post_init_profile_attrs(provider, gp, resolved_headers, headers_passed_to_init=False)
    return provider


def _extract_safe_base_url(provider: Any, protocol: str) -> str:
    """提取 Provider 实例的非敏感 base_url（剥离凭据、查询参数与尾部斜杠）。"""
    if protocol == "demo":
        return ""
    raw = getattr(provider, "base_url", None)
    if not raw:
        client = getattr(provider, "client", None)
        if client is not None:
            raw = getattr(client, "base_url", None)
    raw_str = str(raw).strip() if raw is not None else ""
    if not raw_str:
        return DEFAULT_BASE_URLS.get(protocol, "")
    try:
        parsed = urllib.parse.urlsplit(raw_str)
        if not parsed.scheme or not parsed.hostname:
            return DEFAULT_BASE_URLS.get(protocol, "")
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port_part = f":{parsed.port}" if parsed.port else ""
        path_part = parsed.path.rstrip("/")
        return f"{parsed.scheme.lower()}://{host}{port_part}{path_part}"
    except ValueError:
        return DEFAULT_BASE_URLS.get(protocol, "")


def _build_gateway_info(
    *,
    profile: str | None,
    protocol: str,
    model: str,
    base_url: str,
    source: str,
) -> dict[str, Any]:
    return {
        "profile": profile,
        "protocol": protocol,
        "model": model,
        "base_url": base_url,
        "source": source,
    }


def _build_protocol_provider(protocol: str) -> Provider:
    """按规范协议名通过 `.providers` 构造默认环境变量驱动的 Provider 实例。"""
    from . import providers as providers_mod

    if protocol == "demo":
        return providers_mod.DemoProvider()

    cls_name = PROTOCOL_PROVIDER_CLASSES[protocol]
    provider_cls = getattr(providers_mod, cls_name, None)
    if provider_cls is not None:
        return provider_cls()

    # 若 providers.get_provider 已注册该协议别名则委托调用
    alias_lookup = {
        "anthropic_messages": "anthropic",
        "openai_chat": "openai",
        "openai_responses": "openai_responses",
        "gemini_generate_content": "gemini",
        "demo": "demo",
    }
    return providers_mod.get_provider(alias_lookup[protocol])


def resolve_provider_or_gateway(
    spec: str | None,
    *,
    state_dir: Path | str | None = None,
    workspace: Path | str | None = None,
    explicit_config_path: Path | str | None = None,
    trust_workspace: bool = False,
    tenant_id: str | None = None,
) -> tuple[Provider, dict[str, Any]]:
    """统一解析模型 Provider 或声明式 Gateway Profile，返回 `(provider, gateway_info)`（零密钥）。

    解析优先级：
    1. 显式 `spec="gateway:<name>"`：精确从已加载的 `gateways.json` 查找并构造；
    2. 非空且非 `"auto"` 的 `spec`：
       - 若名称命中 `gateways.json` 中的 profile，优先按 profile 构造；
       - 否则按显式协议/provider 名称（`anthropic`、`openai`、`openai_responses`、`gemini`、`demo` 等）构造；
    3. `spec` 为 `None`、空串或 `"auto"`：
       - 先检查环境变量 `AGENT_GATEWAY_PROFILE`；
       - 其次检查 `gateways.json` 中的 `active_profile`；
       - 再检查环境变量 `AGENT_PROVIDER`；
       - 最后安全回退至离线 `DemoProvider`。
    """
    cfg = load_gateways_config(
        explicit_path=explicit_config_path,
        state_dir=state_dir,
        workspace=workspace,
        trust_workspace=trust_workspace,
        tenant_id=tenant_id,
    )
    profiles: dict[str, dict[str, Any]] = cfg.get("profiles") or {}
    active_profile: str | None = cfg.get("active_profile")
    config_path: str | None = cfg.get("config_path")

    def _from_profile(prof_name: str, source: str) -> tuple[Provider, dict[str, Any]]:
        prof_data = profiles[prof_name]
        provider = build_provider_from_profile(prof_name, prof_data)
        norm_proto = normalize_protocol(prof_data["protocol"])
        resolved_model = (
            str(getattr(provider, "model", "") or "").strip()
            or str(prof_data.get("model") or "").strip()
            or ("demo" if norm_proto == "demo" else "")
        )
        safe_url = str(prof_data.get("base_url") or "") or _extract_safe_base_url(
            provider, norm_proto
        )
        return provider, _build_gateway_info(
            profile=prof_name,
            protocol=norm_proto,
            model=resolved_model,
            base_url=safe_url,
            source=source,
        )

    def _from_protocol(raw_proto: str, source: str) -> tuple[Provider, dict[str, Any]]:
        norm_proto = normalize_protocol(raw_proto)
        provider = _build_protocol_provider(norm_proto)
        resolved_model = (
            str(getattr(provider, "model", "") or "").strip()
            or ("demo" if norm_proto == "demo" else "")
        )
        safe_url = _extract_safe_base_url(provider, norm_proto)
        return provider, _build_gateway_info(
            profile=None,
            protocol=norm_proto,
            model=resolved_model,
            base_url=safe_url,
            source=source,
        )

    raw_spec = spec.strip() if isinstance(spec, str) else ""

    # 1. 显式 gateway:<name>
    if raw_spec.startswith("gateway:"):
        target_name = raw_spec[len("gateway:") :].strip()
        if not target_name or target_name not in profiles:
            raise ValueError(
                f"未找到 gateway profile {target_name!r}（配置文件: {config_path or '未发现'}）"
            )
        return _from_profile(target_name, "gateway_explicit")

    # 2. 显式 profile 名或显式协议/provider 名
    if raw_spec and raw_spec.lower() != "auto":
        if raw_spec in profiles:
            return _from_profile(raw_spec, "gateway_profile")
        if raw_spec.lower() in PROTOCOL_ALIASES:
            return _from_protocol(raw_spec, "provider_spec")
        raise ValueError(
            f"未知的 provider 或 gateway profile: {raw_spec!r}（配置文件: {config_path or '未发现'}）"
        )

    # 3a. 环境变量 AGENT_GATEWAY_PROFILE
    env_profile = os.environ.get("AGENT_GATEWAY_PROFILE", "").strip()
    if env_profile:
        if env_profile.startswith("gateway:"):
            env_profile = env_profile[len("gateway:") :].strip()
        if not env_profile or env_profile not in profiles:
            raise ValueError(
                f"环境变量 AGENT_GATEWAY_PROFILE={env_profile!r} 未在 gateways.json 中找到"
                f"（配置文件: {config_path or '未发现'}）"
            )
        return _from_profile(env_profile, "env_gateway_profile")

    # 3b. gateways.json 中的 active_profile
    if active_profile and active_profile in profiles:
        return _from_profile(active_profile, "active_profile")

    # 3c. 环境变量 AGENT_PROVIDER
    env_provider = os.environ.get("AGENT_PROVIDER", "").strip()
    if env_provider and env_provider.lower() != "auto":
        if env_provider.startswith("gateway:"):
            target_name = env_provider[len("gateway:") :].strip()
            if not target_name or target_name not in profiles:
                raise ValueError(
                    f"环境变量 AGENT_PROVIDER={env_provider!r} 指向的 gateway profile 不存在"
                )
            return _from_profile(target_name, "env_provider_gateway")
        if env_provider in profiles:
            return _from_profile(env_provider, "env_provider_gateway")
        return _from_protocol(env_provider, "env_provider")

    # 3d. 安全默认回退至离线 DemoProvider
    return _from_protocol("demo", "default_demo")


__all__ = [
    "CANONICAL_PROTOCOLS",
    "DEFAULT_API_KEY_ENVS",
    "DEFAULT_BASE_URLS",
    "DEFAULT_GATEWAY_TIMEOUT_SECONDS",
    "DEFAULT_MODEL_ENVS",
    "GATEWAYS_SCHEMA_VERSION",
    "GatewayProfile",
    "MAX_FALLBACK_BASE_URLS",
    "MAX_GATEWAY_HEADERS",
    "MAX_GATEWAY_PROFILES",
    "OFFICIAL_PROTOCOL_HOSTS",
    "PROTOCOL_ALIASES",
    "PROTOCOL_PROVIDER_CLASSES",
    "REDACTED_SECRET_MARKER",
    "build_provider_from_profile",
    "default_gateways_template",
    "discover_gateways_config_path",
    "interpolate_gateway_headers",
    "load_gateways_config",
    "normalize_protocol",
    "redact_gateways_document",
    "resolve_provider_or_gateway",
    "save_gateways_config",
    "set_active_gateway_profile",
    "validate_gateway_url",
    "validate_gateways_document",
]
