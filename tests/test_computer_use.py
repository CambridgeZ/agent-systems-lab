"""桌面级 GUI 操控（Computer Use）、权限审批、多模态截图注入与产物回收测试。"""

from __future__ import annotations

import json
import platform
import uuid

import httpx

from agent_lab.api import ROOT
from agent_lab.computer import MacOSNativeBackend, MockComputerBackend
from agent_lab.providers import AnthropicProvider, OpenAICompatibleProvider
from agent_lab.runtime import AgentRuntime
from agent_lab.storage import Storage


def _call(name: str, **kwargs):
    return {"type": "tool_use", "id": "gui_" + uuid.uuid4().hex[:12], "name": name, "input": kwargs}


def _resp(blocks: list[dict]):
    return {
        "content": blocks,
        "stop_reason": "tool_use" if any(b["type"] == "tool_use" for b in blocks) else "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


async def test_computer_use_screenshot_approval_and_vision_attachment_flow(tmp_path):
    ws = tmp_path / "desk_ws"
    ws.mkdir()
    storage = Storage(tmp_path / "state.db")
    mock_gui = MockComputerBackend(width=1440, height=900)
    captured_vision: list[list[dict]] = []

    class VisionAwareMockProvider:
        supports_vision = True

        def __init__(self):
            self.turn = 0

        async def complete(self, system, messages, tools, max_tokens=2048, *, vision_attachments=None):
            self.turn += 1
            captured_vision.append(vision_attachments or [])
            if self.turn == 1:
                return _resp([_call("computer_screen", action="screenshot")])
            if self.turn == 2:
                # 验证上一步截图已自动作为多模态附件传入本轮请求，而持久化历史消息不含巨型 base64
                assert vision_attachments and len(vision_attachments) == 1
                assert vision_attachments[0]["media_type"] == "image/png"
                assert len(vision_attachments[0]["base64_data"]) > 40
                return _resp([_call("computer_mouse", action="click", x=320, y=180)])
            if self.turn == 3:
                return _resp([_call("computer_keyboard", action="type", text="hello world")])
            return _resp([{"type": "text", "text": "已完成屏幕观察、点击与键盘输入。"}])

    # 1. auto-edit 模式下：截屏自动放行，鼠标点击与键盘输入触发 gui_action 审批
    runtime = AgentRuntime(
        storage,
        ws,
        ROOT / "data/knowledge",
        ROOT / "skills",
        provider=VisionAwareMockProvider(),
        execution_profile="computer",
        permission_mode="auto-edit",
        computer_backend=mock_gui,
    )
    created = runtime.create("截取桌面并点击输入框输入文字", extra_metadata={"direct_workspace": True})
    paused_click = await runtime.execute(created["id"])
    assert paused_click["status"] == "waiting_approval"

    # SQLite 中持久化的历史消息保持轻量文本，不塞入原始 PNG base64
    stored_run = storage.get_run(created["id"])
    assert "iVBORw0KGgo" not in json.dumps(stored_run["messages"])

    click_pid = paused_click["metadata"]["waiting_proposal_ids"][0]
    prop_click = storage.get_proposal(click_pid)
    assert prop_click["kind"] == "gui_action"
    assert prop_click["tool"] == "computer_mouse"

    # 切换为 full-auto 后批准点击，后续的 computer_keyboard 将无需再次暂停直接自动完成
    runtime.set_permission_mode(created["id"], "full-auto")
    completed = await runtime.approve(created["id"], click_pid, True)
    assert completed["status"] == "completed"
    assert mock_gui.cursor_x == 320.0 and mock_gui.cursor_y == 180.0
    assert any(h["type"] == "keyboard" and h["text"] == "hello world" for h in mock_gui.history)

    # 验证截图产物已落盘且终态 GC 能正确清理该产物目录，同时不删直连工作区
    art_dir = tmp_path / "artifacts" / created["id"]
    assert any(p.suffix == ".png" for p in art_dir.iterdir())
    gc_report = storage.gc_expired_resources(workspaces_dir=tmp_path, retention_seconds=0)
    assert gc_report["removed_artifacts"] == 1
    assert not art_dir.exists()
    assert ws.exists()


async def test_openai_and_anthropic_providers_serialize_vision_attachments(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4.1")
    monkeypatch.setenv("AGENT_ALLOW_CUSTOM_BASE_URL", "1")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.example.test/v1")

    seen_oai_payloads: list[dict] = []

    def mock_oai_handler(request: httpx.Request) -> httpx.Response:
        seen_oai_payloads.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "看到截图"}}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 5},
            },
        )

    oai = OpenAICompatibleProvider()
    await oai.client.aclose()
    oai.client = httpx.AsyncClient(
        base_url="https://api.example.test/v1",
        transport=httpx.MockTransport(mock_oai_handler),
    )
    try:
        res = await oai.complete(
            "sys",
            [{"role": "user", "content": "查看屏幕"}],
            [],
            vision_attachments=[
                {
                    "artifact_name": "screen-1.png",
                    "media_type": "image/png",
                    "base64_data": "AAAA",
                    "width": 320,
                    "height": 200,
                }
            ],
        )
        assert res["stop_reason"] == "end_turn"
        last_msg = seen_oai_payloads[0]["messages"][-1]
        assert last_msg["role"] == "user"
        assert any(
            isinstance(part, dict)
            and part.get("type") == "image_url"
            and part["image_url"]["url"].startswith("data:image/png;base64,AAAA")
            for part in last_msg["content"]
        )
    finally:
        await oai.aclose()

    # 验证 AnthropicProvider 将视觉附件追加在最后一条 user 消息的 image 块中，不破坏前序 cache_control
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    ant = AnthropicProvider()
    captured_ant_kwargs: list[dict] = []

    class FakeResp:
        stop_reason = "end_turn"
        content = [{"type": "text", "text": "已识别画面"}]

        class usage:
            input_tokens = 40
            output_tokens = 6
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0

    async def fake_create(**kwargs):
        captured_ant_kwargs.append(kwargs)
        return FakeResp()

    monkeypatch.setattr(ant.client.messages, "create", fake_create)
    try:
        ant_res = await ant.complete(
            "sys",
            [{"role": "user", "content": "查看屏幕"}, {"role": "user", "content": "HARNESS_STATE\n{}"}],
            [],
            tail_state_count=1,
            vision_attachments=[
                {
                    "artifact_name": "screen-2.png",
                    "media_type": "image/png",
                    "base64_data": "BBBB",
                    "width": 320,
                    "height": 200,
                }
            ],
        )
        assert ant_res["stop_reason"] == "end_turn"
        tail_blocks = captured_ant_kwargs[0]["messages"][-1]["content"]
        assert any(
            b.get("type") == "image" and b["source"]["data"] == "BBBB" for b in tail_blocks
        )
    finally:
        await ant.aclose()


def test_macos_native_backend_ctypes_binding_smoke():
    if platform.system() != "Darwin":
        return
    backend = MacOSNativeBackend()
    info = backend.screen_info()
    assert info["backend"] == "macos_coregraphics"
    assert info["logical_width"] > 0 and info["logical_height"] > 0
