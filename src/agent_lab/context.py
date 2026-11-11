"""按协议完整性裁剪对话，而不是对 JSON 做任意字符串截断。"""

from __future__ import annotations

from copy import deepcopy
import json


def _blocks(message: dict, kind: str) -> list[dict]:
    content = message.get("content", [])
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict) and block.get("type") == kind]


def _size(messages: list[dict]) -> int:
    # 这是序列化字符预算，便于离线确定性测试。中文、英文、工具 schema 的真实
    # token 数不同，最终 token 使用量必须以模型服务返回的 usage 为准。
    return len(json.dumps(messages, ensure_ascii=False, separators=(",", ":")))


class ContextManager:
    """保留最初任务与最近信息，整体删除旧的工具调用/结果消息对。

    一条 assistant 消息可能同时请求多个工具；紧随其后的 user 消息必须含有所有
    对应 tool_result。它们组成一个原子组，绝不只留下结果或只留下调用。
    这里实现的是可审计的历史截断，并未调用 LLM 生成语义摘要。持久化完整历史
    由 Storage 负责；build 不会修改输入，故运行记录与裁剪后的推理上下文分离。
    """

    def __init__(self, max_chars: int = 24_000):
        if isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 1:
            raise ValueError("max_chars 必须是正整数")
        self.max_chars = max_chars
        self._stats: dict = {}

    def _atomic_groups(self, messages: list[dict]) -> list[list[dict]]:
        groups: list[list[dict]] = []
        used_ids: set[str] = set()
        index = 0
        while index < len(messages):
            message = messages[index]
            if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
                raise ValueError("上下文消息只能使用 user/assistant 角色")
            if _blocks(message, "tool_result"):
                raise ValueError("发现孤立 tool_result：缺少紧邻的 assistant tool_use")
            calls = _blocks(message, "tool_use")
            if not calls:
                groups.append([message])
                index += 1
                continue
            if message["role"] != "assistant":
                raise ValueError("tool_use 必须属于 assistant 消息")
            ids = [call.get("id") for call in calls]
            if (
                any(not isinstance(call_id, str) or not call_id for call_id in ids)
                or len(set(ids)) != len(ids)
                or used_ids.intersection(ids)
            ):
                raise ValueError("tool_use id 必须非空且在对话中唯一")
            used_ids.update(ids)
            if index + 1 >= len(messages):
                raise ValueError("tool_use 尚无结果，不能构建下一轮模型上下文")
            following = messages[index + 1]
            if not isinstance(following, dict) or following.get("role") != "user":
                raise ValueError("tool_use 后必须紧跟 user tool_result 消息")
            results = _blocks(following, "tool_result")
            result_ids = [result.get("tool_use_id") for result in results]
            if (
                any(not isinstance(result_id, str) or not result_id for result_id in result_ids)
                or len(result_ids) != len(ids)
                or set(result_ids) != set(ids)
            ):
                raise ValueError("tool_result 必须与本轮所有 tool_use 一一对应")
            if _blocks(following, "tool_use"):
                raise ValueError("user 消息不能包含 tool_use")
            groups.append([message, following])
            index += 2
        return groups

    @staticmethod
    def _with_notice(first: dict, dropped: int) -> dict:
        if not dropped:
            return deepcopy(first)
        first = deepcopy(first)
        notice = (
            f"\n\n[上下文管理：已有 {dropped} 条较早消息按完整工具往返移除。"
            "完整记录仍在事件日志中；不能假定被移除的细节，必要时重新读取证据。]"
        )
        if isinstance(first.get("content"), str):
            first["content"] += notice
        elif isinstance(first.get("content"), list):
            first["content"].append({"type": "text", "text": notice})
        else:
            raise ValueError("消息 content 必须是字符串或内容块列表")
        return first

    def build(self, messages: list[dict]) -> list[dict]:
        """返回预算内的消息副本；原任务与最新原子组太大时显式失败。

        不能悄悄截断最近工具返回：那会把 JSON、源码或证据切坏，导致模型依据
        不完整数据行动。调用方应缩小工具输出或提高预算，然后恢复运行。
        """
        if not messages:
            self._stats = {
                "original_messages": 0,
                "kept_messages": 0,
                "dropped_messages": 0,
                "original_chars": 2,
                "kept_chars": 2,
            }
            return []
        copied = deepcopy(messages)
        if not isinstance(copied[0], dict) or copied[0].get("role") != "user":
            raise ValueError("上下文必须从 user 任务消息开始")
        groups = self._atomic_groups(copied)
        # 首条任务始终固定；其余按原子组构造最近后缀。
        if len(groups[0]) != 1:
            raise ValueError("首条消息必须是普通 user 任务，不能是工具结果")
        original_size = _size(copied)
        tail = groups[1:]
        removed = 0
        while True:
            first = self._with_notice(copied[0], removed)
            candidate = [first] + [message for group in tail for message in group]
            if _size(candidate) <= self.max_chars:
                self._stats = {
                    "original_messages": len(messages),
                    "kept_messages": len(candidate),
                    "dropped_messages": removed,
                    "original_chars": original_size,
                    "kept_chars": _size(candidate),
                    "budget_kind": "serialized_characters_not_tokens",
                }
                return candidate
            # 最新一组不能删除；只剩下原任务时同样不能再缩小。
            if len(tail) <= 1:
                raise ValueError("原任务与最新完整消息组超过上下文字符预算")
            removed += len(tail.pop(0))

    def stats(self) -> dict:
        return dict(self._stats)
