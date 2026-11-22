"""多语言代码地图与跨文件符号检索（线程安全、.gitignore/凭据过滤、截断透明性与多语言解析）测试。"""

import shutil
import threading

from agent_lab.api import ROOT
from agent_lab.repomap import RepoMapIndex


def test_repomap_obeys_char_budget_and_ranks_cross_file_dependencies(tmp_path):
    ws = tmp_path / "repo"
    shutil.copytree(ROOT / "data/multi_file_repo", ws)
    index = RepoMapIndex(ws)

    full_map = index.build_map(max_chars=2400, focus_query="checkout discount")
    assert full_map["ok"] is True
    assert full_map["indexed_files"] == 4  # models.py, discounts.py, checkout.py, tests/test_checkout.py
    assert len(full_map["map"]) <= 2400
    assert "discounts.py" in full_map["map"]
    assert "compute_discount_cents" in full_map["map"]

    tight_map = index.build_map(max_chars=220, focus_query="Coupon")
    assert len(tight_map["map"]) <= 220
    assert tight_map["truncated"] is True


def test_symbol_search_finds_definitions_and_cross_file_usages(tmp_path):
    ws = tmp_path / "repo"
    shutil.copytree(ROOT / "data/multi_file_repo", ws)
    index = RepoMapIndex(ws)

    found = index.search_symbol("compute_discount_cents")
    assert found["ok"] is True
    def_paths = [d["path"] for d in found["definitions"]]
    ref_paths = [r["path"] for r in found["references"]]
    assert "discounts.py" in def_paths
    assert "checkout.py" in ref_paths

    class_found = index.search_symbol("LineItem")
    assert any(d["path"] == "models.py" and d["kind"] == "class" for d in class_found["definitions"])


def test_repomap_detects_file_hash_change_and_incrementally_refreshes(tmp_path):
    ws = tmp_path / "repo"
    shutil.copytree(ROOT / "data/multi_file_repo", ws)
    index = RepoMapIndex(ws)
    first = index.build_map(max_chars=2000)
    first_digest = first["index_sha256"]

    # 未修改文件时再次刷新：stale_refreshed 为空，索引摘要保持一致
    second = index.build_map(max_chars=2000)
    assert second["stale_refreshed"] == []
    assert second["index_sha256"] == first_digest

    # 修改单个文件并新增符号：增量仅重解析被改动的 discounts.py
    target = ws / "discounts.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n\ndef clamp_discount(v: int, cap: int) -> int:\n    return min(v, cap)\n",
        encoding="utf-8",
    )
    third = index.build_map(max_chars=2000, focus_query="clamp_discount")
    assert third["stale_refreshed"] == ["discounts.py"]
    assert third["index_sha256"] != first_digest
    sym = index.search_symbol("clamp_discount")
    assert any(d["path"] == "discounts.py" for d in sym["definitions"])


def test_repomap_thread_safe_under_8_concurrent_workers_and_file_edits(tmp_path):
    ws = tmp_path / "repo"
    shutil.copytree(ROOT / "data/multi_file_repo", ws)
    index = RepoMapIndex(ws)
    barrier = threading.Barrier(8)
    errors: list[BaseException] = []
    err_lock = threading.Lock()

    def worker(tid: int) -> None:
        try:
            barrier.wait(timeout=5.0)
            for step in range(20):
                if tid % 2 == 0:
                    mod_file = ws / f"worker_{tid}.py"
                    mod_file.write_text(
                        f"from discounts import compute_discount_cents\n\n"
                        f"def worker_fn_{tid}_{step}(x: int) -> int:\n"
                        f"    return compute_discount_cents(x, None) + {step}\n",
                        encoding="utf-8",
                    )
                meta = index.refresh()
                assert meta["indexed_files"] >= 4
                mapped = index.build_map(max_chars=1800, focus_query="discount")
                assert mapped["ok"] is True
                assert len(mapped["map"]) <= 1800
                found = index.search_symbol("compute_discount_cents")
                assert found["ok"] is True
                assert any(d["path"] == "discounts.py" for d in found["definitions"])
        except BaseException as exc:  # pragma: no cover - 捕获任何并发竞态异常
            with err_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15.0)
        assert not t.is_alive()

    assert errors == []
    final = index.search_symbol("worker_fn_0_19")
    assert any(d["path"] == "worker_0.py" for d in final["definitions"])


def test_repomap_gitignore_venv_secret_exclusion_and_index_truncation(tmp_path):
    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / ".gitignore").write_text(
        "# 忽略自动生成代码与构建目录\n"
        "generated/\n"
        "*.gen.py\n"
        "/scratch_local.py\n",
        encoding="utf-8",
    )
    # 业务文件 3 个 + 测试文件 3 个（验证截断时非 tests/ 文件优先保留）
    for name in ("alpha.py", "beta.py", "gamma.py"):
        (ws / name).write_text(f"def run_{name[:-3]}() -> int:\n    return 1\n", encoding="utf-8")
    (ws / "tests").mkdir()
    for idx in range(3):
        (ws / "tests" / f"test_mod_{idx}.py").write_text(
            f"def test_case_{idx}():\n    assert True\n", encoding="utf-8"
        )

    # 必须被排除的目录与文件：venv / node_modules / .gitignore / 敏感凭据文件
    for skip_dir in (".venv", "venv", "node_modules", "dist", "target", "generated"):
        d = ws / skip_dir
        d.mkdir()
        (d / "leaked.py").write_text("def should_never_index():\n    return 'no'\n", encoding="utf-8")
    (ws / "schema.gen.py").write_text("def generated_op():\n    return 0\n", encoding="utf-8")
    (ws / "scratch_local.py").write_text("def scratch_op():\n    return 0\n", encoding="utf-8")
    for secret_name in ("credentials.py", "secrets.py", "secret.py", "private_key.py", "tokens.py"):
        (ws / secret_name).write_text("API_SECRET = 'super-secret'\ndef leak_token(): pass\n", encoding="utf-8")

    capped = RepoMapIndex(ws, max_index_files=4)
    res = capped.build_map(max_chars=3000)
    assert res["discovered_files"] == 6
    assert res["indexed_files"] == 4
    assert res["index_truncated"] is True
    # 3 个业务文件必须全部排在 tests/ 之前被优先纳入
    assert {"alpha.py", "beta.py", "gamma.py"}.issubset(set(res["files_included"]))
    assert sum(1 for p in res["files_included"] if p.startswith("tests/")) == 1

    full = RepoMapIndex(ws, max_index_files=512)
    full_res = full.build_map(max_chars=4000)
    assert full_res["discovered_files"] == 6
    assert full_res["indexed_files"] == 6
    assert full_res["index_truncated"] is False
    for forbidden in ("should_never_index", "generated_op", "scratch_op", "leak_token", "credentials.py", "secrets.py"):
        assert forbidden not in full_res["map"]


def test_repomap_typescript_and_go_structural_symbol_indexing(tmp_path):
    ws = tmp_path / "polyglot_repo"
    (ws / "web").mkdir(parents=True)
    (ws / "svc").mkdir(parents=True)

    (ws / "web" / "pricing.ts").write_text(
        "// Pricing domain types\n"
        "export interface LineItem {\n"
        "  sku: string;\n"
        "  cents: number;\n"
        "}\n"
        "export type Currency = 'USD' | 'EUR';\n"
        "export enum DiscountTier {\n"
        "  None,\n"
        "  Vip,\n"
        "}\n"
        "export function computeDiscountCents(subtotal: number, rate: number): number {\n"
        "  return Math.floor(subtotal * rate);\n"
        "}\n",
        encoding="utf-8",
    )
    (ws / "web" / "checkout.ts").write_text(
        "import { LineItem, computeDiscountCents } from './pricing';\n\n"
        "export class CheckoutController {\n"
        "  async settleOrder(items: LineItem[]): Promise<number> {\n"
        "    const gross = items.length * 100;\n"
        "    return gross - computeDiscountCents(gross, 0.1);\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )
    (ws / "svc" / "ledger.go").write_text(
        "package svc\n\n"
        "import (\n"
        '\t"fmt"\n'
        ")\n\n"
        "type Auditor interface {\n"
        "\tAudit(id string) error\n"
        "}\n\n"
        "type Invoice struct {\n"
        "\tSubtotalCents int\n"
        "}\n\n"
        "func NormalizeCents(v int) int {\n"
        "\treturn v\n"
        "}\n\n"
        "func (inv *Invoice) FinalTotal(tax int) int {\n"
        "\tfmt.Println(tax)\n"
        "\treturn NormalizeCents(inv.SubtotalCents + tax)\n"
        "}\n",
        encoding="utf-8",
    )

    index = RepoMapIndex(ws)
    rmap = index.build_map(max_chars=4000)
    assert rmap["indexed_files"] == 3
    assert rmap["discovered_files"] == 3
    assert rmap["index_truncated"] is False

    ts_fn = index.search_symbol("computeDiscountCents")
    assert any(
        d["path"] == "web/pricing.ts" and d["kind"] == "function" and d["line"] == 11
        for d in ts_fn["definitions"]
    )
    ref_kinds = {(r["path"], r["kind"], r["line"]) for r in ts_fn["references"]}
    assert ("web/checkout.ts", "import", 1) in ref_kinds
    assert ("web/checkout.ts", "call", 6) in ref_kinds

    ts_method = index.search_symbol("CheckoutController.settleOrder")
    assert any(
        d["path"] == "web/checkout.ts" and d["kind"] == "method" and d["line"] == 4
        for d in ts_method["definitions"]
    )
    ts_iface = index.search_symbol("LineItem")
    assert any(d["path"] == "web/pricing.ts" and d["kind"] == "interface" and d["line"] == 2 for d in ts_iface["definitions"])

    go_struct = index.search_symbol("Invoice")
    assert any(d["path"] == "svc/ledger.go" and d["kind"] == "struct" and d["line"] == 11 for d in go_struct["definitions"])
    go_method = index.search_symbol("Invoice.FinalTotal")
    assert any(
        d["path"] == "svc/ledger.go" and d["kind"] == "method" and d["line"] == 19
        for d in go_method["definitions"]
    )
    go_fn = index.search_symbol("NormalizeCents")
    assert any(d["path"] == "svc/ledger.go" and d["kind"] == "function" and d["line"] == 15 for d in go_fn["definitions"])
    assert any(r["path"] == "svc/ledger.go" and r["kind"] == "call" and r["line"] == 21 for r in go_fn["references"])

    # 验证 Rust 与 Java 结构化符号提取
    (ws / "svc" / "engine.rs").write_text(
        "use crate::pricing::Coupon;\n\n"
        "pub struct TaxRule {\n    pub bps: u32,\n}\n\n"
        "impl TaxRule {\n    pub fn apply_bps(&self, cents: u64) -> u64 {\n        cents\n    }\n}\n",
        encoding="utf-8",
    )
    (ws / "svc" / "BillingService.java").write_text(
        "import com.shop.Ledger;\n\n"
        "public class BillingService {\n"
        "    public int settleInvoice(int amount) {\n        return Ledger.record(amount);\n    }\n}\n",
        encoding="utf-8",
    )
    rs_method = index.search_symbol("TaxRule.apply_bps")
    assert any(d["path"] == "svc/engine.rs" and d["kind"] == "method" and d["line"] == 8 for d in rs_method["definitions"])
    java_method = index.search_symbol("BillingService.settleInvoice")
    assert any(
        d["path"] == "svc/BillingService.java" and d["kind"] == "method" and d["line"] == 4
        for d in java_method["definitions"]
    )


def test_dependency_scores_dampen_and_skip_ubiquitous_symbol_names(tmp_path):
    ws = tmp_path / "large_repo"
    ws.mkdir()
    # 10 个文件定义 medium_hook（>8 且 <=16：阻尼按 weight / len(targets)），20 个文件定义 ubiquitous_validate（>16：直接跳过）
    for i in range(20):
        extra = "\ndef medium_hook():\n    return 1\n" if i < 10 else ""
        (ws / f"mod_{i:02d}.py").write_text(
            f"def ubiquitous_validate():\n    return {i}\n{extra}",
            encoding="utf-8",
        )
    (ws / "caller.py").write_text(
        "def entry():\n    ubiquitous_validate()\n    medium_hook()\n",
        encoding="utf-8",
    )
    index = RepoMapIndex(ws)
    index.refresh()
    scores = index._dependency_scores()
    # mod_15 仅定义 ubiquitous_validate（20 处 > 16 被跳过），只拿基础非测试分 1.0 + 1.5 = 2.5
    assert scores["mod_15.py"] == 2.5
    # mod_00 同时定义 medium_hook（10 处 > 8 阻尼加分 2.5 / 10 = 0.25），总分 2.75
    assert abs(scores["mod_00.py"] - 2.75) < 1e-9
