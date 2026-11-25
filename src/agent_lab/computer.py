"""桌面级 GUI 操控子系统（Computer Use）：屏幕截图与 Retina 逻辑坐标对齐、键鼠事件注入与应用唤起。

================================================================================
模块导读（Module Guide）
================================================================================
1. 模块角色（Role）：
   本模块为 Agent 提供原生桌面图形界面感知与操控闭环（“截屏观察 -> 视觉定位 -> 键鼠/应用动作”）。
   它向下通过 C ABI（`ctypes`）直连 macOS `CoreGraphics` 与 `ApplicationServices` 系统框架，
   向上对接 `AgentRuntime` 的三级权限治理（`ask` / `auto-edit` / `full-auto`）、多模态附件注入
   与 Epoch 租约防脑裂机制，且保持零第三方 GUI 编译依赖（无需安装庞大的 PyObjC）。

2. 推荐阅读顺序（Reading Order）：
   (a) `ComputerBackend` 协议与三类后端：`UnavailableComputerBackend`（跨平台优雅降级） ->
       `MockComputerBackend`（无头 CI 确定性仿真） -> `MacOSNativeBackend`（macOS 原生驱动）；
   (b) `MacOSNativeBackend._bind_signatures`：64 位指针返回类型声明与 ARM64 非变参 FFI 约束；
   (c) `_require_screen_recording` / `_require_accessibility`：macOS TCC 隐私沙箱非阻塞预检；
   (d) `take_screenshot` 与 `_to_logical_point`：Retina 物理像素到 CGEvent 逻辑点的双向比例对齐；
   (e) `mouse_action` / `_post_unicode_chunk` / `keyboard_action` / `open_target`：HID 事件合成、
       UTF-16LE 分块 Unicode 注入与防命令/URI 注入校验；
   (f) `COMPUTER_TOOL_SPECS` 与 `execute_computer_tool`：工具契约、跨轮缩放比例恢复与审批挂起。

3. 核心工程与安全不变量（Core Invariants）：
   - [FFI 64 位指针不截断] `CGEventRef` 是 64 位 CoreFoundation 不透明对象指针。`ctypes` 未显式设置
     `restype` 时默认按 32 位 `c_int` 截断返回值，在 64 位堆地址超过 4GB 时调用 `CGEventPost` 或
     `CFRelease` 会立即触发 `SIGSEGV` 段错误；所有创建函数必须声明 `restype = ctypes.c_void_p`，
     且在 `try...finally` 中配对 `CFRelease` 防止内核事件对象泄漏。
   - [Apple Silicon ARM64 非变参 ABI] 在 macOS arm64 (AAPCS64) 调用约定下，固定参数走寄存器 `x0-x7`，
     而 C 变参函数（`...`）的匿名参数强制压栈传递。若用固定 `argtypes` 调用变参版
     `CGEventCreateScrollWheelEvent`，被调函数从栈帧读取滚轮偏移将读到随机垃圾值甚至崩溃，
     因此必须绑定固定参数签名的 `CGEventCreateScrollWheelEvent2`。
   - [Retina 坐标空间对齐] `screencapture` 截取的是 Retina 物理像素（通常为逻辑点的 2x），而
     `CGEvent` 鼠标定位运行在逻辑点（Points）空间。截屏时用 `sips` 缩放至逻辑分辨率（长边上限
     1440px）并把 `(scale_x, scale_y)` 写入持久化事件，点击时乘回缩放因子并校验主屏边界，根治“看准点偏”。
   - [TCC 显式失败与防注入] 未获录屏授权时 macOS 默认只截出空白壁纸而隐藏所有窗口，未获辅助功能授权时
     `WindowServer` 会静默丢弃注入的键鼠事件；前置调用 `CGPreflightScreenCaptureAccess` 与
     `AXIsProcessTrusted` 可提前抛出明确诊断而非让模型陷入幻觉重试循环。`open_target` 严禁 `shell=True`，
     以参数列表配合应用名正则和 `http/https` Scheme 白名单阻断本地命令与恶意协议（如 `file://`）执行。

4. 技术面试高频追问（Interview Q&A）：
   - Q1: 为什么 `ctypes` 调用 macOS `CGEventCreate*` 漏写 `restype = c_void_p` 会偶发或必现 Segfault？
     A1: Python `ctypes` 历史兼容 C89 隐式 int 规则，默认把返回值当成有符号 32 位整数；64 位进程中堆指针
         高 32 位非零，截断后再传回 `CGEventPost`/`CFRelease` 解引用非法虚拟地址直接触发硬件异常。
   - Q2: 为什么在 M1/M2/M3/M4 (ARM64) Mac 上滚轮事件必须用 `CGEventCreateScrollWheelEvent2`？
     A2: 原版 `CGEventCreateScrollWheelEvent` 是 C 变参函数；Darwin ARM64 ABI 将变参放在栈上而非寄存器，
         `ctypes` 按普通固定参数绑定时把滚轮增量放进寄存器，导致栈槽未初始化，滚轮失灵或越界访存。
   - Q3: 多模态模型在 Retina 屏幕上点击总是偏左上角一半距离，根因是什么？如何支持审批暂停后恢复仍点得准？
     A3: 根因是物理像素（2x）与 `CGEvent` 逻辑点（1x）混用。本实现截屏后用 `sips` 对齐图像尺寸，并将
         `scale_x/scale_y` 记入 SQLite `gui_screenshot` 事件；即使进程因 `waiting_approval` 重启重建了
         Backend，`execute_computer_tool` 也会倒序回放最近一次截图事件恢复比例尺。
   - Q4: 为什么输入任意文本（含中文与 Emoji）不用虚拟键码表，而用 `CGEventKeyboardSetUnicodeString` 分块？
     A4: 虚拟键码仅表示物理按键位置（如美式键盘 `0x00` 恒为 `A` 键），受输入法状态影响且无法直接表达汉字；
         将字符串编码为 UTF-16LE 挂载到空键码事件上可绕过键盘布局直达焦点控件，按 16 字符分块并配对
         Down/Up 则避免超出 Quartz 单事件内部缓冲区上限及防止目标应用输入队列丢字。
   - Q5: 如何在无图形界面的 Linux 容器 CI 中高保真测试 Computer Use 全链路？
     A5: 通过 `ComputerBackend` 协议注入 `MockComputerBackend`，用纯标准库 `zlib/struct` 实时合成合法
         PNG 字节流（刻意让 320x200 图像映射到大尺寸虚拟逻辑桌面以覆盖 `scale != 1.0` 分支），把全部点击、
         拖拽、键入记录到内存 `history` 列表，零 Xvfb/TCC 依赖即可确定性断言多模态序列化与权限状态机。
================================================================================
"""

from __future__ import annotations

import binascii
import contextlib
import ctypes
import ctypes.util
import fcntl
import hashlib
import os
from pathlib import Path
import platform
import re
import shutil
import struct
import subprocess
import threading
import time
from typing import Any, Iterator, Protocol
import urllib.parse
import zlib

from .observability import redact_secrets
from .permissions import evaluate_tool_permission
from .storage import Storage

_GUI_PROCESS_LOCK = threading.RLock()
# 固定 63 位正整数 PostgreSQL 咨询锁键（由常量命名空间 SHA-256 确定性派生，避免与其他业务 advisory lock 冲突）
_PG_GUI_SEAT_LOCK_KEY = (
    int.from_bytes(hashlib.sha256(b"agent-lab:desktop-gui-seat").digest()[:8], "big") & 0x7FFFFFFFFFFFFFFF
)


@contextlib.contextmanager
def _posix_gui_seat_flock(storage: Storage) -> Iterator[None]:
    """跨进程 POSIX flock 物理桌面席位锁（供 SQLite 模式及 PostgreSQL 不可达时的降级兜底）。"""
    storage_path = getattr(storage, "path", None)
    base_dir = Path(storage_path).parent if storage_path is not None else (Path.cwd() / ".agent-lab")
    locks_dir = base_dir / "apply_locks"
    locks_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_file = locks_dir / "desktop-gui-seat.lock"
    if lock_file.is_symlink():
        raise ComputerToolError("unsafe_path", "桌面席位锁文件不能是符号链接。")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(lock_file, flags, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def _desktop_gui_seat_lock(storage: Storage) -> Iterator[None]:
    """跨进程/跨节点物理桌面席位互斥锁（GUI Seat Lock）。

    为什么多 Agent 并发操控本机时必须对物理 GUI 动作加席位锁：
    一台物理电脑同一时刻只有一个系统光标坐标和单个键盘焦点窗口。若两个开启 `--gui` 的并发 Agent
    同时执行“移动鼠标到 (x1, y1) -> 按下左键”与“移动鼠标到 (x2, y2) -> 输入文字”，两者的 CGEvent
    序列会在 WindowServer 事件队列中交错穿插，导致点错窗口或把文字输进另一个 Agent 的输入框。
    - SQLite 模式：进程内 RLock + 跨进程 POSIX flock；
    - PostgreSQL 多 Worker 模式：优先获取 `pg_advisory_lock(_PG_GUI_SEAT_LOCK_KEY)` 分布式咨询锁，
      并在连接不可用时自动降级到本地 POSIX flock 锁文件，绝不跳过桌面席位互斥。
    """
    with _GUI_PROCESS_LOCK:
        pg_config = getattr(storage, "pg_config", None)
        if pg_config is not None:
            pg_con = None
            acquired_pg = False
            try:
                pg_con = pg_config.connect()
                pg_con.execute("SELECT pg_advisory_lock(%s)", (_PG_GUI_SEAT_LOCK_KEY,))
                acquired_pg = True
            except Exception:
                if pg_con is not None:
                    with contextlib.suppress(Exception):
                        pg_con.close()
                pg_con = None
                acquired_pg = False
            if acquired_pg and pg_con is not None:
                try:
                    yield
                finally:
                    with contextlib.suppress(Exception):
                        pg_con.execute("SELECT pg_advisory_unlock(%s)", (_PG_GUI_SEAT_LOCK_KEY,))
                    with contextlib.suppress(Exception):
                        pg_con.close()
                return
            with _posix_gui_seat_flock(storage):
                yield
            return
        with _posix_gui_seat_flock(storage):
            yield

# 截图长边像素上限：1440px 足以保留桌面菜单与小号字体可读性，同时避免 4K/5K 原始大图耗尽视觉模型图片 Token 预算。
_MAX_SCREENSHOT_EDGE = 1440

# macOS CoreGraphics 常量定义（对齐 <CoreGraphics/CGEventTypes.h> 与 <Carbon/HIToolbox/Events.h>）：
# kCGHIDEventTap (=0) 在系统 HID 硬件抽象层最前端注入事件，使前台应用像收到真实物理键鼠输入一样响应。
_kCGHIDEventTap = 0
# 使用像素级（Pixel）而非行级（Line）滚轮单位，确保网页与编辑器平滑精细滚动。
_kCGScrollEventUnitPixel = 0

# CGEventType 鼠标事件枚举值
_kCGEventLeftMouseDown = 1
_kCGEventLeftMouseUp = 2
_kCGEventRightMouseDown = 3
_kCGEventRightMouseUp = 4
_kCGEventMouseMoved = 5
_kCGEventLeftMouseDragged = 6
_kCGEventOtherMouseDown = 25
_kCGEventOtherMouseUp = 26

# CGMouseButton 按钮编号与点击连击计数属性字段（AppKit 依赖 ClickState=2 识别真正的双击，而非两次独立单击）
_kCGMouseButtonLeft = 0
_kCGMouseButtonRight = 1
_kCGMouseButtonCenter = 2
_kCGMouseEventClickState = 1

# CGEventFlags 修饰键位掩码（Shift / Control / Option / Command）
_kCGEventFlagMaskShift = 1 << 17
_kCGEventFlagMaskControl = 1 << 18
_kCGEventFlagMaskAlternate = 1 << 19
_kCGEventFlagMaskCommand = 1 << 20

# macOS ANSI 硬件虚拟键码映射表（kVK_*）：注意其按美式物理键盘行列位置编号而非 ASCII 顺序（如 a=0x00, s=0x01）。
# 仅用于快捷键与功能键触发（action="key"）；任意文本输入（action="type"）走 UTF-16LE Unicode 挂载以免疫输入法干扰。
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

# 跨平台修饰键别名归一化映射：兼容模型可能生成的 cmd/command/super/meta 或 alt/opt/option 等习惯写法。
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

# CoreFoundation / Accessibility (HIServices AXUIElement) ABI 常量：
_kCFStringEncodingUTF8 = 0x08000100
_kAXErrorSuccess = 0
_kAXValueTypeCGPoint = 1
_kAXValueTypeCGSize = 2

# 应用名称安全白名单正则：阻止模型在 `open -a <target>` 中塞入路径穿越（`../`）或附加命令行选项注入。
_SAFE_APP_NAME_RE = re.compile(r"^[A-Za-z0-9 _.+()-]{1,100}$")


class ComputerToolError(ValueError):
    """桌面操控可恢复业务异常：携带机器可读的 `code`，供 Runtime 封装为结构化工具错误反馈给模型自纠错。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class _CGPoint(ctypes.Structure):
    """对齐 64 位 macOS CoreGraphics `struct CGPoint { CGFloat x; CGFloat y; }` 的 C ABI 内存布局。

    在 64 位 Darwin (`__LP64__`) 上，`CGFloat` 等价于 64 位双精度浮点数 `double`（而非 32 位 `float`）；
    按值传参与返回时由 ARM64 浮点寄存器 `d0/d1`（或 x86_64 `xmm0/xmm1`）直接承载。
    """

    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class _CGSize(ctypes.Structure):
    """对齐 64 位 macOS CoreGraphics `struct CGSize { CGFloat width; CGFloat height; }`。"""

    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class _CGRect(ctypes.Structure):
    """对齐 64 位 macOS CoreGraphics `struct CGRect { CGPoint origin; CGSize size; }`。"""

    _fields_ = [("origin", _CGPoint), ("size", _CGSize)]


def _read_png_dimensions(png_bytes: bytes) -> tuple[int, int]:
    """零第三方图像库依赖，直接按 PNG 二进制规范解析 IHDR 头块读取像素宽高。

    PNG 文件前 8 字节固定为魔数签名 `\\x89PNG\\r\\n\\x1a\\n`，紧随其后的首个数据块必为 `IHDR`
    （4 字节长度 + 4 字节块类型 `'IHDR'` 占字节 8..16），因此偏移 16..24 即为网络字节序（大端 `>II`）的宽高。
    """
    if len(png_bytes) < 24 or png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        raise ComputerToolError("invalid_screenshot", "生成的截图文件不是有效的 PNG 格式。")
    width, height = struct.unpack(">II", png_bytes[16:24])
    return int(width), int(height)


def make_synthetic_png(width: int = 320, height: int = 200, rgb: tuple[int, int, int] = (32, 42, 58)) -> bytes:
    """生成零外部依赖的合法 PNG 字节流，用于无头单元测试与多模态回归验证。

    按 PNG RFC 2083 标准拼装最小合法结构：`8B Signature + IHDR + zlib(IDAT) + IEND`：
    - `IHDR` 指定 8-bit 位深、ColorType=2（真彩色 RGB）；
    - 每行像素前置 1 字节自适应滤波器类型 `0x00`（None Filter），经 `zlib` 压缩为 `IDAT` 载荷；
    - 每个 Chunk 尾部附带覆盖 `tag + data` 的无符号 32 位 CRC32 校验和，确保能通过真实多模态网关解码器校验。
    """
    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = binascii.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * width
    idat = zlib.compress(row * height, level=1)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


class ComputerBackend(Protocol):
    """桌面操控硬件抽象协议（Structural Subtyping）：解耦上层权限/多模态编排与底层操作系统 GUI 驱动。"""

    def screen_info(self) -> dict[str, Any]: ...
    def frontmost_app(self) -> dict[str, Any]: ...
    def ax_tree(self, *, max_nodes: int = 80, max_depth: int = 6) -> dict[str, Any]: ...
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
    """优雅降级空后端（Null-Object 模式）：在非 macOS 平台或缺少图形框架时延迟报错。

    之所以不在模块导入或 `AgentRuntime.__init__` 阶段直接抛异常崩溃，是为了保证 Linux 服务端或无头容器在
    不触发 GUI 工具时仍能正常运行代码编辑、RAG 与评测任务；仅当模型真正调用 `computer_*` 时才返回清晰原因。
    """

    def __init__(self, reason: str = "当前操作系统未配置原生桌面 GUI 驱动") -> None:
        self.reason = reason

    def _fail(self) -> dict[str, Any]:
        raise ComputerToolError("gui_unavailable", self.reason)

    def screen_info(self) -> dict[str, Any]:
        return self._fail()

    def frontmost_app(self) -> dict[str, Any]:
        return self._fail()

    def ax_tree(self, *, max_nodes: int = 80, max_depth: int = 6) -> dict[str, Any]:
        del max_nodes, max_depth
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
    """确定性内存虚拟桌面后端：用于无头测试、CI 与权限/多模态链路校验。

    无头测试架构设计：
    1. 零显示服务依赖：无需启动 Xvfb、VNC 或申请 macOS TCC 授权，在任何容器与并发测试进程中毫秒级初始化；
    2. 真实比例尺仿真：`take_screenshot` 输出 320x200 合法 PNG，同时维护默认 1280x800（或自定义尺寸）的逻辑桌面，
       显式检验非 1:1 比例下的 `scale_x / scale_y` 传输、产物落盘与 GC 回收逻辑；
    3. 闭环状态与审计日志：内置虚拟光标 `(cursor_x, cursor_y)`、边界越界检查、前台焦点应用 `active_app` 以及
       只追加的 `history` 轨迹表，供单元测试精确断言每次点击、拖拽、滚轮与键入副作用。
    """

    def __init__(
        self,
        width: int = 1280,
        height: int = 800,
        *,
        ax_nodes: list[dict[str, Any]] | None = None,
    ) -> None:
        self.logical_width = width
        self.logical_height = height
        # 初始光标置于虚拟主屏中心点，避免初始状态贴边触发边界临界歧义
        self.cursor_x = float(width // 2)
        self.cursor_y = float(height // 2)
        self.active_app = "Terminal"
        self.history: list[dict[str, Any]] = []
        self._mock_ax_nodes: list[dict[str, Any]] | None = list(ax_nodes) if ax_nodes is not None else None

    def screen_info(self) -> dict[str, Any]:
        """返回虚拟桌面逻辑分辨率与当前内存光标坐标。"""
        return {
            "logical_width": self.logical_width,
            "logical_height": self.logical_height,
            "cursor_x": round(self.cursor_x, 1),
            "cursor_y": round(self.cursor_y, 1),
            "scale_x": 1.0,
            "scale_y": 1.0,
            "displays": [
                {
                    "display_id": 1,
                    "is_main": True,
                    "x": 0.0,
                    "y": 0.0,
                    "width": float(self.logical_width),
                    "height": float(self.logical_height),
                }
            ],
            "desktop_bounds": {
                "min_x": 0.0,
                "min_y": 0.0,
                "max_x": float(self.logical_width),
                "max_y": float(self.logical_height),
                "width": float(self.logical_width),
                "height": float(self.logical_height),
            },
            "backend": "mock",
        }

    def frontmost_app(self) -> dict[str, Any]:
        """返回当前虚拟前台活动应用名称。"""
        return {"app_name": self.active_app, "backend": "mock"}

    def ax_tree(self, *, max_nodes: int = 80, max_depth: int = 6) -> dict[str, Any]:
        """返回确定性焦点窗口无障碍节点树，便于无头环境测试 AX 控件定位与中心点点击。"""
        clamped_nodes = max(1, min(200, int(max_nodes)))
        clamped_depth = max(1, min(12, int(max_depth)))
        if self._mock_ax_nodes is not None:
            source_nodes = list(self._mock_ax_nodes)
        else:
            win_w = float(self.logical_width)
            win_h = float(self.logical_height)
            source_nodes = [
                {
                    "role": "AXWindow",
                    "title": f"{self.active_app} — Main Window",
                    "value": "",
                    "x": 0.0,
                    "y": 0.0,
                    "width": win_w,
                    "height": win_h,
                    "center_x": round(win_w / 2.0, 1),
                    "center_y": round(win_h / 2.0, 1),
                },
                {
                    "role": "AXTextField",
                    "title": "Command Input",
                    "value": "",
                    "x": 40.0,
                    "y": 80.0,
                    "width": 400.0,
                    "height": 32.0,
                    "center_x": 240.0,
                    "center_y": 96.0,
                },
                {
                    "role": "AXButton",
                    "title": "Run",
                    "value": "",
                    "x": 460.0,
                    "y": 80.0,
                    "width": 88.0,
                    "height": 32.0,
                    "center_x": 504.0,
                    "center_y": 96.0,
                },
            ]
        sliced = source_nodes[:clamped_nodes]
        return {
            "backend": "mock",
            "app_name": self.active_app,
            "window_title": f"{self.active_app} — Main Window",
            "node_count": len(sliced),
            "max_nodes": clamped_nodes,
            "max_depth": clamped_depth,
            "truncated": len(source_nodes) > clamped_nodes,
            "nodes": sliced,
        }

    def take_screenshot(self, output_path: Path) -> dict[str, Any]:
        """将合成的 320x200 PNG 写入产物路径，并返回图像尺寸与逻辑桌面之间的精确缩放比例。"""
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
        """校验坐标越界后更新虚拟光标位置，并将动作事件追加至 `self.history`。"""
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
        """记录虚拟键盘文本输入或组合快捷键事件。"""
        record = {"type": "keyboard", "action": action, "text": text, "key": key}
        self.history.append(record)
        return {"action": action, "chars_typed": len(text) if action == "type" else 0, "key": key}

    def open_target(self, target: str, *, kind: str = "app") -> dict[str, Any]:
        """模拟应用切换或链接打开：当 `kind == 'app'` 时将虚拟前台应用更新为目标应用。"""
        if kind == "app":
            self.active_app = target
        self.history.append({"type": "open", "kind": kind, "target": target})
        return {"opened": target, "kind": kind}


class MacOSNativeBackend:
    """基于 macOS CoreGraphics / ApplicationServices ctypes 的原生桌面 GUI 驱动。

    核心职责与边界：
    - 动态加载系统 dyld 共享缓存中的 `CoreGraphics` 与 `ApplicationServices` 框架；
    - 强制执行 64 位 FFI 类型签名绑定、TCC 录屏/辅助功能权限前置探测、Retina 坐标缩放与 CF 对象引用释放。
    """

    def __init__(self) -> None:
        cg_path = ctypes.util.find_library("CoreGraphics")
        app_path = ctypes.util.find_library("ApplicationServices")
        if not cg_path or not app_path:
            raise ComputerToolError("gui_unavailable", "未找到 macOS CoreGraphics / ApplicationServices 框架。")
        self._cg = ctypes.CDLL(cg_path)
        self._app = ctypes.CDLL(app_path)
        self._bind_signatures()
        # 最近一次截图的图像像素 -> 逻辑点缩放因子（默认 1:1；截屏或跨轮恢复时自动更新）
        self._last_scale_x = 1.0
        self._last_scale_y = 1.0

    def _bind_signatures(self) -> None:
        """显式绑定全部 C 函数参数与返回类型，消除 64 位指针截断与 ARM64 变参 ABI 崩溃隐患。

        关键底层原理：
        1. 防止 32 位指针截断（Pointer Truncation Segfault）：
           `ctypes` 未声明 `restype` 时一律按 `c_int`（32-bit）接收返回值。而 `CGEventCreate*` 返回的
           `CGEventRef` 是 64 位堆内存地址（如 `0x0000600003a48120`），若被截断为 `0x03a48120`，后续传给
           `CGEventPost` 或 `CFRelease` 时将解引用无效低地址页，直接导致 Python 解释器 `SIGSEGV` 崩溃。
        2. 适配 Apple Silicon ARM64 非变参调用约定（AAPCS64 Non-Variadic ABI）：
           旧版 `CGEventCreateScrollWheelEvent` 签名为 `(source, units, wheelCount, wheel1, ...)`，属于 C 变参函数。
           在 Darwin ARM64 ABI 下，固定参数通过寄存器 `x0-x7` 传递，而 `...` 变参强制通过栈内存传递；若用固定
           `argtypes` 调用变参函数，滚轮偏移会被放进寄存器而非栈槽，被调函数将读到未初始化栈垃圾。因此这里必须绑定
           Apple 提供的非变参替代 API `CGEventCreateScrollWheelEvent2`（固定接收 6 个整型/指针寄存器参数）。
        """
        cg = self._cg
        # 主显示器 ID、多显示器枚举及逻辑边界查询
        cg.CGMainDisplayID.argtypes = []
        cg.CGMainDisplayID.restype = ctypes.c_uint32
        cg.CGDisplayPixelsWide.argtypes = [ctypes.c_uint32]
        cg.CGDisplayPixelsWide.restype = ctypes.c_size_t
        cg.CGDisplayPixelsHigh.argtypes = [ctypes.c_uint32]
        cg.CGDisplayPixelsHigh.restype = ctypes.c_size_t
        if hasattr(cg, "CGGetActiveDisplayList"):
            cg.CGGetActiveDisplayList.argtypes = [
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
                ctypes.POINTER(ctypes.c_uint32),
            ]
            cg.CGGetActiveDisplayList.restype = ctypes.c_int32
        if hasattr(cg, "CGDisplayBounds"):
            cg.CGDisplayBounds.argtypes = [ctypes.c_uint32]
            cg.CGDisplayBounds.restype = _CGRect

        # macOS 10.15+ 录屏 TCC 权限无弹窗预检接口
        if hasattr(cg, "CGPreflightScreenCaptureAccess"):
            cg.CGPreflightScreenCaptureAccess.argtypes = []
            cg.CGPreflightScreenCaptureAccess.restype = ctypes.c_bool

        # 空事件创建与当前全局光标逻辑坐标读取（注意 restype 必须为 64 位 c_void_p）
        cg.CGEventCreate.argtypes = [ctypes.c_void_p]
        cg.CGEventCreate.restype = ctypes.c_void_p
        cg.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        cg.CGEventGetLocation.restype = _CGPoint

        # 鼠标事件创建与连击次数（ClickState）整型属性设置
        cg.CGEventCreateMouseEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            _CGPoint,
            ctypes.c_uint32,
        ]
        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventSetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int64]
        cg.CGEventSetIntegerValueField.restype = None

        # 非变参滚轮事件 API（兼容 x86_64 与 Apple Silicon ARM64 寄存器传参 ABI）
        cg.CGEventCreateScrollWheelEvent2.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.c_int32,
        ]
        cg.CGEventCreateScrollWheelEvent2.restype = ctypes.c_void_p

        # 键盘虚拟键码事件、UTF-16LE Unicode 字符串挂载、修饰键掩码、HID 注入与 CF 引用计数释放
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

        # ApplicationServices 辅助功能（Accessibility / AXUIElement）与 CoreFoundation 64 位安全绑定
        app = self._app
        if hasattr(app, "AXIsProcessTrusted"):
            app.AXIsProcessTrusted.argtypes = []
            app.AXIsProcessTrusted.restype = ctypes.c_bool
        if hasattr(app, "AXUIElementCreateSystemWide"):
            app.AXUIElementCreateSystemWide.argtypes = []
            app.AXUIElementCreateSystemWide.restype = ctypes.c_void_p
        if hasattr(app, "AXUIElementCopyAttributeValue"):
            app.AXUIElementCopyAttributeValue.argtypes = [
                ctypes.c_void_p,
                ctypes.c_void_p,
                ctypes.POINTER(ctypes.c_void_p),
            ]
            app.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
        if hasattr(app, "AXValueGetValue"):
            app.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p]
            app.AXValueGetValue.restype = ctypes.c_bool
        if hasattr(app, "AXValueGetTypeID"):
            app.AXValueGetTypeID.argtypes = []
            app.AXValueGetTypeID.restype = ctypes.c_ulong
        if hasattr(app, "CFArrayGetCount"):
            app.CFArrayGetCount.argtypes = [ctypes.c_void_p]
            app.CFArrayGetCount.restype = ctypes.c_long
        if hasattr(app, "CFArrayGetValueAtIndex"):
            app.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
            app.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        if hasattr(app, "CFArrayGetTypeID"):
            app.CFArrayGetTypeID.argtypes = []
            app.CFArrayGetTypeID.restype = ctypes.c_ulong
        if hasattr(app, "CFStringCreateWithCString"):
            app.CFStringCreateWithCString.argtypes = [
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_uint32,
            ]
            app.CFStringCreateWithCString.restype = ctypes.c_void_p
        if hasattr(app, "CFStringGetCString"):
            app.CFStringGetCString.argtypes = [
                ctypes.c_void_p,
                ctypes.c_char_p,
                ctypes.c_long,
                ctypes.c_uint32,
            ]
            app.CFStringGetCString.restype = ctypes.c_bool
        if hasattr(app, "CFStringGetTypeID"):
            app.CFStringGetTypeID.argtypes = []
            app.CFStringGetTypeID.restype = ctypes.c_ulong
        if hasattr(app, "CFGetTypeID"):
            app.CFGetTypeID.argtypes = [ctypes.c_void_p]
            app.CFGetTypeID.restype = ctypes.c_ulong
        if hasattr(app, "CFRelease"):
            app.CFRelease.argtypes = [ctypes.c_void_p]
            app.CFRelease.restype = None

    def _require_accessibility(self) -> None:
        """前置校验 macOS TCC 辅助功能权限：未授权时 WindowServer 会静默丢弃全部注入键鼠事件，必须显式阻断。"""
        if hasattr(self._app, "AXIsProcessTrusted") and not bool(self._app.AXIsProcessTrusted()):
            raise ComputerToolError(
                "accessibility_permission_required",
                "当前终端/进程未获得 macOS『辅助功能 (Accessibility)』权限，请在 系统设置 -> 隐私与安全性 -> 辅助功能 中开启。",
            )

    def _require_screen_recording(self) -> None:
        """前置校验 macOS TCC 屏幕录制权限：未授权时截屏仅含桌面壁纸而无应用窗口，提前报错可避免视觉模型幻觉。"""
        if hasattr(self._cg, "CGPreflightScreenCaptureAccess") and not bool(
            self._cg.CGPreflightScreenCaptureAccess()
        ):
            raise ComputerToolError(
                "screen_recording_permission_required",
                "当前终端/进程未获得 macOS『屏幕录制 (Screen Recording)』权限，请在 系统设置 -> 隐私与安全性 -> 录屏与系统录音 中开启。",
            )

    def _active_displays(self) -> list[dict[str, Any]]:
        """通过 `CGGetActiveDisplayList` + `CGDisplayBounds` 枚举全部在线显示器的逻辑坐标矩形。"""
        if not (hasattr(self._cg, "CGGetActiveDisplayList") and hasattr(self._cg, "CGDisplayBounds")):
            return []
        max_displays = 32
        disp_ids = (ctypes.c_uint32 * max_displays)()
        count = ctypes.c_uint32(0)
        err = int(self._cg.CGGetActiveDisplayList(max_displays, disp_ids, ctypes.byref(count)))
        if err != 0 or count.value == 0:
            return []
        main_id = int(self._cg.CGMainDisplayID())
        displays: list[dict[str, Any]] = []
        for idx in range(min(int(count.value), max_displays)):
            did = int(disp_ids[idx])
            rect = self._cg.CGDisplayBounds(did)
            displays.append(
                {
                    "display_id": did,
                    "is_main": did == main_id,
                    "x": round(float(rect.origin.x), 1),
                    "y": round(float(rect.origin.y), 1),
                    "width": round(float(rect.size.width), 1),
                    "height": round(float(rect.size.height), 1),
                }
            )
        return displays

    def _logical_bounds_and_cursor(self) -> tuple[int, int, float, float]:
        """读取主显示器逻辑点尺寸（Points）与当前光标逻辑坐标，并确保释放临时 CGEvent 对象。"""
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
                # CGEventCreate 遵循 CoreFoundation "Create Rule"（引用计数=1），必须手动 CFRelease 防内存泄漏
                self._cg.CFRelease(ev)
        # 在纯 SSH 无图形登录会话（无 WindowServer 连接）下宽高会返回 0，此时立即宣告桌面不可用
        if width <= 0 or height <= 0:
            raise ComputerToolError("gui_unavailable", "未检测到活动图形显示器会话（Logical Display 为 0）。")
        return width, height, cx, cy

    def screen_info(self) -> dict[str, Any]:
        """获取主屏与多显示器联合边界逻辑坐标、实时光标位置及最近一次截图缓存的坐标比例尺。"""
        w, h, cx, cy = self._logical_bounds_and_cursor()
        displays = self._active_displays()
        if displays:
            min_x = min(d["x"] for d in displays)
            min_y = min(d["y"] for d in displays)
            max_x = max(d["x"] + d["width"] for d in displays)
            max_y = max(d["y"] + d["height"] for d in displays)
        else:
            min_x, min_y, max_x, max_y = 0.0, 0.0, float(w), float(h)
        return {
            "logical_width": w,
            "logical_height": h,
            "cursor_x": round(cx, 1),
            "cursor_y": round(cy, 1),
            "scale_x": round(self._last_scale_x, 4),
            "scale_y": round(self._last_scale_y, 4),
            "displays": displays,
            "desktop_bounds": {
                "min_x": round(min_x, 1),
                "min_y": round(min_y, 1),
                "max_x": round(max_x, 1),
                "max_y": round(max_y, 1),
                "width": round(max_x - min_x, 1),
                "height": round(max_y - min_y, 1),
            },
            "backend": "macos_coregraphics",
        }

    def _cf_string_to_str(self, cf_ref: int | None, *, max_bytes: int = 1024) -> str:
        """安全将 `CFStringRef` 转为 Python UTF-8 字符串，先核验 TypeID 避免误解引用非字符串对象。"""
        if not cf_ref:
            return ""
        if int(self._app.CFGetTypeID(cf_ref)) != int(self._app.CFStringGetTypeID()):
            return ""
        buf = ctypes.create_string_buffer(max_bytes)
        if not self._app.CFStringGetCString(cf_ref, buf, max_bytes, _kCFStringEncodingUTF8):
            return ""
        return buf.value.decode("utf-8", errors="replace")

    def _ax_copy_attr(self, elem_ref: int, attr_name: str) -> int | None:
        """读取指定 AXUIElement 属性（遵循 CoreFoundation Copy Rule：调用方须负责 CFRelease 返回的非空指针）。"""
        cf_attr = self._app.CFStringCreateWithCString(None, attr_name.encode("utf-8"), _kCFStringEncodingUTF8)
        if not cf_attr:
            return None
        try:
            out = ctypes.c_void_p()
            err = int(self._app.AXUIElementCopyAttributeValue(elem_ref, cf_attr, ctypes.byref(out)))
            if err != _kAXErrorSuccess or not out.value:
                return None
            return int(out.value)
        finally:
            self._app.CFRelease(cf_attr)

    def _ax_attr_str(self, elem_ref: int, attr_name: str) -> str:
        val_ref = self._ax_copy_attr(elem_ref, attr_name)
        if not val_ref:
            return ""
        try:
            return self._cf_string_to_str(val_ref)
        finally:
            self._app.CFRelease(val_ref)

    def _ax_geometry(self, elem_ref: int) -> tuple[float, float, float, float] | None:
        """读取 AXUIElement 的屏幕逻辑坐标 `(x, y, width, height)`，严格配对 `CFRelease`。"""
        pos_ref = self._ax_copy_attr(elem_ref, "AXPosition")
        size_ref = self._ax_copy_attr(elem_ref, "AXSize")
        try:
            if not pos_ref or not size_ref:
                return None
            ax_val_tid = int(self._app.AXValueGetTypeID())
            if int(self._app.CFGetTypeID(pos_ref)) != ax_val_tid or int(self._app.CFGetTypeID(size_ref)) != ax_val_tid:
                return None
            pt = _CGPoint()
            sz = _CGSize()
            ok_pt = bool(self._app.AXValueGetValue(pos_ref, _kAXValueTypeCGPoint, ctypes.byref(pt)))
            ok_sz = bool(self._app.AXValueGetValue(size_ref, _kAXValueTypeCGSize, ctypes.byref(sz)))
            if not (ok_pt and ok_sz):
                return None
            return float(pt.x), float(pt.y), float(sz.width), float(sz.height)
        finally:
            if pos_ref:
                self._app.CFRelease(pos_ref)
            if size_ref:
                self._app.CFRelease(size_ref)

    def ax_tree(self, *, max_nodes: int = 80, max_depth: int = 6) -> dict[str, Any]:
        """遍历当前前台焦点窗口的 macOS `AXUIElement` 无障碍控件树，返回结构化 UI 节点与点击中心点。"""
        self._require_accessibility()
        required_syms = (
            "AXUIElementCreateSystemWide",
            "AXUIElementCopyAttributeValue",
            "AXValueGetValue",
            "CFArrayGetCount",
            "CFArrayGetValueAtIndex",
            "CFStringCreateWithCString",
            "CFStringGetCString",
            "CFRelease",
        )
        if not all(hasattr(self._app, sym) for sym in required_syms):
            raise ComputerToolError("ax_unavailable", "当前系统 ApplicationServices 缺少 AXUIElement 符号。")

        clamped_nodes = max(1, min(200, int(max_nodes)))
        clamped_depth = max(1, min(12, int(max_depth)))
        nodes: list[dict[str, Any]] = []
        truncated = False
        app_name = ""
        window_title = ""

        sys_wide = self._app.AXUIElementCreateSystemWide()
        if not sys_wide:
            raise ComputerToolError("ax_failed", "无法创建系统级 AXUIElement 对象。")
        focused_app = None
        focused_win = None
        windows_arr = None
        try:
            focused_app = self._ax_copy_attr(sys_wide, "AXFocusedApplication")
            if not focused_app:
                return {
                    "backend": "macos_coregraphics",
                    "app_name": "",
                    "window_title": "",
                    "node_count": 0,
                    "max_nodes": clamped_nodes,
                    "max_depth": clamped_depth,
                    "truncated": False,
                    "nodes": [],
                }
            app_name = str(redact_secrets(self._ax_attr_str(focused_app, "AXTitle")))[:120]
            focused_win = self._ax_copy_attr(focused_app, "AXFocusedWindow")
            root_elem = focused_win
            if not root_elem:
                windows_arr = self._ax_copy_attr(focused_app, "AXWindows")
                if (
                    windows_arr
                    and int(self._app.CFGetTypeID(windows_arr)) == int(self._app.CFArrayGetTypeID())
                    and int(self._app.CFArrayGetCount(windows_arr)) > 0
                ):
                    root_elem = int(self._app.CFArrayGetValueAtIndex(windows_arr, 0))
            if not root_elem:
                root_elem = focused_app
            window_title = str(redact_secrets(self._ax_attr_str(root_elem, "AXTitle")))[:200]

            cf_arr_tid = int(self._app.CFArrayGetTypeID())

            def _walk(elem: int, depth: int) -> None:
                nonlocal truncated
                if len(nodes) >= clamped_nodes:
                    truncated = True
                    return
                role = self._ax_attr_str(elem, "AXRole")
                raw_title = self._ax_attr_str(elem, "AXTitle") or self._ax_attr_str(elem, "AXDescription")
                # 安全边界：严禁读取密码输入框（AXSecureTextField）的明文值
                raw_val = "" if role == "AXSecureTextField" else self._ax_attr_str(elem, "AXValue")
                geom = self._ax_geometry(elem)
                if geom is not None:
                    gx, gy, gw, gh = geom
                else:
                    gx, gy, gw, gh = 0.0, 0.0, 0.0, 0.0
                if role or raw_title or raw_val or (gw > 0 and gh > 0):
                    nodes.append(
                        {
                            "role": role[:80],
                            "title": str(redact_secrets(raw_title))[:160],
                            "value": str(redact_secrets(raw_val))[:200],
                            "x": round(gx, 1),
                            "y": round(gy, 1),
                            "width": round(gw, 1),
                            "height": round(gh, 1),
                            "center_x": round(gx + gw / 2.0, 1),
                            "center_y": round(gy + gh / 2.0, 1),
                        }
                    )
                if depth >= clamped_depth or len(nodes) >= clamped_nodes:
                    if len(nodes) >= clamped_nodes:
                        truncated = True
                    return
                children_ref = self._ax_copy_attr(elem, "AXChildren")
                if not children_ref:
                    return
                try:
                    if int(self._app.CFGetTypeID(children_ref)) != cf_arr_tid:
                        return
                    cnt = int(self._app.CFArrayGetCount(children_ref))
                    for idx in range(min(cnt, 128)):
                        if len(nodes) >= clamped_nodes:
                            truncated = True
                            break
                        # CFArrayGetValueAtIndex 遵循 CoreFoundation Get Rule（借用引用，禁止单独 CFRelease）
                        child_ptr = self._app.CFArrayGetValueAtIndex(children_ref, idx)
                        if child_ptr:
                            _walk(int(child_ptr), depth + 1)
                finally:
                    self._app.CFRelease(children_ref)

            _walk(int(root_elem), 0)
        finally:
            if windows_arr:
                self._app.CFRelease(windows_arr)
            if focused_win:
                self._app.CFRelease(focused_win)
            if focused_app:
                self._app.CFRelease(focused_app)
            self._app.CFRelease(sys_wide)

        return {
            "backend": "macos_coregraphics",
            "app_name": app_name,
            "window_title": window_title,
            "node_count": len(nodes),
            "max_nodes": clamped_nodes,
            "max_depth": clamped_depth,
            "truncated": truncated,
            "nodes": nodes,
        }

    def frontmost_app(self) -> dict[str, Any]:
        """通过 AppleScript (`System Events`) 查询当前持有键盘焦点的前台应用进程名称。"""
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
        """截取主屏 PNG 并使用系统原生 `sips` 缩放对齐坐标系，消除 Retina 2x 物理像素偏移。

        Retina 坐标对齐原理：
        - 在 MacBook Retina 显示屏上，`CGDisplayPixelsWide/High` 返回逻辑点（如 1512x982），但
          `screencapture` 落盘的 PNG 为 BackingStore 物理像素（如 3024x1964，即 2x HiDPI）。
        - 若直接把 3024x1964 原图喂给多模态大模型，不仅图片体积与视觉 Token 翻 4 倍，模型基于图像像素
          输出的点击坐标 `(x, y)` 直接传给 `CGEvent` 还会偏离目标右下角整整一倍！
        - 因此先将目标尺寸对齐至逻辑分辨率（超 1440px 时等比封顶），调用 `/usr/bin/sips -z <高> <宽>`
          原地重采样，并精确记录 `_last_scale_x = logical_w / img_w` 供后续鼠标动作无损反算。
        """
        self._require_screen_recording()
        logical_w, logical_h, cx, cy = self._logical_bounds_and_cursor()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # `-x` 静默截屏（关闭快门提示音），`-t png` 强制无损 PNG 以便后续解析 IHDR 头
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

        # 注意 macOS `sips -z` 参数顺序为 `pixelsHigh pixelsWide`（先高度 `target_h`，后宽度 `target_w`）
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
        """构造单个底层 CGEvent 鼠标事件并注入 HID 事件流，严格在 `finally` 中释放 CF 对象。"""
        ev = self._cg.CGEventCreateMouseEvent(None, ev_type, pt, button)
        if not ev:
            raise ComputerToolError("cgevent_failed", "无法创建 CGEvent 鼠标事件。")
        try:
            if clicks > 0:
                # 显式写入 kCGMouseEventClickState（单击=1，双击第二拍=2），否则 Cocoa 控件不会触发双击回调
                self._cg.CGEventSetIntegerValueField(ev, _kCGMouseEventClickState, clicks)
            self._cg.CGEventPost(_kCGHIDEventTap, ev)
        finally:
            self._cg.CFRelease(ev)

    def _to_logical_point(self, x: float, y: float, logical_w: int, logical_h: int) -> tuple[float, float]:
        """将模型输出的图像像素坐标换算为多显示器 CoreGraphics 逻辑点坐标，并拦截越界偏移。"""
        lx = float(x) * self._last_scale_x
        ly = float(y) * self._last_scale_y
        displays = self._active_displays()
        if displays:
            in_any = any(
                d["x"] <= lx <= d["x"] + d["width"] and d["y"] <= ly <= d["y"] + d["height"]
                for d in displays
            )
            if not in_any:
                raise ComputerToolError(
                    "coordinates_out_of_bounds",
                    f"换算后的屏幕逻辑坐标 ({lx:.1f}, {ly:.1f}) 超出所有在线显示器边界。",
                )
            return lx, ly
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
        """执行鼠标移动、单双击、右/中键点击、线性插值拖拽或双轴像素级滚轮事件。"""
        self._require_accessibility()
        logical_w, logical_h, cur_x, cur_y = self._logical_bounds_and_cursor()

        if action == "scroll":
            if x is not None and y is not None:
                lx, ly = self._to_logical_point(x, y, logical_w, logical_h)
                self._post_mouse(_kCGEventMouseMoved, _CGPoint(lx, ly))
                cur_x, cur_y = lx, ly
            # 注意 CGEventCreateScrollWheelEvent2 的第 4、5 参数顺序：wheel1 为垂直滚动 (dy)，wheel2 为水平滚动 (dx)
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

        # 点击前先派发 MouseMoved：许多浏览器 DOM 与 Electron 按钮需先触发 hover 激活命中态才响应按下事件；
        # Down 与 Up 之间留出 10~15ms 短暂间隙，防止目标应用事件循环在同一帧合并/丢弃瞬时脉冲。
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
            # 分 5 步线性插值发送 Dragged 轨迹：GUI 框架通常设有最小拖拽距离阈值，直接瞬移到终点不会触发拖拽手势
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
        """将短文本块编码为 UTF-16LE `UniChar` 数组并挂载至空键码事件注入。

        为什么用 `len(utf16_bytes) // 2` 而非 `len(text_chunk)`：
        `CGEventKeyboardSetUnicodeString` 接收的是 16 位 UTF-16 代码单元（`UniChar`）个数。对于超出基本多文种
        平面（BMP）的辅助平面字符（如 `U+1F680` 等 Emoji 或生僻汉字），Python `len(str)` 为 1，但在 UTF-16LE 中占
        2 个代理对（Surrogate Pair，4 字节）；按字节长度除以 2 计算 `count` 才能完整发送代理对而不截断高位字符。
        """
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
        """执行任意 Unicode 文本分块输入（`type`）或带修饰键掩码的硬件快捷键触发（`key`）。"""
        self._require_accessibility()
        if action == "type":
            if not text:
                raise ComputerToolError("invalid_arguments", "type 动作必须提供非空 text。")
            # 按最多 16 个字符分块发送 Unicode 事件：Quartz 单次键盘事件内部 UniChar 缓冲区约 20 单元上限，
            # 分块加 8ms 节流可稳定兼容中日韩字符、Emoji 与长代码片段输入而不溢出丢字。
            for offset in range(0, len(text), 16):
                self._post_unicode_chunk(text[offset : offset + 16])
            return {"action": "type", "chars_typed": len(text)}

        if action == "key":
            parts = [p.strip().lower() for p in key.split("+") if p.strip()]
            if not parts:
                raise ComputerToolError("invalid_arguments", "key 动作必须提供按键名或组合键（如 cmd+c、enter）。")
            flags = 0
            # 前 N-1 段解析为修饰键位掩码（如 cmd+shift+p 中的 cmd 与 shift），末段解析为主物理虚拟键码
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
        """安全唤起本机应用程序或默认浏览器链接（无 shell 参数列表调用 + 严格 Scheme 白名单）。

        安全边界：
        - `kind == 'app'` 强制匹配 `_SAFE_APP_NAME_RE`，阻断路径穿越或向 `/usr/bin/open` 注入恶意选项；
        - `kind == 'url'` 强制限制 `http` / `https` 且含合法域名，阻断 `file:///etc/passwd`、`ssh://` 或
          第三方应用自定义危险 URL Scheme 的自动触发。
        """
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


_X11_KEYSYM_MAP: dict[str, str] = {
    "cmd": "Super_L",
    "command": "Super_L",
    "super": "Super_L",
    "meta": "Super_L",
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "opt": "alt",
    "option": "alt",
    "shift": "shift",
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "tab": "Tab",
    "space": "space",
    "backspace": "BackSpace",
    "delete": "BackSpace",
    "forward_delete": "Delete",
    "left": "Left",
    "arrowleft": "Left",
    "right": "Right",
    "arrowright": "Right",
    "up": "Up",
    "arrowup": "Up",
    "down": "Down",
    "arrowdown": "Down",
    "home": "Home",
    "end": "End",
    "pageup": "Prior",
    "pagedown": "Next",
}


class LinuxX11ComputerBackend:
    """Linux X11 桌面驱动骨架（基于 `scrot`/`import` 截屏与 `xdotool` 键鼠合成，零 `shell=True`）。

    缺少 `DISPLAY`、`xdotool` 或截屏命令时在构造阶段抛出 `ComputerToolError("gui_unavailable", ...)`
    并附带清晰安装提示，由 `get_default_computer_backend()` 优雅降级为 `UnavailableComputerBackend`。
    """

    def __init__(self) -> None:
        self._xdotool = shutil.which("xdotool")
        self._scrot = shutil.which("scrot")
        self._import_bin = shutil.which("import")
        self._xdg_open = shutil.which("xdg-open")
        missing: list[str] = []
        if not os.environ.get("DISPLAY"):
            missing.append("DISPLAY 环境变量")
        if not self._xdotool:
            missing.append("xdotool")
        if not (self._scrot or self._import_bin):
            missing.append("scrot 或 imagemagick (import)")
        if missing:
            raise ComputerToolError(
                "gui_unavailable",
                f"Linux X11 桌面驱动缺少依赖（{', '.join(missing)}）；"
                "请安装：sudo apt-get install -y xdotool scrot（或 imagemagick）并配置 DISPLAY。",
            )
        self._last_scale_x = 1.0
        self._last_scale_y = 1.0

    def _run_cmd(self, cmd: list[str], *, timeout: float = 10.0) -> str:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        if proc.returncode != 0:
            raise ComputerToolError(
                "x11_command_failed",
                f"X11 命令失败（{' '.join(cmd[:2])}）：{(proc.stderr or proc.stdout).strip()[:240]}",
            )
        return proc.stdout

    def screen_info(self) -> dict[str, Any]:
        assert self._xdotool is not None
        geom_out = self._run_cmd([self._xdotool, "getdisplaygeometry"]).strip().split()
        if len(geom_out) < 2:
            raise ComputerToolError("gui_unavailable", "无法解析 xdotool getdisplaygeometry 输出。")
        w, h = int(geom_out[0]), int(geom_out[1])
        loc_out = self._run_cmd([self._xdotool, "getmouselocation", "--shell"])
        cx, cy = 0.0, 0.0
        for line in loc_out.splitlines():
            if line.startswith("X="):
                cx = float(line.split("=", 1)[1])
            elif line.startswith("Y="):
                cy = float(line.split("=", 1)[1])
        return {
            "logical_width": w,
            "logical_height": h,
            "cursor_x": round(cx, 1),
            "cursor_y": round(cy, 1),
            "scale_x": round(self._last_scale_x, 4),
            "scale_y": round(self._last_scale_y, 4),
            "displays": [
                {"display_id": 0, "is_main": True, "x": 0.0, "y": 0.0, "width": float(w), "height": float(h)}
            ],
            "desktop_bounds": {
                "min_x": 0.0,
                "min_y": 0.0,
                "max_x": float(w),
                "max_y": float(h),
                "width": float(w),
                "height": float(h),
            },
            "backend": "linux_x11",
        }

    def frontmost_app(self) -> dict[str, Any]:
        assert self._xdotool is not None
        name = self._run_cmd([self._xdotool, "getactivewindow", "getwindowname"]).strip()
        return {"app_name": name, "backend": "linux_x11"}

    def ax_tree(self, *, max_nodes: int = 80, max_depth: int = 6) -> dict[str, Any]:
        del max_nodes, max_depth
        raise ComputerToolError(
            "ax_unavailable",
            "Linux X11 后端暂未集成 AT-SPI 无障碍树（可改用 computer_screen(action='screenshot') 视觉定位）。",
        )

    def take_screenshot(self, output_path: Path) -> dict[str, Any]:
        info = self.screen_info()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if self._scrot:
            self._run_cmd([self._scrot, "-o", str(output_path)])
        else:
            assert self._import_bin is not None
            self._run_cmd([self._import_bin, "-window", "root", str(output_path)])
        if not output_path.is_file():
            raise ComputerToolError("screenshot_failed", "Linux X11 截屏文件未生成。")
        raw_bytes = output_path.read_bytes()
        img_w, img_h = _read_png_dimensions(raw_bytes)
        logical_w, logical_h = int(info["logical_width"]), int(info["logical_height"])
        self._last_scale_x = logical_w / float(max(1, img_w))
        self._last_scale_y = logical_h / float(max(1, img_h))
        return {
            "image_width": img_w,
            "image_height": img_h,
            "logical_width": logical_w,
            "logical_height": logical_h,
            "scale_x": round(self._last_scale_x, 4),
            "scale_y": round(self._last_scale_y, 4),
            "cursor_x": info["cursor_x"],
            "cursor_y": info["cursor_y"],
            "sha256": hashlib.sha256(raw_bytes).hexdigest(),
            "bytes": len(raw_bytes),
        }

    def _to_logical(self, x: float, y: float, w: int, h: int) -> tuple[int, int]:
        lx = int(round(float(x) * self._last_scale_x))
        ly = int(round(float(y) * self._last_scale_y))
        if not (0 <= lx <= w and 0 <= ly <= h):
            raise ComputerToolError(
                "coordinates_out_of_bounds",
                f"换算后的 X11 坐标 ({lx}, {ly}) 超出屏幕范围 ({w}x{h})。",
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
        assert self._xdotool is not None
        info = self.screen_info()
        w, h = int(info["logical_width"]), int(info["logical_height"])
        cur_x, cur_y = float(info["cursor_x"]), float(info["cursor_y"])
        if action == "scroll":
            if x is not None and y is not None:
                lx, ly = self._to_logical(x, y, w, h)
                self._run_cmd([self._xdotool, "mousemove", "--sync", str(lx), str(ly)])
                cur_x, cur_y = float(lx), float(ly)
            if scroll_dy != 0:
                btn = "4" if scroll_dy > 0 else "5"
                repeats = max(1, min(20, abs(int(scroll_dy)) // 40 or 1))
                self._run_cmd([self._xdotool, "click", "--repeat", str(repeats), btn])
            if scroll_dx != 0:
                btn = "6" if scroll_dx > 0 else "7"
                repeats = max(1, min(20, abs(int(scroll_dx)) // 40 or 1))
                self._run_cmd([self._xdotool, "click", "--repeat", str(repeats), btn])
            return {"action": "scroll", "cursor_x": round(cur_x, 1), "cursor_y": round(cur_y, 1)}

        if x is None or y is None:
            raise ComputerToolError("invalid_arguments", f"鼠标动作 {action} 必须提供目标坐标 x 与 y。")
        lx, ly = self._to_logical(x, y, w, h)
        if action == "move":
            self._run_cmd([self._xdotool, "mousemove", "--sync", str(lx), str(ly)])
        elif action == "click":
            self._run_cmd([self._xdotool, "mousemove", "--sync", str(lx), str(ly), "click", "1"])
        elif action == "double_click":
            self._run_cmd([self._xdotool, "mousemove", "--sync", str(lx), str(ly), "click", "--repeat", "2", "1"])
        elif action == "right_click":
            self._run_cmd([self._xdotool, "mousemove", "--sync", str(lx), str(ly), "click", "3"])
        elif action == "middle_click":
            self._run_cmd([self._xdotool, "mousemove", "--sync", str(lx), str(ly), "click", "2"])
        elif action == "drag":
            if from_x is not None and from_y is not None:
                sx, sy = self._to_logical(from_x, from_y, w, h)
            else:
                sx, sy = int(round(cur_x)), int(round(cur_y))
            self._run_cmd(
                [
                    self._xdotool,
                    "mousemove",
                    "--sync",
                    str(sx),
                    str(sy),
                    "mousedown",
                    "1",
                    "mousemove",
                    "--sync",
                    str(lx),
                    str(ly),
                    "mouseup",
                    "1",
                ]
            )
        else:
            raise ComputerToolError("invalid_arguments", f"不支持的鼠标动作：{action}")
        return {"action": action, "cursor_x": float(lx), "cursor_y": float(ly)}

    def keyboard_action(self, action: str, *, text: str = "", key: str = "") -> dict[str, Any]:
        assert self._xdotool is not None
        if action == "type":
            if not text:
                raise ComputerToolError("invalid_arguments", "type 动作必须提供非空 text。")
            self._run_cmd([self._xdotool, "type", "--clearmodifiers", "--delay", "10", "--", text])
            return {"action": "type", "chars_typed": len(text)}
        if action == "key":
            parts = [p.strip().lower() for p in key.split("+") if p.strip()]
            if not parts:
                raise ComputerToolError("invalid_arguments", "key 动作必须提供按键名或组合键。")
            mapped = [_X11_KEYSYM_MAP.get(p, p) for p in parts]
            self._run_cmd([self._xdotool, "key", "--clearmodifiers", "+".join(mapped)])
            return {"action": "key", "key": key}
        raise ComputerToolError("invalid_arguments", f"不支持的键盘动作：{action}")

    def open_target(self, target: str, *, kind: str = "app") -> dict[str, Any]:
        cleaned = target.strip()
        if kind == "url":
            parsed = urllib.parse.urlparse(cleaned)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ComputerToolError("unsafe_target", "URL 仅允许合法的 http:// 或 https:// 地址。")
            if not self._xdg_open:
                raise ComputerToolError("open_failed", "系统缺少 xdg-open，无法打开 URL。")
            self._run_cmd([self._xdg_open, cleaned])
            return {"opened": cleaned, "kind": kind}
        if kind == "app":
            if not _SAFE_APP_NAME_RE.fullmatch(cleaned):
                raise ComputerToolError("unsafe_target", "应用名称仅允许字母、数字、空格与常见符号。")
            exe = shutil.which(cleaned)
            if not exe:
                raise ComputerToolError("open_failed", f"在 PATH 中未找到可执行应用：{cleaned}")
            subprocess.Popen([exe], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            return {"opened": cleaned, "kind": kind}
        raise ComputerToolError("invalid_arguments", "kind 必须是 app 或 url。")


def get_default_computer_backend() -> ComputerBackend:
    """按宿主操作系统探测默认桌面后端：Darwin 用原生 Quartz/AX，Linux 优先探测 X11，其余优雅降级。"""
    sys_name = platform.system()
    if sys_name == "Darwin":
        try:
            return MacOSNativeBackend()
        except ComputerToolError as exc:
            return UnavailableComputerBackend(str(exc))
    if sys_name == "Linux":
        try:
            return LinuxX11ComputerBackend()
        except ComputerToolError as exc:
            return UnavailableComputerBackend(str(exc))
    return UnavailableComputerBackend(f"当前平台 ({sys_name}) 暂不支持原生桌面 GUI 驱动")


# 暴露给大模型的 Computer Use 四大原子工具 JSON Schema（保持稳定结构以维护 KV Cache 前缀命中）
COMPUTER_TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "computer_screen",
        "description": "获取桌面屏幕信息、前台活动应用、焦点窗口无障碍控件树（ax_tree），或截取当前桌面截图（截图自动缩放对齐并传递视觉画面）。",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["screenshot", "info", "frontmost_app", "ax_tree"],
                    "description": "读取操作：screenshot（截屏）、info（多屏尺寸与光标位置）、frontmost_app（当前前台应用）、ax_tree（焦点窗口无障碍控件树）。",
                },
                "max_nodes": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 200,
                    "description": "action=ax_tree 时最多返回的 UI 节点数（默认 80）。",
                },
                "max_depth": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 12,
                    "description": "action=ax_tree 时的最大遍历树深度（默认 6）。",
                },
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
                "x": {"type": "number", "minimum": -10000, "maximum": 20000},
                "y": {"type": "number", "minimum": -10000, "maximum": 20000},
                "from_x": {"type": "number", "minimum": -10000, "maximum": 20000},
                "from_y": {"type": "number", "minimum": -10000, "maximum": 20000},
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
    """统一执行桌面 GUI 工具（含只读直通、跨轮比例尺恢复、权限提案拦截、Epoch 防脑裂校验与截图持久化）。

    执行管线设计：
    1. 只读观测快速通道（`computer_screen`）：截屏 PNG 落盘到独立 `artifacts/<run_id>/` 目录（不在 SQLite
       消息表塞入数百 KB Base64 以保护上下文预算与数据库吞吐），并写入 `gui_screenshot` 事件供下一轮按需挂载视觉附件；
    2. 无状态 Backend 比例尺自愈：若进程重启或审批恢复后新建了 `MacOSNativeBackend`，在执行鼠标动作前自动倒序
       检索 `gui_screenshot` 历史事件找回 `scale_x / scale_y`；
    3. 权限护栏与脑裂围栏（Epoch Fencing）：在非 `bypass_approval` 时经 `evaluate_tool_permission` 裁决，
       需人工确认时持久化 `gui_action` 提案并挂起；真正向物理桌面注入副作用前强制 `storage.verify_epoch`，
       杜绝超时失联的僵尸 Worker 在用户真实屏幕上乱点乱输。
    """
    if name == "computer_screen":
        action = arguments["action"]
        if action == "info":
            return {"ok": True, **backend.screen_info()}
        if action == "frontmost_app":
            return {"ok": True, **backend.frontmost_app()}
        if action == "ax_tree":
            max_nodes = int(arguments.get("max_nodes", 80))
            max_depth = int(arguments.get("max_depth", 6))
            with _desktop_gui_seat_lock(storage):
                tree = backend.ax_tree(max_nodes=max_nodes, max_depth=max_depth)
            return {"ok": True, "action": "ax_tree", **tree}
        if action == "screenshot":
            # 清洗 call_id 中的非安全路径字符，防止模型构造恶意 tool_use id 造成目录穿越写文件
            safe_cid = re.sub(r"[^A-Za-z0-9_.-]", "_", call_id or f"shot-{int(time.time() * 1000)}")[:64]
            artifact_name = f"screen-{safe_cid}.png"
            out_path = Path(artifacts_dir).resolve() / run_id / artifact_name
            with _desktop_gui_seat_lock(storage):
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

    # 人类审批状态机拦截：ask / auto-edit 模式下键鼠与应用唤起均返回 requires_approval，交由 REPL/Web 确认
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

    # 物理副作用执行前的最后一道防线：校验当前 Worker 持有的租约代数（Epoch）并在跨进程桌面席位锁下原子注入事件
    storage.verify_epoch(run_id, expected_epoch)
    with _desktop_gui_seat_lock(storage):
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
