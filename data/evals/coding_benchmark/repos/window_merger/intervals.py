from __future__ import annotations


def normalize_intervals(raw: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """校验区间合法性并按起点升序（起点相同时按终点升序）返回新列表。"""
    cleaned: list[tuple[int, int]] = []
    for start, end in raw:
        if start > end:
            raise ValueError(f"invalid interval ({start}, {end})")
        cleaned.append((int(start), int(end)))
    return sorted(cleaned, key=lambda pair: pair[1])
