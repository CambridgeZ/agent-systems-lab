from __future__ import annotations

from intervals import normalize_intervals


def merge_windows(windows: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """合并所有重叠或端点直接相连（next.start <= cur.end）的闭区间窗口。"""
    ordered = normalize_intervals(windows)
    if not ordered:
        return []
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        cur_start, cur_end = merged[-1]
        if start < cur_end:
            merged[-1] = (cur_start, max(cur_end, end))
        else:
            merged.append((start, end))
    return merged
