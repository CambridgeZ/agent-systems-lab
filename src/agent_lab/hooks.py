"""确定性生命周期钩子管理器（Lifecycle HookManager）：支持声明式命令钩子与编程式回调。

================================================================================
模块导读（Module Guide）
================================================================================
1. 模块角色（Role）
   本模块实现类似 Claude Code Hooks 的确定性事件拦截与上下文增强管道，覆盖七大核心生命周期事件：
   `SessionStart`、`UserPromptSubmit`、`PreToolUse`、`PostToolUse`、`PreCompact`、`Stop`、
   `Notification`。相比依赖提示词软约束，钩子在关键执行节点通过确定性 Python 回调或外部命令
   强行介入：例如在 `PreToolUse` 拒绝/改写参数、在 `PostToolUse` 注入代码检查诊断与额外上下文、
   或在 `Stop` 阶段执行最终验证门禁并否决过早结束（`veto: true`）。

2. 核心安全与性能不变量（Core Security & Performance Invariants）
   - 零配置零开销（Zero-Overhead No-Op）：当某事件未配置任何钩子时，`run()`/`arun()` 直接命中
     O(1) 快速返回，绝不产生子进程派生、JSON 序列化或冗余拷贝。
   - 项目配置信任门禁：仅加载全局 `<state_dir>/settings.json` 与经信任校验的
     `<workspace>/.agent-lab/settings.json`；未经信任的项目仓库绝不自动执行其声明的任意 Shell 钩子。
   - 凭证剥离与进程组回收：所有命令钩子通过 `scrub_subprocess_env` 彻底剥离宿主 API Key / Token，
     并在独立会话进程组（`start_new_session=True`）内执行；超时后执行 `SIGTERM -> SIGKILL` 全组清场。
   - 失败策略可控（`on_error: "allow" | "deny"`）：命令非零退出、超时或返回非法 JSON 时，按每条钩子
     配置的 `on_error` 策略确定性决定放行降级或失败闭合拦截。
================================================================================
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import dataclass
import fnmatch
import inspect
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Any

from .permissions import load_settings_layers, scrub_subprocess_env

HOOK_EVENTS = frozenset(
    {
        "SessionStart",
        "UserPromptSubmit",
        "PreToolUse",
        "PostToolUse",
        "PreCompact",
        "Stop",
        "Notification",
    }
)

_DEFAULT_HOOK_TIMEOUT = 10.0
_MIN_HOOK_TIMEOUT = 0.2
_MAX_HOOK_TIMEOUT = 60.0
_MAX_HOOK_STDOUT_BYTES = 128 * 1024
_REGEX_HINT_CHARS = frozenset({"|", "^", "$", "(", ")", "+", "{", "}", "\\"})


@dataclass(frozen=True)
class HookEntry:
    """单条生命周期钩子定义（命令式或 Python 编程式回调）。"""

    event: str
    matcher: str = "*"
    command: str | None = None
    callback: Callable[[dict[str, Any]], Any] | None = None
    timeout_seconds: float = _DEFAULT_HOOK_TIMEOUT
    on_error: str = "allow"
    source: str = "inline"
    name: str | None = None


def _normalize_on_error(raw: Any) -> str:
    clean = str(raw or "allow").strip().lower()
    return "deny" if clean == "deny" else "allow"


def _normalize_timeout(raw: Any) -> float:
    try:
        val = float(raw) if raw is not None else _DEFAULT_HOOK_TIMEOUT
    except (TypeError, ValueError):
        return _DEFAULT_HOOK_TIMEOUT
    return max(_MIN_HOOK_TIMEOUT, min(_MAX_HOOK_TIMEOUT, val))


def match_hook_matcher(matcher: str, target: str) -> bool:
    """支持通配符（Glob）与正则表达式（Regex）的钩子匹配器。

    匹配规则：
    - `"*"`、`""`、`".*"` 匹配任意目标；
    - `"/pattern/"` 或 `"regex:pattern"` 显式按正则 `re.search` 匹配；
    - 含 `^` / `$` 时按正则 `re.search` 匹配；
    - 含 `|` / `()` / `+` 等正则元字符时优先按 `re.fullmatch`（兼容 `fs_edit|fs_write`），
      正则解析异常时安全回退至 `fnmatch.fnmatchcase`；
    - 其余按 `fnmatch.fnmatchcase` 匹配（兼容 `fs_*`、`bash_exec`、`mcp__github__*`）。
    """
    pat = (matcher or "*").strip()
    if not pat or pat in {"*", ".*"}:
        return True
    tgt = target or ""

    if len(pat) >= 2 and pat.startswith("/") and pat.endswith("/"):
        try:
            return re.search(pat[1:-1], tgt) is not None
        except re.error:
            return False

    if pat.startswith("regex:"):
        try:
            return re.search(pat[len("regex:") :], tgt) is not None
        except re.error:
            return False

    if any(ch in pat for ch in _REGEX_HINT_CHARS):
        try:
            if "^" in pat or "$" in pat:
                return re.search(pat, tgt) is not None
            if re.fullmatch(pat, tgt) is not None:
                return True
        except re.error:
            pass

    return fnmatch.fnmatchcase(tgt, pat)


def _extract_matcher_target(event: str, payload: dict[str, Any]) -> str:
    """按事件类型提取供 `matcher` 匹配的目标字符串。"""
    if event in {"PreToolUse", "PostToolUse"}:
        return str(payload.get("tool_name") or payload.get("tool") or "")
    for key in ("matcher_target", "tool_name", "trigger", "source", "kind"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return event


def _default_hook_response(event: str) -> dict[str, Any]:
    """构建零钩子命中时的标准默认返回体。"""
    if event == "PreToolUse":
        return {
            "decision": "allow",
            "explicit_decision": None,
            "reason": "",
            "updated_input": None,
            "hooks_run": 0,
            "errors": [],
        }
    if event == "PostToolUse":
        return {
            "additional_context": "",
            "diagnostics": [],
            "hooks_run": 0,
            "errors": [],
        }
    if event == "Stop":
        return {
            "veto": False,
            "reason": "",
            "hooks_run": 0,
            "errors": [],
        }
    if event == "UserPromptSubmit":
        return {
            "decision": "allow",
            "reason": "",
            "additional_context": "",
            "updated_prompt": None,
            "hooks_run": 0,
            "errors": [],
        }
    return {
        "additional_context": "",
        "hooks_run": 0,
        "errors": [],
        "results": [],
    }


def _reap_hook_process_group(proc: subprocess.Popen[bytes], pgid: int) -> None:
    """两阶段进程组回收（SIGTERM -> SIGKILL），防止超时命令钩子残留孤儿进程。"""
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGTERM)
    if proc.poll() is None:
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=0.3)
    deadline = time.monotonic() + 0.15
    while time.monotonic() < deadline:
        try:
            os.killpg(pgid, 0)
            time.sleep(0.02)
        except ProcessLookupError:
            return
        except (PermissionError, OSError):
            break
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(pgid, signal.SIGKILL)
    if proc.poll() is None:
        with contextlib.suppress(subprocess.TimeoutExpired, OSError):
            proc.wait(timeout=0.5)


def _execute_command_hook(
    entry: HookEntry,
    payload: dict[str, Any],
    *,
    workspace: Path | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    """在脱敏环境变量与独立进程组中执行外部命令钩子，通过 stdin 喂入 JSON 并从 stdout 解析 JSON。"""
    assert entry.command is not None
    shell_bin = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
    extra_env: dict[str, str] = {"AGENT_LAB_HOOK_EVENT": entry.event}
    cwd_str: str | None = None
    if workspace is not None and workspace.exists() and workspace.is_dir():
        resolved_ws = str(workspace.resolve())
        cwd_str = resolved_ws
        extra_env["AGENT_LAB_WORKSPACE"] = resolved_ws

    env = scrub_subprocess_env(extra_env)
    stdin_bytes = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")

    try:
        proc = subprocess.Popen(
            [shell_bin, "-c", entry.command],
            cwd=cwd_str,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return None, f"钩子进程启动失败: {exc}"

    pgid = proc.pid
    try:
        stdout_raw, stderr_raw = proc.communicate(input=stdin_bytes, timeout=entry.timeout_seconds)
    except subprocess.TimeoutExpired:
        _reap_hook_process_group(proc, pgid)
        with contextlib.suppress(Exception):
            proc.communicate(timeout=0.5)
        return None, f"钩子执行超时 ({entry.timeout_seconds}s)"
    except BaseException:
        _reap_hook_process_group(proc, pgid)
        raise
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pgid, signal.SIGKILL)

    if proc.returncode != 0:
        err_snippet = (stderr_raw or b"").decode("utf-8", errors="replace").strip()[:300]
        detail = f"exit={proc.returncode}" + (f" ({err_snippet})" if err_snippet else "")
        return None, f"钩子返回非零退出码: {detail}"

    stdout_trimmed = (stdout_raw or b"")[:_MAX_HOOK_STDOUT_BYTES].decode("utf-8", errors="replace").strip()
    if not stdout_trimmed:
        return {}, None

    try:
        parsed = json.loads(stdout_trimmed)
    except json.JSONDecodeError as exc:
        return None, f"钩子 stdout 不是合法 JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "钩子 stdout JSON 顶层必须是对象 (dict)"
    return parsed, None


def _parse_hooks_config_entries(
    hooks_cfg: Any,
    *,
    source: str,
) -> list[HookEntry]:
    """将 `settings.json` 中的 `hooks` 对象解析为标准 `HookEntry` 列表。"""
    if not isinstance(hooks_cfg, dict):
        return []
    entries: list[HookEntry] = []
    for event_name, raw_list in hooks_cfg.items():
        if event_name not in HOOK_EVENTS or not isinstance(raw_list, list):
            continue
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            matcher = str(item.get("matcher") or "*").strip() or "*"
            # 支持扁平格式：{"matcher": "...", "command": "...", "timeout_seconds": 10, "on_error": "allow"|"deny"}
            if isinstance(item.get("command"), str) and item["command"].strip():
                entries.append(
                    HookEntry(
                        event=event_name,
                        matcher=matcher,
                        command=item["command"].strip(),
                        timeout_seconds=_normalize_timeout(item.get("timeout_seconds")),
                        on_error=_normalize_on_error(item.get("on_error")),
                        source=source,
                        name=str(item.get("name")) if item.get("name") else None,
                    )
                )
                continue
            # 兼容嵌套格式：{"matcher": "...", "hooks": [{"command": "...", ...}]}
            nested = item.get("hooks")
            if isinstance(nested, list):
                for sub in nested:
                    if isinstance(sub, dict) and isinstance(sub.get("command"), str) and sub["command"].strip():
                        entries.append(
                            HookEntry(
                                event=event_name,
                                matcher=str(sub.get("matcher") or matcher).strip() or "*",
                                command=sub["command"].strip(),
                                timeout_seconds=_normalize_timeout(
                                    sub.get("timeout_seconds", item.get("timeout_seconds"))
                                ),
                                on_error=_normalize_on_error(sub.get("on_error", item.get("on_error"))),
                                source=source,
                                name=str(sub.get("name")) if sub.get("name") else None,
                            )
                        )
    return entries


class HookManager:
    """确定性生命周期钩子管理器：无钩子配置时零开销直接返回。"""

    def __init__(
        self,
        workspace: Path | None = None,
        *,
        entries: list[HookEntry] | None = None,
    ) -> None:
        self.workspace: Path | None = Path(workspace).resolve() if workspace is not None else None
        self._hooks: dict[str, list[HookEntry]] = {ev: [] for ev in HOOK_EVENTS}
        if entries:
            for entry in entries:
                if entry.event in HOOK_EVENTS:
                    self._hooks[entry.event].append(entry)

    @classmethod
    def from_dict(
        cls,
        config: dict[str, Any],
        *,
        workspace: Path | None = None,
        source: str = "inline",
    ) -> HookManager:
        hooks_obj = config.get("hooks") if isinstance(config, dict) else None
        if not isinstance(hooks_obj, dict) and isinstance(config, dict):
            # 也支持直接传入 {"PreToolUse": [...]}
            if any(k in HOOK_EVENTS for k in config):
                hooks_obj = config
        entries = _parse_hooks_config_entries(hooks_obj, source=source)
        return cls(workspace=workspace, entries=entries)

    @classmethod
    def from_settings(
        cls,
        state_dir: Path | None = None,
        workspace: Path | None = None,
        *,
        trust_project_settings: bool | None = None,
    ) -> HookManager:
        """从全局 `<state_dir>/settings.json` 与经信任校验的项目 `<workspace>/.agent-lab/settings.json` 加载钩子。"""
        global_cfg, project_cfg = load_settings_layers(
            state_dir=state_dir,
            workspace=workspace,
            trust_project_settings=trust_project_settings,
        )
        entries: list[HookEntry] = []
        entries.extend(_parse_hooks_config_entries(global_cfg.get("hooks"), source="global"))
        entries.extend(_parse_hooks_config_entries(project_cfg.get("hooks"), source="project"))
        return cls(workspace=workspace, entries=entries)

    def has_hooks(self, event: str | None = None) -> bool:
        """快速检查是否配置了钩子（用于零开销热路径判断）。"""
        if event is not None:
            return bool(self._hooks.get(event))
        return any(bool(v) for v in self._hooks.values())

    def register(
        self,
        event: str,
        callback: Callable[[dict[str, Any]], Any],
        matcher: str = "*",
        *,
        on_error: str = "allow",
        name: str | None = None,
    ) -> None:
        """编程式注册 Python 回调钩子（供运行时扩展与单元测试使用）。"""
        if event not in HOOK_EVENTS:
            raise ValueError(f"不支持的钩子事件 '{event}'，必须是 {sorted(HOOK_EVENTS)} 之一")
        if not callable(callback):
            raise TypeError("callback 必须是可调用对象")
        self._hooks[event].append(
            HookEntry(
                event=event,
                matcher=str(matcher or "*").strip() or "*",
                callback=callback,
                on_error=_normalize_on_error(on_error),
                source="programmatic",
                name=name or getattr(callback, "__name__", None),
            )
        )

    def register_command(
        self,
        event: str,
        command: str,
        matcher: str = "*",
        *,
        timeout_seconds: float = _DEFAULT_HOOK_TIMEOUT,
        on_error: str = "allow",
        source: str = "programmatic",
        name: str | None = None,
    ) -> None:
        """编程式注册外部命令钩子。"""
        if event not in HOOK_EVENTS:
            raise ValueError(f"不支持的钩子事件 '{event}'，必须是 {sorted(HOOK_EVENTS)} 之一")
        clean_cmd = str(command or "").strip()
        if not clean_cmd:
            raise ValueError("命令钩子的 command 不能为空")
        self._hooks[event].append(
            HookEntry(
                event=event,
                matcher=str(matcher or "*").strip() or "*",
                command=clean_cmd,
                timeout_seconds=_normalize_timeout(timeout_seconds),
                on_error=_normalize_on_error(on_error),
                source=source,
                name=name,
            )
        )

    def list_hooks(self, event: str | None = None) -> list[HookEntry]:
        if event is not None:
            return list(self._hooks.get(event, ()))
        out: list[HookEntry] = []
        for ev in sorted(HOOK_EVENTS):
            out.extend(self._hooks[ev])
        return out

    def _apply_single_result(
        self,
        event: str,
        entry: HookEntry,
        raw_res: dict[str, Any] | None,
        err: str | None,
        state: dict[str, Any],
        current_payload: dict[str, Any],
    ) -> bool:
        """将单条钩子的执行结果并入聚合状态；返回 True 表示应立即短路停止后续钩子。"""
        state["hooks_run"] += 1

        if err is not None:
            state["errors"].append(err)
            if entry.on_error == "deny":
                if event == "PreToolUse":
                    state["decision"] = "deny"
                    state["explicit_decision"] = "deny"
                    state["reason"] = f"钩子执行失败并按 on_error=deny 拦截：{err}"
                    return True
                if event == "Stop":
                    state["veto"] = True
                    state["reason"] = f"Stop 钩子执行失败并按 on_error=deny 否决：{err}"
                    return True
                if event == "UserPromptSubmit":
                    state["decision"] = "deny"
                    state["reason"] = f"UserPromptSubmit 钩子执行失败并按 on_error=deny 拦截：{err}"
                    return True
            return False

        if not isinstance(raw_res, dict):
            return False

        if event == "PreToolUse":
            upd = raw_res.get("updated_input")
            if isinstance(upd, dict):
                merged = dict(upd)
                state["updated_input"] = merged
                current_payload["tool_input"] = merged
            dec_raw = raw_res.get("decision")
            if isinstance(dec_raw, str):
                dec = dec_raw.strip().lower()
                if dec == "deny":
                    state["decision"] = "deny"
                    state["explicit_decision"] = "deny"
                    state["reason"] = str(raw_res.get("reason") or "被 PreToolUse 钩子拒绝")
                    return True
                if dec == "ask":
                    state["decision"] = "ask"
                    state["explicit_decision"] = "ask"
                    if raw_res.get("reason"):
                        state["reason"] = str(raw_res["reason"])
                elif dec == "allow" and state["explicit_decision"] is None:
                    state["explicit_decision"] = "allow"
                    if raw_res.get("reason") and not state["reason"]:
                        state["reason"] = str(raw_res["reason"])
            return False

        if event == "PostToolUse":
            ctx = raw_res.get("additional_context")
            if isinstance(ctx, str) and ctx.strip():
                state["_ctx_parts"].append(ctx.strip())
            diags = raw_res.get("diagnostics")
            if isinstance(diags, list):
                state["diagnostics"].extend(diags)
            return False

        if event == "Stop":
            if bool(raw_res.get("veto")):
                state["veto"] = True
                state["reason"] = str(raw_res.get("reason") or "被 Stop 钩子否决结束")
                return True
            return False

        if event == "UserPromptSubmit":
            ctx = raw_res.get("additional_context")
            if isinstance(ctx, str) and ctx.strip():
                state["_ctx_parts"].append(ctx.strip())
            if isinstance(raw_res.get("updated_prompt"), str):
                state["updated_prompt"] = raw_res["updated_prompt"]
                current_payload["prompt"] = raw_res["updated_prompt"]
            dec_raw = raw_res.get("decision")
            if isinstance(dec_raw, str) and dec_raw.strip().lower() == "deny":
                state["decision"] = "deny"
                state["reason"] = str(raw_res.get("reason") or "被 UserPromptSubmit 钩子拒绝")
                return True
            return False

        # SessionStart / PreCompact / Notification
        ctx = raw_res.get("additional_context")
        if isinstance(ctx, str) and ctx.strip():
            state["_ctx_parts"].append(ctx.strip())
        state["results"].append(raw_res)
        return False

    def _init_aggregation_state(self, event: str) -> dict[str, Any]:
        state = _default_hook_response(event)
        if event in {"PostToolUse", "UserPromptSubmit", "SessionStart", "PreCompact", "Notification"}:
            state["_ctx_parts"] = []
        return state

    @staticmethod
    def _finalize_aggregation_state(state: dict[str, Any]) -> dict[str, Any]:
        parts = state.pop("_ctx_parts", None)
        if parts is not None:
            state["additional_context"] = "\n".join(parts)
        return state

    def run(self, event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """同步触发指定生命周期事件的全部匹配钩子；未配置钩子时零开销直接返回。"""
        if event not in HOOK_EVENTS:
            raise ValueError(f"不支持的钩子事件 '{event}'")
        entries = self._hooks.get(event)
        if not entries:
            return _default_hook_response(event)

        current_payload: dict[str, Any] = dict(payload or {})
        current_payload.setdefault("event", event)
        target = _extract_matcher_target(event, current_payload)
        state = self._init_aggregation_state(event)

        for entry in entries:
            if not match_hook_matcher(entry.matcher, target):
                continue
            raw_res: dict[str, Any] | None = None
            err: str | None = None
            if entry.callback is not None:
                try:
                    out = entry.callback(dict(current_payload))
                    if inspect.isawaitable(out):
                        # 在同步入口遇到协程时安全提示错误而非静默丢弃
                        if inspect.iscoroutine(out):
                            out.close()
                        err = "同步 HookManager.run() 无法直接执行 async 回调，请使用 arun()"
                    elif isinstance(out, dict):
                        raw_res = out
                    elif out is None:
                        raw_res = {}
                    else:
                        err = f"钩子回调返回值必须是 dict 或 None，实际为 {type(out).__name__}"
                except Exception as exc:
                    err = f"钩子回调抛出异常: {exc}"
            elif entry.command is not None:
                raw_res, err = _execute_command_hook(entry, current_payload, workspace=self.workspace)

            if self._apply_single_result(event, entry, raw_res, err, state, current_payload):
                break

        return self._finalize_aggregation_state(state)

    async def arun(self, event: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        """异步触发指定生命周期事件的全部匹配钩子（同时支持同步/异步回调与线程卸载命令钩子）。"""
        if event not in HOOK_EVENTS:
            raise ValueError(f"不支持的钩子事件 '{event}'")
        entries = self._hooks.get(event)
        if not entries:
            return _default_hook_response(event)

        current_payload: dict[str, Any] = dict(payload or {})
        current_payload.setdefault("event", event)
        target = _extract_matcher_target(event, current_payload)
        state = self._init_aggregation_state(event)

        for entry in entries:
            if not match_hook_matcher(entry.matcher, target):
                continue
            raw_res: dict[str, Any] | None = None
            err: str | None = None
            if entry.callback is not None:
                try:
                    out = entry.callback(dict(current_payload))
                    if inspect.isawaitable(out):
                        out = await out
                    if isinstance(out, dict):
                        raw_res = out
                    elif out is None:
                        raw_res = {}
                    else:
                        err = f"钩子回调返回值必须是 dict 或 None，实际为 {type(out).__name__}"
                except Exception as exc:
                    err = f"钩子回调抛出异常: {exc}"
            elif entry.command is not None:
                raw_res, err = await asyncio.to_thread(
                    _execute_command_hook, entry, dict(current_payload), workspace=self.workspace
                )

            if self._apply_single_result(event, entry, raw_res, err, state, current_payload):
                break

        return self._finalize_aggregation_state(state)

    def run_pre_tool_use(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        **extra: Any,
    ) -> dict[str, Any]:
        if not self._hooks["PreToolUse"]:
            return _default_hook_response("PreToolUse")
        payload = {"event": "PreToolUse", "tool_name": tool_name, "tool_input": dict(tool_input), **extra}
        return self.run("PreToolUse", payload)

    async def arun_pre_tool_use(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        **extra: Any,
    ) -> dict[str, Any]:
        if not self._hooks["PreToolUse"]:
            return _default_hook_response("PreToolUse")
        payload = {"event": "PreToolUse", "tool_name": tool_name, "tool_input": dict(tool_input), **extra}
        return await self.arun("PreToolUse", payload)

    def run_post_tool_use(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_result: dict[str, Any],
        **extra: Any,
    ) -> dict[str, Any]:
        if not self._hooks["PostToolUse"]:
            return _default_hook_response("PostToolUse")
        payload = {
            "event": "PostToolUse",
            "tool_name": tool_name,
            "tool_input": dict(tool_input),
            "tool_result": tool_result,
            **extra,
        }
        return self.run("PostToolUse", payload)

    async def arun_post_tool_use(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        tool_result: dict[str, Any],
        **extra: Any,
    ) -> dict[str, Any]:
        if not self._hooks["PostToolUse"]:
            return _default_hook_response("PostToolUse")
        payload = {
            "event": "PostToolUse",
            "tool_name": tool_name,
            "tool_input": dict(tool_input),
            "tool_result": tool_result,
            **extra,
        }
        return await self.arun("PostToolUse", payload)

    def run_stop(self, payload: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
        if not self._hooks["Stop"]:
            return _default_hook_response("Stop")
        merged = {"event": "Stop", **(payload or {}), **extra}
        return self.run("Stop", merged)

    async def arun_stop(self, payload: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
        if not self._hooks["Stop"]:
            return _default_hook_response("Stop")
        merged = {"event": "Stop", **(payload or {}), **extra}
        return await self.arun("Stop", merged)
