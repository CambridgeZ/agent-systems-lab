"""可组合 Harness 回合与工具中间件流水线（TurnPipeline）：洋葱模型拦截、出站 DLP、预算守卫与 Span 遥测。

================================================================================
模块导读（Module Guide）
================================================================================
1. 架构角色（Composable Harness Middleware Pipeline）
   本模块将 Agent 主循环的横切关注点（安全检查、成本/步数熔断、分布式追踪与指标埋点）
   解耦为可自由编排、可独立测试的异步中间件流水线（Onion Model）：
   - `TurnContext` / `ToolCallContext`：显式承载单轮回合与单次工具调用的可变执行上下文、
     短路标志（`aborted` / `blocked`）与遥测元数据；
   - `TurnMiddleware` / `TurnMiddlewareProtocol`：定义 `before_turn`、`before_tool`、
     `after_tool`、`after_turn` 四个标准异步生命周期钩子；
   - `TurnPipeline`：严格按照洋葱模型调度中间件链——前置钩子（`before_turn` / `before_tool`）
     按注册正序执行，一旦命中 `ctx.aborted` 或 `tctx.blocked` 立即短路后续中间件与主体执行；
     后置钩子（`after_tool` / `after_turn`）按已进入中间件的严格逆序展开，确保资源与追踪
     Span 永远 LIFO 对称关闭；
   - 内置三类可复用中间件阶段：
     * `DlpSecurityMiddleware`：调用 `EgressDlpInspector` 执行出站凭据防泄漏与 CaMeL 严格
       数据流门控，并在 `after_tool` 自动登记不可信外部数据血缘污点；
     * `BudgetGuardMiddleware`：在回合前/后强制执行最大步数（`max_steps`）与美元成本上限
       （`max_cost_usd`）熔断；
     * `ObservabilitySpanMiddleware`：通过 `start_span` 维护嵌套 W3C Trace Span（回合 Span
       包裹工具 Span），并在 `after_tool` 通过 `METRICS.record_tool_metric` 上报调用计数与耗时。

2. 核心工程与安全不变量（Core Invariants）
   - 洋葱对称退栈不变量：前置阶段仅将已完成进入的中间件压入退栈队列，后置阶段按逆序全部执行；
     即使某层后置钩子抛出异常，外层中间件（尤其是 `ObservabilitySpanMiddleware`）仍保证完成
     退栈清理，杜绝 `ContextVar` Span 栈泄漏；
   - 安全短路零穿透不变量：一旦 `DlpSecurityMiddleware` 置位 `tctx.blocked = True` 或
     `BudgetGuardMiddleware` 置位 `ctx.aborted = True`，后续前置钩子与目标工具/回合体绝不被调用；
   - 上下文纯 JSON 可序列化不变量：活跃 Span 上下文管理器与退栈状态仅保存在中间件/流水线内部
     映射表中，绝不向 `ctx.metadata` 或 `tctx.metadata` 写入不可序列化对象或原始凭据。
================================================================================
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
import inspect
import math
from typing import Any, Protocol, runtime_checkable
import weakref

from .egress_dlp import (
    DEFAULT_EGRESS_DLP_INSPECTOR,
    EgressDlpInspector,
    TaintRegistry,
    infer_tool_provenance,
)
from .observability import METRICS, MetricsRegistry, start_span

_MAX_TRACKED_CONTEXTS = 1024


# ---------------------------------------------------------------------------
# 1. 回合与工具调用上下文（TurnContext / ToolCallContext）
# ---------------------------------------------------------------------------


@dataclass
class TurnContext:
    """单轮 Agent 回合执行上下文，贯穿 `before_turn -> turn_fn -> after_turn` 全生命周期。"""

    run_id: str
    step_index: int
    permission_mode: str = "ask"
    metadata: dict[str, Any] = field(default_factory=dict)
    aborted: bool = False
    abort_reason: str | None = None
    telemetry: dict[str, Any] = field(default_factory=dict)

    def abort(self, reason: str, *, code: str | None = None) -> None:
        """标记当前回合为中止状态，通知 `TurnPipeline` 立即短路后续前置中间件与主体执行。"""
        self.aborted = True
        self.abort_reason = str(reason)
        if code:
            self.metadata["abort_code"] = str(code)


@dataclass
class ToolCallContext:
    """单次工具调用执行上下文，贯穿 `before_tool -> tool_fn -> after_tool` 全生命周期。"""

    run_id: str
    call_id: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    permission_mode: str = "ask"
    result: dict[str, Any] | None = None
    blocked: bool = False
    block_code: str | None = None
    block_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def block(
        self,
        code: str,
        reason: str,
        *,
        result: dict[str, Any] | None = None,
    ) -> None:
        """阻断本次工具调用并填充结构化拦截结果，短路后续前置中间件与真实工具执行。"""
        self.blocked = True
        self.block_code = str(code)
        self.block_reason = str(reason)
        if result is not None:
            self.result = result
        elif self.result is None:
            self.result = {
                "ok": False,
                "blocked": True,
                "code": self.block_code,
                "error": self.block_reason,
            }


# ---------------------------------------------------------------------------
# 2. 中间件协议、基类与洋葱流水线调度器（TurnMiddleware / TurnPipeline）
# ---------------------------------------------------------------------------


@runtime_checkable
class TurnMiddlewareProtocol(Protocol):
    """回合/工具中间件结构化协议（支持鸭子类型与子类继承两种实现方式）。"""

    async def before_turn(self, ctx: TurnContext) -> None: ...
    async def before_tool(self, tctx: ToolCallContext) -> None: ...
    async def after_tool(self, tctx: ToolCallContext) -> None: ...
    async def after_turn(self, ctx: TurnContext) -> None: ...


class TurnMiddleware:
    """可组合 Harness 中间件基类：默认提供空操作异步钩子，子类仅需覆写关心的生命周期阶段。"""

    name: str = ""

    async def before_turn(self, ctx: TurnContext) -> None:
        return None

    async def before_tool(self, tctx: ToolCallContext) -> None:
        return None

    async def after_tool(self, tctx: ToolCallContext) -> None:
        return None

    async def after_turn(self, ctx: TurnContext) -> None:
        return None


def _store_bounded(mapping: dict[int, Any], key: int, value: Any) -> None:
    if key in mapping:
        del mapping[key]
    elif len(mapping) >= _MAX_TRACKED_CONTEXTS:
        oldest_key = next(iter(mapping))
        del mapping[oldest_key]
    mapping[key] = value


class TurnPipeline:
    """洋葱模型（Onion Model）回合与工具中间件流水线调度器。

    执行次序：
    - `before_turn` / `before_tool`：按 `middlewares` 正序依次执行；一旦检测到
      `ctx.aborted` 或 `tctx.blocked` 为真，立即停止调用后续中间件；
    - `after_tool` / `after_turn`：按本次实际进入的中间件逆序展开（若直接单独调用后置阶段，
      则按全部中间件逆序展开），保证外层追踪 Span 与资源清理严格 LIFO 对称。
    """

    def __init__(self, middlewares: Sequence[TurnMiddlewareProtocol] = ()) -> None:
        self._middlewares: tuple[TurnMiddlewareProtocol, ...] = tuple(middlewares)
        self._turn_entered: dict[int, tuple[weakref.ref[TurnContext], list[TurnMiddlewareProtocol]]] = {}
        self._tool_entered: dict[int, tuple[weakref.ref[ToolCallContext], list[TurnMiddlewareProtocol]]] = {}

    @property
    def middlewares(self) -> tuple[TurnMiddlewareProtocol, ...]:
        return self._middlewares

    async def run_before_turn(self, ctx: TurnContext) -> TurnContext:
        entered: list[TurnMiddlewareProtocol] = []
        try:
            for mw in self._middlewares:
                if ctx.aborted:
                    break
                await mw.before_turn(ctx)
                entered.append(mw)
                if ctx.aborted:
                    break
        finally:
            _store_bounded(self._turn_entered, id(ctx), (weakref.ref(ctx), entered))
        return ctx

    async def run_after_turn(self, ctx: TurnContext) -> TurnContext:
        record = self._turn_entered.pop(id(ctx), None)
        entered = record[1] if (record is not None and record[0]() is ctx) else None
        unwind_order = (
            list(reversed(entered)) if entered is not None else list(reversed(self._middlewares))
        )
        first_exc: BaseException | None = None
        for mw in unwind_order:
            try:
                await mw.after_turn(ctx)
            except BaseException as exc:
                if first_exc is None:
                    first_exc = exc
        if first_exc is not None:
            raise first_exc
        return ctx

    async def run_before_tool(self, tctx: ToolCallContext) -> ToolCallContext:
        entered: list[TurnMiddlewareProtocol] = []
        try:
            for mw in self._middlewares:
                if tctx.blocked:
                    break
                await mw.before_tool(tctx)
                entered.append(mw)
                if tctx.blocked:
                    break
        finally:
            _store_bounded(self._tool_entered, id(tctx), (weakref.ref(tctx), entered))
        return tctx

    async def run_after_tool(self, tctx: ToolCallContext) -> ToolCallContext:
        record = self._tool_entered.pop(id(tctx), None)
        entered = record[1] if (record is not None and record[0]() is tctx) else None
        unwind_order = (
            list(reversed(entered)) if entered is not None else list(reversed(self._middlewares))
        )
        first_exc: BaseException | None = None
        for mw in unwind_order:
            try:
                await mw.after_tool(tctx)
            except BaseException as exc:
                if first_exc is None:
                    first_exc = exc
        if first_exc is not None:
            raise first_exc
        return tctx

    # 语义等价简写别名
    before_turn = run_before_turn
    after_turn = run_after_turn
    before_tool = run_before_tool
    after_tool = run_after_tool

    async def execute_turn(
        self,
        ctx: TurnContext,
        turn_fn: Callable[[TurnContext], Awaitable[Any] | Any] | None = None,
    ) -> TurnContext:
        """以完整洋葱生命周期执行单个回合：`before_turn -> turn_fn（未中止时）-> after_turn`。"""
        inner_exc: BaseException | None = None
        try:
            await self.run_before_turn(ctx)
            if not ctx.aborted and turn_fn is not None:
                output = turn_fn(ctx)
                if inspect.isawaitable(output):
                    output = await output
                if output is not None:
                    ctx.metadata["turn_output"] = output
        except BaseException as exc:
            inner_exc = exc
            ctx.metadata.setdefault("error_type", type(exc).__name__)
            raise
        finally:
            try:
                await self.run_after_turn(ctx)
            except BaseException:
                if inner_exc is None:
                    raise
        return ctx

    async def execute_tool(
        self,
        tctx: ToolCallContext,
        tool_fn: Callable[[ToolCallContext], Awaitable[dict[str, Any]] | dict[str, Any]] | None = None,
    ) -> ToolCallContext:
        """以完整洋葱生命周期执行单次工具调用：`before_tool -> tool_fn（未被阻断时）-> after_tool`。"""
        inner_exc: BaseException | None = None
        try:
            await self.run_before_tool(tctx)
            if not tctx.blocked and tool_fn is not None:
                res = tool_fn(tctx)
                if inspect.isawaitable(res):
                    res = await res
                if res is not None:
                    tctx.result = res
            elif tctx.blocked and tctx.result is None:
                tctx.result = {
                    "ok": False,
                    "blocked": True,
                    "code": tctx.block_code or "blocked",
                    "error": tctx.block_reason or "Tool call blocked by middleware.",
                }
        except BaseException as exc:
            inner_exc = exc
            tctx.metadata.setdefault("error_type", type(exc).__name__)
            raise
        finally:
            try:
                await self.run_after_tool(tctx)
            except BaseException:
                if inner_exc is None:
                    raise
        return tctx


# ---------------------------------------------------------------------------
# 3. 内置可复用中间件阶段（DLP / BudgetGuard / ObservabilitySpan）
# ---------------------------------------------------------------------------


class DlpSecurityMiddleware(TurnMiddleware):
    """出站密文防泄漏（Outbound Egress DLP）与 CaMeL 严格数据流污点拦截中间件。"""

    name: str = "dlp_security"

    def __init__(
        self,
        inspector: EgressDlpInspector | None = None,
        taint_registry: TaintRegistry | None = None,
        strict_dataflow: bool = False,
        *,
        approved_hosts: Sequence[str] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.inspector: EgressDlpInspector = (
            inspector if inspector is not None else DEFAULT_EGRESS_DLP_INSPECTOR
        )
        self.taint_registry: TaintRegistry | None = taint_registry
        self.strict_dataflow: bool = bool(strict_dataflow)
        self.approved_hosts: tuple[str, ...] | None = (
            tuple(approved_hosts) if approved_hosts is not None else None
        )
        self._env: Mapping[str, str] | None = env

    def _resolve_registry(self, tctx: ToolCallContext) -> TaintRegistry | None:
        if self.taint_registry is not None:
            return self.taint_registry
        candidate = tctx.metadata.get("taint_registry")
        return candidate if isinstance(candidate, TaintRegistry) else None

    async def before_tool(self, tctx: ToolCallContext) -> None:
        registry = self._resolve_registry(tctx)
        explicitly_approved = bool(tctx.metadata.get("explicitly_approved", False))
        approved_hosts = tctx.metadata.get("approved_hosts", self.approved_hosts)
        verdict = self.inspector.inspect_outbound_call(
            tctx.tool_name,
            tctx.arguments,
            taint_registry=registry,
            strict_dataflow=self.strict_dataflow,
            approved_hosts=approved_hosts,
            explicitly_approved=explicitly_approved,
            env=self._env,
        )
        tctx.metadata["dlp_allowed"] = bool(verdict.get("allowed", True))
        if not verdict.get("allowed", True):
            code = str(verdict.get("code") or "egress_dlp_blocked")
            decision = str(verdict.get("decision") or "deny")
            reason = str(verdict.get("reason") or "工具调用被出站 DLP 安全策略拦截。")
            tctx.metadata["dlp_verdict"] = {
                "decision": decision,
                "code": code,
                "matched_rules": list(verdict.get("matched_rules") or []),
                "findings_count": len(verdict.get("findings") or ()),
            }
            blocked_result: dict[str, Any] = {
                "ok": False,
                "blocked": True,
                "decision": decision,
                "code": code,
                "error": reason,
                "matched_rules": list(verdict.get("matched_rules") or []),
                "findings": list(verdict.get("findings") or []),
            }
            if "taint_sources" in verdict:
                blocked_result["taint_sources"] = list(verdict["taint_sources"])
            tctx.block(code, reason, result=blocked_result)

    async def after_tool(self, tctx: ToolCallContext) -> None:
        if tctx.blocked:
            return
        registry = self._resolve_registry(tctx)
        if registry is None:
            return
        if isinstance(tctx.result, Mapping) and tctx.result.get("ok") is False:
            return
        prov = infer_tool_provenance(tctx.tool_name)
        tctx.metadata["provenance"] = str(prov)
        if prov.is_untrusted:
            summary_hint = (
                str(
                    tctx.arguments.get("url")
                    or tctx.arguments.get("query")
                    or tctx.arguments.get("peer")
                    or tctx.tool_name
                )
            )[:160]
            registry.record_tool_output(
                tctx.tool_name,
                call_id=tctx.call_id,
                summary=summary_hint,
                provenance=prov,
            )


def _extract_cost_usd(ctx: TurnContext) -> float:
    """从 `TurnContext.telemetry` 或 `TurnContext.metadata` 中安全读取累计美元成本。"""
    for container in (ctx.telemetry, ctx.metadata):
        for key in ("cost_usd", "total_cost_usd"):
            raw = container.get(key)
            if raw is None or isinstance(raw, bool):
                continue
            try:
                val = float(raw)
            except (TypeError, ValueError):
                continue
            if val >= 0.0:
                return val
    return 0.0


class BudgetGuardMiddleware(TurnMiddleware):
    """步数（`max_steps`）与美元费用（`max_cost_usd`）双维度预算熔断中间件。"""

    name: str = "budget_guard"

    def __init__(
        self,
        max_steps: int = 24,
        max_cost_usd: float | None = None,
    ) -> None:
        if isinstance(max_steps, bool) or int(max_steps) < 1:
            raise ValueError("max_steps 必须是大于等于 1 的整数。")
        if max_cost_usd is not None:
            if isinstance(max_cost_usd, bool):
                raise ValueError("max_cost_usd 必须为非负有限浮点数或 None。")
            parsed_cost = float(max_cost_usd)
            if not math.isfinite(parsed_cost) or parsed_cost < 0.0:
                raise ValueError("max_cost_usd 必须为非负有限浮点数或 None。")
            self.max_cost_usd: float | None = parsed_cost
        else:
            self.max_cost_usd = None
        self.max_steps: int = int(max_steps)

    async def before_turn(self, ctx: TurnContext) -> None:
        current_cost = _extract_cost_usd(ctx)
        if ctx.step_index >= self.max_steps:
            reason = f"达到最大步数预算上限 (step_index={ctx.step_index} >= max_steps={self.max_steps})"
            ctx.telemetry["budget_guard"] = {
                "status": "aborted",
                "code": "max_steps_exceeded",
                "step_index": ctx.step_index,
                "max_steps": self.max_steps,
                "cost_usd": current_cost,
                "max_cost_usd": self.max_cost_usd,
            }
            ctx.abort(reason, code="max_steps_exceeded")
            return

        if self.max_cost_usd is not None and current_cost >= self.max_cost_usd:
            reason = (
                f"达到美元成本预算上限 (cost_usd={current_cost:.6f} >= max_cost_usd={self.max_cost_usd:.6f})"
            )
            ctx.telemetry["budget_guard"] = {
                "status": "aborted",
                "code": "max_cost_exceeded",
                "step_index": ctx.step_index,
                "max_steps": self.max_steps,
                "cost_usd": current_cost,
                "max_cost_usd": self.max_cost_usd,
            }
            ctx.abort(reason, code="max_cost_exceeded")
            return

        ctx.telemetry["budget_guard"] = {
            "status": "ok",
            "step_index": ctx.step_index,
            "max_steps": self.max_steps,
            "cost_usd": current_cost,
            "max_cost_usd": self.max_cost_usd,
        }

    async def after_turn(self, ctx: TurnContext) -> None:
        if ctx.aborted or self.max_cost_usd is None:
            return
        current_cost = _extract_cost_usd(ctx)
        if current_cost > self.max_cost_usd:
            reason = (
                f"响应计量后超出美元成本预算上限 (cost_usd={current_cost:.6f} > max_cost_usd={self.max_cost_usd:.6f})"
            )
            ctx.telemetry["budget_guard"] = {
                "status": "aborted",
                "code": "max_cost_exceeded",
                "step_index": ctx.step_index,
                "max_steps": self.max_steps,
                "cost_usd": current_cost,
                "max_cost_usd": self.max_cost_usd,
            }
            ctx.abort(reason, code="max_cost_exceeded")


def _infer_tool_metric_category(tool_name: str) -> str:
    lower = str(tool_name or "").strip().lower()
    if lower in {"bash_exec", "bash_write", "bash_kill", "bash_output", "bash_list"}:
        return "bash_exec"
    if lower.startswith("mcp__") or lower.startswith("mcp_"):
        return "mcp"
    if "subagent" in lower:
        return "subagent"
    if "sandbox" in lower:
        return "sandbox"
    return "tool"


class ObservabilitySpanMiddleware(TurnMiddleware):
    """W3C 分布式追踪 Span（`start_span`）与 Prometheus 工具指标（`METRICS.record_tool_metric`）中间件。"""

    name: str = "observability_span"

    def __init__(self, *, metrics: MetricsRegistry | None = None) -> None:
        self._metrics: MetricsRegistry = metrics if metrics is not None else METRICS
        self._active_turn_spans: dict[int, tuple[weakref.ref[TurnContext], Any, Any]] = {}
        self._active_tool_spans: dict[int, tuple[weakref.ref[ToolCallContext], Any, Any]] = {}

    async def before_turn(self, ctx: TurnContext) -> None:
        cm = start_span(
            "agent.turn",
            attributes={
                "run_id": ctx.run_id,
                "step_index": ctx.step_index,
                "permission_mode": ctx.permission_mode,
            },
        )
        span = cm.__enter__()
        _store_bounded(self._active_turn_spans, id(ctx), (weakref.ref(ctx), cm, span))
        ctx.telemetry["trace_id"] = span.trace_id
        ctx.telemetry["turn_span_id"] = span.span_id
        if span.parent_span_id:
            ctx.telemetry["turn_parent_span_id"] = span.parent_span_id

    async def after_turn(self, ctx: TurnContext) -> None:
        entry = self._active_turn_spans.pop(id(ctx), None)
        if entry is None or entry[0]() is not ctx:
            return
        _, cm, span = entry
        span.set_attribute("aborted", bool(ctx.aborted))
        if ctx.aborted and ctx.abort_reason:
            span.set_attribute("abort_reason", ctx.abort_reason)
        err_type = ctx.metadata.get("error_type")
        if err_type:
            span["status"] = "error"
            span.set_attribute("error.type", str(err_type))
        cm.__exit__(None, None, None)
        ctx.telemetry["turn_duration_ms"] = span.duration_ms
        ctx.telemetry["turn_span"] = dict(span)

    async def before_tool(self, tctx: ToolCallContext) -> None:
        cm = start_span(
            f"agent.tool.{tctx.tool_name}",
            attributes={
                "run_id": tctx.run_id,
                "call_id": tctx.call_id,
                "tool_name": tctx.tool_name,
                "permission_mode": tctx.permission_mode,
            },
        )
        span = cm.__enter__()
        _store_bounded(self._active_tool_spans, id(tctx), (weakref.ref(tctx), cm, span))
        tctx.metadata["trace_id"] = span.trace_id
        tctx.metadata["span_id"] = span.span_id
        if span.parent_span_id:
            tctx.metadata["parent_span_id"] = span.parent_span_id

    async def after_tool(self, tctx: ToolCallContext) -> None:
        err_type = tctx.metadata.get("error_type")
        if tctx.blocked or err_type:
            ok = False
        elif isinstance(tctx.result, Mapping):
            ok = bool(tctx.result.get("ok", True))
        else:
            ok = tctx.result is not None

        entry = self._active_tool_spans.pop(id(tctx), None)
        duration_ms = 0.0
        if entry is not None and entry[0]() is tctx:
            _, cm, span = entry
            span.set_attribute("blocked", bool(tctx.blocked))
            if tctx.block_code:
                span.set_attribute("block_code", tctx.block_code)
            span.set_attribute("ok", ok)
            if err_type:
                span.set_attribute("error.type", str(err_type))
            if not ok:
                span["status"] = "error"
            cm.__exit__(None, None, None)
            duration_ms = span.duration_ms
            tctx.metadata["duration_ms"] = duration_ms
            tctx.metadata["span"] = dict(span)

        category = _infer_tool_metric_category(tctx.tool_name)
        self._metrics.record_tool_metric(tctx.tool_name, duration_ms, ok=ok, category=category)


__all__ = [
    "BudgetGuardMiddleware",
    "DlpSecurityMiddleware",
    "ObservabilitySpanMiddleware",
    "ToolCallContext",
    "TurnContext",
    "TurnMiddleware",
    "TurnMiddlewareProtocol",
    "TurnPipeline",
]
