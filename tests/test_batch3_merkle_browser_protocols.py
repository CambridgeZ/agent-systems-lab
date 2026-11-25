"""Offline deterministic tests for Batch 3 capabilities.

Covers:
1. Persistent Merkle-Tree Semantic Code Index (`code_index.py`)
2. Headless ARIA Snapshot Browser Layer (`browser.py`)
3. MCP Frontier & A2A Frontier (`mcp_client.py`, `a2a.py`, `storage.py`)
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sqlite3
from typing import Any

import httpx
import pytest

from agent_lab.a2a import (
    A2AClientManager,
    JSONRPC_METHOD_NOT_FOUND,
    build_artifact_update_event,
    build_status_update_event,
    deliver_a2a_push_notification,
    jsonrpc_error,
    jsonrpc_success,
    verify_a2a_push_signature,
)
from agent_lab.browser import (
    BROWSER_TOOL_SPECS,
    BrowserError,
    BrowserSessionManager,
    MockBrowserBackend,
    validate_browser_url,
)
from agent_lab.code_index import (
    CODE_INDEX_TOOL_SPECS,
    CodebaseMerkleIndex,
    execute_codebase_semantic_search,
)
from agent_lab.mcp_client import McpClientManager
from agent_lab.retrieval import LocalSubwordDenseEmbedder
from agent_lab.storage import Storage, digest


def _bump_mtime(path: Path, delta_ns: int = 2_000_000_000) -> None:
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + delta_ns))


def _query_index_chunks(db_path: Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT path, symbol, kind, start_line, end_line, content FROM code_chunks ORDER BY path, start_line"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _query_indexed_file_paths(db_path: Path) -> set[str]:
    con = sqlite3.connect(str(db_path))
    try:
        rows = con.execute("SELECT path FROM merkle_nodes WHERE is_dir = 0").fetchall()
        return {str(r[0]) for r in rows}
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 1. Persistent Merkle-Tree Semantic Code Index (code_index.py)
# ---------------------------------------------------------------------------


def test_code_index_initial_refresh_indexes_py_ast_ts_md_and_merkle_root_hash(tmp_path: Path) -> None:
    assert CODE_INDEX_TOOL_SPECS[0]["name"] == "codebase_semantic_search"

    ws = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    (ws / "src" / "billing").mkdir(parents=True)
    (ws / "web").mkdir(parents=True)
    (ws / "docs").mkdir(parents=True)

    py_src = "\n".join(
        [
            '"""Billing computation helpers."""',
            "",
            "def calculate_prorated_invoice(amount_cents: int, days_used: int, total_days: int) -> int:",
            "    if total_days <= 0:",
            "        raise ValueError('total_days must be positive')",
            "    return (amount_cents * days_used) // total_days",
            "",
            "class TaxLedger:",
            "    rate_bps: int = 850",
            "",
            "    def compute_vat_breakdown(self, subtotal_cents: int) -> dict[str, int]:",
            "        vat = (subtotal_cents * self.rate_bps) // 10_000",
            "        return {'subtotal': subtotal_cents, 'vat': vat, 'total': subtotal_cents + vat}",
            "",
            "async def reconcile_settlement_batch(batch_id: str) -> str:",
            "    return f'settled:{batch_id}'",
            "",
        ]
    )
    (ws / "src" / "billing" / "calculator.py").write_text(py_src, encoding="utf-8")

    ts_src = "\n".join(
        [
            "export interface CheckoutLineItem {",
            "  sku: string;",
            "  unitPriceCents: number;",
            "  quantity: number;",
            "}",
            "",
            "export function renderCheckoutSummary(items: CheckoutLineItem[]): number {",
            "  return items.reduce((acc, item) => acc + item.unitPriceCents * item.quantity, 0);",
            "}",
            "",
            "export class CartController {",
            "  count(items: CheckoutLineItem[]): number {",
            "    return items.length;",
            "  }",
            "}",
            "",
        ]
    )
    (ws / "web" / "checkout.ts").write_text(ts_src, encoding="utf-8")

    md_src = "\n".join(
        [
            "# Architecture Overview",
            "Core billing pipeline and persistent indexing notes.",
            "",
            "## Payment Settlement Pipeline",
            "```bash",
            "# Not A Markdown Heading Inside Fence",
            "echo settlement-ok",
            "```",
            "Settlement reconciles invoice totals daily.",
            "",
            "## Merkle Index Invariants",
            "Identical file trees yield identical root_hash digests.",
            "",
        ]
    )
    (ws / "docs" / "architecture.md").write_text(md_src, encoding="utf-8")

    idx = CodebaseMerkleIndex(ws, state_dir)
    res = idx.refresh()

    assert res["ok"] is True
    assert res["files_indexed"] == 3
    assert res["files_reindexed"] == 3
    assert res["files_unchanged"] == 0
    assert res["files_deleted"] == 0
    assert res["chunks_total"] >= 8
    root_hash = res["root_hash"]
    assert isinstance(root_hash, str) and len(root_hash) == 64
    assert all(ch in "0123456789abcdef" for ch in root_hash)
    assert idx.db_path.is_file()

    # Persisted state survives fresh instance re-open without re-running refresh.
    reopened = CodebaseMerkleIndex(ws, state_dir)
    stats = reopened.stats()
    assert stats["root_hash"] == root_hash
    assert stats["files_indexed"] == 3
    assert stats["chunks_total"] == res["chunks_total"]
    assert stats["db_path"] == str(idx.db_path)

    chunks = _query_index_chunks(idx.db_path)
    py_chunks = {c["symbol"]: c for c in chunks if c["path"] == "src/billing/calculator.py"}
    assert "calculate_prorated_invoice" in py_chunks
    assert py_chunks["calculate_prorated_invoice"]["kind"] == "function"
    assert py_chunks["calculate_prorated_invoice"]["start_line"] == 3
    assert py_chunks["calculate_prorated_invoice"]["end_line"] == 6

    assert "TaxLedger" in py_chunks
    assert py_chunks["TaxLedger"]["kind"] == "class"
    assert "TaxLedger.compute_vat_breakdown" in py_chunks
    assert py_chunks["TaxLedger.compute_vat_breakdown"]["kind"] == "method"
    assert py_chunks["TaxLedger.compute_vat_breakdown"]["start_line"] == 11
    assert py_chunks["TaxLedger.compute_vat_breakdown"]["end_line"] == 13

    assert "reconcile_settlement_batch" in py_chunks
    assert py_chunks["reconcile_settlement_batch"]["kind"] == "function"
    assert py_chunks["reconcile_settlement_batch"]["start_line"] == 15
    assert py_chunks["reconcile_settlement_batch"]["end_line"] == 16

    ts_chunks = {c["symbol"]: c for c in chunks if c["path"] == "web/checkout.ts"}
    assert ts_chunks["CheckoutLineItem"]["kind"] == "interface"
    assert ts_chunks["renderCheckoutSummary"]["kind"] == "function"
    assert ts_chunks["CartController"]["kind"] == "class"

    md_symbols = [c["symbol"] for c in chunks if c["path"] == "docs/architecture.md"]
    assert "Architecture Overview" in md_symbols
    assert "Payment Settlement Pipeline" in md_symbols
    assert "Merkle Index Invariants" in md_symbols
    assert "Not A Markdown Heading Inside Fence" not in md_symbols


def test_code_index_incremental_refresh_noop_single_edit_and_delete_pruning(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    (ws / "pkg").mkdir(parents=True)
    (ws / "docs").mkdir(parents=True)

    alpha_path = ws / "pkg" / "alpha.py"
    beta_path = ws / "pkg" / "beta.py"
    guide_path = ws / "docs" / "guide.md"

    alpha_path.write_text("def compute_alpha(x: int) -> int:\n    return x + 1\n", encoding="utf-8")
    beta_path.write_text(
        "def unique_beta_disposal_target(v: int) -> int:\n    return v * 7\n",
        encoding="utf-8",
    )
    guide_path.write_text("# Guide\nSteady documentation section.\n", encoding="utf-8")

    base_embedder = LocalSubwordDenseEmbedder(dim=128)
    embedded_batches: list[list[str]] = []

    def counting_embedder(texts: list[str]) -> list[list[float]]:
        embedded_batches.append(list(texts))
        return base_embedder(texts)

    idx = CodebaseMerkleIndex(ws, state_dir, embedder=counting_embedder)

    # 1. Initial full refresh
    r1 = idx.refresh()
    assert r1["files_indexed"] == 3
    assert r1["files_reindexed"] == 3
    assert r1["files_unchanged"] == 0
    assert r1["files_deleted"] == 0
    root1 = r1["root_hash"]
    assert len(embedded_batches) == 1

    # 2. Second refresh with zero changes -> 0 files reindexed, identical root_hash, zero embedder calls
    r2 = idx.refresh()
    assert r2["files_indexed"] == 3
    assert r2["files_reindexed"] == 0
    assert r2["files_unchanged"] == 3
    assert r2["files_deleted"] == 0
    assert r2["root_hash"] == root1
    assert len(embedded_batches) == 1

    # 2b. Touch mtime without changing content -> SHA-256 matches, still 0 reindexed & identical root_hash
    _bump_mtime(guide_path)
    r2b = idx.refresh()
    assert r2b["files_reindexed"] == 0
    assert r2b["files_unchanged"] == 3
    assert r2b["root_hash"] == root1
    assert len(embedded_batches) == 1

    # 3. Modify ONLY 1 file -> reindexes ONLY that 1 file and updates root_hash
    alpha_path.write_text(
        "def compute_alpha(x: int) -> int:\n    return (x + 1) * 10\n\ndef extra_alpha_helper() -> str:\n    return 'v2'\n",
        encoding="utf-8",
    )
    _bump_mtime(alpha_path)
    r3 = idx.refresh()
    assert r3["files_indexed"] == 3
    assert r3["files_reindexed"] == 1
    assert r3["files_unchanged"] == 2
    assert r3["files_deleted"] == 0
    assert r3["root_hash"] != root1
    assert len(embedded_batches) == 2
    assert all(text.startswith("pkg/alpha.py |") for text in embedded_batches[-1])

    # 4. Delete 1 file -> prunes its chunks and merkle node, updates root_hash
    beta_path.unlink()
    r4 = idx.refresh()
    assert r4["files_indexed"] == 2
    assert r4["files_reindexed"] == 0
    assert r4["files_unchanged"] == 2
    assert r4["files_deleted"] == 1
    assert r4["root_hash"] != r3["root_hash"]
    assert r4["chunks_total"] < r3["chunks_total"]
    assert _query_indexed_file_paths(idx.db_path) == {"pkg/alpha.py", "docs/guide.md"}

    pruned_chunks = _query_index_chunks(idx.db_path)
    assert all(c["path"] != "pkg/beta.py" for c in pruned_chunks)
    beta_search = idx.search("unique_beta_disposal_target", auto_refresh=False)
    assert all(item["path"] != "pkg/beta.py" for item in beta_search["results"])
    assert all("unique_beta_disposal_target" not in item["symbol"] for item in beta_search["results"])


def test_code_index_strictly_excludes_env_secrets_symlinks_hardlinks_and_ignored_dirs(
    tmp_path: Path,
) -> None:
    ws = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    outside = tmp_path / "outside"
    (ws / "src").mkdir(parents=True)
    (ws / "certs").mkdir(parents=True)
    (ws / ".git").mkdir(parents=True)
    (ws / ".venv" / "lib").mkdir(parents=True)
    (ws / "node_modules" / "dep").mkdir(parents=True)
    (ws / ".agent-lab").mkdir(parents=True)
    outside.mkdir(parents=True)

    # Single legitimate source file
    (ws / "src" / "safe_service.py").write_text(
        "def legitimate_health_probe() -> str:\n    return 'healthy'\n",
        encoding="utf-8",
    )

    # Secret & credential filenames (using harmless non-secret placeholder strings)
    (ws / ".env").write_text("SENTINEL_ENV_MARKER=" + "blocked_dot_env_val\n", encoding="utf-8")
    (ws / ".env.local").write_text("SENTINEL_ENV_LOCAL=" + "blocked_local_env_val\n", encoding="utf-8")
    (ws / "secrets.json").write_text('{"marker": "blocked_secrets_json_val"}\n', encoding="utf-8")
    (ws / "credentials.py").write_text("MARKER = 'blocked_credentials_py_val'\n", encoding="utf-8")
    (ws / "certs" / "service.pem").write_text("PLACEHOLDER_CERT_BLOCKED\n", encoding="utf-8")
    (ws / "certs" / "service.key").write_text("PLACEHOLDER_KEY_BLOCKED\n", encoding="utf-8")
    (ws / ".netrc").write_text("machine example.invalid login demo_blocked_netrc\n", encoding="utf-8")
    (ws / "id_ed25519").write_text("PLACEHOLDER_SSH_KEY_BLOCKED\n", encoding="utf-8")

    # Ignored VCS / virtualenv / dependency / internal directories
    (ws / ".git" / "config").write_text("[core]\n\tmarker = blocked_git_config\n", encoding="utf-8")
    (ws / ".venv" / "lib" / "site_pkg.py").write_text(
        "def blocked_venv_symbol() -> None:\n    pass\n",
        encoding="utf-8",
    )
    (ws / "node_modules" / "dep" / "index.js").write_text(
        "export function blockedNodeModulesFn() { return 1; }\n",
        encoding="utf-8",
    )
    (ws / ".agent-lab" / "ledger.json").write_text('{"marker": "blocked_agent_lab"}\n', encoding="utf-8")

    # Symlink to outside file + symlink to outside directory
    outside_file = outside / "escaped.py"
    outside_file.write_text("def blocked_symlink_file_fn() -> int:\n    return 42\n", encoding="utf-8")
    os.symlink(outside_file, ws / "src" / "symlinked_file.py")

    outside_dir = outside / "escaped_dir"
    outside_dir.mkdir()
    (outside_dir / "nested.py").write_text("def blocked_symlink_dir_fn() -> int:\n    return 99\n", encoding="utf-8")
    os.symlink(outside_dir, ws / "src" / "symlinked_dir")

    # Hardlink to outside file (st_nlink == 2)
    outside_hard = outside / "hard_target.py"
    outside_hard.write_text("def blocked_hardlink_file_fn() -> int:\n    return 77\n", encoding="utf-8")
    os.link(outside_hard, ws / "src" / "hardlinked_file.py")

    # Binary NUL file
    (ws / "src" / "binary_blob.txt").write_bytes(b"HEADER\x00blocked_binary_payload")

    idx = CodebaseMerkleIndex(ws, state_dir)
    res = idx.refresh()

    assert res["ok"] is True
    assert res["files_indexed"] == 1
    assert _query_indexed_file_paths(idx.db_path) == {"src/safe_service.py"}

    chunks = _query_index_chunks(idx.db_path)
    assert len(chunks) == 1
    assert chunks[0]["path"] == "src/safe_service.py"
    assert chunks[0]["symbol"] == "legitimate_health_probe"

    for forbidden_query in (
        "blocked_dot_env_val",
        "blocked_secrets_json_val",
        "blocked_credentials_py_val",
        "blocked_venv_symbol",
        "blockedNodeModulesFn",
        "blocked_symlink_file_fn",
        "blocked_symlink_dir_fn",
        "blocked_hardlink_file_fn",
        "blocked_git_config",
    ):
        hits = idx.search(forbidden_query, auto_refresh=False)["results"]
        assert all(h["path"] == "src/safe_service.py" for h in hits)
        assert all(forbidden_query not in h["excerpt"] for h in hits)


def test_code_index_hybrid_bm25_dense_rrf_search_line_numbers_and_path_prefix(tmp_path: Path) -> None:
    ws = tmp_path / "workspace"
    state_dir = tmp_path / "state"
    (ws / "backend").mkdir(parents=True)
    (ws / "frontend").mkdir(parents=True)
    (ws / "docs").mkdir(parents=True)

    backend_lines = [
        '"""Payment settlement routines."""',
        "",
        "DEFAULT_GRACE_HOURS = 48",
        "",
        "def compute_ledger_reconciliation(debits: list[int], credits: list[int]) -> int:",
        "    # Reconcile posted debit and credit entries in minor currency units.",
        "    net_delta = sum(credits) - sum(debits)",
        "    return net_delta",
        "",
        "def validate_refund_window(elapsed_hours: int) -> bool:",
        "    return 0 <= elapsed_hours <= DEFAULT_GRACE_HOURS",
        "",
    ]
    (ws / "backend" / "payments.py").write_text("\n".join(backend_lines), encoding="utf-8")

    (ws / "frontend" / "payments.ts").write_text(
        "\n".join(
            [
                "export function formatLedgerReconciliation(netDelta: number): string {",
                "  return `Ledger reconciliation delta: ${netDelta}`;",
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    (ws / "docs" / "payments.md").write_text(
        "# Ledger Reconciliation Runbook\nOperational checklist for daily settlement reviews and exports.\n",
        encoding="utf-8",
    )

    # Custom hybrid embedder: delegates to LocalSubwordDenseEmbedder (default dim=256) for normal queries,
    # and injects an orthogonal semantic-only match for a zero-lexical-overlap query to verify dense RRF recall.
    base_embedder = LocalSubwordDenseEmbedder(dim=256)

    def hybrid_embedder(texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            if text.strip() == "semantic_only_refund_probe":
                out.append([1.0] + [0.0] * 256)
            elif "validate_refund_window" in text:
                vec = base_embedder([text])[0]
                out.append([2.5, *vec])
            else:
                vec = base_embedder([text])[0]
                out.append([0.0, *vec])
        return out

    idx = CodebaseMerkleIndex(ws, state_dir, embedder=hybrid_embedder)
    idx.refresh()

    # 1. Target function search with exact 1-based line numbers
    res = execute_codebase_semantic_search(
        idx,
        {"query": "compute_ledger_reconciliation minor currency units", "limit": 5},
    )
    assert res["ok"] is True
    assert res["root_hash"]
    assert res["results"]
    top = res["results"][0]
    assert top["path"] == "backend/payments.py"
    assert top["symbol"] == "compute_ledger_reconciliation"
    assert top["kind"] == "function"
    assert top["start_line"] == 5
    assert top["end_line"] == 8
    assert top["score"] > 0
    assert "net_delta = sum(credits) - sum(debits)" in top["excerpt"]

    # 2. path_prefix filter isolates frontend/ from backend/ and docs/
    fe_res = idx.search("ledger reconciliation", limit=5, path_prefix="frontend/", auto_refresh=False)
    assert fe_res["path_prefix"] == "frontend"
    assert fe_res["results"]
    assert all(hit["path"].startswith("frontend/") for hit in fe_res["results"])
    assert fe_res["results"][0]["path"] == "frontend/payments.ts"
    assert fe_res["results"][0]["symbol"] == "formatLedgerReconciliation"
    assert fe_res["results"][0]["start_line"] == 1
    assert fe_res["results"][0]["end_line"] == 3

    be_res = idx.search("ledger reconciliation", limit=5, path_prefix="backend", auto_refresh=False)
    assert be_res["results"]
    assert all(hit["path"].startswith("backend/") for hit in be_res["results"])

    # 3. Pure dense vector recall (zero lexical token overlap with target function)
    dense_res = idx.search("semantic_only_refund_probe", limit=3, auto_refresh=False)
    assert dense_res["results"]
    assert dense_res["results"][0]["path"] == "backend/payments.py"
    assert dense_res["results"][0]["symbol"] == "validate_refund_window"
    assert dense_res["results"][0]["start_line"] == 10
    assert dense_res["results"][0]["end_line"] == 11

    # 4. Path traversal in path_prefix rejected
    with pytest.raises(ValueError, match="path_prefix"):
        idx.search("ledger", path_prefix="../outside", auto_refresh=False)


# ---------------------------------------------------------------------------
# 2. Headless ARIA Snapshot Browser Layer (browser.py)
# ---------------------------------------------------------------------------


async def test_browser_mock_backend_end_to_end_aria_interaction_and_screenshot_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_LAB_BROWSER_ALLOW_LOOPBACK", raising=False)
    expected_tools = {
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_console_logs",
        "browser_screenshot",
    }
    assert {spec["name"] for spec in BROWSER_TOOL_SPECS} == expected_tools

    backend = MockBrowserBackend()
    artifacts_dir = tmp_path / "artifacts"
    mgr = BrowserSessionManager(
        backend=backend,
        artifacts_dir=artifacts_dir,
        run_id="run-browser-e2e",
    )

    try:
        # Navigate to allowed HTTPS URL
        nav = await mgr.execute("browser_navigate", {"url": "https://app.example.com/search"})
        assert nav["ok"] is True
        assert nav["url"] == "https://app.example.com/search"
        assert "app.example.com/search" in nav["title"]

        # ARIA snapshot returns deterministic e1 / e2 / e3 refs
        snap = await mgr.execute("browser_snapshot", {"max_nodes": 50})
        assert snap["ok"] is True
        assert snap["node_count"] == 3
        refs = [n["ref"] for n in snap["nodes"]]
        assert refs == ["e1", "e2", "e3"]
        assert [n["role"] for n in snap["nodes"]] == ["heading", "textbox", "button"]
        assert "[e1]" in snap["tree_text"]
        assert "[e2]" in snap["tree_text"]
        assert "[e3]" in snap["tree_text"]

        # Type into textbox e2 with submit=True
        typed = await mgr.execute(
            "browser_type",
            {"ref_or_selector": "e2", "text": "merkle index", "submit": True},
        )
        assert typed["ok"] is True
        assert typed["ref"] == "e2"
        assert typed["value"] == "merkle index"
        assert typed["submitted"] is True

        # Snapshot reflects updated textbox value
        snap_after_type = await mgr.execute("browser_snapshot", {})
        e2_node = next(n for n in snap_after_type["nodes"] if n["ref"] == "e2")
        assert e2_node["value"] == "merkle index"
        assert "value='merkle index'" in snap_after_type["tree_text"]

        # Click submit button e3
        clicked = await mgr.execute("browser_click", {"ref_or_selector": "e3"})
        assert clicked["ok"] is True
        assert clicked["ref"] == "e3"
        assert clicked["role"] == "button"
        assert clicked["name"] == "Submit"

        # Unknown ref returns structured element_not_found error
        missing = await mgr.execute("browser_click", {"ref_or_selector": "e999"})
        assert missing["ok"] is False
        assert missing["code"] == "element_not_found"

        # Console logs capture navigation, typing, form submit, and click
        logs = await mgr.execute("browser_console_logs", {"level": "info", "limit": 20})
        assert logs["ok"] is True
        log_texts = [m["text"] for m in logs["messages"]]
        assert any("Navigated to https://app.example.com/search" in t for t in log_texts)
        assert any("Typed into e2: merkle index" in t for t in log_texts)
        assert any("Submitted form from e2" in t for t in log_texts)
        assert any("Clicked e3 (Submit)" in t for t in log_texts)

        # Network requests recorded by backend
        net = await backend.network_requests(limit=10)
        assert net["ok"] is True
        assert net["requests"][-1] == {
            "method": "GET",
            "url": "https://app.example.com/search",
            "status": 200,
        }

        # Screenshot returns valid PNG header and persists artifact on disk
        shot = await mgr.execute("browser_screenshot", {"full_page": True})
        assert shot["ok"] is True
        assert shot["artifact_name"] == "browser_shot_001.png"
        assert shot["bytes"] > 20
        saved_path = Path(shot["saved_path"])
        assert saved_path == artifacts_dir / "run-browser-e2e" / "browser_shot_001.png"
        assert saved_path.is_file()
        png_bytes = saved_path.read_bytes()
        assert png_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        assert b"IHDR" in png_bytes and b"IEND" in png_bytes
        preview_bytes = base64.b64decode(shot["base64_preview"])
        assert preview_bytes.startswith(b"\x89PNG\r\n\x1a\n")

        # SSRF blocked cleanly through BrowserSessionManager without altering current_url
        blocked = await mgr.execute("browser_navigate", {"url": "http://169.254.169.254/latest/meta-data/"})
        assert blocked["ok"] is False
        assert blocked["code"] == "ssrf_blocked"
        assert backend.current_url == "https://app.example.com/search"

        # Loopback blocked by default through manager, allowed with allow_loopback=True
        lb_denied = await mgr.execute("browser_navigate", {"url": "http://127.0.0.1:8787/health"})
        assert lb_denied["ok"] is False
        assert lb_denied["code"] == "ssrf_blocked"

        lb_ok = await mgr.execute(
            "browser_navigate",
            {"url": "http://127.0.0.1:8787/health", "allow_loopback": True},
        )
        assert lb_ok["ok"] is True
        assert backend.current_url == "http://127.0.0.1:8787/health"
    finally:
        assert backend.closed is False
        await mgr.aclose()
        assert backend.closed is True


@pytest.mark.parametrize(
    ("bad_url", "expected_code"),
    [
        ("file:///etc/passwd", "blocked_scheme"),
        ("javascript:alert(1)", "blocked_scheme"),
        ("data:text/html,<h1>x</h1>", "blocked_scheme"),
        ("chrome://settings", "blocked_scheme"),
        ("http://169.254.169.254/latest/meta-data/", "ssrf_blocked"),
        ("http://[::ffff:169.254.169.254]/latest/meta-data/", "ssrf_blocked"),
        ("http://metadata.google.internal/computeMetadata/v1/", "ssrf_blocked"),
        ("https://metadata.goog/computeMetadata/v1/", "ssrf_blocked"),
        ("http://10.0.0.5/internal", "ssrf_blocked"),
        ("http://172.16.12.34/admin", "ssrf_blocked"),
        ("http://192.168.1.1/status", "ssrf_blocked"),
        ("http://[::ffff:10.0.0.1]/admin", "ssrf_blocked"),
        ("http://127.0.0.1:8787/", "ssrf_blocked"),
        ("http://localhost:3000/", "ssrf_blocked"),
        ("http://dev.localhost:5173/", "ssrf_blocked"),
        ("http://[::1]:8000/", "ssrf_blocked"),
        ("https://" + "u_demo" + ":" + "p_demo" + "@example.com/", "invalid_url"),
        ("https://example.com/\r\nX-Test: 1", "invalid_url"),
    ],
)
def test_validate_browser_url_blocks_schemes_imds_private_and_loopback(
    bad_url: str,
    expected_code: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_LAB_BROWSER_ALLOW_LOOPBACK", raising=False)
    with pytest.raises(BrowserError) as exc_info:
        validate_browser_url(bad_url)
    assert exc_info.value.code == expected_code


def test_validate_browser_url_loopback_opt_in_still_blocks_imds_and_rfc1918(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_LAB_BROWSER_ALLOW_LOOPBACK", raising=False)

    assert validate_browser_url("https://example.com/docs") == "https://example.com/docs"
    assert validate_browser_url("http://127.0.0.1:8787/app", allow_loopback=True) == "http://127.0.0.1:8787/app"
    assert validate_browser_url("http://localhost:3000/", allow_loopback=True) == "http://localhost:3000/"
    assert validate_browser_url("http://dev.localhost:5173/", allow_loopback=True) == "http://dev.localhost:5173/"

    # Even with allow_loopback=True, IMDS and RFC1918 private IPs MUST stay blocked.
    for forbidden in (
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://10.1.2.3/",
        "http://172.20.0.1/",
        "http://192.168.0.10/",
        "file:///etc/hosts",
    ):
        with pytest.raises(BrowserError):
            validate_browser_url(forbidden, allow_loopback=True)


# ---------------------------------------------------------------------------
# 3. MCP Frontier & A2A Frontier (mcp_client.py, a2a.py, storage.py)
# ---------------------------------------------------------------------------


async def test_mcp_client_manager_roots_sampling_elicitation_and_progress(tmp_path: Path) -> None:
    ws = tmp_path / "mcp_workspace"
    ws.mkdir()

    # 1. Default safe posture: no workspace_root -> empty roots; sampling denied; elicitation declines
    default_mgr = McpClientManager()
    assert default_mgr.list_roots() == []
    denied_sampling = await default_mgr.handle_sampling_request(
        "ext-srv",
        {"messages": [{"role": "user", "content": "summarize"}], "maxTokens": 100},
    )
    assert denied_sampling["ok"] is False
    assert denied_sampling["code"] == "sampling_denied"

    declined_elicit = await default_mgr.handle_elicitation_request(
        "ext-srv",
        {"mode": "form", "message": "Select environment", "requestedSchema": {"type": "object"}},
    )
    assert declined_elicit == {"action": "decline"}

    # 2. Configured manager: roots + sampling budget caps + elicitation delegation + progress recording
    sampling_calls: list[dict[str, Any]] = []
    elicitation_calls: list[dict[str, Any]] = []
    progress_events: list[dict[str, Any]] = []

    async def sampling_handler(payload: dict[str, Any]) -> dict[str, Any]:
        sampling_calls.append(payload)
        if len(sampling_calls) == 1:
            return {"text": "first summary", "model": "mock-host-model", "tokens_used": 140}
        return {"text": "second summary", "model": "mock-host-model", "tokens_used": 60}

    async def elicitation_handler(payload: dict[str, Any]) -> dict[str, Any]:
        elicitation_calls.append(payload)
        if payload["mode"] == "form":
            return {"action": "accept", "content": {"region": "us-east"}}
        return {"action": "cancel"}

    async def progress_cb(entry: dict[str, Any]) -> None:
        progress_events.append(entry)

    mgr = McpClientManager(
        servers={
            "trusted-srv": {"transport": "stdio", "command": "echo", "allow_sampling": True},
            "locked-srv": {"transport": "stdio", "command": "echo", "allow_sampling": False},
        },
        workspace_root=ws,
        sampling_handler=sampling_handler,
        elicitation_handler=elicitation_handler,
        progress_callback=progress_cb,
        sampling_max_tokens=120,
        sampling_token_budget=200,
    )

    # list_roots() returns resolved workspace file:// URI
    roots = mgr.list_roots()
    assert len(roots) == 1
    assert roots[0]["uri"] == ws.resolve().as_uri()
    assert roots[0]["uri"].startswith("file://")
    assert roots[0]["name"] == ws.resolve().name

    caps = mgr.client_capabilities_summary("trusted-srv")
    assert caps["roots"] == {"enabled": True, "root_count": 1}
    assert caps["sampling"]["enabled"] is True
    assert caps["sampling"]["max_tokens_per_request"] == 120
    assert caps["sampling"]["token_budget"] == 200
    assert caps["elicitation"] == {"enabled": True, "modes": ["form", "url"]}
    assert caps["progress"] == {"enabled": True, "callback_configured": True}

    # Per-server allow_sampling=False denies even when sampling_handler is configured
    locked_res = await mgr.handle_sampling_request(
        "locked-srv",
        {"messages": [{"role": "user", "content": "hello"}], "maxTokens": 50},
    )
    assert locked_res["ok"] is False
    assert locked_res["code"] == "sampling_denied"
    assert sampling_calls == []

    # Invalid maxTokens rejected cleanly
    bad_tok = await mgr.handle_sampling_request(
        "trusted-srv",
        {"messages": [{"role": "user", "content": "hello"}], "maxTokens": 0},
    )
    assert bad_tok["ok"] is False
    assert bad_tok["code"] == "sampling_invalid_params"

    # First sampling call clamps requested maxTokens=500 -> per_call_cap=120
    s1 = await mgr.handle_sampling_request(
        "trusted-srv",
        {"messages": [{"role": "user", "content": "analyze diff"}], "maxTokens": 500},
    )
    assert s1["ok"] is True
    assert s1["text"] == "first summary"
    assert s1["tokens_used"] == 140
    assert s1["remaining_token_budget"] == 60
    assert sampling_calls[0]["max_tokens"] == 120
    assert sampling_calls[0]["requested_max_tokens"] == 500
    assert sampling_calls[0]["remaining_token_budget"] == 200

    # Second sampling call clamps maxTokens=100 -> remaining budget=60
    s2 = await mgr.handle_sampling_request(
        "trusted-srv",
        {"messages": [{"role": "user", "content": "second pass"}], "maxTokens": 100},
    )
    assert s2["ok"] is True
    assert s2["remaining_token_budget"] == 0
    assert sampling_calls[1]["max_tokens"] == 60

    # Third sampling call rejected with sampling_budget_exceeded without invoking handler
    s3 = await mgr.handle_sampling_request(
        "trusted-srv",
        {"messages": [{"role": "user", "content": "over budget"}], "maxTokens": 10},
    )
    assert s3["ok"] is False
    assert s3["code"] == "sampling_budget_exceeded"
    assert len(sampling_calls) == 2

    # Elicitation delegation: form accept, url cancel, unsafe scheme rejected before handler
    e_form = await mgr.handle_elicitation_request(
        "trusted-srv",
        {
            "mode": "form",
            "message": "Choose target region",
            "requestedSchema": {"type": "object", "properties": {"region": {"type": "string"}}},
        },
    )
    assert e_form == {"action": "accept", "content": {"region": "us-east"}}
    assert len(elicitation_calls) == 1
    assert elicitation_calls[0]["server"] == "trusted-srv"
    assert elicitation_calls[0]["mode"] == "form"

    e_url = await mgr.handle_elicitation_request(
        "trusted-srv",
        {
            "mode": "url",
            "message": "Complete login",
            "url": "https://consent.example.com/authorize",
            "elicitationId": "el-42",
        },
    )
    assert e_url == {"action": "cancel"}
    assert len(elicitation_calls) == 2
    assert elicitation_calls[1]["url"] == "https://consent.example.com/authorize"
    assert elicitation_calls[1]["elicitation_id"] == "el-42"

    e_bad_scheme = await mgr.handle_elicitation_request(
        "trusted-srv",
        {"mode": "url", "message": "Unsafe", "url": "javascript:alert(1)"},
    )
    assert e_bad_scheme == {"action": "decline"}
    assert len(elicitation_calls) == 2  # handler was NOT called

    # _record_progress + notifications/progress message handler + recent_progress()
    await mgr._record_progress(
        "trusted-srv",
        progress=1,
        total=3,
        message="Step 1: scanning",
        progress_token="ptok-1",
        tool="index_repo",
    )
    await mgr._record_progress(
        "trusted-srv",
        progress=2,
        total=3,
        message="Step 2: chunking",
        progress_token="ptok-1",
        tool="index_repo",
    )
    msg_handler = mgr._make_message_handler("trusted-srv")
    await msg_handler(
        {
            "method": "notifications/progress",
            "params": {
                "progress": 3,
                "total": 3,
                "message": "Step 3: complete",
                "progressToken": "ptok-1",
            },
        }
    )

    recent_all = mgr.recent_progress(limit=10)
    assert len(recent_all) == 3
    assert [p["progress"] for p in recent_all] == [1.0, 2.0, 3.0]
    assert [p["message"] for p in recent_all] == [
        "Step 1: scanning",
        "Step 2: chunking",
        "Step 3: complete",
    ]
    assert all(p["progress_token"] == "ptok-1" for p in recent_all)
    assert len(progress_events) == 3

    recent_last_two = mgr.recent_progress(limit=2)
    assert [p["progress"] for p in recent_last_two] == [2.0, 3.0]
    # Mutating returned snapshot does not corrupt manager's internal buffer
    recent_last_two[0]["message"] = "mutated"
    assert mgr.recent_progress(limit=2)[0]["message"] == "Step 2: chunking"


def test_storage_migration_7_a2a_context_index_and_push_configs_crud_tenant_isolation(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.sqlite3"
    store = Storage(db_path)

    migrations = store.applied_migrations()
    mig_map = {m["version"]: m["name"] for m in migrations}
    assert mig_map.get(7) == "a2a_context_and_push_configs"

    # Verify runs.a2a_context_id column, idx_runs_a2a_context_id index, and a2a_push_configs table exist
    with sqlite3.connect(str(db_path)) as con:
        run_cols = {row[1] for row in con.execute("PRAGMA table_info(runs)").fetchall()}
        assert "a2a_context_id" in run_cols
        indexes = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_runs_a2a_context_id" in indexes
        assert "idx_a2a_push_configs_tenant" in indexes

    # 1. runs.a2a_context_id auto-populated via keyword arg, metadata, and save_run update
    run_a1 = store.create_run("First turn in ctx-shared", tenant_id="tenant-a", a2a_context_id="ctx-shared")
    run_a2 = store.create_run(
        "Second turn in ctx-shared",
        tenant_id="tenant-a",
        metadata={"a2a_context_id": "ctx-shared"},
    )
    run_a_other = store.create_run("Other ctx", tenant_id="tenant-a", a2a_context_id="ctx-other")
    run_b1 = store.create_run(
        "Tenant B same ctx id",
        tenant_id="tenant-b",
        a2a_context_id="ctx-shared",
    )
    run_a3 = store.create_run("Initially no ctx", tenant_id="tenant-a")
    run_a3["metadata"]["a2a_context_id"] = "ctx-shared"
    store.save_run(run_a3)

    # Verify SQL column directly populated & indexed lookup uses idx_runs_a2a_context_id
    with sqlite3.connect(str(db_path)) as con:
        row_col = con.execute("SELECT a2a_context_id FROM runs WHERE id=?", (run_a3["id"],)).fetchone()
        assert row_col == ("ctx-shared",)
        plan_rows = con.execute(
            "EXPLAIN QUERY PLAN SELECT body FROM runs WHERE a2a_context_id=? AND tenant_id=? "
            "ORDER BY created_at DESC, id DESC LIMIT 10",
            ("ctx-shared", "tenant-a"),
        ).fetchall()
        plan_text = " ".join(str(r) for r in plan_rows)
        assert "idx_runs_a2a_context_id" in plan_text

    # list_runs_by_a2a_context respects tenant isolation
    tenant_a_runs = store.list_runs_by_a2a_context("ctx-shared", tenant_id="tenant-a")
    assert {r["id"] for r in tenant_a_runs} == {run_a1["id"], run_a2["id"], run_a3["id"]}
    assert run_a_other["id"] not in {r["id"] for r in tenant_a_runs}
    assert run_b1["id"] not in {r["id"] for r in tenant_a_runs}

    tenant_b_runs = store.list_runs_by_a2a_context("ctx-shared", tenant_id="tenant-b")
    assert [r["id"] for r in tenant_b_runs] == [run_b1["id"]]

    assert store.list_runs_by_a2a_context("ctx-nonexistent", tenant_id="tenant-a") == []

    # 2. a2a_push_configs CRUD with strict tenant isolation
    signing_val_1 = "test-push-" + "signing-key-one"
    signing_val_2 = "test-push-" + "signing-key-two"

    created = store.set_a2a_push_config(
        run_a1["id"],
        "tenant-a",
        "https://hooks.example.com/a2a/v1",
        secret=signing_val_1,
        events=["status-update", "artifact-update", "status-update"],
    )
    assert created["task_id"] == run_a1["id"]
    assert created["tenant_id"] == "tenant-a"
    assert created["url"] == "https://hooks.example.com/a2a/v1"
    assert created["has_secret"] is True
    assert created["token_hash"] == digest(signing_val_1)
    assert created["events"] == ["status-update", "artifact-update"]

    # Read with owning tenant succeeds; cross-tenant read returns None
    fetched = store.get_a2a_push_config(run_a1["id"], tenant_id="tenant-a")
    assert fetched is not None
    assert fetched["url"] == "https://hooks.example.com/a2a/v1"
    assert fetched["secret"] == signing_val_1
    assert store.get_a2a_push_config(run_a1["id"], tenant_id="tenant-b") is None

    # Cross-tenant upsert refused with KeyError("task_not_found")
    with pytest.raises(KeyError, match="task_not_found"):
        store.set_a2a_push_config(
            run_a1["id"],
            "tenant-b",
            "https://attacker.example.com/hijack",
            secret=signing_val_2,
        )

    # Same-tenant update preserves created_at and updates url/secret/events
    updated = store.set_a2a_push_config(
        run_a1["id"],
        "tenant-a",
        "https://hooks.example.com/a2a/v2",
        secret=signing_val_2,
        events=["status-update"],
    )
    assert updated["created_at"] == created["created_at"]
    assert updated["url"] == "https://hooks.example.com/a2a/v2"
    assert updated["token_hash"] == digest(signing_val_2)
    assert updated["events"] == ["status-update"]

    # Cross-tenant delete is a no-op returning False
    assert store.delete_a2a_push_config(run_a1["id"], tenant_id="tenant-b") is False
    assert store.get_a2a_push_config(run_a1["id"], tenant_id="tenant-a") is not None

    # Same-tenant delete removes config
    assert store.delete_a2a_push_config(run_a1["id"], tenant_id="tenant-a") is True
    assert store.delete_a2a_push_config(run_a1["id"], tenant_id="tenant-a") is False
    assert store.get_a2a_push_config(run_a1["id"], tenant_id="tenant-a") is None

    # Deleting a run cascades removal of its push config
    store.set_a2a_push_config(run_a2["id"], "tenant-a", "https://hooks.example.com/a2a/cascade")
    assert store.get_a2a_push_config(run_a2["id"], tenant_id="tenant-a") is not None
    assert store.delete_run(run_a2["id"], tenant_id="tenant-a") is True
    assert store.get_a2a_push_config(run_a2["id"], tenant_id="tenant-a") is None


async def test_a2a_push_notification_ssrf_blocking_and_hmac_sha256_signature(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_LAB_A2A_ALLOW_PRIVATE", raising=False)

    transport_hits = 0

    def fail_if_called(request: httpx.Request) -> httpx.Response:
        nonlocal transport_hits
        transport_hits += 1
        return httpx.Response(200)

    blocked_transport = httpx.MockTransport(fail_if_called)
    sample_event = build_status_update_event("task-101", "ctx-101", "completed", message_text="Done", final=True)

    # SSRF / IMDS / private / loopback / plaintext public HTTP must all be blocked before transport
    for blocked_url in (
        "http://169.254.169.254/latest/meta-data/",
        "https://metadata.google.internal/computeMetadata/v1/",
        "http://127.0.0.1:9000/webhook",
        "http://localhost:9000/webhook",
        "https://10.0.0.8/internal-hook",
        "https://192.168.1.25/hook",
        "http://hooks.example.com/insecure-http",
        "file:///etc/passwd",
    ):
        res = await deliver_a2a_push_notification(
            blocked_url,
            sample_event,
            transport=blocked_transport,
        )
        assert res["ok"] is False, blocked_url
        assert res["status_code"] is None
        assert res["error"]

    assert transport_hits == 0

    # Valid HTTPS webhook signs body with verifiable HMAC-SHA256 X-A2A-Signature
    captured: dict[str, Any] = {}
    hmac_key = "unit-test-a2a-" + "hmac-signing-value"

    def capture_handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(202, json={"accepted": True})

    ok_res = await deliver_a2a_push_notification(
        "https://hooks.example.com/a2a/events",
        sample_event,
        secret=hmac_key,
        transport=httpx.MockTransport(capture_handler),
    )

    assert ok_res == {"ok": True, "status_code": 202, "error": None}
    assert captured["url"] == "https://hooks.example.com/a2a/events"
    sig_header = captured["headers"].get("x-a2a-signature", "")
    assert sig_header.startswith("sha256=")
    assert len(sig_header) == len("sha256=") + 64
    assert verify_a2a_push_signature(captured["body"], sig_header, hmac_key) is True
    assert verify_a2a_push_signature(captured["body"] + b" ", sig_header, hmac_key) is False
    assert verify_a2a_push_signature(captured["body"], sig_header, "wrong-signing-value") is False
    assert verify_a2a_push_signature(captured["body"], "", hmac_key) is False
    assert json.loads(captured["body"].decode("utf-8"))["taskId"] == "task-101"

    # Non-2xx webhook response reported cleanly
    err_res = await deliver_a2a_push_notification(
        "https://hooks.example.com/a2a/events",
        sample_event,
        secret=hmac_key,
        transport=httpx.MockTransport(lambda req: httpx.Response(503, text="unavailable")),
    )
    assert err_res == {"ok": False, "status_code": 503, "error": "HTTP 503"}


async def test_a2a_client_send_message_stream_sse_and_method_not_found_fallback() -> None:
    def _sse_payload(frames: list[dict[str, Any]], *, include_comment: bool = True) -> bytes:
        chunks: list[str] = []
        if include_comment:
            chunks.append(": keepalive\n\n")
        for frame in frames:
            chunks.append(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n")
        chunks.append("data: [DONE]\n\n")
        return "".join(chunks).encode("utf-8")

    # Case 1: Live SSE stream consuming status-update & artifact-update frames over message/stream
    stream_methods_seen: list[str] = []
    ev_working = build_status_update_event("task-stream-1", "ctx-stream-1", "working", message_text="Running")
    ev_art_1 = build_artifact_update_event(
        "task-stream-1", "ctx-stream-1", "art-1", "Hello ", append=True, last_chunk=False
    )
    ev_art_2 = build_artifact_update_event(
        "task-stream-1", "ctx-stream-1", "art-1", "world!", append=True, last_chunk=True
    )
    ev_done = build_status_update_event(
        "task-stream-1", "ctx-stream-1", "completed", message_text="Hello world!", final=True
    )

    def live_sse_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        stream_methods_seen.append(str(body["method"]))
        req_id = body["id"]
        frames = [
            jsonrpc_success(req_id, ev_working),
            jsonrpc_success(req_id, ev_art_1),
            jsonrpc_success(req_id, ev_art_2),
            jsonrpc_success(req_id, ev_done),
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            content=_sse_payload(frames),
        )

    peers_doc = {
        "schema_version": 1,
        "peers": {
            "remote-worker": {
                "name": "remote-worker",
                "url": "https://agent.example.com",
                "enabled": True,
                "allow_private_network": False,
            }
        },
    }
    mgr = A2AClientManager(peers_doc, transport=httpx.MockTransport(live_sse_handler))
    try:
        received: list[dict[str, Any]] = []
        async for ev in mgr.send_message_stream(
            "remote-worker",
            "Stream the status and output",
            context_id="ctx-stream-1",
        ):
            received.append(ev)

        assert stream_methods_seen == ["message/stream"]
        assert [e["kind"] for e in received] == [
            "status-update",
            "artifact-update",
            "artifact-update",
            "status-update",
        ]
        assert received[0]["status"]["state"] == "working"
        combined_text = "".join(
            e["artifact"]["parts"][0]["text"] for e in received if e["kind"] == "artifact-update"
        )
        assert combined_text == "Hello world!"
        assert received[-1]["status"]["state"] == "completed"
        assert received[-1]["final"] is True

        # Unknown peer yields structured error frame
        unknown_events = [e async for e in mgr.send_message_stream("no-such-peer", "hi")]
        assert len(unknown_events) == 1
        assert unknown_events[0]["ok"] is False
        assert unknown_events[0]["code"] == "a2a_peer_not_found"
    finally:
        await mgr.aclose()

    # Case 2: Fallback message/stream -> SendStreamingMessage when JSON response returns -32601
    json_fallback_methods: list[str] = []

    def json_32601_fallback_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = str(body["method"])
        req_id = body["id"]
        json_fallback_methods.append(method)
        if method == "message/stream":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                json=jsonrpc_error(req_id, JSONRPC_METHOD_NOT_FOUND, "Method not found"),
            )
        assert method == "SendStreamingMessage"
        frames = [
            jsonrpc_success(
                req_id,
                build_status_update_event("task-fb-1", "ctx-fb-1", "working"),
            ),
            jsonrpc_success(
                req_id,
                build_artifact_update_event("task-fb-1", "ctx-fb-1", "art-fb", "v1.0 stream ok", last_chunk=True),
            ),
            jsonrpc_success(
                req_id,
                build_status_update_event("task-fb-1", "ctx-fb-1", "completed", final=True),
            ),
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_payload(frames),
        )

    mgr_fb_json = A2AClientManager(peers_doc, transport=httpx.MockTransport(json_32601_fallback_handler))
    try:
        fb_events = [e async for e in mgr_fb_json.send_message_stream("remote-worker", "Fallback test")]
        assert json_fallback_methods == ["message/stream", "SendStreamingMessage"]
        assert [e["kind"] for e in fb_events] == ["status-update", "artifact-update", "status-update"]
        assert fb_events[1]["artifact"]["parts"][0]["text"] == "v1.0 stream ok"
        assert fb_events[2]["final"] is True
    finally:
        await mgr_fb_json.aclose()

    # Case 3: Fallback message/stream -> SendStreamingMessage when SSE frame returns -32601
    sse_fallback_methods: list[str] = []

    def sse_32601_fallback_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        method = str(body["method"])
        req_id = body["id"]
        sse_fallback_methods.append(method)
        if method == "message/stream":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=_sse_payload([jsonrpc_error(req_id, JSONRPC_METHOD_NOT_FOUND, "Use SendStreamingMessage")]),
            )
        assert method == "SendStreamingMessage"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse_payload(
                [
                    jsonrpc_success(
                        req_id,
                        build_status_update_event("task-fb-2", "ctx-fb-2", "completed", message_text="ok", final=True),
                    )
                ]
            ),
        )

    mgr_fb_sse = A2AClientManager(peers_doc, transport=httpx.MockTransport(sse_32601_fallback_handler))
    try:
        sse_fb_events = [e async for e in mgr_fb_sse.send_message_stream("remote-worker", "SSE fallback test")]
        assert sse_fallback_methods == ["message/stream", "SendStreamingMessage"]
        assert len(sse_fb_events) == 1
        assert sse_fb_events[0]["kind"] == "status-update"
        assert sse_fb_events[0]["status"]["state"] == "completed"
    finally:
        await mgr_fb_sse.aclose()
