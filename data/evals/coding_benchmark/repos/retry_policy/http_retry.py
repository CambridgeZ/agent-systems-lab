from __future__ import annotations


def plan_retry(
    attempt: int,
    status_code: int,
    base_ms: int = 100,
    max_ms: int = 2000,
    max_attempts: int = 3,
) -> dict:
    """决定第 attempt 次失败（从 1 开始计数）后是否重试及退避毫秒数。

    规则：
    - 仅 HTTP 429 与 5xx（500-599）属于可重试状态码；其余状态码一律不重试。
    - 若 attempt >= max_attempts，不再重试。
    - 第 1 次失败退避 base_ms，第 2 次 base_ms * 2，第 k 次 min(max_ms, base_ms * 2**(k-1))。
    """
    if attempt < 1 or base_ms <= 0 or max_ms < base_ms or max_attempts < 1:
        raise ValueError("invalid retry policy parameters")
    retryable = status_code >= 400
    if not retryable or attempt >= max_attempts:
        return {"retry": False, "delay_ms": 0}
    delay = base_ms * (2 ** attempt)
    return {"retry": True, "delay_ms": delay}
