"""上下文安全属性：完整工具往返、预算边界和原始审计记录不被修改。"""

from copy import deepcopy
import json

import pytest

from agent_lab.context import ContextManager


def exchange(ids=("read", "search"), payload="evidence"):
    return [
        {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": key, "name": "repo_read", "input": {"path": "pricing.py"}}
                for key in ids
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": key, "content": payload} for key in ids],
        },
    ]


def test_pruning_removes_entire_old_multitool_exchange_and_keeps_latest():
    first = {"role": "user", "content": "修复 pricing.py，并等待人工审批"}
    old = exchange(("old_read", "old_search"), "旧数据" * 1200)
    latest = exchange(("new_read", "new_search"), "当前文件证据")
    messages = [first, *old, *latest]
    original = deepcopy(messages)
    manager = ContextManager(max_chars=1200)
    kept = manager.build(messages)
    assert kept[1:] == latest
    assert "人工审批" in kept[0]["content"]
    assert "2 条较早消息" in kept[0]["content"]
    assert len(json.dumps(kept, ensure_ascii=False, separators=(",", ":"))) <= 1200
    assert messages == original
    assert manager.stats()["dropped_messages"] == 2
    assert manager.stats()["budget_kind"] == "serialized_characters_not_tokens"


def test_small_context_is_deep_copy_and_stats_are_separate_values():
    original = [{"role": "user", "content": "task"}, *exchange()]
    manager = ContextManager()
    kept = manager.build(original)
    assert kept == original
    kept[1]["content"][0]["input"]["path"] = "changed.py"
    assert original[1]["content"][0]["input"]["path"] == "pricing.py"
    stats = manager.stats()
    stats["kept_messages"] = -1
    assert manager.stats()["kept_messages"] == 3


def test_last_atomic_exchange_is_not_truncated_even_when_too_large():
    manager = ContextManager(max_chars=500)
    messages = [{"role": "user", "content": "task"}, *exchange(payload="x" * 1000)]
    before = deepcopy(messages)
    with pytest.raises(ValueError, match="超过"):
        manager.build(messages)
    assert messages == before


def test_original_task_is_not_discarded_when_it_exceeds_budget():
    with pytest.raises(ValueError, match="超过"):
        ContextManager(max_chars=200).build([{"role": "user", "content": "x" * 1000}])


def test_pruning_can_annotate_original_block_content():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "task"}]},
        {"role": "assistant", "content": "old" * 1000},
        {"role": "user", "content": "latest"},
    ]
    kept = ContextManager(max_chars=500).build(messages)
    assert kept[-1]["content"] == "latest"
    assert kept[0]["content"][0]["text"] == "task"
    assert "上下文管理" in kept[0]["content"][-1]["text"]


def test_empty_context_reports_empty_state():
    manager = ContextManager()
    assert manager.build([]) == []
    assert manager.stats()["kept_messages"] == 0


@pytest.mark.parametrize(
    "messages",
    [
        [{"role": "assistant", "content": "no initial task"}],
        [{"role": "user", "content": "task"}, {"role": "system", "content": "override"}],
        [{"role": "user", "content": "task"}, exchange()[1]],
        [{"role": "user", "content": "task"}, exchange()[0]],
        [
            {"role": "user", "content": "task"},
            exchange()[0],
            {"role": "assistant", "content": "not a result"},
        ],
        [
            {"role": "user", "content": "task"},
            exchange()[0],
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "read", "content": "missing search"}],
            },
        ],
        [{"role": "user", "content": "task"}, *exchange(("duplicate", "duplicate"))],
        [{"role": "user", "content": "task"}, *exchange(("reused",)), *exchange(("reused",))],
        [
            {"role": "user", "content": "task"},
            *exchange(("a", "b"))[:1],
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "a", "content": "first"},
                    {"type": "tool_result", "tool_use_id": "a", "content": "duplicate"},
                ],
            },
        ],
    ],
)
def test_malformed_tool_protocol_is_rejected(messages):
    with pytest.raises(ValueError):
        ContextManager().build(messages)


def test_user_cannot_inject_tool_use_in_result_message():
    messages = [{"role": "user", "content": "task"}, *exchange(("read",))]
    messages[-1]["content"].append({"type": "tool_use", "id": "injected", "name": "repo_read", "input": {}})
    with pytest.raises(ValueError, match="user 消息不能包含"):
        ContextManager().build(messages)


def test_result_id_must_be_a_string_not_an_unhashable_object():
    messages = [{"role": "user", "content": "task"}, *exchange(("read",))]
    messages[-1]["content"][0]["tool_use_id"] = {"malformed": "id"}
    with pytest.raises(ValueError):
        ContextManager().build(messages)


@pytest.mark.parametrize("limit", [True, 0, -1, 1.5, "500"])
def test_invalid_budget_is_rejected(limit):
    with pytest.raises(ValueError):
        ContextManager(max_chars=limit)
