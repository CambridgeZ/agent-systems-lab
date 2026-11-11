"""模型适配层：运行时只处理标准化 content / usage。

DemoProvider 是确定性脚本，用来复现状态机和故障，不能代表 LLM 能力。
AnthropicProvider 由真实模型决定下一步工具。两者共享同一工具执行、审批和恢复路径。
"""

from __future__ import annotations

import json
import os
import uuid
from typing import Protocol


class Provider(Protocol):
    async def complete(
        self, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 2048
    ) -> dict: ...


def _call(name: str, **arguments: object) -> dict:
    return {"type": "tool_use", "id": "call_" + uuid.uuid4().hex[:16], "name": name, "input": arguments}


def _response(content: list[dict]) -> dict:
    return {
        "content": content,
        "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in content) else "end_turn",
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


class DemoProvider:
    """只演示内置 pricing.py 百分比 bug；输入其他任务时明确说明边界。"""

    async def complete(
        self, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 2048
    ) -> dict:
        results: list[dict] = []
        for message in messages:
            if not isinstance(message.get("content"), list):
                continue
            for block in message["content"]:
                if block.get("type") == "tool_result":
                    try:
                        results.append(json.loads(block["content"]))
                    except (ValueError, TypeError):
                        pass
        if "ROLE:reviewer" in system or "ROLE:tester" in system:
            if not results:
                return _response([_call("repo_read", path="pricing.py")])
            role = (
                "测试建议：检查 0%、10%、100% 折扣及非法参数"
                if "ROLE:tester" in system
                else ("代码分析：discount_percent 是百分数，应先除以 100；修改范围限于返回表达式")
            )
            return _response([{"type": "text", "text": role}])
        original = str(messages[0].get("content", ""))
        if not any(term in original.lower() for term in ("折扣", "discount", "pricing", "百分比")):
            return _response(
                [
                    {
                        "type": "text",
                        "text": "离线 demo 仅支持内置 pricing.py 折扣任务。"
                        "请尝试：修复 pricing.py 的百分比折扣 bug，并验证修改。任意编码任务需真实模型。",
                    }
                ]
            )
        # 只识别运行时在后续独立 user 消息中写入的审批标记。原始任务和工具正文
        # 即使包含同样字符串，也不能被 demo 当作审批（真正写入仍由工具策略保护）。
        decisions = [
            m.get("content", "")
            for m in messages[1:]
            if m.get("role") == "user" and isinstance(m.get("content"), str)
        ]
        if any(text.startswith("REVIEW_REJECTED: ") for text in decisions):
            return _response([{"type": "text", "text": "补丁已被用户拒绝，工作区保持原状。本次任务停止。"}])
        if any(text.startswith("REVIEW_APPROVED: ") for text in decisions):
            if not any("valid" in result or "syntax_ok" in result for result in results):
                return _response([_call("verify_python", path="pricing.py"), _call("sandbox_test")])
            sandbox = next(
                (
                    r
                    for r in results
                    if r.get("code")
                    in {"passed", "sandbox_unavailable", "sandbox_timeout", "sandbox_error", "tests_failed"}
                ),
                {},
            )
            citations = [
                m.get("chunk_id", m.get("id"))
                for r in results
                for m in r.get("matches", [])
                if m.get("chunk_id") or m.get("id")
            ]
            evidence = f" 知识参考 [{citations[0]}]。" if citations else ""
            return _response(
                [
                    {
                        "type": "text",
                        "text": "已修复 pricing.py：百分比先除以 100，再计算折扣。"
                        "补丁经人工审批后应用；AST 语法检查通过。容器测试状态："
                        + sandbox.get("code", "未返回结果")
                        + "。"
                        "若状态不是 passed，不能宣称单元测试通过。" + evidence,
                    }
                ]
            )
        if not any("content" in result and result.get("path") == "pricing.py" for result in results):
            return _response(
                [
                    _call("repo_list"),
                    _call("repo_read", path="pricing.py"),
                    _call("knowledge_search", query="coding agent discount verification 安全 审批"),
                ]
            )
        read = next(
            result for result in results if result.get("path") == "pricing.py" and "content" in result
        )
        if "return subtotal * (1 - discount_percent)" not in read["content"]:
            return _response([{"type": "text", "text": "示例中的原始错误表达式已不存在，无需重复应用补丁。"}])
        if not any(result.get("requires_approval") for result in results):
            return _response(
                [
                    _call(
                        "propose_patch",
                        path="pricing.py",
                        old_text="return subtotal * (1 - discount_percent)",
                        new_text="return subtotal * (1 - discount_percent / 100)",
                    )
                ]
            )
        return _response([{"type": "text", "text": "等待你审批补丁。"}])


class AnthropicProvider:
    """SDK 做有限网络重试和请求超时；凭证不进入 prompt / SQLite / 事件。"""

    def __init__(self) -> None:
        from anthropic import AsyncAnthropic

        model = os.environ.get("ANTHROPIC_MODEL", "").strip()
        if not os.environ.get("ANTHROPIC_API_KEY") or not model:
            raise ValueError("真实模式需要 ANTHROPIC_API_KEY 与 ANTHROPIC_MODEL")
        self.model = model
        self.client = AsyncAnthropic(max_retries=2, timeout=45.0, base_url="https://api.anthropic.com")

    async def aclose(self) -> None:
        await self.client.close()

    async def complete(
        self, system: str, messages: list[dict], tools: list[dict], max_tokens: int = 2048
    ) -> dict:
        response = await self.client.messages.create(
            model=self.model, system=system, messages=messages, tools=tools, max_tokens=max_tokens
        )
        content = [block.model_dump(exclude_none=True) for block in response.content]
        if any(block["type"] not in {"text", "tool_use"} for block in content):
            raise ValueError("模型返回当前适配器不支持的内容块")
        return {
            "content": content,
            "stop_reason": response.stop_reason,
            "usage": {
                "input_tokens": response.usage.input_tokens,
                "output_tokens": response.usage.output_tokens,
            },
        }


def get_provider(name: str) -> Provider:
    if name == "demo":
        return DemoProvider()
    if name == "anthropic":
        return AnthropicProvider()
    raise ValueError(f"未知 provider: {name}")
