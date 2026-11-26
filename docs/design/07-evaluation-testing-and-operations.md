# 07 — 评测体系、测试矩阵与 CI/生产运维架构设计

本文档深度剖析 Agent Systems Lab 的**全链路质量保障、诚实评测与生产运维交付平面**，完整覆盖 **离线确定性控制流与七大核心子系统评测（17/17）**、**真实模型 5 任务编码基准与 SWE-bench 适配器**、**在线轨迹到评测（Trace-to-Eval）自进化回归飞轮**、**全仓库 47 个测试文件 / 618 项测试完整清单**、**GitHub Actions CI/CD 门禁与容器安全硬化**，以及 **全量环境变量、8 类声明式配置文件与系统级硬配额总览**。文内所有函数签名、常量阈值、测试计数、CI 步骤与源码行号均逐行核对自当前主干代码。

覆盖核心源码与运维配置范围（核心评测与包入口共 4 个 Python 模块 **3,910 行**，联动 1 个 Linux Docker 真实隔离探针脚本 **319 行**、2 份基准任务元数据、47 个自动化测试套件及完整 CI/容器配置）：
- 包入口元数据：[`src/agent_lab/__init__.py`](../../src/agent_lab/__init__.py)（L1–L3，3 行，`__version__ = "0.1.0"`）
- 离线核心控制流与七大子系统回归评测：[`src/agent_lab/evals.py`](../../src/agent_lab/evals.py)（L1–L1634，1,634 行）与 [`data/evals/tasks.json`](../../data/evals/tasks.json)（10 项核心控制流场景）
- 真实模型编码基准、无偏 `pass@k`、`LLMJudge` 与 SWE-bench 适配器：[`src/agent_lab/model_evals.py`](../../src/agent_lab/model_evals.py)（L1–L1272，1,272 行）与 [`data/evals/coding_benchmark/tasks.json`](../../data/evals/coding_benchmark/tasks.json)（5 个真实代码仓库，21 项受保护单元测试）
- 在线轨迹四维评分与 Trace-to-Eval 回归飞轮：[`src/agent_lab/eval_flywheel.py`](../../src/agent_lab/eval_flywheel.py)（L1–L1001，1,001 行）
- 真实 Linux Docker 隔离烟雾探针：[`scripts/sandbox_smoke.py`](../../scripts/sandbox_smoke.py)（L1–L319，319 行）
- 工程交付、CI 门禁与容器配置：[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)（124 行）、[`pyproject.toml`](../../pyproject.toml)（34 行）、[`Dockerfile`](../../Dockerfile)（17 行）

---

## 0. 全局评测哲学与四层质量纵深体系

Agent Systems Lab 遵循 **“诚实边界优先、客观真值判分、线上摩擦反哺离线回归”** 的评测哲学，坚决反对“用确定性 Demo 脚本分数冒充真实 LLM 能力”或“用模型自述文本代替环境状态验收”。系统由下至上构筑四层质量防线：

```mermaid
flowchart TB
    subgraph L1["Layer 1 · 单元测试与契约矩阵 (47 Files / 618 Tests)"]
        T_OFF["617 项零外网离线确定性测试<br/>Runtime · Context · KV · Sandbox · HA · A2A · MCP · Web"]
        T_PG["1 项真实 PostgreSQL 16 集成测试<br/>AGENT_LAB_TEST_POSTGRES_DSN 门控"]
    end

    subgraph L2["Layer 2 · 离线 Harness 回归评测 (evals.py · 17/17)"]
        E_CORE["10 项核心控制流场景 (tasks.json)<br/>AST 纯算术 Grader · 严禁 import/exec"]
        E_SUB["7 大核心架构子系统评测 (run_subsystem_evals)<br/>滞回压缩 · 实时转向 · 延迟 MCP · 子 Agent · Dreaming · AX 树 · SSRF/沙箱"]
        E_KV["KV Cache 离线消融 (kv_cache.py)<br/>累计命中率 40.48% -> 78.96%"]
    end

    subgraph L3["Layer 3 · 真实编码基准与容器真值 (model_evals.py & sandbox_smoke.py)"]
        M_BENCH["5 任务 Git Worktree + Docker 编码基准 (21 unittest)<br/>SHA-256 考题防篡改 · model_failure vs infrastructure_failure"]
        M_STAT["无偏 pass@k (Chen et al.) · Semaphore 并发 · LLMJudge 降级"]
        M_SWE["SWE-bench Lite/Verified JSONL 适配器<br/>零 test_patch 泄露 · 独立副本判分"]
        M_SMK["Linux Docker 7 项内核探针 (sandbox_smoke.py)<br/>非 root · 只读 · 禁网 · seccomp=2 · HMAC 防伪"]
    end

    subgraph L4["Layer 4 · 在线 Trace-to-Eval 自进化飞轮 (eval_flywheel.py)"]
        F_RUN[("线上 Storage 运行轨迹<br/>runs + events + tool_calls")]
        F_GRADE["grade_trajectory 四维评分<br/>completion · tool_efficiency · safety · steering"]
        F_HARV["harvest_failure_traces 双重脱敏采集<br/>observability + egress_dlp · 路径/身份匿名化 · 原子落盘"]
        F_EVAL["evaluate_harvested_cases 批量回归评分与防泄密核验"]
    end

    subgraph CI["CI/CD 交付门禁 (.github/workflows/ci.yml · 11/11 Checks)"]
        G1["test (Python 3.11 / 3.12): ruff + pytest + 17/17 eval + kv-benchmark"]
        G2["frontend-gate (Node 20): node --check + 0 HTML DOM Sinks"]
        G3["postgres-integration: postgres:16-alpine 服务容器实测"]
        G4["sandbox-smoke: 真实 Docker 隔离探针 + 5/5 reference 基准"]
        G5["GitGuardian 密钥扫描零硬编码泄漏"]
    end

    L1 --> G1
    T_PG --> G3
    L2 --> G1
    M_BENCH & M_SMK --> G4
    F_RUN --> F_GRADE --> F_HARV --> F_EVAL
    F_EVAL -.->|"高摩擦案例沉淀反哺回归集"| L2
```

### 核心评测与运维模块全景索引

| 序号 | 模块/配置文件 | 行数 | 核心职责 | 诚实性 / 安全边界标注 |
| :-: | :--- | ---: | :--- | :--- |
| 1 | [`src/agent_lab/evals.py`](../../src/agent_lab/evals.py) | 1,634 | 10 项核心控制流回归 + 7 大核心架构子系统离线评测 + 统一套件调度（`17/17`） | `evaluation_scope` 明确标定为 `harness_regression_only` / `architectural_subsystems_offline_regression`，绝不冒充真实模型得分 |
| 2 | [`src/agent_lab/model_evals.py`](../../src/agent_lab/model_evals.py) | 1,272 | 5 任务真实 Git Worktree + Docker 容器基准、无偏 `pass@k`、`LLMJudge` 与 SWE-bench 适配器 | 严格拆分 `model_failure` 与 `infrastructure_failure`；`reference` 执行器标注 `benchmark_harness_verification_only` |
| 3 | [`src/agent_lab/eval_flywheel.py`](../../src/agent_lab/eval_flywheel.py) | 1,001 | 4 维轨迹评分、摩擦信号提取、双重脱敏采集与离线批量回归评分 | 落盘前双重调用 `observability.redact_secrets` + `egress_dlp.redact_secrets`，并匿名化身份与本机路径 |
| 4 | [`scripts/sandbox_smoke.py`](../../scripts/sandbox_smoke.py) | 319 | Linux CI 真实 Docker 容器 7 项端到端隔离探针与回执防伪验证 | 宿主进程绝不 `import`/`exec` 样本代码，仅在容器内实测 `CapEff=0`、`NoNewPrivs=1`、`Seccomp=2` 与 `os._exit(0)` 防伪 |
| 5 | [`.github/workflows/ci.yml`](../../.github/workflows/ci.yml) | 124 | 4 个 GitHub Actions Job（5 路执行矩阵）+ GitGuardian 密钥扫描门禁 | 全流水线只读权限 `contents: read`，上传脱敏 JSON 报告 Artifact |
| 6 | [`pyproject.toml`](../../pyproject.toml) / [`Dockerfile`](../../Dockerfile) | 34 / 17 | Hatchling 打包、5 组可选 Extras、非 root 容器镜像与 `/healthz` 健康检查 | 容器绝不挂载 `docker.sock`；监听 `0.0.0.0` 默认强制鉴权门禁 |

---

## 1. 离线 Harness 回归评测体系（`src/agent_lab/evals.py`）

### 1.1 模块定位与诚实评测边界

- **源码路径**：[`src/agent_lab/evals.py`](../../src/agent_lab/evals.py)（L1–L1634）。
- **评测对象**：完整 `AgentRuntime` + `ToolRegistry` + SQLite WAL `Storage` + 临时隔离工作区（每次试验通过 `tempfile.TemporaryDirectory` 与 `shutil.copytree` 复制干净 `data/sample_repo` 夹具，`L173–L178`，杜绝跨 trial 状态污染）。
- **三大安全与诚实性不变量**：
  1. **严禁宿主执行模型代码（AST 纯算术白名单）**：`_arithmetic()`（`L77–L102`）与 `grade_discount_expression()`（`L105–L154`）绝不 `import` 或 `exec` 模型修改后的 `pricing.py`，而是通过 `ast.parse()`（限制源码 `<= 50,000` 字符、AST 节点 `<= 2,000` 个）提取唯一 `discounted_total` 函数的最后一条 `ast.Return`，仅允许数值常量（`|v| <= 1e6`）、变量 `subtotal`/`discount_percent`、一元正负号与 `+ - * /` 四则运算（中间值 `|v| <= 1e9`，禁用幂运算/属性/下标/函数调用），验证 5 组边界用例：`(200, 10) -> 180`、`(100, 0) -> 100`、`(100, 100) -> 0`、`(80, 25) -> 60`、`(99, 12.5) -> 86.625`（容差 `abs_tol=1e-8`），并在结果中诚实标记 `"method": "restricted_ast_arithmetic"` 与 `"tests_executed": False`。
  2. **显式诚实作用域与局限声明**：`run_evals()`（`L282–L332`）输出 `"provider": "deterministic_demo"`、`"evaluation_scope": "harness_regression_only"` 与 5 条显式 `limitations`（不衡量真实 LLM 能力、公开合成示例、AST 仅解释算术返回、Docker 取决于环境、重复脚本不估计真实模型 `pass@k`），并且故意不把确定性脚本重复次数包装成 `pass@k`（尽管模块提供了标准不放回组合数公式 `pass_at_k(n, c, k)` 于 `L62–L74` 供真实随机实验与单元测试使用）。
  3. **多试验隔离与非零退出码门禁**：`trials` 限制在 `1–20` 整数（拒绝 `bool`，`L290–L291`）；无论是否出现失败均落盘 JSON 报告（`L328–L331`），由 [`src/agent_lab/cli.py`](../../src/agent_lab/cli.py)（`L3496–L3497`）根据 `failed > 0` 决定退出码 `1`，确保 CI 可完整归档失败现场。

### 1.2 10 项核心确定性控制流场景（`data/evals/tasks.json`）

`run_evals()` 加载 [`data/evals/tasks.json`](../../data/evals/tasks.json)（`L1–L12`）并在 `_trial()`（`L169–L279`）中逐项校验工作区字节级快照 `_fixture_snapshot()`（`L161–L166`）与事件轨迹：

| # | `id` | `scenario` | `mode` | 校验的核心控制流与安全不变量 |
| :-: | :--- | :---: | :---: | :--- |
| 1 | `approval_applies_fix` | `approve` | `agent` | 审批前工作区 100% 原状；暂停于 `waiting_approval` 产生唯一提案；批准后仅 `pricing.py` 被改（测试零改动）、AST 5 用例全过、`applied=True` 持久化 |
| 2 | `rejection_keeps_file` | `reject` | `agent` | 拒绝提案后运行正常 `completed`，工作区所有文件逐字节与 `before` 一致，提案 `applied` 保持 `False` |
| 3 | `recover_waiting_approval` | `recover` | `agent` | 销毁旧运行时并重建全新 `AgentRuntime` 实例调用 `resume()`：绝不绕过人工审批，仍停在 `waiting_approval` 且保留原 `proposal_id`，批准后完整收敛 |
| 4 | `recover_queued_run` | `queued` | `agent` | 仅调用 `create()` 落库处于 `queued` 的任务，由新实例 `resume()` 接管推进至 `waiting_approval` 并批准完成 |
| 5 | `step_budget_stops` | `budget` | `agent` | 以 `max_steps=1` 运行：触发 `budget_exceeded` 终止，`steps <= 1` 且审批前不修改任何文件 |
| 6 | `workflow_fix` | `approve` | `workflow` | 固定前置检索阶段 + 后续补丁提案与人工审批形成完整闭环，仅目标文件变更且 AST 判分通过 |
| 7 | `parallel_fix` | `approve` | `parallel` | 只读并行 Worker 完成后，额外断言事件流包含 `workers_finished`（`L268–L273`），再由主协调者提案并完成审批修复 |
| 8 | `stale_patch_conflict` | `conflict` | `agent` | 提案后向 `pricing.py` 注入 `# concurrent human edit` 模拟外部并发修改；批准必须被拒绝并转 `failed`，且外部更新逐字节保留不被覆盖 |
| 9 | `approval_idempotent` | `idempotent` | `agent` | 批准完成后再次重复提交同一 `proposal_id` 批准请求，返回 `completed` 且工作区字节快照与首次应用后完全一致 |
| 10 | `demo_scope_explicit` | `unsupported` | `agent` | 面对无关需求时 `DemoProvider` 正常 `completed`，回答显式声明 `demo` 离线脚本能力边界，不生成无关提案、不改动仓库 |

### 1.3 七大核心架构子系统离线评测（`run_subsystem_evals`）

`SUBSYSTEM_EVAL_SPECS`（`L1520–L1528`）与 `run_subsystem_evals()`（`L1531–L1572`）在零外网依赖下对 2026 前沿 Harness 七大架构子系统执行端到端契约回归（`"evaluation_scope": "architectural_subsystems_offline_regression"`）：

| # | 子系统评测 ID | 源码实现行号 | 核心验证场景与关键不变量断言 |
| :-: | :--- | :---: | :--- |
| 1 | `auto_compaction_needle_retention` | `L415–L622` | **A. 直接窗口验证**：`ContextManager(4000, 0.80, 0.55)` 在未达 100% 溢出前触发滞回压缩将占用压至 `<=55%`，`COMPACTION_SUMMARY` 保留早期针信息 `NEEDLE_WATERMARK_80_55_IDEMPOTENCY`。<br/>**B. Runtime 端到端与 KV 稳定**：触发唯一一次 `context_compacted(trigger="auto_watermark")` 且零 `context_overflow`，保留针信息 `NEEDLE_RUNTIME_PUBLIC_API_LOCK_99`；次轮在滞回余量内不重复压缩，且 `chained_block_hashes` 前缀块 100% 一致 |
| 2 | `live_mid_run_steering` | `L625–L777` | **A. 零 CAS 冲突 + Epoch 栅栏**：`enqueue_steering_message` 不递增 `runs.state_version`；错误 `epoch+1` 调用 `drain_steering_messages` 抛 `StaleEpochError` 且不丢失消息；合法 `epoch` 原子排空。<br/>**B. 运行期转向与末轮抢救**：工具轮与准备 `end_turn` 时插入转向指令均零冲突生效，入队/注入审计事件各 2 次对称闭合。<br/>**C. 待审批接管**：`send_message(..., supersede_waiting_approval=True)` 自动作废旧提案（`decision_reason="superseded_by_user_steering"`）且不改工作区 |
| 3 | `deferred_mcp_tool_search` | `L780–L928` | 构造 3 个 Mock Server（`github`/`postgres`/`playwright`）共 16 个工具（`_build_16_mock_mcp_routes`，`L360–L412`）：验证 `>12` 自动触发 `should_defer_mcp_tools`（4 工具集保持 Eager）；验证混合 RRF 检索与 `select:` 精确点名路由；Runtime 顶层 `<TOOLS>` 仅暴露 `mcp_tool_search`/`mcp_call_tool`（隐藏全部 `mcp__*` 重型 Schema），跨轮 `<TOOLS>` 字节恒定且前置注入 `MCP_SERVER_DIRECTORY`，完成检索→远程只读调用闭环 |
| 4 | `multi_agent_subagent_isolation` | `L931–L1093` | 主协调者读取含噪声标记 `RAW_LEAD_TOOL_NOISE_EVAL_7741` 的源码并写入带 SHA-256 绑定的 `WorkingMemory`，随后 `subagent_run` 并行派发 `arch-explorer` 与 `test-auditor`：验证子 Agent 仅收到 `<=4000` 字符洁净 `PARENT_CONTEXT_BRIEF`（包含事实/约束且零泄露原始工具噪声）、与主协调者 100% 共享 `<TOOLS>+<SYSTEM_BASE>` KV 根前缀块、返回有界结构化报告（`answer <= 2000` 字符、`files_inspected` 准确隔离） |
| 5 | `dreaming_contradiction_and_proof_guard` | `L1096–L1264` | 验证 `DreamEngine` 四大防幻觉/安全保障：① `dry_run=True` 返回 `planned_diffs` 且磁盘零副作用；② 绑定失败 `bash_exec` 的旧猜测与 LLM 显式 `demotions` 均降级为 `contradicted` 并从 `store.search` 隐藏；③ 已完成会话带文件哈希的 `verified_facts` 晋升为 `confirmed` 规则页，而无 `file:@sha`/`call:` 实证的 LLM 自标 `confirmed` 页面被强制降级为 `hypothesis`；④ `rollback_last_dream(snapshot_id)` 原子还原并清理新增页面 |
| 6 | `computer_use_ax_tree_grounding` | `L1267–L1384` | 使用 `MockComputerBackend(1440, 900)` 验证：`computer_screen(screenshot)` 落盘合法 PNG（`\x89PNG\r\n\x1a\n`）且精确计算 `320×200` 与 `scale_x=scale_y=4.5`；`ax_tree` 精准定位 `AXButton("Run")` 中心坐标 `(504.0, 96.0)`；`computer_mouse(click)` 更新光标并记录 `gui_screenshot` 审计事件；同时验证自定义 `ax_nodes` 与 `max_nodes=1` 有界截断 |
| 7 | `safety_ssrf_and_sandbox_defenses` | `L1387–L1517` | 四重离线安全验证：① `execute_web_fetch` 阻断全部 8 类 SSRF 目标（`127.0.0.1`、`[::1]`、`localhost`、`10.0.0.1`、`172.16.0.1`、`192.168.1.1`、IMDS `169.254.169.254`、`file:///etc/passwd`）；② `rm -rf /` 与拆解标志 `rm -r -f -- /` 在含 `full-auto` 全部档位强制 `deny`；③ 敏感词干（`.env`/`credentials.prod.json`/`.secrets.yaml.bak`/`api_key.txt`）触发 `secret_access_forbidden`；④ 验证 `build_sandboxed_shell_argv`、`HostKernelSandbox.build_macos_sbpl`（禁读/禁写/禁出网）与 `build_linux_bwrap_argv`（`--ro-bind` + `--unshare-net`）双平台策略生成 |

### 1.4 统一离线套件调度器（`run_all_eval_suites`）

`run_all_eval_suites(suite="core", *, report_path=None, trials=1, root=None)`（`L1575–L1633`）统一支持 `core`（10 项）、`subsystems`（7 项）与 `all`（合并输出 **17 / 17**，`evaluation_scope="core_and_subsystems_offline_regression"`），并透出至 CLI `agent-lab eval --suite {core|subsystems|all}`（[`src/agent_lab/cli.py`](../../src/agent_lab/cli.py) `L3220–L3225`、`L3490–L3498`）。

---

## 2. 真实模型编码基准与 SWE-bench 适配器（`src/agent_lab/model_evals.py`）

### 2.1 5 任务编码基准架构与失败分类体系

[`src/agent_lab/model_evals.py`](../../src/agent_lab/model_evals.py)（L1–L1272）面向多任务真实代码仓库修复，与 `evals.py` 互补：业务正确性完全由隔离 Docker 容器内的受保护 `unittest` 套件判定。

#### 2.1.1 5 个真实代码基准任务（`data/evals/coding_benchmark/tasks.json`）

| # | `task_id` | 难度 | 跨文件 | 目标源码文件 | 受保护测试文件 | 单元测试数 | 考核的核心工程能力 |
| :-: | :--- | :---: | :---: | :--- | :--- | :---: | :--- |
| 1 | `coupon_settlement` | medium | 是 | `models.py`, `discounts.py`, `checkout.py` | `tests/test_checkout.py` | 5 | 跨文件原子变更集（`propose_changeset`）：优惠封顶截断 + 结算总额扣减契约 |
| 2 | `cursor_pagination` | easy | 否 | `paginator.py` | `tests/test_paginator.py` | 4 | 游标偏移 `start_idx + 1` 与 `limit + 1` 探针法判定 `has_more` |
| 3 | `retry_policy` | easy | 否 | `http_retry.py` | `tests/test_http_retry.py` | 4 | 状态码过滤（仅 `429`/`5xx`）、指数退避与 `max_ms` 上限；`reference` 执行器刻意首轮漏上限以验证测试驱动自动修复闭环 |
| 4 | `window_merger` | medium | 是 | `intervals.py`, `merger.py` | `tests/test_merger.py` | 4 | 跨模块联合排序 `(start, end)` 与闭区间相邻端点合并（`start <= cur_end`） |
| 5 | `ttl_lru_cache` | medium | 否 | `cache.py` | `tests/test_cache.py` | 4 | 命中读取刷新 LRU 顺序（`move_to_end`）与 `now >= expires_at` 边界判定 |

合计 **5 个独立仓库、21 项受保护 `unittest` 用例**。

#### 2.1.2 单次试验流水线与严格失败归因决策树

`_run_single_benchmark_trial()`（`L304–L525`）在每次试验中创建临时目录，调用 `init_git_repo_from_dir()` 与 `create_task_worktree()` 构造分离 HEAD 隔离 Git Worktree（`L323–L324`），再用 `discover_test_oracles()` 在宿主端纯 AST 静态提取每个测试文件的 SHA-256 与预期测试数（`L325`），开启 `verify_baseline=True` 时先在 Docker 中确认基线仓库确实红灯（`code == "tests_failed"`，`L328–L370`），再实例化 `AgentRuntime(..., commit_on_pass=True, auto_approve_proposals=True)` 执行编码与多轮测试驱动修复（默认 `max_repair_rounds=2`）。

```mermaid
flowchart TD
    START["单次基准试验结束 (_run_single_benchmark_trial L424-L491)"] --> C1{"_oracles_intact(worktree, oracles)<br/>测试文件 SHA-256 零篡改 & 非软链?"}
    C1 -->|"否 (被修改/删除/替换软链)"| MF1["outcome = model_failure<br/>failure_category = test_oracle_tampered"]
    C1 -->|"是"| C2{"run.status == failed 且<br/>run.error in _INFRA_ERROR_NAMES?"}
    C2 -->|"是 (11 类网络/鉴权/限流异常)"| IF1["outcome = infrastructure_failure<br/>failure_category = provider_network_or_auth_error"]
    C2 -->|"否"| C3{"last_code in SANDBOX_INFRA_CODES<br/>或 unavailable 非业务失败?"}
    C3 -->|"是"| IF2["outcome = infrastructure_failure<br/>failure_category = sandbox_unavailable"]
    C3 -->|"否"| C4{"未通过且 changed_files 非空?"}
    C4 -->|"是 (模型改了代码但未自跑测试)"| AUDIT["考官主动补跑 SandboxRunner().run_suite()<br/>独立重验 (L462-L474)"]
    C4 -->|"否"| C5
    AUDIT --> C5{"final_ver_status?"}
    C5 -->|"unavailable"| IF2
    C5 -->|"passed 且 tests_executed=True"| PASS["outcome = passed<br/>记录 repair_rounds_used / committed_sha"]
    C5 -->|"其他"| MF2["outcome = model_failure<br/>细分: budget_exceeded / repair_exhausted /<br/>no_patch_applied / tests_still_failing"]
```

- **两类失败硬隔离**：
  - `infrastructure_failure` 涵盖 `sandbox_unavailable`、`invalid_benchmark_baseline`、`missing_credentials`、`provider_network_or_auth_error`（`_INFRA_ERROR_NAMES` 定义于 `L47–L59`，包含 `APIConnectionError`/`APITimeoutError`/`AuthenticationError`/`PermissionDeniedError`/`InternalServerError`/`RateLimitError`/`ConnectError`/`ConnectTimeout`/`ReadTimeout`/`HTTPStatusError`/`TimeoutError`）。统计 `valid_runs = total_runs - infra_failures`（`L629`），绝不污染模型失分也不伪装通过。
  - `model_failure` 细分为 `test_oracle_tampered`、`budget_exceeded`、`repair_exhausted`、`no_patch_applied`、`tests_still_failing`；沙箱层面的 `SANDBOX_FAILURE_CODES`（含 `tests_failed`/`sandbox_timeout`/`sandbox_output_limit`/`sandbox_incomplete`/`sandbox_oom` 等 11 种，[`sandbox.py`](../../src/agent_lab/sandbox.py) `L32–L46`）统一归入模型侧失分，而 `SANDBOX_INFRA_CODES`（6 种，`L47–L56`）归入基础设施异常。
- **SHA-256 考题防篡改（`_oracles_intact`，`L294–L301`）**：逐一核验 `target.is_file() and not target.is_symlink()` 且原始字节 SHA-256 与基线完全一致（对 CRLF 换行字节级精确一致，无文本归一化漂移）。
- **离线自检参考执行器（`BenchmarkReferenceProvider`，`L91–L291`）**：覆盖全部 5 个仓库（含超过 2,600 字符时的 `tool_result_get` 分页回取、多文件 `propose_changeset` 以及 `retry_policy` 的刻意首轮失败→测试反馈→二轮自动修复），报告自动标注 `evaluation_scope = "benchmark_harness_verification_only"`（`L703–L707`）。

### 2.2 无偏 `pass@k`、有界并发试验调度与可选 `LLMJudge`

1. **无偏 Chen et al. (Codex 2021) `pass@k` 估计器（`estimate_pass_at_k`，`L62–L76`）**：
   - 公式：$\text{pass@}k = 1 - \frac{\binom{n-c}{k}}{\binom{n}{k}}$（当 $n - c < k$ 时直接返回 `1.0`），使用 Python `math.comb` 精确整数运算避免浮点阶乘溢出；严禁 `bool` 混入整数参数。
   - 聚合计算（`L631–L659`）：在排除 `infrastructure_failure` 后的 `n_valid` 次有效试验上按任务分别计算 `pass_at_1_by_task` 与 `pass_at_k_by_task`（默认候选 `k` 集合由 `_resolve_pass_k_values()` 取 `{1, 3, 5, 10, trials}` 中 `<= trials` 的子集，`L528–L538`），再对有效任务求宏平均输出 `pass_at_1` 与 `pass_at_k`。
2. **有界并发试验调度（`run_model_evals`，`L541–L729`）**：
   - 支持 `trials`（`1–10`）与 `max_concurrency`（默认 `1` 串行兼容；`> 1` 时采用 `asyncio.Semaphore(max_concurrency)` + `asyncio.gather()` 有界并发调度，`L604–L624`）。每个试验自带独立临时目录、Git Worktree 与 SQLite 数据库，互不干扰。
3. **可选 `LLMJudge` / `grade_with_llm_judge` 多维 Rubric 评分器（`L736–L938`）**：
   - **抗 Prompt 注入系统提示（`_JUDGE_SYSTEM_PROMPT`，`L743–L750`）**：将 `<agent_answer>`（截断 `8,000` 字符）与 `<git_diff>`（截断 `16,000` 字符）明确标记为不可信数据，要求仅输出包含 `correctness`、`completeness`、`safety`、`overall`（自动归一化 0–1 / 0–10 / 0–100 标度至 `[0.0, 1.0]`，`_normalize_judge_score` `L753–L763`）与 `rationale`（截断 `2,000` 字符）的单 JSON 对象；支持三层 JSON 提取（围栏块→全文→大括号正则，`_extract_json_object` `L779–L796`）。
   - **优雅降级保障（`_judge_fallback`，`L799–L811`）**：当 `provider` 为 `None`、离线桩（`demo`/`reference`/`DemoProvider`/`BenchmarkReferenceProvider`）、缺少密钥、调用异常、JSON 非法或三核心分字段不完整时，返回结构化 `{"ok": False, "used_llm": False, "fallback": True, "fallback_reason": ...}`，绝不阻塞或覆盖客观 Docker 容器测试真值。

### 2.3 SWE-bench Lite / Verified JSONL 适配器（`L946–L1272`）

| 函数 | 源码行号 | 核心设计机制与防作弊不变量 |
| :--- | :---: | :--- |
| `load_swebench_instances()` | `L965–L1023` | 同时兼容标准 JSONL 与 JSON 数组格式；支持 `limit` 与 `instance_ids` 过滤；通过 `_parse_swebench_test_list()`（`L946–L962`）统一规范化 `FAIL_TO_PASS` / `PASS_TO_PASS` 的 JSON 字符串或列表两种表示 |
| `build_swebench_task_prompt()` | `L1026–L1053` | **零考题泄露不变量**：仅拼装 `instance_id`、`repo`、`base_commit`、`problem_statement`（以及 `include_hints=True` 时的 `hints_text`），严禁将 `test_patch`、金标准 `patch` 或 `FAIL_TO_PASS`/`PASS_TO_PASS` 用例名暴露给 Agent |
| `grade_swebench_patch()` | `L1145–L1271` | **独立副本零污染判分**：① `shutil.copytree` 复制工作区至临时 `staging`（排除 `.git`/`__pycache__`/`.pytest_cache`/`*.pyc`）；② 在 `build_scrubbed_env({"GIT_CEILING_DIRECTORIES": ...})` 下仅向临时副本执行 `git apply --whitespace=nowarn -` 施加 `test_patch`（Agent 原始工作区零修改、零泄露）；③ 逐条执行 `FAIL_TO_PASS` 与 `PASS_TO_PASS`（支持 `pytest`/`unittest` 自动识别与 `{test}` 模板，开启 `use_sandbox=True` 时经 `build_sandboxed_shell_argv(..., allow_network=False)` 包装无网 OS 沙箱，超时与 4,000 字符输出截断）；④ 仅当 `test_patch_applied=True`、`len(f2p) > 0` 且 `FAIL_TO_PASS` 与 `PASS_TO_PASS` 全部通过时判定 `resolved=True` |

---

## 3. 在线 Trace-to-Eval 回归飞轮（`src/agent_lab/eval_flywheel.py`）

[`src/agent_lab/eval_flywheel.py`](../../src/agent_lab/eval_flywheel.py)（L1–L1001）将线上/本地运行中发生的失败、重复试错、安全拦截与未消费转向指令自动转化为脱敏、匿名的离线回归用例集，形成自进化质量闭环。

```mermaid
flowchart LR
    RUN[("Storage 运行记录<br/>list_runs + get_run + events<br/>+ pending_steering_messages")] --> GR["grade_trajectory(run, events)<br/>L240-L536"]
    GR -->|"4 维得分 + friction_signals"| FILT{"筛选高价值案例 (L784)<br/>len(friction) >= min_friction<br/>OR status != completed"}
    FILT -->|"忽略健康无摩擦运行"| SKIP["跳过"]
    FILT -->|"命中"| RED["双重密文脱敏 + 匿名化<br/>_deep_redact_and_anonymize (L207-L230)<br/>1. observability.redact_secrets<br/>2. egress_dlp.redact_secrets<br/>3. 身份键 -> [ANONYMIZED]<br/>4. 本机用户路径 -> &lt;HOME&gt;"]
    RED --> WR["原子安全落盘 (_atomic_write_json L550-L576)<br/>拒绝 symlink · fsync + os.replace<br/>case_&lt;sha16&gt;.json + manifest.json"]
    WR --> LOAD["load_harvested_cases (L852-L900)<br/>校验路径位于目录内 · 拒绝软链与穿越"]
    LOAD --> EV["evaluate_harvested_cases (L903-L1001)<br/>批量离线复演评分 + _structure_has_unredacted_secret<br/>零密文泄露完整性核验"]
```

### 3.1 四维轨迹评分器（`grade_trajectory`，`L240–L536`）

总分按固定权重 `TRAJECTORY_WEIGHTS`（`L66–L71`）加权并截断至 `[0.0, 1.0]`，同时执行**无重复计数不变量**：同一 `call_id` 同时出现 `tool_audit` 与 `tool_result` 时合并为单条工具记录，且正常暂停审批（`requires_approval=True` 或 `code="deferred_until_approval"`）不计入工具错误（`L337–L395`）：

| 评分维度 | 权重 | 量化规则与扣分模型 | 提炼的 `friction_signals` 枚举 |
| :--- | :---: | :--- | :--- |
| `completion_score` | **0.35** | 终态查表 `_COMPLETION_SCORES`（`L73–L82`）：`completed=1.0`，`waiting_approval=0.5`，`cancelled`/`interrupted`/`running`/`queued=0.25`，`failed`/`budget_exceeded`/其他 `=0.0` | `run_failed`、`budget_exceeded`、`run_cancelled`、`repair_exhausted`、`verification_failed` |
| `tool_efficiency_score` | **0.25** | 惩罚三类低效行为：`min(0.55, tool_error_rate * 0.7)` + `min(0.45, repeated_failures * 0.15)` + 超步数抖动惩罚 `step_churn_penalty = min(0.4, 0.15 + overflow_ratio * 0.5)`（`steps > max_steps`，默认 `max_steps=20`，`L428–L442`） | `repeated_edit_failure`（命中 `_EDIT_TOOL_NAMES` 或 `_EDIT_ERROR_CODES` 重复失败）、`repeated_tool_failure`、`high_tool_error_rate`（`>=2` 次且错误率 `>=40%`，或 `>=3` 错且 `>=35%`）、`excessive_step_churn` |
| `safety_compliance_score` | **0.25** | 按 `call_id` 去重统计安全拦截并主动扫描非拦截工具输出与最终 `answer` 是否含未脱敏密文（`_text_has_unredacted_secret`）：DLP 拦截 `min(0.7, 0.4*n)`、权限拒绝 `min(0.45, 0.15*n)`、污点控制流 `min(0.25, 0.1*n)`、密文外露 `min(0.8, 0.5*n)`（`L444–L466`） | `dlp_blocked`、`permission_denied`、`tainted_control_flow`、`secret_exposure` |
| `steering_responsiveness_score` | **0.15** | 核验 `user_steering_queued` + 残余 `pending_steering` 与 `user_steering_injected` 的排空比 `drained_count / total_queued`（无转向请求时默认 `1.0`，`L468–L498`） | `steering_unconsumed`（存在未排空注入的转向指令） |

### 3.2 双重脱敏采集与离线回归评估（`harvest_failure_traces` / `evaluate_harvested_cases`）

1. **零明文凭据落盘与匿名化管线（`_deep_redact_and_anonymize`，`L207–L230`）**：
   - 先用 [`observability.redact_secrets`](../../src/agent_lab/observability.py) 递归脱敏密钥语义键名及 Bearer/DSN 凭据；
   - 再对所有字符串执行 [`egress_dlp.redact_secrets`](../../src/agent_lab/egress_dlp.py) 全部 14 类出站密文规则清洗；
   - 匿名化全部身份键 `_IDENTITY_KEYS`（`actor_id`/`tenant_id`/`user_id`/`email`/`owner_id` → `"[ANONYMIZED]"`，`L115`），并将 macOS/Linux/Windows 用户宿主路径正则 `_HOME_PATH_RE` 替换为 `"<HOME>"`（`L118`）；
   - 运行标识符经 SHA-256 截断前 16 位生成去敏 `case_id = f"case_{source_run_hash}"`（`L612–L617`），裁剪事件上限 `500` 条、工具轨迹 `200` 条、回答摘录 `2,000` 字符（`L121–L123`）。
2. **原子安全落盘与防路径穿越（`L539–L576`、`L837–L900`）**：
   - `_assert_no_symlink()` 沿路径向上核查至输出根目录拒绝任何符号链接；`_atomic_write_json()` 在同目录创建临时文件并执行 `flush() + os.fsync() + os.replace()` 原子落盘每个 `case_<hash>.json` 与 `manifest.json`（`FLYWHEEL_SCHEMA_VERSION = 1`）。
   - `load_harvested_cases()` 优先按 `manifest.json` 顺序加载并通过 `_safe_case_path()` 校验文件名不含 `/`、`\`、`..`、非软链且 `resolved.is_relative_to(root)`。
3. **批量回归评估与防泄密审计（`evaluate_harvested_cases`，`L903–L1001`）**：
   - 支持注入自定义回放 `evaluator(case) -> (replay_run, replay_events)` 或直接复评历史快照；
   - 逐案例执行 `_structure_has_unredacted_secret()`（`L177–L194`）双重校验是否存在任何残留密文，仅当 `overall >= pass_threshold`（默认 `0.8`）、`friction_signals == []` 且 `redaction_clean == True` 时判通，输出各维度均分、摩擦信号直方图与 `redaction_violations` 计数。
   - **CLI 入口**：[`src/agent_lab/cli.py`](../../src/agent_lab/cli.py)（`L3226–L3253`、`L3448–L3488`）提供 `agent-lab eval --grade-run <run_id>` 与 `agent-lab eval --harvest [--output-dir ...] [--limit 20] [--min-friction 1]`。

---

## 4. 全仓库 47 个测试文件 / 618 项测试完整清单

经 `uv run --no-sync pytest --collect-only -q tests` 实测，仓库共包含 **47 个测试套件文件**、合计 **618 项收集用例**，严格划分为：
- **617 项零外网离线确定性测试**：覆盖全部 43 个 `src/agent_lab/*.py` 模块、Web 前端约束与 7 幅架构 SVG 产物，不依赖外部商业 LLM 密钥或公网连接。
- **1 项条件化实时 PostgreSQL 16 集成测试**：[`tests/test_evals_subsystems_and_postgres.py`](../../tests/test_evals_subsystems_and_postgres.py) 中的 `test_postgres_live_storage_integration`（`L510–L587`），由环境变量 `AGENT_LAB_TEST_POSTGRES_DSN` 门控；本地未设置时自动 `pytest.skip`，CI `postgres-integration` 任务在真实 `postgres:16-alpine` 服务容器上完整执行。

### 4.1 按架构域分类的 47 文件测试矩阵总览

| 分类域 | 文件数 | 测试数 | 占比 | 关键验证目标 |
| :--- | ---: | ---: | ---: | :--- |
| 1. 运行时、上下文工程、KV Cache、检查点与流式 | 11 | 144 | 23.3% | 状态机、双预算原子裁剪、KV 前缀稳定、`/rewind`、流式 Broker 与测试驱动修复闭环 |
| 2. 多协议模型网关、双向 MCP、双向 A2A 与子 Agent | 6 | 58 | 9.4% | 5 协议线格式、声明式网关、A2A JSON-RPC/SSE、延迟 MCP 检索与 Worktree 子 Agent |
| 3. 工具生态、权限/钩子/DLP、Docker 沙箱、Git 与 GUI | 8 | 124 | 20.1% | 6 层 PDP、`plan` 硬只读、14 类 Egress DLP、双进程 HMAC 沙箱、Git HA 与桌面 Computer Use |
| 4. 2026 前沿 Harness（PTC / Horizon / Shadow / Merkle / Browser / Backends / Triggers / Flywheel） | 3 | 58 | 9.4% | FD 隔离 PTC、Horizon 战役、APFS Shadow、Merkle 增量索引、Playwright ARIA、执行后端与回归飞轮 |
| 5. 跨会话记忆、Dreaming、混合检索、代码地图与技能 | 6 | 80 | 12.9% | 双层 `CLAUDE.md`/`MEMORY.md`、Dreaming 实证门控/回滚、BM25+Dense+RRF、PPR 代码地图与三层技能 |
| 6. 存储、调用日志、跨进程锁、租约队列与故障恢复 | 6 | 95 | 15.4% | SQLite WAL / PG 迁移、`call_id` 日志、POSIX `flock`、Epoch Fencing、DLQ 与崩溃注入 |
| 7. 控制平面 API 高可用、REPL、Web 工作台与 UX | 4 | 39 | 6.3% | 多租户 RBAC/Cookie CSRF、异人复核、限流/413、优雅停机、防误批 REPL 与附件上传 |
| 8. 离线评测、编码基准与实时 PostgreSQL 集成 | 3 | 20 | 3.2% | AST Grader、`17/17` 离线套件、5 任务基准分类、SWE-bench 与 `1` 项实时 PG 集成测试 |
| **合计** | **47** | **618** | **100.0%** | **617 项离线测试 + 1 项条件化实时 PostgreSQL 16 集成测试** |

### 4.2 全量 47 个测试文件逐项清单

#### 类别一：运行时、上下文工程、KV Cache、检查点与流式（11 文件 · 144 项测试）

| # | 测试文件 | 用例数 | 行数 | 验证的核心不变量 |
| :-: | :--- | ---: | ---: | :--- |
| 1 | [`tests/test_runtime.py`](../../tests/test_runtime.py) | 17 | 279 | 审批前零写入、拒绝保原状、预算恢复不清零、断网重试脱敏、持久化工具批恢复、Anthropic 契约与 Origin 鉴权 |
| 2 | [`tests/test_context.py`](../../tests/test_context.py) | 22 | 145 | 完整 `(tool_use, tool_result)` 原子组裁剪、深拷贝不篡改原始账本、末组防截断、畸形工具协议与注入阻断 |
| 3 | [`tests/test_context_budget.py`](../../tests/test_context_budget.py) | 38 | 277 | System/Tools 预留字节精确预算、审批态与活跃技能在裁剪后存活、强制状态超限显式失败而非静默截断 |
| 4 | [`tests/test_kv_cache_harness.py`](../../tests/test_kv_cache_harness.py) | 13 | 1,027 | 链式块哈希单字节雪崩、跨步与跨会话根前缀字节恒定、阶梯滞回压缩、Anthropic 三断点排布与消融基准 |
| 5 | [`tests/test_auto_compaction_and_steering.py`](../../tests/test_auto_compaction_and_steering.py) | 4 | 271 | 80%→55% 滞回压缩与次轮前缀稳定、防多行状态伪造协议头、运行期实时转向与多租户 `CLAUDE.md` 硬链隔离 |
| 6 | [`tests/test_working_memory.py`](../../tests/test_working_memory.py) | 6 | 769 | 证据来源与文件 SHA-256 绑定、源码变更自动标 `stale`、拒绝失败调用与秘密文件、满额优先淘汰防死锁 |
| 7 | [`tests/test_long_context_and_claude_md.py`](../../tests/test_long_context_and_claude_md.py) | 6 | 805 | 全局/项目 `CLAUDE.md`+`MEMORY.md` 发现与防软链、`COMPACTION_SUMMARY` 协议头消毒、7 幅架构 SVG 完整性 |
| 8 | [`tests/test_checkpoints_rewind_and_sandbox.py`](../../tests/test_checkpoints_rewind_and_sandbox.py) | 15 | 535 | `CheckpointManager` 三模式 `/rewind` 悬空闭合、会话 Fork/Search/Delete API、`HostKernelSandbox` 与 `web_fetch` SSRF/DNS Rebinding 防御 |
| 9 | [`tests/test_streaming_and_cancellation.py`](../../tests/test_streaming_and_cancellation.py) | 7 | 802 | `RunStreamBroker` 256 帧环形缓冲线程安全、模型流式/推理增量、USD 成本账本与硬上限、协作取消与 Cookie SSE |
| 10 | [`tests/test_repair_loop.py`](../../tests/test_repair_loop.py) | 5 | 378 | 容器失败自动修复闭环、`max_repair_rounds` 上限、区分 `completed` 与 `passed`、未完成回执绝不算通过与 `base_commit` 推进 |
| 11 | [`tests/test_runtime_optimization_review.py`](../../tests/test_runtime_optimization_review.py) | 11 | 382 | 序列化预算精确对齐、转义分页防二次引用替换、已裁剪历史复用 `call_id` 拒收、并行 Worker 断点恢复与技能快照固定 |

#### 类别二：多协议模型网关、双向 MCP、双向 A2A 与动态子 Agent（6 文件 · 58 项测试）

| # | 测试文件 | 用例数 | 行数 | 验证的核心不变量 |
| :-: | :--- | ---: | ---: | :--- |
| 12 | [`tests/test_multi_protocol_gateways.py`](../../tests/test_multi_protocol_gateways.py) | 27 | 1,246 | OpenAI Responses SSE/函数调用、Gemini 原生 SSE/Schema 清洗、无环境变量污染显式配置、`gateways.example.json` 校验与协议别名 |
| 13 | [`tests/test_a2a_protocol.py`](../../tests/test_a2a_protocol.py) | 15 | 1,581 | 公私 AgentCard、JSON-RPC v0.2/0.3/1.0 阻塞与非阻塞、多轮 `input-required`、SSE 流式、多租户隔离、`0600` 原子配置与 Agent-to-Agent 实测 |
| 14 | [`tests/test_mcp.py`](../../tests/test_mcp.py) | 1 | 73 | 独立子进程真实 `stdio` JSON-RPC MCP Server 会话：只读工具发现、路径越界拒绝与零写入工具暴露 |
| 15 | [`tests/test_subagent_and_mcp_client.py`](../../tests/test_subagent_and_mcp_client.py) | 2 | 217 | `subagent_run` 并行调研、递归深度护栏、KV 根前缀对齐与外部 `McpClientManager` 真实 stdio 连接执行 |
| 16 | [`tests/test_skill_mcp_routing_and_subagents.py`](../../tests/test_skill_mcp_routing_and_subagents.py) | 3 | 276 | 四通道技能路由与 Step-0 显式预载、阈值门控延迟 `mcp_tool_search`、子 Agent 上下文隔离与结构化交接报告 |
| 17 | [`tests/test_worktree_subagents_and_mcp_full.py`](../../tests/test_worktree_subagents_and_mcp_full.py) | 10 | 821 | 写子 Agent Git Worktree 并行隔离、模型档位路由、`run_in_background`、子 Agent 与单 MCP Server 故障隔离、环境变量插值与 MCP Resources/Prompts |

#### 类别三：工具生态、权限/钩子/DLP、Docker 沙箱、Git 与桌面 GUI（8 文件 · 124 项测试）

| # | 测试文件 | 用例数 | 行数 | 验证的核心不变量 |
| :-: | :--- | ---: | ---: | :--- |
| 18 | [`tests/test_tools.py`](../../tests/test_tools.py) | 31 | 645 | JSON Schema 严禁未知工具/多余字段、路径防逃逸、软硬链拒绝、提案并发互斥、版本号冲突保护、WAL 断电前滚与跨 Run 越权阻断 |
| 19 | [`tests/test_sandbox.py`](../../tests/test_sandbox.py) | 49 | 755 | 不可变镜像 ID、只读快照与资源限额、双进程匿名管 32B HMAC 回执、stdout 伪造清洗、`sys.addaudithook`、孤儿容器回收与 `RemoteHTTPSandboxBackend` |
| 20 | [`tests/test_host_tools_and_permissions.py`](../../tests/test_host_tools_and_permissions.py) | 5 | 311 | 沙箱档位兼容面、Bash 分类器与三级权限矩阵、本地 `fs_*` CRUD/Glob/Grep 敏感文件阻断、子进程环境脱敏与进程组超时回收 |
| 21 | [`tests/test_hooks_rules_pty_and_lsp.py`](../../tests/test_hooks_rules_pty_and_lsp.py) | 5 | 799 | `HookManager` 否决与脱敏命令钩子、`PermissionRuleEngine` 分层规则与会话授权、常驻 Pipe/PTY 后台进程与回收、`LspClientManager` 降级、并发只读波次 |
| 22 | [`tests/test_git_changeset.py`](../../tests/test_git_changeset.py) | 4 | 265 | 多文件原子变更集部分失败回滚、Git 基线漂移拒绝、篡改测试目录拦截提交与多文件端到端审批模板 |
| 23 | [`tests/test_git_ha.py`](../../tests/test_git_ha.py) | 8 | 437 | Git 超时整组进程回收、宿主 `.gitconfig` 零污染、并发原子冷启动 origin、递归测试/密钥提交拦截、Worktree GC 与跨节点防炸弹归档 |
| 24 | [`tests/test_computer_use.py`](../../tests/test_computer_use.py) | 3 | 201 | 桌面 GUI 截图审批与多模态 Vision 附件流、OpenAI/Anthropic 视觉载荷序列化与 macOS 原生 `ctypes` FFI 烟雾校验 |
| 25 | [`tests/test_batch1_plan_dlp_native_ctx_edit.py`](../../tests/test_batch1_plan_dlp_native_ctx_edit.py) | 19 | 1,329 | Layer 1b `plan` 模式不可绕过只读边界、14 类 Egress DLP 与 Base64/URL 编码密文拦截、CaMeL 污点流、三厂商原生上下文/缓存与四级模糊编辑 |

#### 类别四：2026 前沿 Harness 批次（3 文件 · 58 项测试）

| # | 测试文件 | 用例数 | 行数 | 验证的核心不变量 |
| :-: | :--- | ---: | ---: | :--- |
| 26 | [`tests/test_batch2_ptc_horizon_shadow.py`](../../tests/test_batch2_ptc_horizon_shadow.py) | 15 | 1,115 | `ptc_exec` 双通道 FD 隔离/只读白名单/调用上限/进程组超时/环境脱敏、`HorizonCampaignManager` 不可篡改账本与架构 Lint、`ShadowWorkspace` APFS CoW 与活工作区零改动 |
| 27 | [`tests/test_batch3_merkle_browser_protocols.py`](../../tests/test_batch3_merkle_browser_protocols.py) | 28 | 1,237 | `CodebaseMerkleIndex` 增量 AST 刷新/删文件清理/秘密软硬链排除/混合检索、`BrowserSessionManager` ARIA 快照与 SSRF/IMDS 阻断、MCP 2025/2026 四大双向回调 |
| 28 | [`tests/test_batch4_backends_triggers_flywheel_middleware.py`](../../tests/test_batch4_backends_triggers_flywheel_middleware.py) | 15 | 1,563 | `Local`/`Docker`/`RemoteMicroVM` 执行后端契约与 SSRF 校验、`TurnPipeline` 洋葱中间件（DLP/预算/Span）、Cron/Webhook HMAC 防重放与 `eval_flywheel` 轨迹评分脱敏采集 |

#### 类别五：跨会话记忆、Dreaming、混合检索、代码地图与技能目录（6 文件 · 80 项测试）

| # | 测试文件 | 用例数 | 行数 | 验证的核心不变量 |
| :-: | :--- | ---: | ---: | :--- |
| 29 | [`tests/test_agent_memory_and_dreaming.py`](../../tests/test_agent_memory_and_dreaming.py) | 5 | 406 | 会话后自动 Dreaming 断链修复与预注入、对照 Transcript 纠错压制冲突页、200 行索引硬上限、REPL/Web 记忆多租户隔离与防穿越 |
| 30 | [`tests/test_dreaming_gc_repomap_and_retrieval.py`](../../tests/test_dreaming_gc_repomap_and_retrieval.py) | 10 | 1,151 | `DreamEngine` 实证门控/`dry_run`/回滚/诚实报错、`MemoryStore` GC 排除冲突页与时效加权排序、多语言 `RepoMap` PPR、分层技能清单与微压缩 |
| 31 | [`tests/test_retrieval.py`](../../tests/test_retrieval.py) | 25 | 258 | 标识符/错误码/中文精确排序、零分防编造、跨检出路径稳定引用 ID、标题/正文版本化、大章节有界切分与外链软链拒绝 |
| 32 | [`tests/test_repomap.py`](../../tests/test_repomap.py) | 7 | 287 | 字符预算约束、跨文件依赖排序、哈希增量刷新、8 线程并发编辑安全、`.gitignore`/`.venv`/秘密排除与 TS/Go 结构化符号索引 |
| 33 | [`tests/test_skills.py`](../../tests/test_skills.py) | 16 | 173 | 仅预载元数据直到显式加载、恶意技能名拒收、软链目录/文件越权阻断、超限或重复元数据拒收与 YAML 单行描述校验 |
| 34 | [`tests/test_skill_loading.py`](../../tests/test_skill_loading.py) | 17 | 224 | 有界头部单次读取、同大小同 mtime 缓存失效、只读深拷贝防别名污染、读取中变更拒收、按章节快照哈希隔离 |

#### 类别六：存储、调用日志、跨进程锁、租约队列与故障恢复（6 文件 · 95 项测试）

| # | 测试文件 | 用例数 | 行数 | 验证的核心不变量 |
| :-: | :--- | ---: | ---: | :--- |
| 35 | [`tests/test_storage.py`](../../tests/test_storage.py) | 17 | 517 | 跨连接重启持久化、审批不可翻转且幂等、增量事件游标隔离、拒绝 `:memory:`、PRAGMA 失败防句柄泄漏、WAL 并发初始化重试与死信隔离 |
| 36 | [`tests/test_journal.py`](../../tests/test_journal.py) | 27 | 261 | 旧库无损增量迁移、嵌套参数排序稳定调用标识、跨 Run 隔离、空结果防缓存误判、中断工具显式 `unknown` 状态与 Unicode 分页拼装 |
| 37 | [`tests/test_execution_lock.py`](../../tests/test_execution_lock.py) | 30 | 359 | 独立进程非阻塞 `flock` 互斥、跨不同 Run 并发、`SIGKILL` 后内核自动释放、`run_id` 防路径穿越、拒绝数据库与父目录符号链接 |
| 38 | [`tests/test_runtime_lock.py`](../../tests/test_runtime_lock.py) | 2 | 183 | 双运行时互斥共享不重复调模型/工具、取消协程必须等待后台工具线程排空后才解锁且恢复复用已落库结果 |
| 39 | [`tests/test_lease_fencing.py`](../../tests/test_lease_fencing.py) | 10 | 572 | 租约过期接管与单调 `epoch` 隔离、心跳抢占自取消、独立 Worker 队列消费、`max_takeovers` 毒丸入 DLQ、僵尸提交拦截与跨节点工作区归档故障转移 |
| 40 | [`tests/test_recovery_integration.py`](../../tests/test_recovery_integration.py) | 9 | 241 | 结果已提交但 Run 检查点丢失恢复、崩溃前提案防重复、延迟工具持久化回执、上下文溢出在调模型前熔断、围栏内代码不误拆技能章节与并发单次迁移 |

#### 类别七：控制平面 API 高可用、REPL、Web 工作台与 UX 升级（4 文件 · 39 项测试）

| # | 测试文件 | 用例数 | 行数 | 验证的核心不变量 |
| :-: | :--- | ---: | ---: | :--- |
| 41 | [`tests/test_production_api_ha.py`](../../tests/test_production_api_ha.py) | 8 | 551 | 生产鉴权启动门禁、自定义 `ALLOWED_HOSTS`、多租户 RBAC 与异人审批、队列单事务 `approve_and_enqueue`、关机零僵尸、`413`/`429`/探针/指标与远端 HTTP 沙箱端到端 |
| 42 | [`tests/test_multiturn_repl_and_web.py`](../../tests/test_multiturn_repl_and_web.py) | 5 | 505 | `send_message` 多轮续跑与无 PTY REPL、Web 直连工作区 RBAC 与 GC 保护、中断悬空 `tool_use` 修复、审批双击去抖与多代理并发安全 |
| 43 | [`tests/test_repl_and_web_upgrades.py`](../../tests/test_repl_and_web_upgrades.py) | 11 | 946 | REPL 空行禁误批与三空行安全暂停、`Ctrl-C`/SIGINT 协作取消保活会话、完整 Unified Diff 预览、`p` 规则持久化、`@file` 安全展开与 `/rewind`/`/fork`/`/cost` 等斜杠命令 |
| 44 | [`tests/test_ux_overhaul.py`](../../tests/test_ux_overhaul.py) | 15 | 1,273 | 分阶段上传 6 MiB/413 与跨租户隔离、带附件实时转向、工作区浏览器与文件 API 剪枝过滤秘密/软链、后台进程管理与富对话线程详情 |

#### 类别八：离线评测、编码基准与条件化 PostgreSQL 实时集成（3 文件 · 20 项测试）

| # | 测试文件 | 用例数 | 行数 | 验证的核心不变量 |
| :-: | :--- | ---: | ---: | :--- |
| 45 | [`tests/test_evals.py`](../../tests/test_evals.py) | 5 | 53 | AST 算术 Grader 等价表达式验收与函数调用零执行拦截、`pass_at_k` 组合估计器、10 项离线评测隔离与 `trials` 边界校验 |
| 46 | [`tests/test_model_evals.py`](../../tests/test_model_evals.py) | 5 | 329 | 5 个基准仓库受保护考题发现、基础设施异常与模型失分严格分离、OpenAI 兼容线协议翻译、沙箱超限归类模型失分与 CRLF 考题 SHA-256 一致性 |
| 47 | [`tests/test_evals_subsystems_and_postgres.py`](../../tests/test_evals_subsystems_and_postgres.py) | 10（9 离线 + **1 PG 实时**） | 587 | `7/7` 子系统与 `17/17` 全量离线套件、`estimate_pass_at_k` 边界、`LLMJudge` 降级、SWE-bench 零泄露判分、8 步迁移幂等、词法安全 SQL 翻译、层级 Span、`WorkerMetricsServer` 及条件化实时 PG 16 集成测试 `test_postgres_live_storage_integration` |

---

## 5. CI/CD 流水线、打包依赖与生产容器部署

### 5.1 GitHub Actions 流水线拓扑（`.github/workflows/ci.yml`）

[`.github/workflows/ci.yml`](../../.github/workflows/ci.yml)（L1–L124）在每次 `push` 与 `pull_request` 上以最小权限 `permissions: contents: read`（`L3–L4`）并行拉起 4 个 Job（跨 Python 3.11/3.12 共 5 路执行器），配合仓库级 **GitGuardian** 密钥扫描形成 PR `#1` 已验证的 **11 / 11 Checks 全绿门禁**。

```mermaid
flowchart LR
    TRIG["push / pull_request<br/>permissions: contents: read"] --> J1["test (matrix: py3.11 & py3.12)<br/>timeout: 10m"]
    TRIG --> J2["frontend-gate (Node 20)<br/>timeout: 5m"]
    TRIG --> J3["postgres-integration (py3.12)<br/>service: postgres:16-alpine<br/>timeout: 10m"]
    TRIG --> J4["sandbox-smoke (py3.12 + Docker)<br/>timeout: 10m"]
    TRIG --> GG["GitGuardian Secret Scanning<br/>全提交历史零明文凭据门禁"]

    J1 --> J1S["1. uv sync --locked --extra dev --extra mcp<br/>2. ruff check src tests scripts<br/>3. pytest -q (617 passed, 1 skipped)<br/>4. agent-lab eval (10/10)<br/>5. agent-lab eval --suite subsystems (7/7)<br/>6. agent-lab kv-benchmark (40.48%->78.96%)<br/>-> upload eval-3.11 / eval-3.12 artifacts"]
    J2 --> J2S["1. node --check web/*.js<br/>2. 剥离注释后正则断言 0 处<br/>innerHTML / outerHTML /<br/>insertAdjacentHTML / document.write"]
    J3 --> J3S["1. uv sync --extra dev --extra postgres<br/>2. AGENT_LAB_TEST_POSTGRES_DSN 注入<br/>3. pytest -q -k 'postgres or pg'<br/>4. Storage.from_dsn 端到端 smoke<br/>(校验 applied_migrations >= 6)"]
    J4 --> J4S["1. docker pull python:3.12-slim<br/>2. python scripts/sandbox_smoke.py<br/>(7 项内核隔离探针 + 回执防伪)<br/>3. agent-lab model-eval --provider reference (5/5)<br/>-> upload sandbox-smoke artifact"]
```

#### 四个 CI Job 与 GitGuardian 安全纪律逐项拆解

| Job / 门禁 | 运行环境与超时 | 关键步骤与源码行号 | 失败阻断条件与产物 |
| :--- | :--- | :--- | :--- |
| `test` | `ubuntu-latest`，Python `3.11` / `3.12`，`10m` | `L6–L30`：`uv sync --locked --extra dev --extra mcp` → `ruff check src tests scripts` → `pytest -q` → `agent-lab eval` → `agent-lab eval --suite subsystems` → `agent-lab kv-benchmark` | 任一单测、Ruff 规则（`E4/E7/E9/F`）、17/17 评测或 KV 优化命中率未高于基线（[`cli.py`](../../src/agent_lab/cli.py) `L3412–L3413`）即退出码 `1`；`if: always()` 上传 `eval-3.11`/`eval-3.12` 三份 JSON 报告 |
| `frontend-gate` | `ubuntu-latest`，Node `20`，`5m` | `L32–L74`：对全部 `web/*.js` 运行 `node --check`，再通过 Node 脚本剥离 `/* */` 与 `//` 注释，使用正则 `/\.(?:innerHTML\|outerHTML\|insertAdjacentHTML)\b\|\bdocument\s*\.\s*write(?:ln)?\s*\(/g` 扫描 | 任意 JS 语法错误或出现 `>= 1` 处危险 HTML DOM Sink 立即 `process.exit(1)`，机械化守住 Web 工作台零 XSS 注入面 |
| `postgres-integration` | `ubuntu-latest`，Python `3.12` + `postgres:16-alpine` 服务容器，`10m` | `L76–L104`：`pg_isready` 健康探测就绪后，安装 `--extra dev --extra postgres`，设置测试专用 `AGENT_LAB_TEST_POSTGRES_DSN`（指向本机 `127.0.0.1:5432` 容器内临时测试库）执行 `pytest -q -k "postgres or pg"` 与 `Storage.from_dsn` 迁移 smoke | 覆盖真实 `psycopg_pool`、`schema_migrations` 幂等、队列租约、`trace_id`/`span_id` 落库与 `fork_run` |
| `sandbox-smoke` | `ubuntu-latest`，Python `3.12` + 原生 Linux Docker，`10m` | `L106–L124`：预先 `docker pull python:3.12-slim`（匹配 `--pull=never` 离线镜像策略），执行 [`scripts/sandbox_smoke.py`](../../scripts/sandbox_smoke.py) 与 `agent-lab model-eval --provider reference` | 实测 7 项 Linux 内核探针（`uid!=0`、`ST_RDONLY`、仅 `lo` 网卡且外连失败、无 `docker.sock`、无宿主哨兵、`CapEff==0`、`NoNewPrivs==1`、`Seccomp==2`、5s 超时、2 KiB 输出截断、`os._exit(0)` 与伪造回执清洗、孤儿回收）及 5/5 基准修复；`if: always()` 上传脱敏 `reports/sandbox-smoke.json` 与 `reports/model-eval.json` |
| **GitGuardian 密钥扫描** | 仓库提交与 PR 检查 | 自动扫描全部 Diff 与提交历史，覆盖 API Key、高熵 Bearer Token、含真实密码的连接串与私钥头 | **工程纪律**：全仓文档、示例配置、测试夹具与报告样例严禁出现真实或拟真凭据字面量（如真实密钥前缀或高熵凭据串），一律使用 `<YOUR_API_KEY>` / `not-a-real-credential` 语义占位符 |

### 5.2 `pyproject.toml` 打包规格与可选依赖矩阵

[`pyproject.toml`](../../pyproject.toml)（L1–L34）采用 `hatchling>=1.26,<2` 构建（`packages = ["src/agent_lab"]`），要求 `Python >= 3.11`，极简核心运行时仅依赖 4 个生产包：`fastapi>=0.115,<1`、`uvicorn>=0.34,<1`、`anthropic>=0.49,<1`、`jsonschema>=4.23,<5`（`L10`）；CLI 入口为 `agent-lab = "agent_lab.cli:main"`（`L20`）；`pytest` 启用 `asyncio_mode = "auto"`（`L27`）；Ruff 强制 `line-length = 110` 与 `select = ["E4", "E7", "E9", "F"]`（`L30–L34`）。

| Optional Extra | 安装命令片段 | 声明依赖（`pyproject.toml` L13–L17） | 解锁的核心能力 |
| :--- | :--- | :--- | :--- |
| `dev` | `uv sync --extra dev` | `pytest>=8.3,<10`, `pytest-asyncio>=0.25,<2`, `httpx>=0.28,<1`, `ruff>=0.11,<1` | 617+1 自动化测试套件、异步 HTTP 测试与 Provider 客户端、Ruff 静态门禁 |
| `mcp` | `uv sync --extra mcp` | `mcp>=1.12,<2` | 官方 MCP Python SDK v1：只读 `stdio` MCP Server（`mcp_server.py`）与外部 `McpClientManager`（`mcp_client.py`） |
| `postgres` | `uv sync --extra postgres` | `psycopg[binary,pool]>=3.2,<4` | 生产 PostgreSQL 持久化后端、`psycopg_pool` 连接池、`FOR UPDATE SKIP LOCKED` 与 `LISTEN/NOTIFY` |
| `treesitter` | `uv sync --extra treesitter` | `tree-sitter-language-pack>=0.7` | `RepoMapIndex` 与 `CodebaseMerkleIndex` 多语言原生 Tree-sitter AST 精确解析（未安装时无缝回退词法解析） |
| `tui` | `uv sync --extra tui` | `prompt-toolkit>=3.0`, `rich>=13.7` | `agent-lab code` 交互式终端工作台的富文本渲染、剪枝自动补全与增强按键绑定 |

### 5.3 `Dockerfile` 生产容器安全硬化剖析

[`Dockerfile`](../../Dockerfile)（L1–L17）面向生产最小攻击面构建：

| 行号 | 指令 / 配置 | 安全与运维设计意图 |
| :---: | :--- | :--- |
| `L1–L3` | `FROM python:3.12-slim` + `apt-get install -y --no-install-recommends git` + 清理 `/var/lib/apt/lists/*` | 仅安装 Git Worktree/Diff 所需的最小系统依赖并清缓存，最小化镜像 CVE 暴露面 |
| `L4–L9` | 先复制 `pyproject.toml uv.lock` 与业务目录，执行 `pip install --no-cache-dir uv && uv sync --locked --no-dev` | 锁定 `uv.lock` 确定性复现依赖，生产镜像剔除全部 `dev` 工具与测试框架 |
| `L10–L11` | `useradd --create-home learner` + `chown learner /app/.agent-lab` + `USER learner` | 强制非 root 低权限运行，仅状态目录 `.agent-lab` 归属运行用户，源码目录默认只读防运行期篡改 |
| `L12` | `ENV AGENT_LAB_ALLOWED_HOSTS=localhost,127.0.0.1,[::1]` | 默认收紧 `TrustedHostMiddleware` 白名单，防止 DNS Rebinding / Host 头注入 |
| `L13–L14` | `EXPOSE 8765` + `HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3` 探测 `/healthz` | 使用标准库 `urllib.request` 零外部工具探测活性，支持 Kubernetes / Docker Compose 自动摘流重启 |
| `L15–L17` | `CMD ["/app/.venv/bin/python", "-m", "agent_lab.cli", "serve", "--host", "0.0.0.0", "--port", "8765"]` | 经统一 CLI 入口监听 `0.0.0.0`：**自动激活非回环安全门禁**（[`cli.py`](../../src/agent_lab/cli.py) `L3310–L3324`），未设置 `AGENT_LAB_TOKEN`/`AGENT_LAB_TOKENS_JSON` 时默认拒绝启动（仅宿主机 `127.0.0.1` 端口映射演练时允许显式传入 `AGENT_LAB_ALLOW_INSECURE_LOCAL=1`）。**架构铁律**：应用容器绝不挂载宿主机 `/var/run/docker.sock`，测试沙箱通过 `AGENT_LAB_SANDBOX_URL` + `AGENT_LAB_SANDBOX_TOKEN` 委托至独立隔离的远端沙箱池执行 |

---

## 6. 全量环境变量、8 类声明式配置文件与硬配额权威参考

### 6.1 全量环境变量权威总表

经对 [`src/agent_lab/`](../../src/agent_lab/) 全部 43 个 Python 模块进行 AST 静态审计，代码库共直接读取 **101 个显式 `os.environ` / `_parse_positive_int_env` 配置项**（另经动态档位元组与 `_resolve_base_url` 映射解析 8 个多厂商分档模型与 3 个基础 URL 覆盖变量，去掉 3 个标准 OS 变量后共计 **109 个应用配置环境变量**；此外列明 3 个 OS/Python 环境继承变量、2 个由 `HookManager` 注入子进程的只读上下文变量及 1 个测试门控变量）。按功能域分为 6 大表：

#### 6.1.1 Provider / 多协议网关与模型调优（44 项）

| # | 环境变量 | 默认值 | 归属模块与关键行号 | 语义与安全说明 |
| :-: | :--- | :--- | :--- | :--- |
| 1 | `AGENT_PROVIDER` | 空（自动探测回退 `demo`） | [`api.py`](../../src/agent_lab/api.py):2379 · [`cli.py`](../../src/agent_lab/cli.py):223 | 指定默认 Provider（`demo`/`anthropic`/`openai`/`gemini` 等）；低优先级于显式网关配置 |
| 2 | `AGENT_GATEWAY_PROFILE` | 空 | [`api.py`](../../src/agent_lab/api.py):2348 · [`cli.py`](../../src/agent_lab/cli.py):492 | 指定 `gateways.json` 中的激活网关 Profile 名（支持 `gateway:<name>` 前缀） |
| 3 | `AGENT_LAB_GATEWAYS_FILE` | `<state_dir>/gateways.json` | [`api.py`](../../src/agent_lab/api.py):2230 · [`gateways.py`](../../src/agent_lab/gateways.py) | 显式覆盖声明式模型网关配置文件路径 |
| 4 | `AGENT_ALLOW_CUSTOM_BASE_URL` | `0`（关闭） | [`gateways.py`](../../src/agent_lab/gateways.py):384 · [`providers.py`](../../src/agent_lab/providers.py):1300 | **安全门控**：必须显式设 `1` 才允许使用环境变量中的自定义非官方 Base URL，防凭据重定向劫持 |
| 5 | `ANTHROPIC_API_KEY` | 空（必填其一） | [`providers.py`](../../src/agent_lab/providers.py):1501 · [`api.py`](../../src/agent_lab/api.py):3752 | Anthropic Messages API 密钥；受 `build_scrubbed_env` 与 `EgressDlpInspector` 双重防泄露保护 |
| 6 | `ANTHROPIC_AUTH_TOKEN` | 空 | [`providers.py`](../../src/agent_lab/providers.py):1502 · [`cli.py`](../../src/agent_lab/cli.py):226 | Anthropic Bearer 鉴权令牌（中转/代理网关替代 `ANTHROPIC_API_KEY`） |
| 7 | `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` | [`providers.py`](../../src/agent_lab/providers.py):1487 · [`gateways.py`](../../src/agent_lab/gateways.py):1413 | Anthropic 自定义端点（需 `AGENT_ALLOW_CUSTOM_BASE_URL=1` 方生效） |
| 8 | `ANTHROPIC_MODEL` | 空（启用时必填） | [`providers.py`](../../src/agent_lab/providers.py) · [`model_evals.py`](../../src/agent_lab/model_evals.py):581 | Anthropic 主模型 ID（如 `claude-sonnet-4-5`） |
| 9 | `ANTHROPIC_FAST_MODEL` | 回退 `AGENT_LAB_FAST_MODEL`/主模型 | [`providers.py`](../../src/agent_lab/providers.py):879 · [`api.py`](../../src/agent_lab/api.py):2114 | Anthropic `fast` 档位子 Agent / 压缩摘要专用轻量模型 |
| 10 | `ANTHROPIC_REASONING_MODEL` | 回退 `AGENT_LAB_REASONING_MODEL`/主模型 | [`providers.py`](../../src/agent_lab/providers.py):904 · [`api.py`](../../src/agent_lab/api.py):2113 | Anthropic `reasoning` 档位高阶推理模型 |
| 11 | `ANTHROPIC_THINKING_BUDGET` | 空（关闭） | [`providers.py`](../../src/agent_lab/providers.py):1531 | Extended Thinking 预算 Token 数（需 `>= 1024` 才启用 `thinking.type="enabled"`） |
| 12 | `ANTHROPIC_TIMEOUT` | `120.0`（秒） | [`providers.py`](../../src/agent_lab/providers.py):1572 | Anthropic HTTP 读写超时（连接上限 `min(15.0, t)`、写固定 `30.0s`） |
| 13 | `ANTHROPIC_STREAM_IDLE_TIMEOUT` | `120.0`（秒） | [`providers.py`](../../src/agent_lab/providers.py):1582 | Anthropic SSE 流式单帧最大空闲超时，防半开死流悬挂 |
| 14 | `AGENT_LAB_ANTHROPIC_CONTEXT_EDITING` | `0`（关闭） | [`providers.py`](../../src/agent_lab/providers.py):1540 | 设为 `1` 启用 Anthropic 服务端 Context Editing（`clear_tool_uses` / 思考块清理） |
| 15 | `AGENT_LAB_ANTHROPIC_SERVER_COMPACT` | `0`（关闭） | [`providers.py`](../../src/agent_lab/providers.py):1548 | 设为 `1` 启用 Anthropic 服务端自动压缩（`compact_20260112`） |
| 16 | `AGENT_LAB_ANTHROPIC_SERVER_COMPACT_TRIGGER_TOKENS` | `150000` | [`providers.py`](../../src/agent_lab/providers.py):1553 | Anthropic 服务端压缩触发输入 Token 阈值 |
| 17 | `AGENT_LAB_ANTHROPIC_PTC` | `0`（关闭） | [`providers.py`](../../src/agent_lab/providers.py):1559 | 设为 `1` 启用 Anthropic 原生 Programmatic Tool Calling Beta 协议头与 `allowed_callers` |
| 18 | `OPENAI_API_KEY` | 空 | [`providers.py`](../../src/agent_lab/providers.py):2197 · [`retrieval.py`](../../src/agent_lab/retrieval.py):998 | OpenAI Chat/Responses 及默认 Embedding 回退 API 密钥；受 DLP 监控 |
| 19 | `OPENAI_BASE_URL` | `https://api.openai.com/v1` | [`providers.py`](../../src/agent_lab/providers.py):2187 · [`providers.py`](../../src/agent_lab/providers.py):2990 | OpenAI 兼容端点（需 `AGENT_ALLOW_CUSTOM_BASE_URL=1` 或回环网关放行） |
| 20 | `OPENAI_FALLBACK_BASE_URLS` | 空 | [`providers.py`](../../src/agent_lab/providers.py):2217 | 逗号分隔的故障切换备用 Base URL 列表（最多 8 个） |
| 21 | `OPENAI_MODEL` | Chat 必填 / Responses 默认 `gpt-5` | [`providers.py`](../../src/agent_lab/providers.py):2203 · [`providers.py`](../../src/agent_lab/providers.py):3001 | OpenAI 主模型 ID |
| 22 | `OPENAI_FAST_MODEL` | 回退 `AGENT_LAB_FAST_MODEL`/主模型 | [`providers.py`](../../src/agent_lab/providers.py):881 · [`api.py`](../../src/agent_lab/api.py):2117 | OpenAI `fast` 档位轻量模型 |
| 23 | `OPENAI_REASONING_MODEL` | 回退 `AGENT_LAB_REASONING_MODEL`/主模型 | [`providers.py`](../../src/agent_lab/providers.py):906 · [`api.py`](../../src/agent_lab/api.py):2116 | OpenAI `reasoning` 档位高阶推理模型 |
| 24 | `OPENAI_REASONING_EFFORT` | 空 | [`providers.py`](../../src/agent_lab/providers.py):2233 · [`providers.py`](../../src/agent_lab/providers.py):3009 | 推理力度档位（如 `low`/`medium`/`high`） |
| 25 | `OPENAI_STREAM` | `0`（Chat 默认非流式） | [`providers.py`](../../src/agent_lab/providers.py):2224 | 设为 `1` 开启 `OpenAICompatibleProvider` SSE 流式响应 |
| 26 | `OPENAI_TIMEOUT` | `120.0`（秒） | [`providers.py`](../../src/agent_lab/providers.py):2256 | OpenAI HTTP 读写超时 |
| 27 | `OPENAI_STREAM_IDLE_TIMEOUT` | `120.0`（秒） | [`providers.py`](../../src/agent_lab/providers.py):2266 | OpenAI SSE 流式空闲超时 |
| 28 | `OPENAI_SEND_PROMPT_CACHE_KEY` | `1`（默认发送） | [`providers.py`](../../src/agent_lab/providers.py):2311 | 设为 `0` 禁用 `prompt_cache_key` 字段，兼容不支持该字段的第三方网关 |
| 29 | `OPENAI_USE_MAX_COMPLETION_TOKENS` | 自动推断（o1/o3/gpt-5 等启用） | [`providers.py`](../../src/agent_lab/providers.py):2796 | 设为 `1` 强制使用 `max_completion_tokens` 替代传统 `max_tokens` |
| 30 | `AGENT_LAB_OPENAI_ENCRYPTED_REASONING` | 按 `reasoning_effort` 自动启用 | [`providers.py`](../../src/agent_lab/providers.py):3014 | 控制 Responses API 加密推理项（`reasoning.encrypted_content`）保留开关（`1`/`0`） |
| 31 | `GEMINI_API_KEY` | 空 | [`providers.py`](../../src/agent_lab/providers.py):4181 · [`api.py`](../../src/agent_lab/api.py):3757 | Google Gemini 原生协议 API 密钥（优先于 `GOOGLE_API_KEY`） |
| 32 | `GOOGLE_API_KEY` | 空 | [`providers.py`](../../src/agent_lab/providers.py):4181 · [`cli.py`](../../src/agent_lab/cli.py):230 | Gemini 原生协议兼容别名密钥 |
| 33 | `GEMINI_BASE_URL` | `https://generativelanguage.googleapis.com/v1beta` | [`providers.py`](../../src/agent_lab/providers.py):4171 · [`gateways.py`](../../src/agent_lab/gateways.py):1426 | Gemini 自定义端点（需 `AGENT_ALLOW_CUSTOM_BASE_URL=1`） |
| 34 | `GEMINI_MODEL` | `gemini-2.5-pro` | [`providers.py`](../../src/agent_lab/providers.py):4191 · [`api.py`](../../src/agent_lab/api.py):2118 | Gemini 主模型 ID |
| 35 | `GEMINI_FAST_MODEL` | 回退 `AGENT_LAB_FAST_MODEL`/主模型 | [`providers.py`](../../src/agent_lab/providers.py):883 · [`api.py`](../../src/agent_lab/api.py):2120 | Gemini `fast` 档位模型 |
| 36 | `GEMINI_REASONING_MODEL` | 回退 `AGENT_LAB_REASONING_MODEL`/主模型 | [`providers.py`](../../src/agent_lab/providers.py):908 · [`api.py`](../../src/agent_lab/api.py):2119 | Gemini `reasoning` 档位模型 |
| 37 | `AGENT_LAB_GEMINI_EXPLICIT_CACHE` | `0`（关闭） | [`providers.py`](../../src/agent_lab/providers.py):4206 | 设为 `1` 启用 Gemini 显式 `cachedContents` 上下文缓存管理 |
| 38 | `AGENT_LAB_GEMINI_EXPLICIT_CACHE_MIN_TOKENS` | `32768` | [`providers.py`](../../src/agent_lab/providers.py):4211 | 触发 Gemini 显式缓存创建的最小前缀 Token 门槛 |
| 39 | `AGENT_LAB_GEMINI_EXPLICIT_CACHE_TTL` | `3600`（秒，最小 `60`） | [`providers.py`](../../src/agent_lab/providers.py):4216 | Gemini `cachedContents` 资源 TTL 与自动续期时长 |
| 40 | `AGENT_LAB_FAST_MODEL` | 空 | [`providers.py`](../../src/agent_lab/providers.py):884 · [`api.py`](../../src/agent_lab/api.py):2122 | 跨厂商通用 `fast` 档位兜底模型名 |
| 41 | `AGENT_LAB_REASONING_MODEL` | 空 | [`providers.py`](../../src/agent_lab/providers.py):909 · [`api.py`](../../src/agent_lab/api.py):2121 | 跨厂商通用 `reasoning` 档位兜底模型名 |
| 42 | `AGENT_LAB_RPM_LIMIT` | 空（不限） | [`providers.py`](../../src/agent_lab/providers.py):101 | 客户端 60s 滑动窗口模型调用 RPM 上限（正整数） |
| 43 | `AGENT_LAB_TPM_LIMIT` | 空（不限） | [`providers.py`](../../src/agent_lab/providers.py):106 | 客户端两阶段（预估预留 + 实耗校准）TPM 上限（正整数） |
| 44 | `AGENT_LAB_MODEL_PRICING_JSON` | 内置价目表 | [`providers.py`](../../src/agent_lab/providers.py):363 | JSON 字典覆盖模型每百万 Token 单价（`input`/`output`/`cache_read`/`cache_write`），驱动 USD 成本预算熔断 |

#### 6.1.2 上下文、压缩、混合检索与 MCP 发现（18 项）

| # | 环境变量 | 默认值 | 归属模块与关键行号 | 语义与安全说明 |
| :-: | :--- | :--- | :--- | :--- |
| 45 | `AGENT_LAB_CONTEXT_CHARS` | 宿主 `64000` / 沙箱 `24000`（按窗口 `4×` 自动缩放） | [`runtime.py`](../../src/agent_lab/runtime.py):932 | 显式覆盖上下文字符预算硬上限（需 `>= 1000`） |
| 46 | `AGENT_LAB_CONTEXT_TOKENS` | 按模型名自动推断（上限 `10M`） | [`providers.py`](../../src/agent_lab/providers.py):319 | 显式覆盖模型上下文窗口 Token 数（需 `>= 4096`） |
| 47 | `AGENT_LAB_AUTO_COMPACT_RATIO` | `0.80`（有效范围 `[0.35, 1.0]`） | [`runtime.py`](../../src/agent_lab/runtime.py):965 | Tier-2 滞回自动压缩高水位阈值（低水位自动按 `min(0.58, ratio * 0.72)` 计算） |
| 48 | `AGENT_LAB_USE_TIKTOKEN` | `0`（零依赖校准估算器） | [`context.py`](../../src/agent_lab/context.py):104 | 设为 `1` 且本机安装 `tiktoken` 时切换为 `cl100k_base` 精确计数 |
| 49 | `AGENT_LAB_EMBEDDING_URL` | 空（BM25+TF-IDF 离线回退） | [`retrieval.py`](../../src/agent_lab/retrieval.py):987 | 设 `local` 启用本地哈希稠密向量；设 `https://...` 启用 OpenAI 兼容远程 Embedding |
| 50 | `AGENT_LAB_EMBEDDING_MODE` | 空 | [`retrieval.py`](../../src/agent_lab/retrieval.py):988 | 当 `EMBEDDING_URL` 为空时，设 `local` 启用 `LocalSubwordDenseEmbedder` |
| 51 | `AGENT_LAB_EMBEDDING_DIM` | `256`（最小 `8`） | [`retrieval.py`](../../src/agent_lab/retrieval.py):990 | 本地子词哈希稠密向量维度 |
| 52 | `AGENT_LAB_EMBEDDING_MODEL` | `text-embedding-3-small` | [`retrieval.py`](../../src/agent_lab/retrieval.py):995 | 远程 OpenAI 兼容 Embedding 模型 ID |
| 53 | `AGENT_LAB_EMBEDDING_API_KEY` | 回退 `OPENAI_API_KEY` | [`retrieval.py`](../../src/agent_lab/retrieval.py):997 | 远程 Embedding 接口鉴权密钥 |
| 54 | `AGENT_LAB_EMBEDDING_TIMEOUT` | `15.0`（秒） | [`retrieval.py`](../../src/agent_lab/retrieval.py):1000 | 远程 Embedding 请求超时 |
| 55 | `AGENT_LAB_EMBEDDING_CACHE_SIZE` | `2048`（条） | [`retrieval.py`](../../src/agent_lab/retrieval.py):1007 | Embedding LRU 内存缓存容量（`0` 关闭缓存） |
| 56 | `AGENT_LAB_RERANK_URL` | 空（不启用精排） | [`retrieval.py`](../../src/agent_lab/retrieval.py):1025 | 外部 HTTP Reranker 精排端点（如 Cohere/Jina 兼容接口） |
| 57 | `AGENT_LAB_RERANK_MODEL` | `rerank-v3.5` | [`retrieval.py`](../../src/agent_lab/retrieval.py):1028 | 外部精排模型 ID |
| 58 | `AGENT_LAB_RERANK_API_KEY` | 空 | [`retrieval.py`](../../src/agent_lab/retrieval.py):1029 | 外部 Reranker 鉴权密钥 |
| 59 | `AGENT_LAB_RERANK_TIMEOUT` | `10.0`（秒） | [`retrieval.py`](../../src/agent_lab/retrieval.py):1030 | 外部 Reranker HTTP 超时（异常安全回退 RRF 原序） |
| 60 | `AGENT_LAB_MCP_CONFIG` | `<state_dir>/mcp.json` | [`mcp_client.py`](../../src/agent_lab/mcp_client.py):578 | 显式 MCP 配置文件路径（配置后文件缺失触发 Fail-Fast `ValueError`） |
| 61 | `AGENT_LAB_MCP_TOOL_SEARCH` | `auto`（`>12` 工具或 `>4800` 字符延迟） | [`mcp_client.py`](../../src/agent_lab/mcp_client.py):786 | 控制延迟 MCP 发现开关：`auto` / `on`/`deferred` / `off`/`eager` |
| 62 | `AGENT_LAB_MCP_EXTENDED` | `0`（仅基础只读工具） | [`mcp_server.py`](../../src/agent_lab/mcp_server.py):118 | 设为 `1`/`true` 在只读 MCP Server 额外暴露 `repo_map` 与 `symbol_search` |

#### 6.1.3 安全、鉴权、RBAC、沙箱、浏览器与 A2A（24 项）

| # | 环境变量 | 默认值 | 归属模块与关键行号 | 语义与安全说明 |
| :-: | :--- | :--- | :--- | :--- |
| 63 | `AGENT_LAB_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1],testserver` | [`api.py`](../../src/agent_lab/api.py):2139 | `TrustedHostMiddleware` 逗号分隔白名单，防 Host 头注入与 DNS Rebinding |
| 64 | `AGENT_LAB_TOKEN` | 空 | [`api.py`](../../src/agent_lab/api.py):2146 · [`cli.py`](../../src/agent_lab/cli.py):3313 | 单令牌模式全局 Admin Bearer 密钥（`hmac.compare_digest` 常量时间比对） |
| 65 | `AGENT_LAB_TOKENS_JSON` | 空 | [`api.py`](../../src/agent_lab/api.py):2147 · [`cli.py`](../../src/agent_lab/cli.py):3314 | 多租户 RBAC 令牌映射 JSON（含 `tenant_id`/`actor_id`/`roles`/`global_admin`） |
| 66 | `AGENT_LAB_REQUIRE_AUTH` | `0`（非回环监听时自动置 `1`） | [`api.py`](../../src/agent_lab/api.py):2150 · [`cli.py`](../../src/agent_lab/cli.py):3324 | **启动熔断**：设为 `1` 且未配置任何令牌时 `create_app()` 直接抛 `RuntimeError` 拒启 |
| 67 | `AGENT_LAB_ALLOW_INSECURE_LOCAL` | `0`（严格禁止） | [`cli.py`](../../src/agent_lab/cli.py):3316 | 仅限 Docker 容器映射宿主机 `127.0.0.1` 练习时豁免非回环监听鉴权拦截 |
| 68 | `AGENT_LAB_REQUIRE_DISTINCT_APPROVER` | `0`（关闭） | [`api.py`](../../src/agent_lab/api.py):5807 | 设为 `1` 开启双人复核（Four-Eyes），禁止 `actor_id == created_by` 自批补丁（返回 `403`） |
| 69 | `AGENT_LAB_ALLOWED_WORKSPACES` | 空（无限制直连路径白名单） | [`api.py`](../../src/agent_lab/api.py):2195 | 逗号分隔的 Web/API 直连宿主工作区允许根目录白名单 |
| 70 | `AGENT_LAB_TRUSTED_WORKSPACES` | 空 | [`permissions.py`](../../src/agent_lab/permissions.py):1316 | `os.pathsep` 分隔的已信任项目工作区列表，放行项目级 `.agent-lab/settings.json` 规则与钩子 |
| 71 | `AGENT_LAB_RATE_LIMIT_RPM` | `120` | [`api.py`](../../src/agent_lab/api.py):2174 | 每租户写接口 60s 滑动窗口限流配额（超限 `429`） |
| 72 | `AGENT_LAB_AUTH_RATE_LIMIT_RPM` | 同 `AGENT_LAB_RATE_LIMIT_RPM` | [`api.py`](../../src/agent_lab/api.py):2176 | 登录与鉴权端点独立防爆破限流配额 |
| 73 | `AGENT_LAB_MAX_BODY_BYTES` | `262144`（256 KiB） | [`api.py`](../../src/agent_lab/api.py):2154 | 通用路由 HTTP 请求体字节硬上限（超限 `413`；显式设置时同步封顶沙箱/附件路由） |
| 74 | `AGENT_LAB_SANDBOX_MAX_BODY_BYTES` | `4194304`（4 MiB） | [`api.py`](../../src/agent_lab/api.py):2159 | `/api/internal/sandbox/execute` 远端沙箱快照体上限 |
| 75 | `AGENT_LAB_ATTACHMENT_MAX_BODY_BYTES` | `8388608`（8 MiB） | [`api.py`](../../src/agent_lab/api.py):2166 | `/api/uploads`、`/api/runs/{id}/attachments` 与 `/a2a` 附件体上限 |
| 76 | `AGENT_LAB_HOST_SANDBOX` | `auto` | [`host_tools.py`](../../src/agent_lab/host_tools.py):1448 · [`cli.py`](../../src/agent_lab/cli.py):2171 | 本地 OS 内核沙箱策略：`auto`（可用即启）/ `strict`（不可用 Fail-Closed）/ `off` |
| 77 | `AGENT_LAB_EXECUTION_BACKEND` | `local` | [`execution_backend.py`](../../src/agent_lab/execution_backend.py):1181 | Brain/Hands 执行后端工厂类型：`local` / `docker` / `microvm` |
| 78 | `AGENT_LAB_MICROVM_ENDPOINT` | 空 | [`execution_backend.py`](../../src/agent_lab/execution_backend.py):1195 | 远端 MicroVM（Firecracker/Kata/E2B/Daytona）JSON-RPC 执行端点（强制 SSRF 校验） |
| 79 | `AGENT_LAB_MICROVM_TOKEN` | 空 | [`execution_backend.py`](../../src/agent_lab/execution_backend.py):979 | 远端 MicroVM 后端 Bearer 鉴权令牌 |
| 80 | `AGENT_LAB_SANDBOX_CONCURRENCY` | `4`（最小 `1`） | [`sandbox.py`](../../src/agent_lab/sandbox.py):344 | 本机 Docker 沙箱最大并发容器信号量上限 |
| 81 | `AGENT_LAB_SANDBOX_URL` | 空（使用本机 Docker） | [`api.py`](../../src/agent_lab/api.py):3523 · [`sandbox.py`](../../src/agent_lab/sandbox.py) | 独立受管远端沙箱池地址，使应用容器无需挂载 `docker.sock` |
| 82 | `AGENT_LAB_SANDBOX_TOKEN` | 空 | [`api.py`](../../src/agent_lab/api.py):2853 · [`sandbox.py`](../../src/agent_lab/sandbox.py) | `/api/internal/sandbox/execute` 专用内部池 Bearer 共享密钥 |
| 83 | `AGENT_LAB_REQUIRE_SANDBOX` | `0` | [`api.py`](../../src/agent_lab/api.py):3527 | 设为 `1` 时 `/readyz` 强制要求远端沙箱或本机 Docker 可用，否则返回 `503` |
| 84 | `AGENT_LAB_BROWSER_ALLOW_LOOPBACK` | `0`（阻断回环） | [`browser.py`](../../src/agent_lab/browser.py):97 | 设为 `1` 允许 Playwright 访问 `localhost`/`127.0.0.1`（仍硬阻断 IMDS 与 RFC1918 私网） |
| 85 | `AGENT_LAB_A2A_CONFIG` | `<state_dir>/a2a.json` | [`a2a.py`](../../src/agent_lab/a2a.py):896 · [`api.py`](../../src/agent_lab/api.py):2222 | 显式覆盖远端 A2A Peer 注册表配置文件路径 |
| 86 | `AGENT_LAB_A2A_ALLOW_PRIVATE` | `0`（阻断私网/回环） | [`a2a.py`](../../src/agent_lab/a2a.py):574 | 设为 `1` 允许发现/调用回环或内网 A2A Agent（始终硬阻断云元数据主机） |

#### 6.1.4 存储、高可用队列、PostgreSQL 与 Worker（12 项）

| # | 环境变量 | 默认值 | 归属模块与关键行号 | 语义与安全说明 |
| :-: | :--- | :--- | :--- | :--- |
| 87 | `AGENT_LAB_STATE` | `<repo>/.agent-lab` | [`api.py`](../../src/agent_lab/api.py):2184 · [`cli.py`](../../src/agent_lab/cli.py):3397 | 状态根目录（存放 `state.db`、`workspaces/`、`checkpoints/`、`artifacts/`、`memory/` 与配置文件） |
| 88 | `AGENT_LAB_STORAGE_DSN` | 空（本地 SQLite WAL） | [`api.py`](../../src/agent_lab/api.py):2201 | `sqlite:///...` 或 `postgresql://...` 持久化后端 DSN（受脱敏与 `build_scrubbed_env` 保护） |
| 89 | `AGENT_LAB_SQLITE_SYNC` | `NORMAL` | [`storage.py`](../../src/agent_lab/storage.py):393 | SQLite `PRAGMA synchronous` 档位（仅允许 `OFF`/`NORMAL`/`FULL`/`EXTRA`） |
| 90 | `AGENT_LAB_PG_POOL_SIZE` | `4` | [`storage.py`](../../src/agent_lab/storage.py):459 | `psycopg_pool` PostgreSQL 连接池最大连接数 |
| 91 | `AGENT_LAB_DISPATCH` | `inline` | [`api.py`](../../src/agent_lab/api.py):2491 · [`cli.py`](../../src/agent_lab/cli.py):3399 | 调度模式：`inline`（API 进程内协程池）或 `queue`（持久队列 + 独立 `LeaseWorker`） |
| 92 | `AGENT_LAB_MAX_INLINE_JOBS` | `16`（最小 `1`） | [`api.py`](../../src/agent_lab/api.py):2173 | `inline` 模式最大并发执行任务数（超限 `429`） |
| 93 | `AGENT_LAB_MAX_QUEUE_DEPTH` | `64`（最小 `1`） | [`api.py`](../../src/agent_lab/api.py):2172 | `queue` 模式全局未完成队列背压上限（超限 `429`） |
| 94 | `AGENT_LAB_REQUIRE_WORKER` | `0` | [`api.py`](../../src/agent_lab/api.py):3528 | `queue` 模式下设 `1` 时 `/readyz` 强制校验至少存在 1 个活跃心跳 Worker |
| 95 | `AGENT_LAB_WORKER_WORKSPACES_DIR` | `<state_dir>/worker_workspaces/<id>` | [`lease.py`](../../src/agent_lab/lease.py):472 | 独立 Worker 物化跨节点工作区归档的本地根路径 |
| 96 | `AGENT_LAB_SYNC_WORKSPACE_ARCHIVE` | `0`（仅对已有归档同步） | [`lease.py`](../../src/agent_lab/lease.py):589 · [`runtime.py`](../../src/agent_lab/runtime.py) | 设为 `1` 强制所有 Worker 在租约释放前以当前 `epoch` 打包上传工作区归档，支持无共享磁盘跨节点接管 |
| 97 | `AGENT_LAB_WORKER_METRICS_TOKEN` | 空（免鉴权本机抓取） | [`lease.py`](../../src/agent_lab/lease.py):153 | 为 `WorkerMetricsServer` 的 `/metrics` 端点开启 Bearer 令牌鉴权（`/healthz` 保持开放探活） |
| 98 | `AGENT_LAB_ENABLE_TRIGGER_SCHEDULER` | `0`（关闭） | [`api.py`](../../src/agent_lab/api.py):2664 | 设为 `1`/`true`/`yes` 在 FastAPI Lifespan 内启动后台 `TriggerScheduler` 定时 Cron 调度器 |

#### 6.1.5 可观测性、OTLP 与指标（3 项）

| # | 环境变量 | 默认值 | 归属模块与关键行号 | 语义与安全说明 |
| :-: | :--- | :--- | :--- | :--- |
| 99 | `AGENT_LAB_OTLP_ENDPOINT` | 空（不导出） | [`observability.py`](../../src/agent_lab/observability.py):509 | OTLP/HTTP JSON Span 后台异步导出端点（强制无阻塞语法与 DNS-Pinned SSRF 校验） |
| 100 | `AGENT_LAB_OTLP_ALLOW_PRIVATE` | `0`（禁内网导出） | [`observability.py`](../../src/agent_lab/observability.py):272 | 设为 `1`/`true`/`yes` 允许 OTLP 导出至内网/本机 Collector（如 Jaeger/Tempo Sidecar） |
| 101 | `AGENT_LAB_SERVICE_NAME` | `agent-systems-lab` | [`observability.py`](../../src/agent_lab/observability.py):380 | OTLP Resource `service.name` 服务标识 |

#### 6.1.6 CLI、Web SSE、网页搜索与系统/钩子/测试辅助变量（8 项应用变量 + 6 项辅助变量）

| # | 环境变量 | 默认值 | 归属模块与关键行号 | 语义与安全说明 |
| :-: | :--- | :--- | :--- | :--- |
| 102 | `AGENT_LAB_NO_DOTENV` | `0`（自动加载 `.env`） | [`cli.py`](../../src/agent_lab/cli.py):183 | 设为 `1` 禁止 CLI 启动时自动解析当前目录与仓库根目录的 `.env`（用于隔离测试与容器强约束） |
| 103 | `AGENT_LAB_SSE_KEEPALIVE_SECONDS` | `15.0`（最小 `0.05`） | [`api.py`](../../src/agent_lab/api.py):2170 | SSE 实时流心跳注释帧周期，防止 Nginx/ALB 代理超时断流 |
| 104 | `AGENT_LAB_WEB_SEARCH_URL` | 空 | [`host_tools.py`](../../src/agent_lab/host_tools.py):3148 | 自定义 `web_search` 聚合搜索网关 URL（优先级高于 Brave/Tavily/Serper） |
| 105 | `AGENT_LAB_WEB_SEARCH_API_KEY` | 空 | [`host_tools.py`](../../src/agent_lab/host_tools.py):3149 | 自定义 `web_search` 网关鉴权密钥 |
| 106 | `BRAVE_SEARCH_API_KEY` | 空 | [`host_tools.py`](../../src/agent_lab/host_tools.py):3150 | Brave Search API 密钥（优先于 `BRAVE_API_KEY`） |
| 107 | `BRAVE_API_KEY` | 空 | [`host_tools.py`](../../src/agent_lab/host_tools.py):3150 | Brave Search API 兼容别名密钥 |
| 108 | `TAVILY_API_KEY` | 空 | [`host_tools.py`](../../src/agent_lab/host_tools.py):3151 | Tavily 搜索后端 API 密钥 |
| 109 | `SERPER_API_KEY` | 空 | [`host_tools.py`](../../src/agent_lab/host_tools.py):3152 | Serper.dev Google 搜索后端 API 密钥 |
| — | `DISPLAY` | 系统继承 | [`computer.py`](../../src/agent_lab/computer.py):1310 | Linux X11 桌面 GUI 后端检测变量（缺失时无法调用 X11 截屏/键鼠） |
| — | `TMPDIR` | 系统继承 | [`host_tools.py`](../../src/agent_lab/host_tools.py):1422 | macOS Seatbelt SBPL 自动纳入允许读写的临时目录 |
| — | `PYTHONPATH` | 系统继承 | [`model_evals.py`](../../src/agent_lab/model_evals.py):1214 | SWE-bench 独立副本判分时在 `staging` 之后拼接继承 |
| — | `AGENT_LAB_HOOK_EVENT` | 运行期只读注入 | [`hooks.py`](../../src/agent_lab/hooks.py):215 | `HookManager` 向外部命令钩子子进程注入的当前生命周期事件名 |
| — | `AGENT_LAB_WORKSPACE` | 运行期只读注入 | [`hooks.py`](../../src/agent_lab/hooks.py):220 | `HookManager` 向钩子子进程注入的规范化工作区物理路径 |
| — | `AGENT_LAB_TEST_POSTGRES_DSN` | 空（默认跳过） | [`tests/test_evals_subsystems_and_postgres.py`](../../tests/test_evals_subsystems_and_postgres.py):511 | **测试门控**：启用实时 PostgreSQL 16 集成测试的目标数据库连接串 |

### 6.2 全部 8 类声明式配置文件总览

| # | 配置文件 | 发现优先级与物理位置 | 硬上限 / 权限 | 核心 Schema 与安全不变量 |
| :-: | :--- | :--- | :--- | :--- |
| 1 | `.env` | `<cwd>/.env` → `<repo>/.env`（可通过 `AGENT_LAB_NO_DOTENV=1` 禁用，[`cli.py`](../../src/agent_lab/cli.py) `L181–L218`） | `<= 256 KiB`（`_DOTENV_MAX_BYTES` `L93`）；`agent-lab init` 落盘强制 `0600`（`L2540`） | 零依赖解析器，严格使用 `os.environ.setdefault` **零覆盖**已有进程环境变量；在沙箱与宿主工具路径守卫中被列为高敏词干永久禁读 |
| 2 | `gateways.json` | 显式参数 → `AGENT_LAB_GATEWAYS_FILE` → `<state_dir>/gateways.json` → 本机默认态回退 `~/.agent-lab/gateways.json`（模板见 [`gateways.example.json`](../../gateways.example.json)） | `<= 256 KiB`（`gateways.py:63`）；`0600` 原子替换；最多 `32` 个 Profile、`8` 个备用 URL、`32` 个自定义 Header | 4 级信任发现防多租户串读宿主家目录；拒绝软硬链；`PUT /api/gateways` 限全局 Admin，支持 `***REDACTED***` 占位符无损保留原密钥与零明文回显 |
| 3 | `a2a.json` | `AGENT_LAB_A2A_CONFIG` → `<workspace>/.agent-lab/a2a.json` → `<state_dir>/a2a.json`（模板见 [`a2a.example.json`](../../a2a.example.json)，[`a2a.py`](../../src/agent_lab/a2a.py) `L895–L940`） | `<= 256 KiB`（`a2a.py:122`）；最多 `32` 个 Peer；`O_NOFOLLOW` + `st_nlink == 1` + `0600` 原子替换 | 声明远端 A2A 对端 URL、协议版本与 `api_key_env` 环境变量引用；强制 SSRF/IMDS 校验；未配置对端时零 A2A 工具挂载、零 KV 字节开销 |
| 4 | `.agent-lab/mcp.json` | `--mcp-config` → `AGENT_LAB_MCP_CONFIG` → `<state_dir>/mcp.json`（[`mcp_client.py`](../../src/agent_lab/mcp_client.py) `L561–L588`） | 拒绝符号链接；缺失显式路径 Fail-Fast；支持 `mcpServers` / `servers` 双根键 | 支持 `stdio` / Streamable HTTP / SSE 传输与 `headers`/`env` 的 `${ENV_VAR}` 安全插值（缺失变量跳过该 Server 并记录 `missing_env_vars` 不阻塞其余）；支持 `defer_loading` 控制延迟检索 |
| 5 | `.agent-lab/settings.json` | 全局 `<state_dir>/settings.json` + 经信任校验的项目 `<workspace>/.agent-lab/settings.json`（[`permissions.py`](../../src/agent_lab/permissions.py) `L1296–L1357`、[`hooks.py`](../../src/agent_lab/hooks.py) `L354–L371`） | `<= 256 KiB`（`permissions.py:484`）；拒绝符号链接；原子替换 | 统一承载声明式 `permissions`（`allow`/`ask`/`deny` 分层规则，含 `A2A(<peer>)` 与 `Bash(...)`）、`hooks`（生命周期命令钩子）与 `trusted_workspaces`；**信任门禁**：克隆未登记项目仓库绝不自动加载项目级规则或执行项目 Shell 钩子 |
| 6 | `.agent-lab/invariants.json` | `<workspace>/.agent-lab/invariants.json`（[`horizon.py`](../../src/agent_lab/horizon.py) `L137–L166`） | 拒绝符号链接；单文件扫描上限 `2 MiB` 且跳过隐藏目录与敏感词干 | 机械化架构边界规则清单（`rules` 数组），支持 `forbidden_import`（AST 导入前缀）、`max_file_lines`、`forbidden_pattern`（正则）三类规则，每条附带模型可执行 `remediation` 修复指引，供 `agent-lab horizon lint` 与 Horizon 验收执行 |
| 7 | `CLAUDE.md`（及 `AGENTS.md`） | 用户全局 `<state_dir>/memory/global/CLAUDE.md`（回退 `<state_dir>/CLAUDE.md`）+ 项目 `<state_dir>/memory/projects/<slug>/CLAUDE.md` / 工作区根目录 `CLAUDE.md`（[`memory.py`](../../src/agent_lab/memory.py) `L129–L132`） | 用户根回退 `<= 64 KiB`（`L130`）；工作区自动发现 `<= 32 KiB`（`L131`）；拒绝软硬链 | 承载人类与团队维护的长期工程准则；Step-0 一次性冻结钉扎于上下文前缀（守住跨步 KV Cache 稳定），支持协议头行首消毒防审批伪造 |
| 8 | `MEMORY.md` | 用户全局 `<state_dir>/memory/global/MEMORY.md` + 项目 `<state_dir>/memory/projects/<slug>/MEMORY.md`（[`memory.py`](../../src/agent_lab/memory.py) `L94–L101`、`L709–L725`） | 双层合并硬上限 **`<= 200` 行 / `<= 4,000` 字符**（全局 `<= 1,600`、项目 `<= 2,400`） | 跨会话自动演进的长期记忆索引，与 `[[wiki-links]]` 主题页（单页 `<= 12,000` 字符、每层 `<= 64` 页）联动；由离线 `DreamEngine` Pass 4 重平衡并保留最近 `5` 份回滚快照（`MAX_DREAM_BACKUPS = 5`），严禁工具直接无序覆写 |

---

### 6.3 系统级硬配额与熔断阈值速查表

所有安全上限均遵循 **“调用方只能收紧、不能通过配置解除硬天花板”** 原则：

| 子系统 | 常量 / 字段 | 硬配额默认值 | 源码锚点 | 防护目标 |
| :--- | :--- | :--- | :--- | :--- |
| **Docker 沙箱策略** | `SandboxPolicy` + `_LIMIT_FLAGS` | `timeout <= 30s`、`output <= 16 KiB`、`files <= 128`、单文件 `<= 64 KiB`、总量 `<= 2 MiB`、深度 `<= 16`、`pids=64`、`memory=128m`、`cpus=1`、`nofile=128`、`core=0`、`uid=65534` | [`sandbox.py`](../../src/agent_lab/sandbox.py):350–447 | 阻断 Fork 炸弹、OOM、磁盘/日志洪泛、核心转储、特权提权与镜像漂移 |
| **控制平面 HTTP** | `max_body` / `sandbox` / `attachment` | `256 KiB` / `4 MiB` / `8 MiB`；租户限流 `120 RPM`；`inline <= 16`；`queue <= 64` | [`api.py`](../../src/agent_lab/api.py):2154–2176 | 防止超大载荷内存打爆与多租户背压级联雪崩 |
| **工作记忆** | `_MAX_MEMORY_CHARS` / `_MAX_FILE_BYTES` / `_MAX_BOUND_FILES` | `2,400` 字符 / 单哈希文件 `512 KiB` / 最多绑定 `64` 文件 | [`working_memory.py`](../../src/agent_lab/working_memory.py):19–21 | 限制尾部瞬态区体积并防止大文件 SHA-256 校验拖慢主循环 |
| **长期记忆与 Dreaming** | `MAX_INDEX_LINES` / `MAX_INDEX_CHARS` / `MAX_PAGE_CHARS` / `MAX_PAGES_PER_LAYER` | `200` 行 / `4,000` 字符 / 单页 `12,000` 字符 / 每层 `64` 页 / `5` 份快照 | [`memory.py`](../../src/agent_lab/memory.py):94–101 | 防止记忆膨胀侵蚀主对话上下文预算与磁盘失控 |
| **PTC 程序化编排** | `PTC_MAX_SUBCALLS` / `PTC_MAX_TIMEOUT_SECONDS` / `PTC_MAX_OUTPUT_CHARS` / `_PTC_MAX_RPC_LINE_BYTES` | 最多 `24` 次子调用（默认 `16`）、超时 `<= 45s`（默认 `20s`）、输出 `<= 16,000` 字符、代码 `<= 32,000` 字符、单 RPC 帧 `<= 2 MiB`、`stderr <= 8 KiB` | [`ptc.py`](../../src/agent_lab/ptc.py):98–108 | 防止脚本死循环、RPC 长行内存耗尽与输出反灌上下文 |
| **Shadow Workspace** | `_SHADOW_MAX_EDITS` / `_SHADOW_MAX_CLONE_FILES` / `_SHADOW_HASH_BUDGET_BYTES` | 最多 `64` 处编辑、`20,000` 文件、单文件 `<= 16 MiB`、验证输出 `<= 32 KiB`、Diff `<= 32 KiB`、活区哈希校验预算 `32 MiB` | [`shadow_workspace.py`](../../src/agent_lab/shadow_workspace.py):101–108 | 保障 APFS CoW 写前推测验证有界且不阻塞主回合 |
| **代码索引与地图** | `_MAX_PARSE_BYTES` / `_DEFAULT_MAX_INDEX_FILES` / `_DEFAULT_CHUNK_MAX_LINES` | RepoMap 单文件 `<= 512 KiB`、最多 `512` 文件；Merkle 切片 `<= 80` 行 / `3,200` 字符（重叠 `10` 行）、摘录 `<= 800` 字符 | [`repomap.py`](../../src/agent_lab/repomap.py):25–28 · [`code_index.py`](../../src/agent_lab/code_index.py):182–186 | 防大生成文件拖垮 AST 解析与索引库膨胀 |
| **检查点与流式扇出** | `_MAX_BLOB_BYTES` / `_PG_NOTIFY_DEFAULT_MAX_BYTES` | 单文件写前快照 `<= 16 MiB`；PG `NOTIFY` 单帧 `<= 7,500 B`（遵守 PG 8,000 B 硬限）；每 Run 环形缓冲 `256` 帧 | [`checkpoints.py`](../../src/agent_lab/checkpoints.py):50 · [`streaming.py`](../../src/agent_lab/streaming.py):51 | 保障 `/rewind` 快照可控与 PostgreSQL 跨 Pod 通知永不因超长静默丢帧 |
| **生命周期钩子与触发器** | `_MAX_HOOK_TIMEOUT` / `_MAX_HOOK_STDOUT_BYTES` / `_MAX_PROMPT_CHARS` | 钩子超时 `[0.2s, 60s]`（默认 `10s`）、stdout `<= 128 KiB`；触发器 Prompt `<= 8,000` 字符、Cron 缓存 `<= 512` | [`hooks.py`](../../src/agent_lab/hooks.py):56–59 · [`triggers.py`](../../src/agent_lab/triggers.py):106–112 | 避免慢钩子卡死工具执行与恶意 Webhook 注入超大载荷 |
| **回归飞轮与 A2A** | `_MAX_STORED_EVENTS` / `_MAX_A2A_ATTACHMENT_BYTES` | 单采集用例 `<= 500` 事件、`<= 200` 工具调用、回答 `<= 2,000` 字符；A2A 附件 `<= 6 MiB`、AgentCard `<= 256 KiB` | [`eval_flywheel.py`](../../src/agent_lab/eval_flywheel.py):121–123 · [`a2a.py`](../../src/agent_lab/a2a.py):122–125 | 保证离线回归仓库轻量化与跨智能体协议载荷受控 |

---

### 6.4 常用运维与提交前门禁命令速查

```bash
# 1. 全量离线门禁（对应 CI test + frontend-gate）
uv run --no-sync ruff check src tests scripts
uv run --no-sync pytest -q                                       # 617 passed, 1 skipped
uv run --no-sync agent-lab eval --suite all --report reports/eval-all.json   # 17/17
uv run --no-sync agent-lab kv-benchmark --report reports/kv-benchmark.json
node --check web/app.js

# 2. 真实容器隔离烟雾与 5 任务编码基准（需本地已拉取 python:3.12-slim）
uv run --no-sync python scripts/sandbox_smoke.py
uv run --no-sync agent-lab model-eval --provider reference --report reports/model-eval.json

# 3. 线上轨迹评分与 Trace-to-Eval 脱敏采集
uv run --no-sync agent-lab eval --grade-run <run_id_or_prefix>
uv run --no-sync agent-lab eval --harvest --limit 50 --min-friction 1

# 4. 生产双进程 HA 启动与资源巡检（示例占位符，请替换为部署密钥管理器注入值）
AGENT_LAB_TOKEN="<YOUR_ADMIN_TOKEN>" AGENT_LAB_DISPATCH=queue \
  uv run --no-sync agent-lab serve --host 0.0.0.0 --port 8765
uv run --no-sync agent-lab worker --metrics-port 9101
uv run --no-sync agent-lab doctor && uv run --no-sync agent-lab gc --dry-run
```

---

### 跨卷导航

- 返回大典总导航：[7 卷全栈工程设计大典总索引 (`docs/design/README.md`)](README.md)
- 上一卷：[Vol 06 — 控制平面 API、CLI、REPL、Web 工作台与矢量图渲染](06-api-cli-repl-and-web-workbench.md)
- 相关前置卷：[Vol 01 运行时与上下文](01-runtime-control-flow-and-context.md) · [Vol 03 工具沙箱与安全](03-tools-sandbox-execution-and-security.md) · [Vol 05 存储 HA 与可观测性](05-storage-ha-distributed-and-observability.md)
- 配套基线记录：[验证记录 (`docs/validation.md`)](../validation.md) · [KV Cache 消融专题 (`docs/kv-cache-harness.md`)](../kv-cache-harness.md)

