# Agent Systems Lab maintenance notes

This is a Chinese learning project for coding-agent engineering. Keep implementation boundaries honest.

- Explain non-obvious design decisions with Chinese comments/docstrings; preserve clear, readable code.
- Keep DemoProvider and the `reference` benchmark executor explicitly labeled (`harness_regression_only` / `benchmark_harness_verification_only`). Never describe scripted or reference passes as real LLM coding scores.
- Default `execution_profile="sandbox"` keeps all model writes (`propose_patch` and `propose_changeset`) strictly proposal-gated and never executes generated Python on the host. Opt-in `execution_profile="host"` / `"computer"` (`agent-lab code`, `agent-lab run --cwd`, or Web `local_cwd`) enables host filesystem (`fs_*`), scrubbed `bash_exec`, Desktop GUI (`computer_*`), dynamic `subagent_run`, and external `McpClientManager` under explicit three-tier permissions (`ask` / `auto-edit` / `full-auto`), and `gc_expired_resources` never deletes `direct_workspace` directories.
- Preserve complete tool_use/tool_result pairs during context trimming and checkpoint recovery.
- Working memory facts (`working_memory_write`) must verify SHA-256 provenance against workspace files or committed `tool_calls` and flip to `stale` when underlying files change; working memory never overrides `HARNESS_STATE` approval records.
- Do not add credentials, local state, personal repositories or blog full text to source control.
- Default server is loopback-only, per-run workspace or detached Git worktree. Queue dispatch (`AGENT_LAB_DISPATCH=queue` + `agent-lab worker`) enforces single-host SQLite leases, heartbeats, and monotonically increasing `epoch` fencing tokens (`StaleEpochError`); do not claim cross-host distributed HA without shared transactional storage.
- `uv sync --locked --extra dev --extra mcp` installs all test tools.
- Validate meaningful behavior: `uv run --no-sync ruff check src tests scripts`, `uv run --no-sync pytest -q`, `uv run --no-sync agent-lab eval --report reports/eval.json`, and (when Docker is available) `uv run --no-sync agent-lab model-eval --provider reference --report reports/model-eval.json`.
- Keep README, learning notes and validation evidence in sync when behavior changes.
