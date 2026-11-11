from __future__ import annotations


def paginate_records(
    records: list[dict], limit: int, after_id: str | None = None
) -> dict:
    """按游标 after_id 返回严格位于其后的最多 limit 条记录，并给出 next_cursor 与 has_more。"""
    if limit <= 0:
        raise ValueError("limit must be positive")
    start_idx = 0
    if after_id is not None:
        matched = [i for i, row in enumerate(records) if row.get("id") == after_id]
        if not matched:
            raise KeyError(f"unknown cursor: {after_id}")
        start_idx = matched[0]
    window = records[start_idx : start_idx + limit]
    has_more = len(window) >= limit
    next_cursor = window[-1]["id"] if has_more and window else None
    return {"items": window, "has_more": has_more, "next_cursor": next_cursor}
