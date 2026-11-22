"""桌面级 GUI 操控子系统（Computer Use）：屏幕截图与 Retina 逻辑坐标对齐、键鼠事件注入与应用唤起。

设计要点：
1. 在 macOS (Darwin) 上使用零第三方依赖的原生 CoreGraphics + ApplicationServices (ctypes) 与
   screencapture / sips / osascript / open，避免引入庞大的 PyObjC 二进制编译链。
2. 调用前显式预检 macOS 屏幕录制权限（CGPreflightScreenCaptureAccess）与辅助功能权限（AXIsProcessTrusted），
   未获系统授权时返回明确错误码而非空白截图或静默丢弃点击。
3. Retina 屏幕物理截图自动按主屏逻辑点尺寸缩放（上限 1440px 并记录 scale_x/scale_y），保证多模态模型在
   截图上读取的像素坐标与 CGEvent 鼠标逻辑坐标精确对应。
4. 提供确定性的 MockComputerBackend，确保全部 GUI 工具、审批状态机与多模态协议转换在无头 CI 上 100% 可测。
"""

from __future__ import annotations

import binascii
import ctypes
import ctypes.util
import hashlib
from pathlib import Path
import platform
import re
import struct
import subprocess
import time
from typing import Any, Protocol
import urllib.parse
import zlib

from .permissions import evaluate_tool_permission
from .storage import Storage

_MAX_SCREENSHOT_EDGE = 1440

# macOS CoreGraphics 常量定义
_kCGHIDEventTap = 0
_kCGScrollEventUnitPixel = 0

_kCGEventLeftMouseDown = 1
_kCGEventLeftMouseUp = 2
_kCGEventRightMouseDown = 3
_kCGEventRightMouseUp = 4
_kCGEventMouseMoved = 5
_kCGEventLeftMouseDragged = 6
_kCGEventOtherMouseDown = 25
_kCGEventOtherMouseUp = 26

_kCGMouseButtonLeft = 0
_kCGMouseButtonRight = 1
_kCGMouseButtonCenter = 2
_kCGMouseEventClickState = 1

_kCGEventFlagMaskShift = 1 << 17
_kCGEventFlagMaskControl = 1 << 18
_kCGEventFlagMaskAlternate = 1 << 19
_kCGEventFlagMaskCommand = 1 << 20

# macOS ANSI 虚拟键码映射表（常用控制键、功能键与字母数字）
_MACOS_KEYCODES: dict[str, int] = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05, "z": 0x06, "x": 0x07,
    "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10,
    "t": 0x11, "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "6": 0x16, "5": 0x17, "=": 0x18,
    "9": 0x19, "7": 0x1A, "-": 0x1B, "8": 0x1C, "0": 0x1D, "]": 0x1E, "o": 0x1F, "u": 0x20,
    "[": 0x21, "i": 0x22, "p": 0x23, "return": 0x24, "enter": 0x24, "l": 0x25, "j": 0x26,
    "'": 0x27, "k": 0x28, ";": 0x29, "\\": 0x2A, ",": 0x2B, "/": 0x2C, "n": 0x2D, "m": 0x2E,
    ".": 0x2F, "tab": 0x30, "space": 0x31, "`": 0x32, "backspace": 0x33, "delete": 0x33,
    "escape": 0x35, "esc": 0x35, "forward_delete": 0x75,
    "left": 0x7B, "arrowleft": 0x7B, "right": 0x7C, "arrowright": 0x7C,
    "down": 0x7D, "arrowdown": 0x7D, "up": 0x7E, "arrowup": 0x7E,
    "home": 0x73, "end": 0x77, "pageup": 0x74, "pagedown": 0x79,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61,
}

_MODIFIER_FLAGS: dict[str, int] = {
    "cmd": _kCGEventFlagMaskCommand,
    "command": _kCGEventFlagMaskCommand,
    "super": _kCGEventFlagMaskCommand,
    "meta": _kCGEventFlagMaskCommand,
    "ctrl": _kCGEventFlagMaskControl,
    "control": _kCGEventFlagMaskControl,
    "alt": _kCGEventFlagMaskAlternate,
    "opt": _kCGEventFlagMaskAlternate,
    "option": _kCGEventFlagMaskAlternate,
    "shift": _kCGEventFlagMaskShift,
}

_SAFE_APP_NAME_RE = re.compile(r"^[A-Za-z0-9 _.+()-]{1,100}$")


class ComputerToolError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


def _read_png_dimensions(png_bytes: bytes) -> tuple[int, int]:
    if len(png_bytes) < 24 or png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        raise ComputerToolError("invalid_screenshot", "生成的截图文件不是有效的 PNG 格式。")
    width, height = struct.unpack(">II", png_bytes[16:24])
    return int(width), int(height)


def make_synthetic_png(width: int = 320, height: int = 200, rgb: tuple[int, int, int] = (32, 42, 58)) -> bytes:
    """生成零外部依赖的合法 PNG 字节流，用于无头单元测试与多模态回归验证。"""
    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = binascii.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * width
    idat = zlib.compress(row * height, level=1)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


class ComputerBackend(Protocol):
    def screen_info(self) -> dict[str, Any]: ...
    def frontmost_app(self) -> dict[str, Any]: ...
    def take_screenshot(self, output_path: Path) -> dict[str, Any]: ...
    def mouse_action(
        self,
        action: str,
        *,
        x: float | None = None,
        y: float | None = None,
        from_x: float | None = None,
        from_y: float | None = None,
        scroll_dx: int = 0,
        scroll_dy: int = 0,
    ) -> dict[str, Any]: ...
    def keyboard_action(self, action: str, *, text: str = "", key: str = "") -> dict[str, Any]: ...
    def open_target(self, target: str, *, kind: str = "app") -> dict[str, Any]: ...


class UnavailableComputerBackend:
    def __init__(self, reason: str = "当前操作系统未配置原生桌面 GUI 驱动") -> None:
        self.reason = reason

    def _fail(self) -> dict[str, Any]:
        raise ComputerToolError("gui_unavailable", self.reason)

    def screen_info(self) -> dict[str, Any]:
        return self._fail()

    def frontmost_app(self) -> dict[str, Any]:
        return self._fail()

    def take_screenshot(self, output_path: Path) -> dict[str, Any]:
        del output_path
        return self._fail()

    def mouse_action(self, action: str, **kwargs: Any) -> dict[str, Any]:
        del action, kwargs
        return self._fail()

    def keyboard_action(self, action: str, **kwargs: Any) -> dict[str, Any]:
        del action, kwargs
        return self._fail()

    def open_target(self, target: str, *, kind: str = "app") -> dict[str, Any]:
        del target, kind
        return self._fail()


class MockComputerBackend:
    """确定性内存虚拟桌面后端：用于无头测试、CI 与权限/多模态链路校验。"""

    def __init__(self, width: int = 1280, height: int = 800) -> None:
        self.logical_width = width
        self.logical_height = height
        self.cursor_x = float(width // 2)
        self.cursor_y = float(height // 2)
        self.active_app = "Terminal"
        self.history: list[dict[str, Any]] = []

    def screen_info(self) -> dict[str, Any]:
        return {
            "logical_width": self.logical_width,
            "logical_height": self.logical_height,
            "cursor_x": round(self.cursor_x, 1),
            "cursor_y": round(self.cursor_y, 1),
            "scale_x": 1.0,
            "scale_y": 1.0,
            "backend": "mock",
        }

    def frontmost_app(self) -> dict[str, Any]:
        return {"app_name": self.active_app, "backend": "mock"}

    def take_screenshot(self, output_path: Path) -> dict[str, Any]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        png = make_synthetic_png(320, 200)
        output_path.write_bytes(png)
        return {
            "image_width": 320,
            "image_height": 200,
            "logical_width": self.logical_width,
            "logical_height": self.logical_height,
            "scale_x": round(self.logical_width / 320.0, 4),
            "scale_y": round(self.logical_height / 200.0, 4),
            "cursor_x": round(self.cursor_x, 1),
            "cursor_y": round(self.cursor_y, 1),
            "sha256": hashlib.sha256(png).hexdigest(),
            "bytes": len(png),
        }

    def mouse_action(
        self,
        action: str,
        *,
        x: float | None = None,
        y: float | None = None,
        from_x: float | None = None,
        from_y: float | None = None,
        scroll_dx: int = 0,
        scroll_dy: int = 0,
    ) -> dict[str, Any]:
        if x is not None and y is not None:
            if not (0 <= x <= self.logical_width and 0 <= y <= self.logical_height):
                raise ComputerToolError("coordinates_out_of_bounds", "鼠标目标坐标超出屏幕范围。")
            self.cursor_x = float(x)
            self.cursor_y = float(y)
        record = {
            "type": "mouse",
            "action": action,
            "x": self.cursor_x,
            "y": self.cursor_y,
            "from_x": from_x,
            "from_y": from_y,
            "scroll_dx": scroll_dx,
            "scroll_dy": scroll_dy,
        }
        self.history.append(record)
        return {"action": action, "cursor_x": self.cursor_x, "cursor_y": self.cursor_y}

    def keyboard_action(self, action: str, *, text: str = "", key: str = "") -> dict[str, Any]:
        record = {"type": "keyboard", "action": action, "text": text, "key": key}
        self.history.append(record)
        return {"action": action, "chars_typed": len(text) if action == "type" else 0, "key": key}

    def open_target(self, target: str, *, kind: str = "app") -> dict[str, Any]:
        if kind == "app":
            self.active_app = target
        self.history.append({"type": "open", "kind": kind, "target": target})
        return {"opened": target, "kind": kind}


class MacOSNativeBackend:
    """基于 macOS CoreGraphics / ApplicationServices ctypes 的原生桌面 GUI 驱动。"""

    def __init__(self) -> None:
        cg_path = ctypes.util.find_library("CoreGraphics")
        app_path = ctypes.util.find_library("ApplicationServices")
        if not cg_path or not app_path:
            raise ComputerToolError("gui_unavailable", "未找到 macOS CoreGraphics / ApplicationServices 框架。")
        self._cg = ctypes.CDLL(cg_path)
        self._app = ctypes.CDLL(app_path)
        self._bind_signatures()
        # 最近一次截图的图像像素 -> 逻辑点缩放因子（默认 1:1）
        self._last_scale_x = 1.0
        self._last_scale_y = 1.0

    def _bind_signatures(self) -> None:
        cg = self._cg
        cg.CGMainDisplayID.argtypes = []
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
        cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
        cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
        cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t

        if hasattr(cg, "CGPreflightScreenCaptureAccess"):
            cg.CGPreflightScreenCaptureAccess.argtypes = []
            cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool

        cg.CGEventCreate.argtypes = [ctypes.c_void_p]
        cg.CGEventCreate.restype = ctypes.c_void_p
        cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        cg.CGEventGetLocation.restype = _CGPoint

        cg.CGEventCreateMouseEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            _CGPoint,
            ctypes.c_uint32,
        ]
        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
        cg.CGEventSetIntegerValueField.restype = None

        cg.CGEventCreateScrollWheelEvent2.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_int32,
        ]
        cg.CGEventCreateScrollWheelEvent2.restype = ctypes.c_void_p

        cg.CGEventCreateKeyboardEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_bool]
        cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        cg.CGEventKeyboardSetUnicodeString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_uint16),
        ]
        cg.CGEventKeyboardSetUnicodeString.restype = None
        cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        cg.CGEventSetFlags.restype = None
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cg.CGEventPost.restype = None
        cg.CFRelease.argtypes = [ctypes.c_void_p]
        cg.CFRelease.restype = None

        if hasattr(self._app, "AXIsProcessTrusted"):
            self._app.AXIsProcessTrusted.argtypes = []
            self._app.AXIsProcessTrusted.restype = ctypes.c_bool

    def _require_accessibility(self) -> None:
        if hasattr(self._app, "AXIsProcessTrusted") and not bool(self._app.AXIsProcessTrusted()):
            raise ComputerToolError(
                "accessibility_permission_required",
                "当前终端/进程未获得 macOS『辅助功能 (Accessibility)』权限，请在 系统设置 -> 隐私与安全性 -> 辅助功能 中开启。",
            )

    def _require_screen_recording(self) -> None:
        if hasattr(self._cg, "CGPreflightScreenCaptureAccess") and not bool(
            self._cg.CGPreflightScreenCaptureAccess()
        ):
            raise ComputerToolError(
                "screen_recording_permission_required",
                "当前终端/进程未获得 macOS『屏幕录制 (Screen Recording)』权限，请在 系统设置 -> 隐私与安全性 -> 录屏与系统录音 中开启。",
            )

    def _logical_bounds_and_cursor(self) -> tuple[int, int, float, float]:
        disp = self._cg.CGMainDisplayID()
        width = int(self._cg.CGDisplayPixelsWide(disp))
        height = int(self._cg.CGDisplayPixelsHigh(disp))
        ev = self._cg.CGEventCreate(None)
        cx, cy = 0.0, 0.0
        if ev:
            try:
                loc = self._cg.CGEventGetLocation(ev)
                cx, cy = float(loc.x), float(loc.y)
            finally:
                self._cg.CFRelease(ev)
        if width <= 0 or height <= 0:
            raise ComputerToolError("gui_unavailable", "未检测到活动图形显示器会话（Logical Display 为 0）。")
        return width, height, cx, cy

    def screen_info(self) -> dict[str, Any]:
        w, h, cx, cy = self._logical_bounds_and_cursor()
        return {
            "logical_width": w,
            "logical_height": h,
            "cursor_x": round(cx, 1),
            "cursor_y": round(cy, 1),
            "scale_x": round(self._last_scale_x, 4),
            "scale_y": round(self._last_scale_y, 4),
            "backend": "macos_coregraphics",
        }

    def frontmost_app(self) -> dict[str, Any]:
        script = 'tell application "System Events" to get name of first application process whose frontmost is true'
        proc = subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0:
            raise ComputerToolError(
                "osascript_failed",
                f"读取前台应用失败：{(proc.stderr or proc.stdout).strip()[:240]}",
            )
        return {"app_name": proc.stdout.strip(), "backend": "macos_coregraphics"}

    def take_screenshot(self, output_path: Path) -> dict[str, Any]:
        self._require_screen_recording()
        logical_w, logical_h, cx, cy = self._logical_bounds_and_cursor()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            ["/usr/sbin/screencapture", "-x", "-t", "png", str(output_path)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if proc.returncode != 0 or not output_path.is_file():
            raise ComputerToolError(
                "screenshot_failed",
                f"screencapture 截屏失败：{(proc.stderr or proc.stdout).strip()[:240]}",
            )
        raw_bytes = output_path.read_bytes()
        img_w, img_h = _read_png_dimensions(raw_bytes)

        # 对齐到逻辑分辨率或上限 _MAX_SCREENSHOT_EDGE，确保视觉模型坐标与逻辑屏幕精确映射
        target_w, target_h = logical_w, logical_h
        if max(target_w, target_h) > _MAX_SCREENSHOT_EDGE:
            ratio = _MAX_SCREENSHOT_EDGE / float(max(target_w, target_h))
            target_w = max(1, int(round(target_w * ratio)))
            target_h = max(1, int(round(target_h * ratio)))

        if (img_w, img_h) != (target_w, target_h) and Path("/usr/bin/sips").exists():
            subprocess.run(
                ["/usr/bin/sips", "-z", str(target_h), str(target_w), str(output_path)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            raw_bytes = output_path.read_bytes()
            img_w, img_h = _read_png_dimensions(raw_bytes)

        self._last_scale_x = logical_w / float(max(1, img_w))
        self._last_scale_y = logical_h / float(max(1, img_h))
        return {
            "image_width": img_w,
            "image_height": img_h,
            "logical_width": logical_w,
            "logical_height": logical_h,
            "scale_x": round(self._last_scale_x, 4),
            "scale_y": round(self._last_scale_y, 4),
            "cursor_x": round(cx, 1),
            "cursor_y": round(cy, 1),
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "bytes": len(raw_bytes),
            "coordinate_hint": (
                "截图已对齐至 (image_width, image_height) 坐标系；调用 computer_mouse 传入截图上的像素坐标时会自动换算为屏幕逻辑点。"
            ),
        }

    def _post_mouse(self, ev_type: int, pt: _CGPoint, button: int = _kCGMouseButtonLeft, clicks: int = 0) -> None:
        ev = self._cg.CGEventCreateMouseEvent(None, ev_type, pt, button)
        if not ev:
            raise ComputerToolError("cgevent_failed", "无法创建 CGEvent 鼠标事件。")
        try:
            if clicks > 0:
                self._cg.CGEventSetIntegerValueField(ev, _kCGMouseEventClickState, clicks)
            self._cg.CGEventPost(_kCGHIDEventTap, ev)
        finally:
            self._cg.CFRelease(ev)

    def _to_logical_point(self, x: float, y: float, logical_w: int, logical_h: int) -> tuple[float, float]:
        lx = float(x) * self._last_scale_x
        ly = float(y) * self._last_scale_y
        if not (0.0 <= lx <= logical_w and 0.0 <= ly <= logical_h):
            raise ComputerToolError(
                "coordinates_out_of_bounds",
                f"换算后的屏幕逻辑坐标 ({lx:.1f}, {ly:.1f}) 超出主屏边界 ({logical_w}x{logical_h})。",
            )
        return lx, ly

    def mouse_action(
        self,
        action: str,
        *,
        x: float | None = None,
        y: float | None = None,
        from_x: float | None = None,
        from_y: float | None = None,
        scroll_dx: int = 0,
        scroll_dy: int = 0,
    ) -> dict[str, Any]:
        self._require_accessibility()
        logical_w, logical_h, cur_x, cur_y = self._logical_bounds_and_cursor()

        if action == "scroll":
            if x is not None and y is not None:
                lx, ly = self._to_logical_point(x, y, logical_w, logical_h)
                self._post_mouse(_kCGEventMouseMoved, _CGPoint(lx, ly))
                cur_x, cur_y = lx, ly
            ev = self._cg.CGEventCreateScrollWheelEvent2(
                None, _kCGScrollEventUnitPixel, 2, int(scroll_dy), int(scroll_dx), 0
            )
            if not ev:
                raise ComputerToolError("cgevent_failed", "无法创建滚轮事件。")
            try:
                self._cg.CGEventPost(_kCGHIDEventTap, ev)
            finally:
                self._cg.CFRelease(ev)
            return {"action": "scroll", "cursor_x": round(cur_x, 1), "cursor_y": round(cur_y, 1)}

        if x is None or y is None:
            raise ComputerToolError("invalid_arguments", f"鼠标动作 {action} 必须提供目标坐标 x 与 y。")
        lx, ly = self._to_logical_point(x, y, logical_w, logical_h)
        target_pt = _CGPoint(lx, ly)

        if action == "move":
            self._post_mouse(_kCGEventMouseMoved, target_pt)
        elif action == "click":
            self._post_mouse(_kCGEventMouseMoved, target_pt)
            self._post_mouse(_kCGEventLeftMouseDown, target_pt, _kCGMouseButtonLeft, 1)
            time.sleep(0.015)
            self._post_mouse(_kCGEventLeftMouseUp, target_pt, _kCGMouseButtonLeft, 1)
        elif action == "double_click":
            self._post_mouse(_kCGEventMouseMoved, target_pt)
            for click_num in (1, 2):
                self._post_mouse(_kCGEventLeftMouseDown, target_pt, _kCGMouseButtonLeft, click_num)
                time.sleep(0.01)
                self._post_mouse(_kCGEventLeftMouseUp, target_pt, _kCGMouseButtonLeft, click_num)
        elif action == "right_click":
            self._post_mouse(_kCGEventMouseMoved, target_pt)
            self._post_mouse(_kCGEventRightMouseDown, target_pt, _kCGMouseButtonRight, 1)
            time.sleep(0.015)
            self._post_mouse(_kCGEventRightMouseUp, target_pt, _kCGMouseButtonRight, 1)
        elif action == "middle_click":
            self._post_mouse(_kCGEventMouseMoved, target_pt)
            self._post_mouse(_kCGEventOtherMouseDown, target_pt, _kCGMouseButtonCenter, 1)
            time.sleep(0.015)
            self._post_mouse(_kCGEventOtherMouseUp, target_pt, _kCGMouseButtonCenter, 1)
        elif action == "drag":
            if from_x is not None and from_y is not None:
                sx, sy = self._to_logical_point(from_x, from_y, logical_w, logical_h)
            else:
                sx, sy = cur_x, cur_y
            start_pt = _CGPoint(sx, sy)
            self._post_mouse(_kCGEventMouseMoved, start_pt)
            self._post_mouse(_kCGEventLeftMouseDown, start_pt, _kCGMouseButtonLeft, 1)
            for step_i in range(1, 6):
                frac = step_i / 5.0
                mid_pt = _CGPoint(sx + (lx - sx) * frac, sy + (ly - sy) * frac)
                self._post_mouse(_kCGEventLeftMouseDragged, mid_pt, _kCGMouseButtonLeft, 1)
                time.sleep(0.01)
            self._post_mouse(_kCGEventLeftMouseUp, target_pt, _kCGMouseButtonLeft, 1)
        else:
            raise ComputerToolError("invalid_arguments", f"不支持的鼠标动作：{action}")

        return {"action": action, "cursor_x": round(lx, 1), "cursor_y": round(ly, 1)}

    def _post_unicode_chunk(self, text_chunk: str) -> None:
        utf16_bytes = text_chunk.encode("utf-16-le")
        count = len(utf16_bytes) // 2
        buf = (ctypes.c_uint16 * count).from_buffer_copy(utf16_bytes)
        for is_down in (True, False):
            ev = self._cg.CGEventCreateKeyboardEvent(None, 0, is_down)
            if not ev:
                raise ComputerToolError("cgevent_failed", "无法创建键盘输入事件。")
            try:
                self._cg.CGEventKeyboardSetUnicodeString(ev, count, buf)
                self._cg.CGEventPost(_kCGHIDEventTap, ev)
            finally:
                self._cg.CFRelease(ev)
        time.sleep(0.008)

    def keyboard_action(self, action: str, *, text: str = "", key: str = "") -> dict[str, Any]:
        self._require_accessibility()
        if action == "type":
            if not text:
                raise ComputerToolError("invalid_arguments", "type 动作必须提供非空 text。")
            # 按最多 16 个字符分块发送 Unicode 事件，兼容中日韩字符与符号
            for offset in range(0, len(text), 16):
                self._post_unicode_chunk(text[offset : offset + 16])
            return {"action": "type", "chars_typed": len(text)}

        if action == "key":
            parts = [p.strip().lower() for p in key.split("+") if p.strip()]
            if not parts:
                raise ComputerToolError("invalid_arguments", "key 动作必须提供按键名或组合键（如 cmd+c、enter）。")
            flags = 0
            for mod in parts[:-1]:
                if mod not in _MODIFIER_FLAGS:
                    raise ComputerToolError("invalid_key", f"未知的修饰键：{mod}")
                flags |= _MODIFIER_FLAGS[mod]
            main_key = parts[-1]
            if main_key not in _MACOS_KEYCODES:
                raise ComputerToolError("invalid_key", f"不支持的按键名：{main_key}")
            keycode = _MACOS_KEYCODES[main_key]
            for is_down in (True, False):
                ev = self._cg.CGEventCreateKeyboardEvent(None, keycode, is_down)
                if not ev:
                    raise ComputerToolError("cgevent_failed", "无法创建快捷键事件。")
                try:
                    if flags:
                        self._cg.CGEventSetFlags(ev, flags)
                    self._cg.CGEventPost(_kCGHIDEventTap, ev)
                finally:
                    self._cg.CFRelease(ev)
                time.sleep(0.01)
            return {"action": "key", "key": key}

        raise ComputerToolError("invalid_arguments", f"不支持的键盘动作：{action}")

    def open_target(self, target: str, *, kind: str = "app") -> dict[str, Any]:
        cleaned = target.strip()
        if kind == "app":
            if not _SAFE_APP_NAME_RE.fullmatch(cleaned):
                raise ComputerToolError("unsafe_target", "应用名称仅允许字母、数字、空格与常见符号。")
            cmd = ["/usr/bin/open", "-a", cleaned]
        elif kind == "url":
            parsed = urllib.parse.urlparse(cleaned)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ComputerToolError("unsafe_target", "URL 仅允许合法的 http:// 或 https:// 地址。")
            cmd = ["/usr/bin/open", cleaned]
        else:
            raise ComputerToolError("invalid_arguments", "kind 必须是 app 或 url。")

        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, check=False)
        if proc.returncode != 0:
            raise ComputerToolError(
                "open_failed",
                f"打开目标失败：{(proc.stderr or proc.stdout).strip()[:240]}",
            )
        return {"opened": cleaned, "kind": kind}


def get_default_computer_backend() -> ComputerBackend:
    if platform.system() == "Darwin":
        try:
            return MacOSNativeBackend()
        except ComputerToolError as exc:
            return UnavailableComputerBackend(str(exc))
    return UnavailableComputerBackend(f"当前平台 ({platform.system()}) 未启用 macOS CoreGraphics 桌面驱动")


COMPUTER_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "computer_screen",
        "description": "获取桌面屏幕信息、前台活动应用，或截取当前桌面截图（截图自动缩放对齐并传递视觉画面）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["screenshot", "info", "frontmost_app"],
                    "description": "读取操作：screenshot（截屏）、info（屏幕尺寸与光标位置）、frontmost_app（当前前台应用）。",
                }
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "computer_mouse",
        "description": "移动桌面光标、点击（左键/双击/右键/中键）、拖拽或滚轮滚动；坐标对应最近一次截图坐标系。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["move", "click", "double_click", "right_click", "middle_click", "drag", "scroll"],
                },
                "x": {"type": "number", "minimum": 0, "maximum": 10000},
                "y": {"type": "number", "minimum": 0, "maximum": 10000},
                "from_x": {"type": "number", "minimum": 0, "maximum": 10000},
                "from_y": {"type": "number", "minimum": 0, "maximum": 10000},
                "scroll_dx": {"type": "integer", "minimum": -5000, "maximum": 5000},
                "scroll_dy": {"type": "integer", "minimum": -5000, "maximum": 5000},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "computer_keyboard",
        "description": "向当前桌面焦点窗口输入文本（type）或发送按键/快捷键组合（key，如 cmd+c、cmd+space、enter、escape）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["type", "key"]},
                "text": {"type": "string", "maxLength": 2000, "description": "action=type 时输入的字符串。"},
                "key": {"type": "string", "maxLength": 80, "description": "action=key 时的按键或组合键（如 cmd+l）。"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    {
        "name": "computer_open",
        "description": "唤起本机桌面应用程序（kind=app，如 Safari、Calculator）或用默认浏览器打开 http/https 链接（kind=url）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "target": {"type": "string", "minLength": 1, "maxLength": 500},
                "kind": {"type": "string", "enum": ["app", "url"]},
            },
            "required": ["target"],
            "additionalProperties": False,
        },
    },
]


def execute_computer_tool(
    backend: ComputerBackend,
    storage: Storage,
    run_id: str,
    *,
    name: str,
    arguments: dict[str, Any],
    artifacts_dir: Path,
    permission_mode: str,
    call_id: str | None = None,
    expected_epoch: int | None = None,
    bypass_approval: bool = False,
) -> dict[str, Any]:
    """统一执行桌面 GUI 工具（含权限策略拦截、动作提案生成与截图产物持久化）。"""
    if name == "computer_screen":
        action = arguments["action"]
        if action == "info":
            return {"ok": True, **backend.screen_info()}
        if action == "frontmost_app":
            return {"ok": True, **backend.frontmost_app()}
        if action == "screenshot":
            safe_cid = re.sub(r"[^A-Za-z0-9_.-]", "_", call_id or f"shot-{int(time.time() * 1000)}")[:64]
            artifact_name = f"screen-{safe_cid}.png"
            out_path = Path(artifacts_dir).resolve() / run_id / artifact_name
            meta = backend.take_screenshot(out_path)
            storage.add_event(
                run_id,
                "gui_screenshot",
                {
                    "artifact_name": artifact_name,
                    "image_width": meta["image_width"],
                    "image_height": meta["image_height"],
                    "scale_x": meta.get("scale_x", 1.0),
                    "scale_y": meta.get("scale_y", 1.0),
                    "sha256": meta["sha256"],
                },
            )
            return {
                "ok": True,
                "action": "screenshot",
                "artifact_name": artifact_name,
                **meta,
            }
        raise ComputerToolError("invalid_arguments", f"未知的 computer_screen 动作：{action}")

    # 跨审批或跨轮重建 Backend 时，自动从持久事件恢复最近一次截图的坐标缩放比例
    if name == "computer_mouse" and hasattr(backend, "_last_scale_x"):
        for ev in reversed(storage.events(run_id)):
            if ev.get("kind") == "gui_screenshot":
                pl = ev.get("payload") or {}
                if isinstance(pl.get("scale_x"), (int, float)) and isinstance(pl.get("scale_y"), (int, float)):
                    backend._last_scale_x = float(pl["scale_x"])
                    backend._last_scale_y = float(pl["scale_y"])
                break

    if not bypass_approval:
        perm = evaluate_tool_permission(name, arguments, permission_mode=permission_mode)
        if perm["decision"] == "ask":
            summary = f"[GUI] {name} {arguments}"
            proposal = storage.create_action_proposal(
                run_id,
                kind="gui_action",
                tool_name=name,
                arguments=arguments,
                summary=summary[:240],
                risk_level=perm["risk"],
                operation_id=call_id,
                expected_epoch=expected_epoch,
            )
            storage.add_event(
                run_id,
                "action_proposed",
                {
                    "proposal_id": proposal["id"],
                    "kind": "gui_action",
                    "tool": name,
                    "arguments": arguments,
                    "reason": perm["reason"],
                },
            )
            return {
                "ok": True,
                "requires_approval": True,
                "proposal_id": proposal["id"],
                "kind": "gui_action",
                "tool": name,
                "arguments": arguments,
                "reason": perm["reason"],
            }

    storage.verify_epoch(run_id, expected_epoch)
    if name == "computer_mouse":
        res = backend.mouse_action(
            arguments["action"],
            x=arguments.get("x"),
            y=arguments.get("y"),
            from_x=arguments.get("from_x"),
            from_y=arguments.get("from_y"),
            scroll_dx=int(arguments.get("scroll_dx", 0)),
            scroll_dy=int(arguments.get("scroll_dy", 0)),
        )
    elif name == "computer_keyboard":
        res = backend.keyboard_action(
            arguments["action"],
            text=str(arguments.get("text", "")),
            key=str(arguments.get("key", "")),
        )
    elif name == "computer_open":
        res = backend.open_target(
            arguments["target"],
            kind=str(arguments.get("kind", "app")),
        )
    else:
        raise ComputerToolError("unknown_tool", f"未知的 GUI 工具：{name}")

    storage.add_event(run_id, "gui_action_executed", {"tool": name, "result": res})
    return {"ok": True, **res}
