# Agent Systems Lab maintenance notes

This is a Chinese learning project for coding-agent engineering. Keep implementation boundaries honest.

- Explain non-obvious design decisions with Chinese comments/docstrings; preserve clear, readable code.
- Keep DemoProvider explicitly scripted. Do not describe offline success as model performance.
- All model writes must remain proposals bound to the exact run/current approval and source hash.
- Do not execute generated Python on the host. AST parsing is syntax-only; optional Docker tests fail closed.
- Preserve complete tool_use/tool_result pairs during context trimming and checkpoint recovery.
- Do not add credentials, local state, personal repositories or blog full text to source control.
- Default server is loopback-only, one process, per-run workspace. Do not introduce multi-worker claims without leases.
- `uv sync --locked --extra dev --extra mcp` installs all test tools.
- Validate meaningful behavior: `uv run --no-sync ruff check src tests`, `uv run --no-sync pytest -q`, and `uv run --no-sync agent-lab eval --report reports/eval.json`.
- Keep README, learning notes and validation evidence in sync when behavior changes.
