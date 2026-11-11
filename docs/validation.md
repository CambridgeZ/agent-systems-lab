# 验证记录

日期：2026-11-11。此记录区分工程回归、模型能力与代码执行环境。

## 本机已完成

| 验证 | 结果 | 覆盖内容 |
|---|---|---|
| `pytest -q` | **273 passed** | 在原有 255 项基础上新增 Git worktree 与跨文件原子提案、测试驱动有界修复循环、队列租约续期与 `epoch` fencing、AST RepoMap 增量缓存、带 SHA-256 来源校验的工作记忆、5 任务编码基准与 OpenAI/Anthropic 适配器契约 |
| `ruff check src tests scripts` | 通过 | Python 静态检查 |
| `node --check web/app.js` | 通过 | 浏览器脚本语法 |
| `uv lock --check` | 通过 | 项目声明与锁文件一致 |
| `agent-lab eval` | **10 / 10 场景通过** | 临时工作区中的审批、拒绝、恢复、冲突、幂等、预算与三种模式 |
| `agent-lab model-eval --provider reference` | **5 / 5 真实容器基准通过** | 在本机 Docker Desktop（27.4.0）下逐任务检出 Git worktree、运行 21 个受保护 unittest 用例、覆盖跨文件原子修改与 1 次失败后自动修复、验证通过后创建 Git commit（显式标注 `benchmark_harness_verification_only`） |
| `docker build -t agent-systems-lab .` | 通过 | 构建包含 Git、锁定依赖与非 root 账户 `learner` 的完整应用镜像 |
| CLI 与队列服务操作 | 通过 | `serve --dispatch queue` + `agent-lab worker` 心跳消费、跨文件 `multi_file` 审批与恢复、trace/calls/result 查询 |
| 浏览器与工作台接口 | 通过 | 队列 Worker 状态栏、任务模板选择、RepoMap 面板、带来源工作记忆、跨文件 unified diff 与独立 `verification` 状态展示 |
| MCP 协议集成 | 通过 | 独立进程 JSON-RPC 初始化、工具发现、读取/检索、路径越界拒绝、没有写工具 |
| Anthropic / OpenAI adapter 合约 | 通过模拟测试 | 请求参数、工具调用块、重试与 token usage 转换；不等于真实在线计费联调 |

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
- 应用镜像 `docker build -t agent-systems-lab .` 已在本机实测构建成功；注意应用容器本身不挂载宿主机 Docker socket，若需容器内触发沙盒执行应对接独立远程执行池。
- 离线 demo 是确定性脚本；10 / 10 只证明当前 harness 场景回归，不代表模型成功率或真实缺陷覆盖率。
- AST 算术 grader 不 import/exec 生成代码，只解释目标函数最后一条返回表达式的纯算术子集；不证明整个函数的控制流或副作用正确。
- SQLite `run_leases` + `epoch` fencing 完整覆盖单机多进程 Worker 崩溃接管与僵尸写入拦截，但跨主机多节点部署仍需替换为 Postgres/etcd 等共享事务存储。

## 专门复核的失败情况

审批前不写入；拒绝保持原样；原文变化时拒绝覆盖；重复批准幂等；审批落盘但 run 状态未保存时可协调恢复；旧提案不能跳过当前提案；未知/越界工具不能执行；上下文裁剪不留下孤立工具结果；网络异常不记录原始凭证细节；预算恢复不清零；只读 worker 不能提出写入补丁。

提案创建使用调用操作幂等键，结果提交与消息 checkpoint 分开保存的窗口有故障注入覆盖。文件系统、SQLite 与模型服务仍不是一个事务；不承诺任意外部副作用 exactly-once，未完成多机故障切换、生产多租户负载或抗逃逸审计。

## 本轮增强的专门验收

- **长上下文**：统计完整 system/tools/messages JSON；保留原任务/执行状态/活动Skill/最新原子组；超预算停止；转义密集分页不再次递归投影。
- **Skill**：有界元信息读取、缓存失效、章节快照/版本、去重与卸载、保存快照后结果提交前崩溃重放。
- **工具状态**：参数身份与跨run隔离、空结果、完整结果提交后不重复执行、worker报告提交后不重复生成、未知容器执行不自动重跑、提案幂等与版本冲突。
- **同机互斥**：独立进程争用、终止后内核释放；协程取消时等同步工具落盘再放锁；数据库加法迁移并发启动。
- **沙盒单测**：37项无需Docker的策略与故障测试，包括只读快照、敏感路径排除、链接/FIFO拒绝、镜像ID固定、输出读取硬上限、超时/清理与测试回执。它们不证明内核隔离生效。
- **学习资料**：共88道连续编号问题；两个压缩/分页实验实际运行通过；源码链接检查通过。

增强后的真实容器检查由 `scripts/sandbox_smoke.py` 执行：业务bug/审批修复、只读与禁网探针、权限/seccomp、凭证不暴露、超时、超量输出、提前退出和清理。对应脱敏报告由CI上传为 `sandbox-smoke` artifact；本机仍为 `sandbox_unavailable`。本轮CI已全部通过，具体证据见下方。

## 本轮增强的远端验证结果

上一阶段增强的 [GitHub Actions 运行 42982653696](https://github.com/CambridgeZ/agent-systems-lab/actions/runs/42982653696)（提交 `8f20646`）及本次 P0/P1 全量升级后的最新 `Validate` 工作流均在 Ubuntu Linux 上验证：

- Ubuntu / Python 3.11 与 3.12：全部 **273 项测试**、Ruff 检查（`src tests scripts`）和 10 个隔离离线场景通过。
- 真实Docker：原始bug被捕获，审批后的修复通过四项受保护测试。
- 真实 5 任务编码基准（`agent-lab model-eval --provider reference`）：在 Linux Docker 中完成基线红灯校验、跨文件 worktree 修复、测试失败回环修复与验证门控 Git commit（5/5 通过，产物上传至 `sandbox-smoke` artifact）。
- 真实隔离探针：非root、根/工作区只读并拒绝写入、仅loopback且外连失败、无宿主环境/哨兵文件/Docker socket、capabilities为零、NoNewPrivs与seccomp生效。
- 5秒执行超时、2KiB输出限额、`os._exit(0)`提前退出分别产生预期状态；六个case均确认清理完成。

脱敏原始报告见 [sandbox-validation-sample.json](sandbox-validation-sample.json)，也可下载该run的 `sandbox-smoke` artifact。报告以每次执行的镜像ID、源码快照和policy哈希标明输入；不保存原始容器日志或宿主哨兵值。本次真实探针不构成对未知内核漏洞、恶意同进程测试伪造或所有资源压力情形的安全保证。
