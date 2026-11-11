# 验证记录

日期：2026-11-11。此记录区分工程回归、模型能力与代码执行环境。

## 本机已完成

| 验证 | 结果 | 覆盖内容 |
|---|---|---|
| `pytest -q` | **108 passed** | 检索、引用、上下文协议、技能、SQLite、工具约束、审批、恢复、预算、API、真实 MCP stdio |
| `ruff check src tests scripts` | 通过 | Python 静态检查 |
| `node --check web/app.js` | 通过 | 浏览器脚本语法 |
| `uv lock --check` | 通过 | 项目声明与锁文件一致 |
| `agent-lab eval` | **10 / 10 场景通过** | 临时工作区中的审批、拒绝、恢复、冲突、幂等、预算与三种模式 |
| CLI 操作 | 通过 | demo → resume → approve → trace → list |
| 浏览器操作 | 通过 | 创建任务、查看 unified diff、批准后完成、显示语法与容器状态 |
| MCP 协议集成 | 通过 | 独立进程 JSON-RPC 初始化、工具发现、读取/检索、路径越界拒绝、没有写工具 |
| Anthropic adapter 合约 | 通过模拟测试 | 请求参数、工具块和 token usage 转换；不等于真实服务联调 |

运行环境：macOS arm64，Python 3.11.5，单进程。完整可复演报告见 [evaluation-sample.json](evaluation-sample.json)；该报告不含密钥或个人仓库内容。

测试产生 1 条依赖弃用警告：当前锁定的 Starlette 提示后续更换测试 HTTP 客户端。测试现已通过，该警告不影响本次功能验证；升级依赖时应重新验证兼容性。

## 远端 CI 已完成

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

文件系统与 SQLite 不是一个事务。提案创建与工具结果 checkpoint 之间仍有可能出现未引用记录；没有声称任意外部副作用 exactly-once，也没有完成生产多租户、负载或对抗性安全审计。
