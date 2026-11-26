# 14 天读懂并能讲清这个代码 Agent

这条路线以“能运行、能解释、能修改、能验证”为验收标准。每天约 1—2 小时；先保留一份原始运行报告，再开始练习修改。前半段不需要模型密钥。真实模型与容器实验只在具备相应环境时进行，并单独记录尚未完成的验证。

> **配套源码级大典与前沿复盘（当前验证基线：43 个 Python 模块 · 8 步 `schema_migrations` · 617 项离线 + 1 项真实 PostgreSQL 测试 · 17/17 离线评测 · PR `#1` `a674d9f` 11/11 CI & GitGuardian 全绿）**：
> - **[7 卷全栈工程设计大典总导航 (`docs/design/README.md`)](design/README.md)** — 全仓 48 个核心源码/脚本文件（+2 个协议模板）→ 卷次速查表、4 条端到端请求生命周期全链路走读与按角色阅读路线，建议每日精读代码时对照查阅。
> - **[2026 前沿 Agent Harness 架构设计与复盘手册 (`docs/frontier-harness-engineering.md`)](frontier-harness-engineering.md)** — 完成 14 天主线后用于进阶研读 4 批次 12/12 前沿能力（Plan Mode、CaMeL/DLP、PTC、Horizon 战役、Shadow Workspace、Merkle 代码索引、Playwright ARIA、MCP/A2A、Brain/Hands 解耦、Cron/Webhook 与 Trace→Eval 飞轮）。

所有命令从仓库根目录执行。初次准备：

```bash
uv sync --locked --extra dev --extra mcp
uv run --no-sync agent-lab demo
```

普通 `demo` 会停在等待审批。保留输出里的 run ID 与 proposal ID，按 [操作演示](demo-walkthrough.md) 审批。每个新任务都有独立示例仓库，因此再次运行无需手动还原模板。

| 天 | 阅读与运行 | 动手练习 | 当天必须能回答 |
|---|---|---|---|
| 1 | 阅读 [快速开始](../README.md) 与 [`data/sample_repo`](../data/sample_repo)，运行默认 demo | 画出预期金额：`200, 10 → 180`；确认审批前任务副本尚未改变 | 这个项目的业务目标是什么？demo 证明了哪部分？ |
| 2 | 读 [`providers.py`](../src/agent_lab/providers.py)、[`runtime.py`](../src/agent_lab/runtime.py)；配读文章 1、2 | 跟踪一次 `tool_use` 的 id 到对应 `tool_result`；用文字写出停止条件 | 模型何时决定下一步？执行器掌握哪些权力？ |
| 3 | 读工具目录与 JSON Schema；配读文章 4 | 在临时测试中传入未知字段、错误类型、未知工具名，观察结构化错误 | Schema 合法为什么仍可能越权？ |
| 4 | 读 `propose_patch` / `apply_proposal` 与存储提案代码 | 拒绝一次补丁；再创建新任务并批准；对照两个任务的文件状态 | 提案、批准、写入为什么分成三个动作？ |
| 5 | 读 [`tests/test_tools.py`](../tests/test_tools.py)；配读文章 18、19 | 增加一个路径边界测试或硬链接测试；在临时副本中制造版本冲突 | 审批怎样绑定具体版本？为什么不能保证抵御所有 TOCTOU？ |
| 6 | 读 [`storage.py`](../src/agent_lab/storage.py) 与恢复测试；配读文章 6 | 在等待审批时退出进程，再用 CLI 列表与审批命令继续；记录相同提案 ID | 只保存聊天记录为什么不够？文件和数据库之间有哪些崩溃窗口？ |
| 7 | 读 [`context.py`](../src/agent_lab/context.py)；配读文章 3 | 构造同时请求两个工具的历史；缩小字符预算，证明不会留下孤立结果 | 字符预算、token 用量和上下文窗口分别是什么？ |
| 8 | 读 [`retrieval.py`](../src/agent_lab/retrieval.py)；配读文章 16 | 对 10 个工程问题记录 Top-3；新增原创笔记后比较排名；记录无匹配问题 | BM25、TF-IDF cosine、RRF 怎样工作？为什么这不是语义检索？ |
| 9 | 读 [`skills.py`](../src/agent_lab/skills.py)、`memory_get` 与记忆 API；配读文章 7、15 | 增加一个只含元数据和 Markdown 正文的受限技能；显式保存一条用户笔记 | 技能、记忆、当前上下文和检索资料有什么区别？ |
| 10 | 分别运行 `workflow`、`parallel`；读 `_workers`；配读文章 11—13 | 比较三种模式的模型轮次和事件；说明哪些分析可并行，哪些写入需收敛 | 这里是否有动态 planner？为什么不能声称多 Agent 更有效？ |
| 11 | 启动只读 MCP server，读 [`mcp_server.py`](../src/agent_lab/mcp_server.py)；配读文章 8—10 | 用测试客户端发现工具并读取 `pricing.py`；确认无审批与写工具 | MCP server、client、Agent harness 各自做什么？ |
| 12 | 读 [`evals.py`](../src/agent_lab/evals.py) 与任务 JSON；配读文章 5、17 | 运行评测；为一种新的状态边界增加任务与 grader，不要只匹配回答字符串 | 单测、harness 回归、真实模型 benchmark 有何不同？ |
| 13 | 阅读真实 provider 和架构限制；配读文章 20 | 有凭证时在相同任务上跑少量真实模型试验；有 Docker 时独立验证容器路径；否则写明待验证项 | 预算是否是硬费用上限？生产多副本需要补什么？ |
| 14 | 回看文章 14，通读 [面试指南](interview-guide.md) | 录制 5 分钟演示；用 60 秒介绍项目；回答 10 个随机追问并指出代码证据 | 哪些是实际实现，哪些经过验证，哪些只是后续方案？ |

文章编号对应 [20 篇文章阅读地图](anthropic-reading-map.md)。第 10 天的模式命令：

```bash
uv run --no-sync agent-lab demo --mode workflow
uv run --no-sync agent-lab demo --mode parallel
```

第 12 天的回归命令（当前主干基线：**617 项离线测试 + 1 项真实 PostgreSQL 集成测试**、**17 / 17 核心与子系统离线评测**、PR `#1` `a674d9f` **11 / 11 CI & GitGuardian 全绿**）：

```bash
uv run --no-sync pytest -q
uv run --no-sync agent-lab eval --report reports/eval.json
uv run --no-sync agent-lab eval --suite subsystems --report reports/eval-subsystems.json
uv run --no-sync ruff check src tests scripts
```

`--no-sync` 使用已安装的开发与 MCP 依赖，避免后续普通命令改变可选依赖集合。离线评测套件共 **17 / 17** 项（10 个核心控制流场景覆盖审批、恢复、冲突、幂等、预算与三种模式 + 7 大核心子系统离线评测）；它们检验 harness 不变量，不构成真实 LLM 编码能力评测。

## 建议留下的学习成果

每天写一段短记录即可，不需要抄代码：

- 本次运行的输入、mode/provider 和验收条件。
- 一个实际观察到的事件或最终文件状态。
- 一个曾经理解错的点，以及对应代码位置。
- 一项新的测试或实验，注明结果与尚未验证的范围。

第 8 天的检索练习可以做成小表：查询、期望文档、Top-3、是否召回、耗时。后续升级 embedding 时复用同一批标注查询，才能说明变化来自何处。第 10 天比较模式时，demo 的固定行为只能比较工程开销；评估效果需要真实模型、多个任务和重复试验。

## 用代码支撑面试表达

一个可根据实际完成情况调整的 60 秒介绍：

> 我做了一个用于学习代码开发 Agent 的执行框架，围绕读取仓库、检索工程知识、提出补丁、人工审批、应用和验证形成闭环。模型接口、工具权限和持久化状态分开实现。补丁绑定原始文件摘要，应用前检查冲突；运行中保存消息与待执行结果，支持中断后继续。项目还有词法混合检索、按需技能、用户显式记忆、固定双 worker 只读研究和只读 MCP server。我通过单元测试与隔离离线评测验证工程约束；真实模型效果和容器实测会单独报告。

实际面试时删去自己尚未读懂或验证的部分。不要把模板中的“我通过测试”直接替代自己运行和理解测试。

可准备的深入追问：

1. 用一次被拒绝的提案说明模型无法自行获得写权限。
2. 用一次旧版本冲突说明审批对象为何必须绑定版本。
3. 用一个完整工具消息组说明上下文裁剪的不变量。
4. 用一个失败任务说明恢复会不会重置预算、是否重复副作用。
5. 用评测报告解释为什么算术 grader 通过仍不能说完整函数测试通过。

## 从教学项目走向个人作品

完成基础路线后，优先做一个有证据的扩展，再增加功能数量：

| 扩展方向 | 先明确的验收标准 |
|---|---|
| 真正模型评测 | 固定模型与配置，增加不同函数和未见缺陷，逐任务记录成功、失败、用量与环境 |
| 语义检索 | 建立检索标注集，再比较词法基线、embedding、融合与重排序的 Recall@k 和延迟 |
| 通用仓库接入 | 显式选择仓库和隔离副本，定义可编辑范围、测试命令白名单和污染保护 |
| 多步开发 | 使用功能清单、依赖关系和逐项验收，保存可恢复任务状态 |
| 生产任务调度 | 两个执行者竞争同一任务时只允许一个持有有效 lease，过期后可安全接管 |

先做实验，再把数字写入 README 或简历。模型成功率、提效比例、成本下降和生产安全能力都需要对应证据。

## 专项练习：上下文与恢复

完成基础路线后，阅读[长上下文、Skill 与工具状态专项说明](context-skills-state.md)，运行其中两个离线实验，再对照 `tests/test_runtime_optimization_review.py` 与 `tests/test_recovery_integration.py` 解释崩溃窗口。接着准备[进阶题45—80](interview-supplement.md)，优先回答51、53、54、74、75。

阅读[沙盒专题](sandbox-mechanisms.md)，沿 `ToolRegistry.sandbox_test → SandboxRunner.run` 分析：只读副本为什么比直接挂载原仓库更可控，为什么日志也要限制，为什么超时必须清理容器，退出码0为什么不足以证明四个测试执行完成。

## 进阶模块研读：本地电脑操控、外部 MCP 与全生命周期 Dreaming 记忆

完成上述 14 天基础闭环后，按以下顺序精读带教材级中文注释的新增核心模块（完整架构图与高频面试问答见[《本地操控、Computer Use、外部 MCP 与分层 Dreaming 记忆架构详解》](computer-use-and-memory-architecture.md)）：

1. **权限治理与宿主工具平面**：阅读 [`permissions.py`](../src/agent_lab/permissions.py) → [`host_tools.py`](../src/agent_lab/host_tools.py)，配跑 `pytest tests/test_host_tools_and_permissions.py -q`，理解 `ask / auto-edit / full-auto` 三级策略、纯词法优先于 `resolve()` 的软硬链接防御、64KB 管道后台排水与进程组清场。
2. **原生桌面操控与多模态缓存对齐**：阅读 [`computer.py`](../src/agent_lab/computer.py) → [`providers.py`](../src/agent_lab/providers.py)，配跑 `pytest tests/test_computer_use.py -q`，理解 macOS CoreGraphics 64 位指针防截断、ARM64 非变参 ABI、Retina 逻辑点缩放以及尾部瞬态图像块如何保住 100% 历史 KV Cache。
3. **动态子 Agent 与外部 MCP Client**：阅读 [`mcp_client.py`](../src/agent_lab/mcp_client.py) → [`runtime.py`](../src/agent_lab/runtime.py)（`_dispatch_call` 与 `_invoke_subagent_run`）→ [`repl.py`](../src/agent_lab/repl.py)，配跑 `pytest tests/test_subagent_and_mcp_client.py tests/test_multiturn_repl_and_web.py -q`。
4. **海马体—新皮层双系统跨会话记忆**：阅读 [`memory.py`](../src/agent_lab/memory.py) → [`retrieval.py`](../src/agent_lab/retrieval.py)，配跑 `pytest tests/test_agent_memory_and_dreaming.py -q`，理解 `<=200` 行 `MEMORY.md` 头部预载、互链 Wiki + Transcript 混合 RRF 检索，以及无锁 LLM 合成 + 短 `flock` 提交的四阶段离线 Dreaming。
5. **2026 前沿 Harness（Batch 1—Batch 4）、多协议网关与双向 A2A**：通读 [2026 前沿 Agent Harness 架构设计与复盘手册](frontier-harness-engineering.md) 并按 [7 卷全栈工程设计大典总导航](design/README.md) 速查对应模块——依次精读 [`egress_dlp.py`](../src/agent_lab/egress_dlp.py) / [`gateways.py`](../src/agent_lab/gateways.py) / [`a2a.py`](../src/agent_lab/a2a.py) / [`ptc.py`](../src/agent_lab/ptc.py) / [`horizon.py`](../src/agent_lab/horizon.py) / [`shadow_workspace.py`](../src/agent_lab/shadow_workspace.py) / [`code_index.py`](../src/agent_lab/code_index.py) / [`browser.py`](../src/agent_lab/browser.py) / [`execution_backend.py`](../src/agent_lab/execution_backend.py) / [`turn_pipeline.py`](../src/agent_lab/turn_pipeline.py) / [`triggers.py`](../src/agent_lab/triggers.py) / [`eval_flywheel.py`](../src/agent_lab/eval_flywheel.py)，并配跑：
   ```bash
   uv run --no-sync pytest tests/test_batch1_plan_dlp_native_ctx_edit.py tests/test_batch2_ptc_horizon_shadow.py tests/test_batch3_merkle_browser_protocols.py tests/test_batch4_backends_triggers_flywheel_middleware.py tests/test_multi_protocol_gateways.py tests/test_a2a_protocol.py -q
   ```
