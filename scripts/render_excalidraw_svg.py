#!/usr/bin/env python3
"""将 docs/diagrams/*.excalidraw 确定性渲染为出版级纯矢量 SVG 图集。

设计目标与工程约束：
1. 零第三方依赖：仅使用 Python 3.11+ 标准库（json / math / pathlib / unicodedata / xml），
   无需 Node.js、无头浏览器或 Cairo/Skia 原生扩展，可在任何 CI 与开发者本机毫秒级复现。
2. 字节级确定性输出：对相同输入的 .excalidraw JSON，生成的 SVG 在不同运行批次间完全一致
   （统一数值格式化精度、按颜色与方向排序 <defs> 箭头标记、稳定遍历非删除图元）。
3. 忠实还原 Excalidraw v2 几何与排版语义：
   - 画布视口：跨全部非删除图元（含多折点负偏移连线与中英文估算字宽）精确计算外接包围盒，
     留出均匀边距（默认 36px）并铺设圆角背景底板，兼顾 GitHub 浅色与深色主题阅读体验。
   - 基础几何图元：完整支持 rectangle（自适应圆角 rx/ry）、ellipse、diamond（多边形顶点）、
     line 与 arrow（支持多折点圆角贝塞尔过渡、双向/单向着色 <marker> 箭头及容器边界裁剪）。
   - 文本与标签排版：严格转义 XML 实体，保留多行 <tspan x="..." dy="..."> 结构、左/中/右对齐、
     容器垂直居中基线校准、连线标签背景遮罩（防止线条贯穿文字），以及兼顾中英文混排的
     无衬线 / 等宽（fontFamily == 3）系统字体栈。
4. 内置 XML 语法自检：每生成一幅 SVG 立即通过标准库 xml.etree.ElementTree.parse 解析校验，
   确保 100% 符合 XML 1.0 与 SVG 1.1 规范。
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import re
import unicodedata
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape

# 默认路径约定：相对于仓库根目录定位输入 Excalidraw 画布与输出 SVG 目录
REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = REPO_ROOT / "docs" / "diagrams"
DEFAULT_OUTPUT_DIR = DEFAULT_INPUT_DIR / "svg"
DEFAULT_PADDING = 36.0

# 字体栈定义：兼顾 macOS / Windows / Linux 与中英文混排清晰度
FONT_SANS = (
    "ui-sans-serif, -apple-system, BlinkMacSystemFont, 'Segoe UI', "
    "'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'Noto Sans CJK SC', sans-serif"
)
FONT_SERIF = (
    "ui-serif, Georgia, Cambria, 'Times New Roman', "
    "'Songti SC', 'SimSun', 'Noto Serif CJK SC', serif"
)
FONT_MONO = (
    "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "
    "'Liberation Mono', 'PingFang SC', 'Microsoft YaHei', monospace"
)

# XML 1.0 非法控制字符过滤正则（保留 \t \n \r）
_ILLEGAL_XML_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


def _fmt(value: float) -> str:
    """将浮点数格式化为紧凑确定性字符串（保留最多 2 位小数并消除 -0.0）。"""
    if abs(value) < 1e-9:
        return "0"
    rounded = round(float(value), 2)
    if abs(rounded) < 1e-9:
        return "0"
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def _esc_text(text: str) -> str:
    """转义 SVG 文本节点内容并剔除非法 XML 控制字符。"""
    cleaned = _ILLEGAL_XML_CHARS_RE.sub("", text)
    return xml_escape(cleaned)


def _esc_attr(value: str) -> str:
    """转义 SVG 双引号属性值。"""
    cleaned = _ILLEGAL_XML_CHARS_RE.sub("", str(value))
    return xml_escape(cleaned, {'"': "&quot;"})


def _color_token(color: str) -> str:
    """将颜色字符串归一化为可用于 XML id 的稳定字母数字标识。"""
    raw = (color or "#1e1e1e").strip().lower()
    token = re.sub(r"[^a-z0-9]+", "", raw)
    return token or "default"


def _font_family_for(code: int | None) -> str:
    """映射 Excalidraw fontFamily 枚举：3 为等宽代码字体，2 为衬线，其余采用现代无衬线栈。"""
    if code == 3:
        return FONT_MONO
    if code == 2:
        return FONT_SERIF
    return FONT_SANS


def _estimate_line_width(line: str, font_size: float, is_mono: bool = False) -> float:
    """按东亚字符宽度属性（East Asian Width）估算单行像素宽度，保障包围盒与连线标签遮罩充裕。"""
    width = 0.0
    ascii_factor = 0.62 if is_mono else 0.58
    cjk_factor = 1.02
    for ch in line:
        eaw = unicodedata.east_asian_width(ch)
        if eaw in ("W", "F"):
            width += font_size * cjk_factor
        else:
            width += font_size * ascii_factor
    return width


def _normalize_rect(element: dict) -> tuple[float, float, float, float]:
    """归一化图元矩形坐标，兼容负宽高情形，返回 (x, y, width, height)。"""
    x = float(element.get("x", 0.0))
    y = float(element.get("y", 0.0))
    w = float(element.get("width", 0.0))
    h = float(element.get("height", 0.0))
    if w < 0:
        x += w
        w = -w
    if h < 0:
        y += h
        h = -h
    return x, y, w, h


def _corner_radius(width: float, height: float, roundness: dict | int | float | None) -> float:
    """根据 Excalidraw v2 roundness 配置计算圆角半径 rx/ry。"""
    if not roundness:
        return 0.0
    min_dim = min(width, height)
    if min_dim <= 0:
        return 0.0
    if isinstance(roundness, (int, float)):
        return min(float(roundness), min_dim * 0.5)
    if isinstance(roundness, dict):
        if "value" in roundness and roundness["value"] is not None:
            return min(float(roundness["value"]), min_dim * 0.5)
        rtype = roundness.get("type")
        if rtype == 2:
            return min(min_dim * 0.25, 24.0)
        if rtype == 3:
            # 自适应工程卡片圆角：小徽章按比例缩放，大容器封顶 14px，保持架构图利落感
            return min(14.0, min_dim * 0.12)
    return 0.0


def _stroke_attrs(element: dict) -> list[str]:
    """构造描边颜色、线宽与虚线样式属性列表。"""
    stroke_color = str(element.get("strokeColor") or "none").strip()
    stroke_width = float(element.get("strokeWidth", 1.0))
    stroke_style = str(element.get("strokeStyle") or "solid").strip()
    if not stroke_color or stroke_color == "transparent" or stroke_width <= 0:
        return ['stroke="none"']

    attrs = [
        f'stroke="{_esc_attr(stroke_color)}"',
        f'stroke-width="{_fmt(stroke_width)}"',
    ]
    if stroke_style == "dashed":
        dash = max(6.0, stroke_width * 4.0)
        gap = max(4.5, stroke_width * 3.0)
        attrs.append(f'stroke-dasharray="{_fmt(dash)} {_fmt(gap)}"')
    elif stroke_style == "dotted":
        dot = max(1.5, stroke_width * 1.1)
        gap = max(3.5, stroke_width * 2.4)
        attrs.append(f'stroke-dasharray="{_fmt(dot)} {_fmt(gap)}"')
        attrs.append('stroke-linecap="round"')
    return attrs


def _fill_attrs(element: dict) -> list[str]:
    """构造填充颜色与填充样式属性列表（兼容 solid / hachure / cross-hatch）。"""
    bg = str(element.get("backgroundColor") or "transparent").strip()
    if not bg or bg == "transparent":
        return ['fill="none"']
    fill_style = str(element.get("fillStyle") or "solid").strip()
    attrs = [f'fill="{_esc_attr(bg)}"']
    # 非纯色手绘填充在纯矢量工程图中折算为柔和半透明底色，既保留层次又避免杂乱斜线干扰文字阅读
    if fill_style in {"hachure", "cross-hatch"}:
        attrs.append('fill-opacity="0.35"')
    return attrs


def _opacity_attr(element: dict) -> list[str]:
    """提取透明度属性（0..100 映射至 0..1，100 时省略以保持 SVG 精简）。"""
    opacity = float(element.get("opacity", 100.0))
    if opacity >= 99.9:
        return []
    clamped = max(0.0, min(1.0, opacity / 100.0))
    return [f'opacity="{_fmt(clamped)}"']


def _rotation_attr(element: dict, cx: float, cy: float) -> list[str]:
    """当 angle 非零时生成围绕图元中心 (cx, cy) 的 SVG rotate 变换属性。"""
    angle_rad = float(element.get("angle") or 0.0)
    if abs(angle_rad) < 1e-6:
        return []
    angle_deg = math.degrees(angle_rad)
    return [f'transform="rotate({_fmt(angle_deg)} {_fmt(cx)} {_fmt(cy)})"']


def _rotate_point(px: float, py: float, cx: float, cy: float, angle_rad: float) -> tuple[float, float]:
    """将点 (px, py) 绕 (cx, cy) 旋转 angle_rad 弧度，用于精确计算旋转图元外接框。"""
    if abs(angle_rad) < 1e-6:
        return px, py
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)
    dx = px - cx
    dy = py - cy
    return cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a


def _extract_world_points(element: dict) -> list[tuple[float, float]]:
    """提取 line / arrow 的全局坐标折点序列，并剔除相邻重复零长点。"""
    ox = float(element.get("x", 0.0))
    oy = float(element.get("y", 0.0))
    raw_points = element.get("points") or []
    if not raw_points:
        w = float(element.get("width", 0.0))
        h = float(element.get("height", 0.0))
        raw_points = [[0.0, 0.0], [w, h]]

    pts: list[tuple[float, float]] = []
    for item in raw_points:
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            wx = ox + float(item[0])
            wy = oy + float(item[1])
            if not pts or math.hypot(wx - pts[-1][0], wy - pts[-1][1]) > 1e-3:
                pts.append((wx, wy))
    if len(pts) == 1:
        pts.append((pts[0][0] + 0.01, pts[0][1]))
    return pts


def _adjust_bound_endpoint(
    endpoint: tuple[float, float],
    neighbor: tuple[float, float],
    binding: dict | None,
    elements_by_id: dict[str, dict],
    has_arrowhead: bool,
) -> tuple[float, float]:
    """结合 startBinding / endBinding 校准连线端点：裁剪穿入框内的线段并留出微距呼吸感。"""
    if not binding or not isinstance(binding, dict):
        return endpoint
    target_id = binding.get("elementId")
    target = elements_by_id.get(str(target_id)) if target_id else None
    if not target or target.get("type") not in {"rectangle", "diamond", "ellipse"}:
        return endpoint

    tx, ty, tw, th = _normalize_rect(target)
    ex, ey = endpoint
    nx, ny = neighbor
    seg_len = math.hypot(ex - nx, ey - ny)
    if seg_len < 12.0:
        return endpoint

    # 若端点已深入目标矩形内部（常见于手工拖拽微调后未重算折点），用 Liang-Barsky 射线求交回退至矩形外框
    strictly_inside = (tx + 1.5 < ex < tx + tw - 1.5) and (ty + 1.5 < ey < ty + th - 1.5)
    if strictly_inside:
        dx = ex - nx
        dy = ey - ny
        t_min, t_max = 0.0, 1.0
        for p, q in ((-dx, nx - tx), (dx, tx + tw - nx), (-dy, ny - ty), (dy, ty + th - ny)):
            if abs(p) < 1e-9:
                if q < 0:
                    t_min, t_max = 2.0, 1.0
                    break
            else:
                t = q / p
                if p < 0:
                    t_min = max(t_min, t)
                else:
                    t_max = min(t_max, t)
        if 0.0 <= t_min <= t_max and t_min <= 1.0:
            ex = nx + t_min * dx
            ey = ny + t_min * dy
            seg_len = math.hypot(ex - nx, ey - ny)

    # 对带箭头的终点沿来向微缩 1.5px，使箭头尖端精准落于组件边框外沿而不压盖边框描边
    if has_arrowhead and seg_len > 16.0:
        retract = 1.5
        ux = (ex - nx) / seg_len
        uy = (ey - ny) / seg_len
        return ex - ux * retract, ey - uy * retract
    return ex, ey


def _build_routed_path_d(points: list[tuple[float, float]], rounded: bool) -> str:
    """将折点序列编译为 SVG path 的 d 指令；当开启圆角且折点数 >= 3 时生成二次贝塞尔平滑转角。"""
    if len(points) < 2:
        return ""
    if not rounded or len(points) == 2:
        cmds = [f"M {_fmt(points[0][0])} {_fmt(points[0][1])}"]
        for px, py in points[1:]:
            cmds.append(f"L {_fmt(px)} {_fmt(py)}")
        return " ".join(cmds)

    cmds = [f"M {_fmt(points[0][0])} {_fmt(points[0][1])}"]
    for i in range(1, len(points) - 1):
        p_prev = points[i - 1]
        p_curr = points[i]
        p_next = points[i + 1]
        v1x, v1y = p_curr[0] - p_prev[0], p_curr[1] - p_prev[1]
        v2x, v2y = p_next[0] - p_curr[0], p_next[1] - p_curr[1]
        len1 = math.hypot(v1x, v1y)
        len2 = math.hypot(v2x, v2y)
        if len1 < 1e-3 or len2 < 1e-3:
            cmds.append(f"L {_fmt(p_curr[0])} {_fmt(p_curr[1])}")
            continue
        radius = min(12.0, len1 * 0.35, len2 * 0.35)
        start_arc = (p_curr[0] - (v1x / len1) * radius, p_curr[1] - (v1y / len1) * radius)
        end_arc = (p_curr[0] + (v2x / len2) * radius, p_curr[1] + (v2y / len2) * radius)
        cmds.append(f"L {_fmt(start_arc[0])} {_fmt(start_arc[1])}")
        cmds.append(f"Q {_fmt(p_curr[0])} {_fmt(p_curr[1])} {_fmt(end_arc[0])} {_fmt(end_arc[1])}")
    cmds.append(f"L {_fmt(points[-1][0])} {_fmt(points[-1][1])}")
    return " ".join(cmds)


def _marker_id(direction: str, kind: str, stroke_color: str) -> str:
    """生成稳定唯一的 SVG <marker> ID（按起止方向、箭头样式与颜色区分）。"""
    return f"mk-{direction}-{kind}-{_color_token(stroke_color)}"


def _render_marker_def(direction: str, kind: str, stroke_color: str) -> str:
    """渲染单个 SVG 1.1 兼容的 <marker> 定义（显式区分 start 与 end 避免旧渲染器不支持 auto-start-reverse）。"""
    mid = _marker_id(direction, kind, stroke_color)
    esc_color = _esc_attr(stroke_color)
    if kind == "dot":
        return (
            f'    <marker id="{mid}" viewBox="0 0 10 10" refX="5" refY="5" '
            f'markerWidth="5.5" markerHeight="5.5" orient="auto">'
            f'<circle cx="5" cy="5" r="3.6" fill="{esc_color}" />'
            f"</marker>"
        )
    if kind == "bar":
        return (
            f'    <marker id="{mid}" viewBox="0 0 10 10" refX="5" refY="5" '
            f'markerWidth="6" markerHeight="6" orient="auto">'
            f'<line x1="5" y1="1" x2="5" y2="9" stroke="{esc_color}" stroke-width="2" stroke-linecap="round" />'
            f"</marker>"
        )
    # 默认实心流线型工程箭头（兼容 arrow / triangle）
    if direction == "start":
        return (
            f'    <marker id="{mid}" viewBox="0 0 12 10" refX="1.5" refY="5" '
            f'markerWidth="6.5" markerHeight="6.5" orient="auto">'
            f'<path d="M 10.5 1.2 L 1.2 5 L 10.5 8.8 L 8.6 5 Z" fill="{esc_color}" '
            f'stroke="{esc_color}" stroke-width="0.6" stroke-linejoin="round" />'
            f"</marker>"
        )
    return (
        f'    <marker id="{mid}" viewBox="0 0 12 10" refX="10.5" refY="5" '
        f'markerWidth="6.5" markerHeight="6.5" orient="auto">'
        f'<path d="M 1.5 1.2 L 10.8 5 L 1.5 8.8 L 3.4 5 Z" fill="{esc_color}" '
        f'stroke="{esc_color}" stroke-width="0.6" stroke-linejoin="round" />'
        f"</marker>"
    )


def _compute_text_layout(
    element: dict,
    elements_by_id: dict[str, dict],
) -> tuple[list[str], float, float, float, str, float, float, float]:
    """计算文本元素的精确排版参数。

    返回值：(lines, anchor_x, first_baseline_y, line_px, text_anchor, box_x, box_y, box_w)
    其中 box_x/box_y/box_w 是结合东亚字宽估算后的外接盒参数，供全局视口与连线标签遮罩复用。
    """
    raw_text = str(element.get("text") or "")
    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    font_size = float(element.get("fontSize", 16.0))
    line_height = float(element.get("lineHeight", 1.25))
    line_px = font_size * line_height
    is_mono = element.get("fontFamily") == 3

    x, y, w, h = _normalize_rect(element)
    text_align = str(element.get("textAlign") or "left").strip()
    vertical_align = str(element.get("verticalAlign") or "top").strip()

    container_id = element.get("containerId")
    container = elements_by_id.get(str(container_id)) if container_id else None
    is_shape_container = (
        container is not None and container.get("type") in {"rectangle", "diamond", "ellipse"}
    )

    est_w = max(
        w,
        max((_estimate_line_width(line, font_size, is_mono=is_mono) for line in lines), default=0.0),
    )
    total_text_h = len(lines) * line_px

    if is_shape_container and container is not None:
        cx, cy, cw, ch = _normalize_rect(container)
        pad_x = 12.0
        if text_align == "center":
            anchor_x = cx + cw * 0.5
            text_anchor = "middle"
            box_x = anchor_x - est_w * 0.5
        elif text_align == "right":
            anchor_x = cx + cw - pad_x
            text_anchor = "end"
            box_x = anchor_x - est_w
        else:
            anchor_x = cx + pad_x
            text_anchor = "start"
            box_x = anchor_x

        if vertical_align == "middle":
            top_y = cy + (ch - total_text_h) * 0.5
        elif vertical_align == "bottom":
            top_y = cy + ch - total_text_h - 8.0
        else:
            top_y = y
    else:
        if text_align == "center":
            anchor_x = x + w * 0.5
            text_anchor = "middle"
            box_x = anchor_x - est_w * 0.5
        elif text_align == "right":
            anchor_x = x + w
            text_anchor = "end"
            box_x = anchor_x - est_w
        else:
            anchor_x = x
            text_anchor = "start"
            box_x = anchor_x

        if vertical_align == "middle" and h > 0:
            top_y = y + (h - total_text_h) * 0.5
        elif vertical_align == "bottom" and h > 0:
            top_y = y + h - total_text_h
        else:
            top_y = y

    # 字形基线校准：半行距 + 0.82 倍字号上行高度，使中英文视觉中心与容器几何中心严丝合缝
    first_baseline_y = top_y + (line_px - font_size) * 0.5 + font_size * 0.82
    return lines, anchor_x, first_baseline_y, line_px, text_anchor, box_x, top_y, est_w


def _compute_canvas_bounds(
    elements: list[dict],
    elements_by_id: dict[str, dict],
    padding: float,
) -> tuple[float, float, float, float]:
    """遍历全部非删除图元，计算包含描边厚度、旋转角与字体宽度冗余的精确视口 (vx, vy, vw, vh)。"""
    if not elements:
        return 0.0, 0.0, 800.0, 600.0

    min_x = float("inf")
    min_y = float("inf")
    max_x = float("-inf")
    max_y = float("-inf")

    def _include(px: float, py: float, margin: float = 0.0) -> None:
        nonlocal min_x, min_y, max_x, max_y
        min_x = min(min_x, px - margin)
        min_y = min(min_y, py - margin)
        max_x = max(max_x, px + margin)
        max_y = max(max_y, py + margin)

    for elem in elements:
        etype = elem.get("type")
        sw = float(elem.get("strokeWidth", 1.0))
        angle = float(elem.get("angle") or 0.0)

        if etype in {"rectangle", "ellipse", "diamond"}:
            x, y, w, h = _normalize_rect(elem)
            cx, cy = x + w * 0.5, y + h * 0.5
            half_sw = sw * 0.5
            for px, py in ((x, y), (x + w, y), (x + w, y + h), (x, y + h)):
                rx, ry = _rotate_point(px, py, cx, cy, angle)
                _include(rx, ry, half_sw)
        elif etype in {"line", "arrow"}:
            pts = _extract_world_points(elem)
            has_head = bool(elem.get("startArrowhead") or elem.get("endArrowhead"))
            margin = sw * 1.5 + (8.0 if has_head else 0.0)
            if abs(angle) > 1e-6 and pts:
                xs = [p[0] for p in pts]
                ys = [p[1] for p in pts]
                cx = (min(xs) + max(xs)) * 0.5
                cy = (min(ys) + max(ys)) * 0.5
                pts = [_rotate_point(px, py, cx, cy, angle) for px, py in pts]
            for px, py in pts:
                _include(px, py, margin)
        elif etype == "text":
            lines, _, _, line_px, _, box_x, box_y, box_w = _compute_text_layout(elem, elements_by_id)
            box_h = max(float(elem.get("height", 0.0)), len(lines) * line_px)
            cx = box_x + box_w * 0.5
            cy = box_y + box_h * 0.5
            for px, py in (
                (box_x, box_y),
                (box_x + box_w, box_y),
                (box_x + box_w, box_y + box_h),
                (box_x, box_y + box_h),
            ):
                rx, ry = _rotate_point(px, py, cx, cy, angle)
                _include(rx, ry, 2.0)
        else:
            x, y, w, h = _normalize_rect(elem)
            _include(x, y, sw)
            _include(x + w, y + h, sw)

    vx = min_x - padding
    vy = min_y - padding
    vw = (max_x - min_x) + padding * 2.0
    vh = (max_y - min_y) + padding * 2.0
    return vx, vy, vw, vh


def _find_backdrop_color(
    cx: float,
    cy: float,
    prior_backdrops: list[tuple[float, float, float, float, str]],
    canvas_bg: str,
) -> str:
    """查找连线标签中心点 (cx, cy) 下方最顶层的不透明背景色，用于绘制无缝融合的文字衬底胶囊。"""
    for bx, by, bw, bh, color in reversed(prior_backdrops):
        if bx <= cx <= bx + bw and by <= cy <= by + bh:
            return color
    return canvas_bg


def render_excalidraw_to_svg(data: dict, *, padding: float = DEFAULT_PADDING) -> str:
    """将反序列化后的 Excalidraw v2 JSON 字典渲染为完整 SVG 字符串。"""
    if data.get("type") != "excalidraw":
        raise ValueError(f"非法的 Excalidraw 文档类型: {data.get('type')!r}")

    raw_elements = data.get("elements") or []
    elements = [e for e in raw_elements if isinstance(e, dict) and not e.get("isDeleted")]
    elements_by_id = {str(e["id"]): e for e in elements if "id" in e}

    app_state = data.get("appState") or {}
    canvas_bg = str(app_state.get("viewBackgroundColor") or "#ffffff").strip() or "#ffffff"

    vx, vy, vw, vh = _compute_canvas_bounds(elements, elements_by_id, padding=padding)

    # 第 1 遍：收集所有 arrow/line 所需的着色 <marker>，按元组排序以保证 <defs> 确定性
    needed_markers: set[tuple[str, str, str]] = set()
    for elem in elements:
        if elem.get("type") in {"arrow", "line"}:
            stroke_color = str(elem.get("strokeColor") or "#1e1e1e").strip()
            if not stroke_color or stroke_color == "transparent":
                continue
            start_head = elem.get("startArrowhead")
            end_head = elem.get("endArrowhead")
            if start_head:
                needed_markers.add(("start", str(start_head), stroke_color))
            if end_head:
                needed_markers.add(("end", str(end_head), stroke_color))

    # 提取主标题（若有）写入无障碍 <title>，方便屏幕阅读器与浏览器标签页预览
    doc_title = ""
    for elem in elements:
        if elem.get("type") == "text" and not elem.get("containerId"):
            candidate = str(elem.get("text") or "").strip().splitlines()
            if candidate:
                doc_title = candidate[0].strip()
                break

    svg_lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" version="1.1" '
            f'viewBox="{_fmt(vx)} {_fmt(vy)} {_fmt(vw)} {_fmt(vh)}" '
            f'width="{_fmt(vw)}" height="{_fmt(vh)}" '
            f'role="img" aria-label="{_esc_attr(doc_title or "Architecture Diagram")}">'
        ),
    ]
    if doc_title:
        svg_lines.append(f"  <title>{_esc_text(doc_title)}</title>")

    if needed_markers:
        svg_lines.append("  <defs>")
        for direction, kind, color in sorted(needed_markers):
            svg_lines.append(_render_marker_def(direction, kind, color))
        svg_lines.append("  </defs>")

    # 画布底板：赋予轻微圆角以在 GitHub 深色模式下同样呈现干净白底图纸质感
    svg_lines.append(
        f'  <rect x="{_fmt(vx)}" y="{_fmt(vy)}" width="{_fmt(vw)}" height="{_fmt(vh)}" '
        f'rx="12" ry="12" fill="{_esc_attr(canvas_bg)}" />'
    )

    # 记录已绘制的实心背景矩形，供后续连线文字标签采样底层底色
    prior_backdrops: list[tuple[float, float, float, float, str]] = []

    # 第 2 遍：严格按 Excalidraw 画布图层顺序逐个渲染几何体、连线与文字
    for elem in elements:
        etype = elem.get("type")
        eid = str(elem.get("id") or "")

        if etype == "rectangle":
            x, y, w, h = _normalize_rect(elem)
            rx = _corner_radius(w, h, elem.get("roundness"))
            cx, cy = x + w * 0.5, y + h * 0.5
            attrs = [
                f'x="{_fmt(x)}"',
                f'y="{_fmt(y)}"',
                f'width="{_fmt(w)}"',
                f'height="{_fmt(h)}"',
            ]
            if rx > 0:
                attrs.append(f'rx="{_fmt(rx)}"')
                attrs.append(f'ry="{_fmt(rx)}"')
            attrs.extend(_fill_attrs(elem))
            attrs.extend(_stroke_attrs(elem))
            attrs.extend(_opacity_attr(elem))
            attrs.extend(_rotation_attr(elem, cx, cy))
            svg_lines.append(f"  <rect {' '.join(attrs)} />")

            bg = str(elem.get("backgroundColor") or "transparent").strip()
            if bg and bg != "transparent" and abs(float(elem.get("angle") or 0.0)) < 1e-6:
                prior_backdrops.append((x, y, w, h, bg))

        elif etype == "ellipse":
            x, y, w, h = _normalize_rect(elem)
            cx, cy = x + w * 0.5, y + h * 0.5
            attrs = [
                f'cx="{_fmt(cx)}"',
                f'cy="{_fmt(cy)}"',
                f'rx="{_fmt(w * 0.5)}"',
                f'ry="{_fmt(h * 0.5)}"',
            ]
            attrs.extend(_fill_attrs(elem))
            attrs.extend(_stroke_attrs(elem))
            attrs.extend(_opacity_attr(elem))
            attrs.extend(_rotation_attr(elem, cx, cy))
            svg_lines.append(f"  <ellipse {' '.join(attrs)} />")

        elif etype == "diamond":
            x, y, w, h = _normalize_rect(elem)
            cx, cy = x + w * 0.5, y + h * 0.5
            pts_str = " ".join(
                f"{_fmt(px)},{_fmt(py)}"
                for px, py in ((cx, y), (x + w, cy), (cx, y + h), (x, cy))
            )
            attrs = [f'points="{pts_str}"', 'stroke-linejoin="round"']
            attrs.extend(_fill_attrs(elem))
            attrs.extend(_stroke_attrs(elem))
            attrs.extend(_opacity_attr(elem))
            attrs.extend(_rotation_attr(elem, cx, cy))
            svg_lines.append(f"  <polygon {' '.join(attrs)} />")

        elif etype in {"line", "arrow"}:
            pts = _extract_world_points(elem)
            if len(pts) < 2:
                continue
            start_head = elem.get("startArrowhead")
            end_head = elem.get("endArrowhead")
            pts[0] = _adjust_bound_endpoint(
                pts[0],
                pts[1],
                elem.get("startBinding"),
                elements_by_id,
                has_arrowhead=bool(start_head),
            )
            pts[-1] = _adjust_bound_endpoint(
                pts[-1],
                pts[-2],
                elem.get("endBinding"),
                elements_by_id,
                has_arrowhead=bool(end_head),
            )

            is_closed = (
                etype == "line"
                and len(pts) >= 3
                and math.hypot(pts[0][0] - pts[-1][0], pts[0][1] - pts[-1][1]) < 1e-2
            )
            rounded = elem.get("roundness") is not None and not is_closed
            stroke_color = str(elem.get("strokeColor") or "#1e1e1e").strip()

            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            cx = (min(xs) + max(xs)) * 0.5
            cy = (min(ys) + max(ys)) * 0.5

            common_attrs: list[str] = _fill_attrs(elem) if is_closed else ['fill="none"']
            common_attrs.extend(_stroke_attrs(elem))
            if not any(a.startswith("stroke-linecap=") for a in common_attrs):
                common_attrs.append('stroke-linecap="round"')
            common_attrs.append('stroke-linejoin="round"')
            if start_head and stroke_color and stroke_color != "transparent":
                mid = _marker_id("start", str(start_head), stroke_color)
                common_attrs.append(f'marker-start="url(#{mid})"')
            if end_head and stroke_color and stroke_color != "transparent":
                mid = _marker_id("end", str(end_head), stroke_color)
                common_attrs.append(f'marker-end="url(#{mid})"')
            common_attrs.extend(_opacity_attr(elem))
            common_attrs.extend(_rotation_attr(elem, cx, cy))

            if rounded and len(pts) >= 3:
                path_d = _build_routed_path_d(pts, rounded=True)
                svg_lines.append(f'  <path d="{path_d}" {" ".join(common_attrs)} />')
            else:
                poly_pts = " ".join(f"{_fmt(px)},{_fmt(py)}" for px, py in pts)
                tag = "polygon" if is_closed else "polyline"
                svg_lines.append(f'  <{tag} points="{poly_pts}" {" ".join(common_attrs)} />')

        elif etype == "text":
            lines, anchor_x, first_y, line_px, text_anchor, box_x, box_y, box_w = (
                _compute_text_layout(elem, elements_by_id)
            )
            font_size = float(elem.get("fontSize", 16.0))
            font_family = _font_family_for(elem.get("fontFamily"))
            fill_color = str(elem.get("strokeColor") or "#1e1e1e").strip() or "#1e1e1e"

            container_id = elem.get("containerId")
            container = elements_by_id.get(str(container_id)) if container_id else None
            container_type = container.get("type") if container else None

            # 若文本绑定在连线/箭头上，先用所在分区底色绘制圆角遮罩胶囊，防止连线穿过汉字影响辨认
            if container_type in {"arrow", "line"}:
                box_h = len(lines) * line_px
                pad_x, pad_y = 5.0, 2.5
                lcx = box_x + box_w * 0.5
                lcy = box_y + box_h * 0.5
                halo_color = _find_backdrop_color(lcx, lcy, prior_backdrops, canvas_bg)
                svg_lines.append(
                    f'  <rect x="{_fmt(box_x - pad_x)}" y="{_fmt(box_y - pad_y)}" '
                    f'width="{_fmt(box_w + pad_x * 2.0)}" height="{_fmt(box_h + pad_y * 2.0)}" '
                    f'rx="4" ry="4" fill="{_esc_attr(halo_color)}" fill-opacity="0.92" />'
                )

            # 视觉层级字重：主标题加粗 (700)，分区栏标题半粗 (600)，组件卡片首行标题在多行时半粗突出
            base_weight = ""
            if container is None:
                if font_size >= 22.0:
                    base_weight = "700"
                elif font_size >= 16.0 and not eid.startswith("subtitle"):
                    base_weight = "600"

            box_h_for_rot = max(float(elem.get("height", 0.0)), len(lines) * line_px)
            tcx = box_x + box_w * 0.5
            tcy = box_y + box_h_for_rot * 0.5

            text_attrs = [
                f'x="{_fmt(anchor_x)}"',
                f'y="{_fmt(first_y)}"',
                f'fill="{_esc_attr(fill_color)}"',
                f'font-family="{_esc_attr(font_family)}"',
                f'font-size="{_fmt(font_size)}"',
                f'text-anchor="{text_anchor}"',
            ]
            if base_weight:
                text_attrs.append(f'font-weight="{base_weight}"')
            text_attrs.extend(_opacity_attr(elem))
            text_attrs.extend(_rotation_attr(elem, tcx, tcy))

            emphasize_card_header = (
                container_type in {"rectangle", "diamond", "ellipse"} and len(lines) >= 2
            )

            if len(lines) == 1:
                content = _esc_text(lines[0]) if lines[0] else "&#160;"
                svg_lines.append(f"  <text {' '.join(text_attrs)}>{content}</text>")
            else:
                svg_lines.append(f"  <text {' '.join(text_attrs)}>")
                for idx, line in enumerate(lines):
                    # 空白行填入不间断空格，防止部分浏览器忽略空 <tspan> 的 dy 行距推进
                    content = _esc_text(line) if line else "&#160;"
                    dy_val = "0" if idx == 0 else _fmt(line_px)
                    extra_w = ' font-weight="600"' if (idx == 0 and emphasize_card_header) else ""
                    svg_lines.append(
                        f'    <tspan x="{_fmt(anchor_x)}" dy="{dy_val}"{extra_w}>{content}</tspan>'
                    )
                svg_lines.append("  </text>")

    svg_lines.append("</svg>")
    return "\n".join(svg_lines) + "\n"


def convert_directory(
    input_dir: Path = DEFAULT_INPUT_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    *,
    padding: float = DEFAULT_PADDING,
) -> list[Path]:
    """批量转换指定目录下全部 *.excalidraw 画布并校验生成的 XML 结构完整性。"""
    source_files = sorted(input_dir.glob("*.excalidraw"))
    if not source_files:
        raise FileNotFoundError(f"未在 {input_dir} 下找到任何 .excalidraw 文件")

    output_dir.mkdir(parents=True, exist_ok=True)
    generated: list[Path] = []

    for src_path in source_files:
        raw_data = json.loads(src_path.read_text(encoding="utf-8"))
        svg_text = render_excalidraw_to_svg(raw_data, padding=padding)
        out_path = output_dir / f"{src_path.stem}.svg"
        out_path.write_text(svg_text, encoding="utf-8")

        # 强制使用 ElementTree 重新解析落盘文件，确保输出 100% 为良构 XML
        tree = ET.parse(out_path)
        root = tree.getroot()
        if not root.tag.endswith("svg"):
            raise ValueError(f"生成的 SVG 根节点标签异常: {out_path} -> {root.tag}")
        generated.append(out_path)

    return generated


def main(argv: list[str] | None = None) -> int:
    """命令行入口：支持自定义输入目录、输出目录与视口边距。"""
    parser = argparse.ArgumentParser(
        description="将 Excalidraw (.excalidraw) 工程架构画布确定性渲染为纯矢量 SVG。"
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=DEFAULT_INPUT_DIR,
        help=f"包含 .excalidraw 源文件的目录（默认: {DEFAULT_INPUT_DIR}）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"生成的 .svg 输出目录（默认: {DEFAULT_OUTPUT_DIR}）",
    )
    parser.add_argument(
        "--padding",
        type=float,
        default=DEFAULT_PADDING,
        help=f"画布四周留白像素值（默认: {DEFAULT_PADDING}）",
    )
    args = parser.parse_args(argv)

    outputs = convert_directory(
        input_dir=args.input_dir.resolve(),
        output_dir=args.output_dir.resolve(),
        padding=args.padding,
    )
    for svg_path in outputs:
        size_kb = svg_path.stat().st_size / 1024.0
        print(f"[OK] {svg_path.relative_to(REPO_ROOT) if svg_path.is_relative_to(REPO_ROOT) else svg_path} ({size_kb:.1f} KiB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
