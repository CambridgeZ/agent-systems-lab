# Agent Systems Lab maintenance notes

This is a Chinese learning project for coding-agent engineering. Keep implementation boundaries honest.

- Explain non-obvious design decisions with Chinese comments/docstrings; preserve clear, readable code.
- Keep DemoProvider and the `reference` benchmark executor explicitly labeled (`harness_regression_only` / `benchmark_harness_verification_only`). Never describe scripted or reference passes as real LLM coding scores.
- All model writes (single-file `propose_patch` and multi-file atomic `propose_changeset`) must remain proposals bound to the exact run/current approval, per-file source SHA-256, Git worktree baseline (`base_commit`), and active lease `epoch`.
- Do not execute generated Python on the host. AST parsing is syntax-only; Docker test runs (`run` and `run_suite`) fail closed and keep `verification.status` strictly separate from `run.status == "completed"`.
- Preserve complete tool_use/tool_result pairs during context trimming and checkpoint recovery.
- Working memory facts (`working_memory_write`) must verify SHA-256 provenance against workspace files or committed `tool_calls` and flip to `stale` when underlying files change; working memory never overrides `HARNESS_STATE` approval records.
- Do not add credentials, local state, personal repositories or blog full text to source control.
- Default server is loopback-only, per-run workspace or detached Git worktree. Queue dispatch (`AGENT_LAB_DISPATCH=queue` + `agent-lab worker`) enforces single-host SQLite leases, heartbeats, and monotonically increasing `epoch` fencing tokens (`StaleEpochError`); do not claim cross-host distributed HA without shared transactional storage.
- `uv sync --locked --extra dev --extra mcp` installs all test tools.
- Validate meaningful behavior: `uv run --no-sync ruff check src tests scripts`, `uv run --no-sync pytest -q`, `uv run --no-sync agent-lab eval --report reports/eval.json`, and (when Docker is available) `uv run --no-sync agent-lab model-eval --provider reference --report reports/model-eval.json`.
- Keep README, learning notes and validation evidence in sync when behavior changes.
