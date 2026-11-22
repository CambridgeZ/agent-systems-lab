# 验证记录

日期：2026-11-17。此记录区分工程回归、模型能力与代码执行环境；15 项生产级高可用加固的完整设计与验证映射见 [production-ha-hardening.md](production-ha-hardening.md)。

## 本机已完成

| 验证 | 结果 | 覆盖内容 |
|---|---|---|
| `pytest -q` | **401 passed** | 覆盖全部 15 项生产级高可用加固维度、本地直连文件系统与进程组脱敏 `bash_exec`、三级权限策略（`ask`/`auto-edit`/`full-auto`）、桌面级 GUI Computer Use 与多模态视觉注入、动态 `subagent_run` 与外部 `McpClientManager`、多轮交互工作台（`agent-lab code`）以及双层分层记忆 + 互链 Wiki + 四阶段离线 Dreaming 引擎 |
| `pytest tests/test_git_ha.py tests/test_production_api_ha.py -q` | **16 passed（8 + 8）** | 新增专项高可用回归套件：`test_git_ha.py` 验证跨节点 `workspace_archives` 打包物化/解包防炸弹、单调世代拦截、原子 Git origin 初始化、全扩展名凭证拦截与源仓零污染；`test_production_api_ha.py` 验证多租户 RBAC、异人审批、滑动限流、请求体 413 拦截、`/healthz` & `/readyz` & `/metrics`、W3C `traceparent` 透传、脱敏结构化 `error_detail`、单事务 `approve_and_enqueue` 与运行中协作取消 |
| `ruff check src tests scripts` | 通过 | Python 静态检查（含新增 `observability.py`、`test_git_ha.py`、`test_production_api_ha.py`） |
| `node --check web/app.js` | 通过 | 浏览器脚本语法（含单飞互斥轮询、`after=eventCursor` 增量事件游标与按需 RepoMap 缓存） |
| `uv lock --check` | 通过 | 项目声明与锁文件一致 |
| `agent-lab eval` | **10 / 10 场景通过** | 临时工作区中的审批、拒绝、恢复、冲突、幂等、预算与三种模式 |
| `agent-lab kv-benchmark` | **累计块命中率 40.48% → 78.96%** | 2 子代理 + 16 步主循环离线消融：末步命中率达 **88.14%**，未命中 Prefill 减少 **66.82%**，预估有效输入成本节省 **57.27%**，滑动重排从 8 次降至 2 次（详见 [kv-cache-harness.md](kv-cache-harness.md) 与 [kv-cache-ablation-sample.json](kv-cache-ablation-sample.json)） |
| `agent-lab model-eval --provider reference` | **5 / 5 真实容器基准通过** | 在本机 Docker Desktop（27.4.0）下逐任务检出 Git worktree、运行 21 个受保护 unittest 用例、覆盖跨文件原子修改与 1 次失败后自动修复、验证通过后创建 Git commit 并推进 `base_commit`（显式标注 `benchmark_harness_verification_only`） |
| `docker build -t agent-systems-lab .` | 通过 | 构建包含最小化 `git`、`uv sync --locked --no-dev`、非 root 账户 `learner`、内置 `/healthz` `HEALTHCHECK` 与非回环强制鉴权门禁的生产镜像 |
| CLI 与队列服务操作 | 通过 | `serve --dispatch queue` + `agent-lab worker` 心跳消费与优雅停机、跨文件 `multi_file` 审批与恢复、`agent-lab gc` 资源与孤儿容器回收、trace/calls/result 查询 |
| 浏览器与工作台接口 | 通过 | 队列 Worker 状态栏、任务模板选择、多语言 RepoMap 面板、带文件哈希绑定的工作记忆、跨文件增删改 unified diff、独立 `verification` 状态与增量事件时间线 |
| MCP 协议集成 | 通过 | 独立进程 JSON-RPC 初始化、工具发现、读取/检索、路径越界拒绝、没有写工具 |
| Anthropic / OpenAI adapter 合约 | 通过模拟测试 | Extended Thinking 缓存断点兼容、429 `Retry-After` 退避与备用网关切换、SSE 流式工具块拼装、截断 JSON 降级与加权缓存 token 折算；不等于真实在线计费联调 |

运行环境：macOS arm64，Python 3.11.5，Docker Desktop 27.4.0，支持 `inline` 单进程与 `queue` + `LeaseWorker` 双进程模式。完整可复演报告见 [evaluation-sample.json](evaluation-sample.json) 与 [model-eval-sample.json](model-eval-sample.json)；报告均不含密钥或个人仓库内容。

测试产生 1 条依赖弃用警告：当前锁定的 Starlette 提示后续更换测试 HTTP 客户端。测试现已通过，该警告不影响本次功能验证；升级依赖时应重新验证兼容性。

## 初始版本的远端 CI 证据

[GitHub Actions 运行记录](https://github.com/CambridgeZ/agent-systems-lab/actions/runs/42978975700) 验证代码提交 `a7d3921`：

- Ubuntu / Python 3.11：108 项测试通过、静态检查通过、10 个隔离评测场景通过。
- Ubuntu / Python 3.12：相同检查全部通过。
- `sandbox-smoke`：真实拉取 `python:3.12-slim` 并执行容器测试，确认原始 bug 会失败，经过审批后同一组受保护测试通过。

该证据只覆盖内置示例与指定容器配置，不证明任意代码安全或任意仓库兼容。

## 额外环境与准确边界

- **真实商业模型得分未冒充**：`model-eval --provider reference`（5/5）仅验证基准任务、Git worktree、Docker 测试套件与修复回环本身通畅；真实 Claude 或 OpenAI 模型的 `pass@1`、修复收益与 token 成本需在配置自有 API 密钥后实测报告。
- **macOS Docker Desktop 与原生 Linux 内核差异**：本机 Docker Desktop（LinuxKit VM）已实测通过全部单文件与多文件容器测试套件；而 `scripts/sandbox_smoke.py` 中的内核级探针专门针对原生 Linux CI 要求 `Seccomp == 2` 且网络命名空间仅含 `lo`（macOS LinuxKit VM 默认 `Seccomp: 0` 且内核静态编译了未启用的 `tunl0/sit0` 隧道设备），因此该严格探针保持不放宽，继续在 Ubuntu GitHub Actions CI 中强制门禁。
- CI 包含 Python 3.11 / 3.12 的回归任务，以及真实 Docker smoke job；本次均已通过，后续提交仍以对应 GitHub Actions 结果为准。
- 应用镜像 `docker build -t agent-systems-lab .` 已在本机实测构建成功；应用容器自身绝不挂载宿主机 `/var/run/docker.sock`，容器内触发沙盒验证通过 `AGENT_LAB_SANDBOX_URL` + `RemoteHTTPSandboxBackend` 对接独立受管沙盒池（`/api/internal/sandbox/execute`）。
- 离线 demo 是确定性脚本；10 / 10 只证明当前 harness 场景回归，不代表模型成功率或真实缺陷覆盖率。
- AST 算术 grader 不 import/exec 生成代码，只解释目标函数最后一条返回表达式的纯算术子集；不证明整个函数的控制流或副作用正确。
- 存储层同时支持单机 SQLite WAL（含 `state_version` CAS 与 `summary_json` 投影）及 `Storage.from_dsn("postgresql://...")`（`FOR UPDATE SKIP LOCKED` 队列抢占 + `workspace_archives` 跨节点工作区物化同步）；当前自动化回归使用 SQLite 与契约桩验证 SQL 转换和并发隔离，真实跨主机 PostgreSQL 集群压测仍需按目标生产拓扑独立验收。

## 专门复核的失败情况

审批前不写入；拒绝保持原样；原文变化时拒绝覆盖；跨文件变更集中途断电可通过 `.agent-apply-*.json` 前滚恢复；重复批准幂等；审批与重入队单事务原子提交；旧提案不能跳过当前提案；任务创建者在开启异人复核时不能自批；未知/越界工具不能执行；超大工具参数与结果经二级投影不触发上下文死锁且不留下孤立工具结果；日志与错误事件递归脱敏不泄露原始凭证；预算含并发预留且恢复不清零；毒丸任务超限转入 `dead_letter`；只读 worker 不能提出写入补丁。

提案创建使用调用操作幂等键，结果提交与消息 checkpoint 分开保存的窗口有故障注入覆盖。文件系统、数据库与外部模型网关仍不是同一个分布式事务；不承诺任意外部副作用 exactly-once，也未完成公网恶意代码抗容器逃逸审计。

## 本轮增强的专门验收

- **长上下文与 KV 保活**：BPE Token + 字符双预算统计完整 system/tools/messages；紧急尾部投影防大补丁死锁；中途动态 `skill_load` 路由至尾部 `tail_state` 且前 128 个根前缀块钉扎防驱逐；转义密集分页不递归投影。
- **Skill 与工作记忆**：有界元信息读取、缓存失效、章节快照/版本、去重与卸载；`working_memory` 严拒失败工具调用、绑定 `bound_files` 文件哈希自动标 `stale`、支持条目删除与满额自动淘汰陈旧事实。
- **工具状态、多语言索引与 Git 高可用**：`RepoMapIndex` 线程安全锁 + Python/TS/JS/Go/Rust/Java 六语言解析 + `.gitignore` 过滤；`repo_read` 行区间读取；`propose_changeset` 支持文件增删改与 `.toml/.json/.yaml` 校验；`test_git_ha.py` 覆盖跨节点归档同步与 Git worktree 基线推进。
- **同机与跨进程栅栏**：`exclusive_run`（POSIX `flock`，兼容 `0711` 受限父目录）与数据库 `expected_epoch` / `state_version` CAS 双向互锁；`SIGTERM` 优雅归还队列；`test_production_api_ha.py` 覆盖 RBAC、限流、探针、指标与可观测性闭环。
- **沙盒单测（`tests/test_sandbox.py`，45 项）**：无需 Docker 即可离线复验双进程 Supervisor + Child 匿名管道 HMAC 回执、stdout `[SPOOF_BLOCKED_RECEIPT_]` 欺骗清洗、`sys.addaudithook` 阻断 `sys._getframe`/`ctypes`/`gc`、`SANDBOX_FAILURE_CODES` vs `SANDBOX_INFRA_CODES`（含 exit-137 `oom_killed`）、`src/` 包布局与非秘密测试夹具、纯标准库 `def test_*()` / `async def test_*()` 加载、容器标签与 `reap_orphan_containers()`、`EBUSY` 清理异常保留原始结论以及 `RemoteHTTPSandboxBackend` 契约。它们不替代真实 Linux 内核隔离探针。
- **学习资料与架构文档**：共 88 道连续编号问题；两个压缩/分页实验实际运行通过；新增 [production-ha-hardening.md](production-ha-hardening.md) 详述 15 项生产级演进；源码链接检查通过。

增强后的真实容器检查由 `scripts/sandbox_smoke.py` 执行：业务bug/审批修复、只读与禁网探针、权限/seccomp、凭证不暴露、超时、超量输出、提前退出和清理。对应脱敏报告由CI上传为 `sandbox-smoke` artifact；本机仍为 `sandbox_unavailable`。本轮CI已全部通过，具体证据见下方。

## 本轮增强的远端验证结果

本次 P0/P1 全量升级的 [GitHub Actions 运行 42994021180](https://github.com/CambridgeZ/agent-systems-lab/actions/runs/42994021180)（对应代码提交 `cb969de`；上一阶段见 [42982653696](https://github.com/CambridgeZ/agent-systems-lab/actions/runs/42982653696)）在 Ubuntu Linux 上全部通过：

- Ubuntu / Python 3.11 与 3.12：全部 **273 项测试**、Ruff 检查（`src tests scripts`）和 10 个隔离离线场景通过。
- 真实Docker：原始bug被捕获，审批后的修复通过四项受保护测试。
- 真实 5 任务编码基准（`agent-lab model-eval --provider reference`）：在 Linux Docker 中完成基线红灯校验、跨文件 worktree 修复、测试失败回环修复与验证门控 Git commit（5/5 通过，产物上传至 `sandbox-smoke` artifact）。
- 真实隔离探针：非root、根/工作区只读并拒绝写入、仅loopback且外连失败、无宿主环境/哨兵文件/Docker socket、capabilities为零、NoNewPrivs与seccomp生效。
- 5秒执行超时、2KiB输出限额、`os._exit(0)`提前退出分别产生预期状态；六个case均确认清理完成。

脱敏原始报告见 [sandbox-validation-sample.json](sandbox-validation-sample.json)，也可下载该run的 `sandbox-smoke` artifact。报告以每次执行的镜像ID、源码快照和policy哈希标明输入；不保存原始容器日志或宿主哨兵值。本次真实探针不构成对未知内核漏洞、恶意同进程测试伪造或所有资源压力情形的安全保证。
