"""Headless/CDP Playwright 浏览器自动化层：基于 ARIA 无障碍快照（Ref 精准定位）与 SSRF 护栏的跨平台 Web 操控。

================================================================================
模块导读（Module Guide）
================================================================================
1. 为什么在 `computer.py` 桌面像素操控之外单独提供 Browser ARIA Snapshot 层？
   - 对 Web 应用验收、表单测试、控制台错误排查而言，OS 截屏 + 绝对像素坐标点击不仅分辨率脆弱、跨平台成本高，
     还拿不到浏览器 Console 错误与 Network 状态码；
   - 对标 Microsoft Playwright MCP / Foundry Browser Automation：本模块以结构化 ARIA Accessibility Tree
     为核心，为可交互节点分配确定性短引用（`e1`, `e2`, ...），模型可直接按 `ref` 精准点击/键入，并获取结构化
     控制台日志、网络请求与截图，同时提供零依赖确定性 `MockBrowserBackend` 供离线测试与 CI 使用。
2. 核心安全不变量：
   - 所有导航 URL 必须经过 `validate_browser_url` 校验：仅允许 `http://` 与 `https://`，严格拦截
     `file://` / `javascript:` / `data:` / `chrome://`、URL 内嵌凭据、云元数据 IMDS（`169.254.169.254` /
     `metadata.google.internal`），默认阻断私网与回环（仅当显式 `allow_loopback=True` 时允许本地开发服务器）。
================================================================================
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
import ipaddress
import os
from pathlib import Path
import socket
import struct
from typing import Any, Protocol
import urllib.parse
import zlib

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
_MAX_SNAPSHOT_CHARS = 8000


class BrowserError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class BrowserUnavailableError(BrowserError):
    def __init__(self, message: str) -> None:
        super().__init__("browser_unavailable", message)


def _is_blocked_browser_ip(
    ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address, *, allow_loopback: bool
) -> bool:
    if isinstance(ip_obj, ipaddress.IPv6Address):
        if ip_obj.ipv4_mapped is not None:
            return _is_blocked_browser_ip(ip_obj.ipv4_mapped, allow_loopback=allow_loopback)
        if ip_obj.sixtofour is not None:
            return _is_blocked_browser_ip(ip_obj.sixtofour, allow_loopback=allow_loopback)
        if ip_obj.teredo is not None:
            return _is_blocked_browser_ip(ip_obj.teredo[0], allow_loopback=allow_loopback) or (
                _is_blocked_browser_ip(ip_obj.teredo[1], allow_loopback=allow_loopback)
            )
    if ip_obj.is_link_local or ip_obj.is_unspecified or ip_obj.is_multicast or ip_obj.is_reserved:
        return True
    if ip_obj.is_loopback:
        return not allow_loopback
    if ip_obj.is_private:
        return True
    return False


def validate_browser_url(url: str, *, allow_loopback: bool = False, check_dns: bool = False) -> str:
    """校验浏览器导航 URL，阻断非 http(s)、本地文件、云元数据 IMDS 及未授权私网/回环。"""
    if not isinstance(url, str) or not url.strip():
        raise BrowserError("invalid_url", "URL 不能为空")
    clean = url.strip()
    if any(c in clean for c in ("\r", "\n", "\x00")):
        raise BrowserError("invalid_url", "URL 包含非法控制字符")
    parsed = urllib.parse.urlsplit(clean)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise BrowserError("blocked_scheme", f"浏览器仅允许 http/https 协议，已拒绝: {scheme!r}")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserError("invalid_url", "URL 禁止内嵌用户名或密码")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise BrowserError("invalid_url", "URL 缺少合法主机名")
    if host in _BLOCKED_METADATA_HOSTS:
        raise BrowserError("ssrf_blocked", f"安全防护拒绝访问云元数据地址: {host}")

    eff_loopback = allow_loopback or (
        os.environ.get("AGENT_LAB_BROWSER_ALLOW_LOOPBACK", "").strip() == "1"
    )
    is_localhost = host == "localhost" or host.endswith(".localhost")
    if is_localhost and not eff_loopback:
        raise BrowserError(
            "ssrf_blocked",
            "安全防护默认禁止访问回环地址（测试本地开发服务请传 allow_loopback=true）",
        )
    try:
        ip_obj = ipaddress.ip_address(host)
    except ValueError:
        ip_obj = None
    if ip_obj is not None and _is_blocked_browser_ip(ip_obj, allow_loopback=eff_loopback):
        raise BrowserError("ssrf_blocked", f"安全防护拒绝访问受限 IP: {host}")
    if check_dns and ip_obj is None and not is_localhost:
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if scheme == "https" else 80))
        except OSError:
            infos = []
        for info in infos:
            try:
                resolved_ip = ipaddress.ip_address(info[4][0])
            except ValueError:
                continue
            if _is_blocked_browser_ip(resolved_ip, allow_loopback=eff_loopback):
                raise BrowserError("ssrf_blocked", f"域名 {host} 解析到受限 IP {resolved_ip}")
    return clean


def _tiny_valid_png_bytes() -> bytes:
    """生成 1x1 像素合法 PNG 字节，供离线 MockBrowserBackend 截屏测试使用。"""
    def _chunk(ctype: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + ctype + data + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    idat = zlib.compress(b"\x00\x2d\x63\x4c")
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


class BrowserBackend(Protocol):
    async def navigate(self, url: str) -> dict[str, Any]: ...
    async def snapshot(self, *, max_nodes: int = 80) -> dict[str, Any]: ...
    async def click(self, ref_or_selector: str) -> dict[str, Any]: ...
    async def type_text(self, ref_or_selector: str, text: str, *, submit: bool = False) -> dict[str, Any]: ...
    async def console_logs(self, *, level: str = "info", limit: int = 30) -> dict[str, Any]: ...
    async def network_requests(self, *, limit: int = 30) -> dict[str, Any]: ...
    async def screenshot(self, *, full_page: bool = False) -> bytes: ...
    async def aclose(self) -> None: ...


class MockBrowserBackend:
    """零依赖确定性离线浏览器后端：维护模拟 ARIA 节点、交互状态、Console 与 Network 日志。"""

    def __init__(self, pages: Mapping[str, dict[str, Any]] | None = None) -> None:
        self._pages: dict[str, dict[str, Any]] = dict(pages or {})
        self.current_url: str = "about:blank"
        self.title: str = "Blank"
        self._nodes: list[dict[str, Any]] = []
        self._console: list[dict[str, str]] = []
        self._network: list[dict[str, Any]] = []
        self.closed: bool = False

    def _default_nodes_for(self, url: str) -> tuple[str, list[dict[str, Any]]]:
        if url in self._pages:
            page = self._pages[url]
            return str(page.get("title") or url), [dict(n) for n in (page.get("nodes") or [])]
        return (
            f"Mock Page ({url})",
            [
                {"ref": "e1", "role": "heading", "name": f"Welcome to {url}", "value": ""},
                {"ref": "e2", "role": "textbox", "name": "Search", "value": ""},
                {"ref": "e3", "role": "button", "name": "Submit", "value": ""},
            ],
        )

    async def navigate(self, url: str) -> dict[str, Any]:
        self.current_url = url
        self.title, self._nodes = self._default_nodes_for(url)
        self._network.append({"method": "GET", "url": url, "status": 200})
        self._console.append({"level": "info", "text": f"Navigated to {url}"})
        return {"ok": True, "url": self.current_url, "title": self.title}

    async def snapshot(self, *, max_nodes: int = 80) -> dict[str, Any]:
        nodes = self._nodes[: max(1, min(max_nodes, 200))]
        lines = [f"# {self.title} ({self.current_url})"]
        for n in nodes:
            val_part = f" value={n['value']!r}" if n.get("value") else ""
            lines.append(f"- [{n['ref']}] {n['role']} {n['name']!r}{val_part}")
        return {
            "ok": True,
            "url": self.current_url,
            "title": self.title,
            "node_count": len(nodes),
            "nodes": nodes,
            "tree_text": "\n".join(lines)[:_MAX_SNAPSHOT_CHARS],
        }

    def _find_node(self, ref_or_selector: str) -> dict[str, Any] | None:
        key = ref_or_selector.strip()
        for n in self._nodes:
            if n.get("ref") == key or n.get("name") == key:
                return n
        return None

    async def click(self, ref_or_selector: str) -> dict[str, Any]:
        node = self._find_node(ref_or_selector)
        if node is None:
            return {"ok": False, "code": "element_not_found", "error": f"未找到节点: {ref_or_selector}"}
        node["clicked"] = int(node.get("clicked", 0)) + 1
        self._console.append({"level": "info", "text": f"Clicked {node['ref']} ({node['name']})"})
        target_url = node.get("href")
        if isinstance(target_url, str) and target_url:
            await self.navigate(target_url)
        return {"ok": True, "ref": node["ref"], "role": node["role"], "name": node["name"], "url": self.current_url}

    async def type_text(self, ref_or_selector: str, text: str, *, submit: bool = False) -> dict[str, Any]:
        node = self._find_node(ref_or_selector)
        if node is None:
            return {"ok": False, "code": "element_not_found", "error": f"未找到输入节点: {ref_or_selector}"}
        node["value"] = str(text)
        self._console.append({"level": "info", "text": f"Typed into {node['ref']}: {text[:40]}"})
        if submit:
            self._console.append({"level": "info", "text": f"Submitted form from {node['ref']}"})
        return {"ok": True, "ref": node["ref"], "value": node["value"], "submitted": bool(submit)}

    async def console_logs(self, *, level: str = "info", limit: int = 30) -> dict[str, Any]:
        severity = {"debug": 0, "info": 1, "warning": 2, "error": 3}
        min_sev = severity.get(level.lower(), 1)
        filtered = [c for c in self._console if severity.get(c["level"].lower(), 1) >= min_sev]
        return {"ok": True, "messages": filtered[-max(1, min(limit, 200)) :]}

    async def network_requests(self, *, limit: int = 30) -> dict[str, Any]:
        return {"ok": True, "requests": self._network[-max(1, min(limit, 200)) :]}

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        del full_page
        return _tiny_valid_png_bytes()

    async def aclose(self) -> None:
        self.closed = True


class PlaywrightBrowserBackend:
    """真实 Headless Chromium Playwright 后端（惰性导入可选 `playwright` 依赖）。"""

    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._ref_selectors: dict[str, str] = {}
        self._console: list[dict[str, str]] = []
        self._network: list[dict[str, Any]] = []

    async def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserUnavailableError(
                "未安装 playwright；请运行 `pip install playwright && playwright install chromium` 后重试。"
            ) from exc
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self._headless)
        self._context = await self._browser.new_context(viewport={"width": 1280, "height": 800})
        self._page = await self._context.new_page()
        self._page.on(
            "console",
            lambda msg: self._console.append({"level": str(msg.type), "text": str(msg.text)[:500]}),
        )
        self._page.on(
            "response",
            lambda resp: self._network.append(
                {"method": resp.request.method, "url": resp.url[:300], "status": resp.status}
            ),
        )
        return self._page

    async def navigate(self, url: str) -> dict[str, Any]:
        page = await self._ensure_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        return {"ok": True, "url": page.url, "title": await page.title()}

    async def snapshot(self, *, max_nodes: int = 80) -> dict[str, Any]:
        page = await self._ensure_page()
        self._ref_selectors.clear()
        nodes = await page.evaluate(
            """(limit) => {
              const interactive = document.querySelectorAll(
                'a, button, input, textarea, select, [role="button"], [role="link"], [role="textbox"], h1, h2, h3'
              );
              const out = [];
              let idx = 1;
              for (const el of interactive) {
                if (out.length >= limit) break;
                const rect = el.getBoundingClientRect();
                if (rect.width === 0 && rect.height === 0) continue;
                const ref = 'e' + idx++;
                el.setAttribute('data-a11y-ref', ref);
                out.push({
                  ref,
                  role: el.getAttribute('role') || el.tagName.toLowerCase(),
                  name: (el.getAttribute('aria-label') || el.innerText || el.placeholder || el.name || '').trim().slice(0, 120),
                  value: typeof el.value === 'string' ? el.value.slice(0, 120) : '',
                });
              }
              return out;
            }""",
            max(1, min(max_nodes, 200)),
        )
        for n in nodes:
            self._ref_selectors[n["ref"]] = f'[data-a11y-ref="{n["ref"]}"]'
        title = await page.title()
        lines = [f"# {title} ({page.url})"]
        for n in nodes:
            val_part = f" value={n['value']!r}" if n.get("value") else ""
            lines.append(f"- [{n['ref']}] {n['role']} {n['name']!r}{val_part}")
        return {
            "ok": True,
            "url": page.url,
            "title": title,
            "node_count": len(nodes),
            "nodes": nodes,
            "tree_text": "\n".join(lines)[:_MAX_SNAPSHOT_CHARS],
        }

    def _resolve_selector(self, ref_or_selector: str) -> str:
        return self._ref_selectors.get(ref_or_selector.strip(), ref_or_selector.strip())

    async def click(self, ref_or_selector: str) -> dict[str, Any]:
        page = await self._ensure_page()
        sel = self._resolve_selector(ref_or_selector)
        await page.locator(sel).first.click(timeout=10_000)
        return {"ok": True, "target": ref_or_selector, "url": page.url}

    async def type_text(self, ref_or_selector: str, text: str, *, submit: bool = False) -> dict[str, Any]:
        page = await self._ensure_page()
        sel = self._resolve_selector(ref_or_selector)
        loc = page.locator(sel).first
        await loc.fill(str(text), timeout=10_000)
        if submit:
            await loc.press("Enter")
        return {"ok": True, "target": ref_or_selector, "submitted": bool(submit)}

    async def console_logs(self, *, level: str = "info", limit: int = 30) -> dict[str, Any]:
        severity = {"debug": 0, "log": 1, "info": 1, "warning": 2, "error": 3}
        min_sev = severity.get(level.lower(), 1)
        filtered = [c for c in self._console if severity.get(c["level"].lower(), 1) >= min_sev]
        return {"ok": True, "messages": filtered[-max(1, min(limit, 200)) :]}

    async def network_requests(self, *, limit: int = 30) -> dict[str, Any]:
        return {"ok": True, "requests": self._network[-max(1, min(limit, 200)) :]}

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        page = await self._ensure_page()
        return bytes(await page.screenshot(full_page=bool(full_page), type="png"))

    async def aclose(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None
        self._page = None


class BrowserSessionManager:
    """运行级浏览器会话管理器：统一执行 URL SSRF 防护、截图落盘与资源清理。"""

    def __init__(
        self,
        *,
        backend: BrowserBackend | None = None,
        artifacts_dir: Path | None = None,
        run_id: str | None = None,
    ) -> None:
        self._backend: BrowserBackend = backend if backend is not None else PlaywrightBrowserBackend()
        self._artifacts_dir = artifacts_dir
        self._run_id = run_id or "browser"
        self._shot_seq = 0

    async def execute(self, tool_name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        try:
            if tool_name == "browser_navigate":
                allow_lb = bool(arguments.get("allow_loopback", False))
                url = validate_browser_url(
                    str(arguments.get("url") or ""),
                    allow_loopback=allow_lb,
                    check_dns=not isinstance(self._backend, MockBrowserBackend),
                )
                return await self._backend.navigate(url)
            if tool_name == "browser_snapshot":
                return await self._backend.snapshot(max_nodes=int(arguments.get("max_nodes", 80)))
            if tool_name == "browser_click":
                return await self._backend.click(str(arguments.get("ref_or_selector") or ""))
            if tool_name == "browser_type":
                return await self._backend.type_text(
                    str(arguments.get("ref_or_selector") or ""),
                    str(arguments.get("text") or ""),
                    submit=bool(arguments.get("submit", False)),
                )
            if tool_name == "browser_console_logs":
                return await self._backend.console_logs(
                    level=str(arguments.get("level") or "info"),
                    limit=int(arguments.get("limit", 30)),
                )
            if tool_name == "browser_screenshot":
                png_bytes = await self._backend.screenshot(full_page=bool(arguments.get("full_page", False)))
                self._shot_seq += 1
                artifact_name = f"browser_shot_{self._shot_seq:03d}.png"
                saved_path: str | None = None
                if self._artifacts_dir is not None:
                    out_dir = self._artifacts_dir / self._run_id
                    out_dir.mkdir(parents=True, exist_ok=True)
                    target = out_dir / artifact_name
                    target.write_bytes(png_bytes)
                    saved_path = str(target)
                return {
                    "ok": True,
                    "artifact_name": artifact_name,
                    "bytes": len(png_bytes),
                    "saved_path": saved_path,
                    "base64_preview": base64.b64encode(png_bytes[:256]).decode("ascii"),
                }
            return {"ok": False, "code": "unknown_browser_tool", "error": f"未知浏览器工具: {tool_name}"}
        except BrowserError as exc:
            return {"ok": False, "code": exc.code, "error": str(exc)}
        except Exception as exc:
            return {"ok": False, "code": "browser_error", "error": str(exc)[:300]}

    async def aclose(self) -> None:
        await self._backend.aclose()


BROWSER_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "browser_navigate",
        "description": "在隔离 Headless 浏览器中打开受 SSRF 护栏保护的网页（验证本地开发服务需显式 allow_loopback=true）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "minLength": 1, "maxLength": 2000},
                "allow_loopback": {"type": "boolean", "description": "是否允许访问 127.0.0.1/localhost 本地开发服务"},
            },
            "required": ["url"],
            "additionalProperties": False,
        },
    },
    {
        "name": "browser_snapshot",
        "description": "捕获当前页面的结构化 ARIA 无障碍快照树，为每个交互节点分配确定性 ref（e1/e2/...），便于精准点击或输入。",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_nodes": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "browser_click",
        "description": "按 ARIA 快照返回的 ref（如 e3）或 CSS 选择器点击页面元素。",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref_or_selector": {"type": "string", "minLength": 1, "maxLength": 240},
            },
            "required": ["ref_or_selector"],
            "additionalProperties": False,
        },
    },
    {
        "name": "browser_type",
        "description": "向指定 ref 或 CSS 选择器的输入框键入文本（可选回车提交）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "ref_or_selector": {"type": "string", "minLength": 1, "maxLength": 240},
                "text": {"type": "string", "maxLength": 4000},
                "submit": {"type": "boolean"},
            },
            "required": ["ref_or_selector", "text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "browser_console_logs",
        "description": "获取浏览器控制台日志（排查前端 JS 异常与警告）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "level": {"type": "string", "enum": ["debug", "info", "warning", "error"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 200},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "browser_screenshot",
        "description": "截取当前 Headless 浏览器视口或整页 PNG 截图并存入产物目录。",
        "input_schema": {
            "type": "object",
            "properties": {
                "full_page": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
]
