# 验证记录

日期：2026-11-11。此记录区分工程回归、模型能力与代码执行环境。

## 本机已完成

| 验证 | 结果 | 覆盖内容 |
|---|---|---|
| `pytest -q` | **255 passed** | 检索、完整请求预算与协议、Skill快照/缓存、调用账本、故障恢复、同机互斥/取消、沙盒策略/流式限额、审批、API、真实 MCP stdio |
| `ruff check src tests scripts` | 通过 | Python 静态检查 |
| `node --check web/app.js` | 通过 | 浏览器脚本语法 |
| `uv lock --check` | 通过 | 项目声明与锁文件一致 |
| `agent-lab eval` | **10 / 10 场景通过** | 临时工作区中的审批、拒绝、恢复、冲突、幂等、预算与三种模式 |
| CLI 操作 | 通过 | demo → resume → approve → trace → list → calls → result |
| 浏览器操作 | 通过 | 创建任务、查看 unified diff、批准后完成、显示语法与容器状态 |
| MCP 协议集成 | 通过 | 独立进程 JSON-RPC 初始化、工具发现、读取/检索、路径越界拒绝、没有写工具 |
| Anthropic adapter 合约 | 通过模拟测试 | 请求参数、工具块和 token usage 转换；不等于真实服务联调 |

运行环境：macOS arm64，Python 3.11.5，单进程。完整可复演报告见 [evaluation-sample.json](evaluation-sample.json)；该报告不含密钥或个人仓库内容。

测试产生 1 条依赖弃用警告：当前锁定的 Starlette 提示后续更换测试 HTTP 客户端。测试现已通过，该警告不影响本次功能验证；升级依赖时应重新验证兼容性。

## 初始版本的远端 CI 证据

[GitHub Actions 运行记录](https://github.com/CambridgeZ/agent-systems-lab/actions/runs/42978975700) 验证代码提交 `a7d3921`：

- Ubuntu / Python 3.11：108 项测试通过、静态检查通过、10 个隔离评测场景通过。
- Ubuntu / Python 3.12：相同检查全部通过。
- `sandbox-smoke`：真实拉取 `python:3.12-slim` 并执行容器测试，确认原始 bug 会失败，经过审批后同一组受保护测试通过。

该证据只覆盖内置示例与指定容器配置，不证明任意代码安全或任意仓库兼容。

## 额外环境与准确边界

- **真实 Claude 请求未执行**：交付环境没有 `ANTHROPIC_API_KEY`。配置自己账户可用的模型后，需独立验证编码效果、用量、错误和模型协议。
- **本机 Docker 代码执行未运行**：已安装 CLI，但守护进程未启动；工具实际返回 `sandbox_unavailable`。没有回退到宿主执行代码。
- CI 包含 Python 3.11 / 3.12 的回归任务，以及真实 Docker smoke job；本次均已通过，后续提交仍以对应 GitHub Actions 结果为准。
- Dockerfile 已提供，但本机未构建应用镜像。不要把提供文件视为部署成功。
- 离线 demo 是确定性脚本；10 / 10 只证明当前 harness 场景回归，不代表模型成功率或真实缺陷覆盖率。
- AST 算术 grader 不 import/exec 生成代码，只解释目标函数最后一条返回表达式的纯算术子集；不证明整个函数的控制流或副作用正确。

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

[GitHub Actions 运行42982653696](https://github.com/CambridgeZ/agent-systems-lab/actions/runs/42982653696) 对应代码提交 `8f20646`，全部成功：

- Ubuntu / Python 3.11 与3.12：各255项测试、Ruff检查和10个隔离离线场景通过。
- 真实Docker：原始bug被捕获，审批后的修复通过四项受保护测试。
- 真实隔离探针：非root、根/工作区只读并拒绝写入、仅loopback且外连失败、无宿主环境/哨兵文件/Docker socket、capabilities为零、NoNewPrivs与seccomp生效。
- 5秒执行超时、2KiB输出限额、`os._exit(0)`提前退出分别产生预期状态；六个case均确认清理完成。

脱敏原始报告见 [sandbox-validation-sample.json](sandbox-validation-sample.json)，也可下载该run的 `sandbox-smoke` artifact。报告以每次执行的镜像ID、源码快照和policy哈希标明输入；不保存原始容器日志或宿主哨兵值。本次真实探针不构成对未知内核漏洞、恶意同进程测试伪造或所有资源压力情形的安全保证。
