# 可复演的代码修复演示

演示目标：修复示例 `pricing.py` 把百分数当小数使用的问题。输入 `subtotal=200`、`discount_percent=10`，期望结果为 `180`。每次新任务都会复制独立仓库，不会直接修改 `data/sample_repo` 模板。

本页演示离线 `DemoProvider`，无需 API key。它按固定脚本选择工具，用来检查执行框架；不能拿这一次成功说明真实模型可自主修复任意代码。

## 1. 准备并启动网页

从仓库根目录执行：

```bash
uv sync --locked --extra dev --extra mcp
uv run --no-sync agent-lab serve
```

打开 [本机工作台](http://127.0.0.1:8765)。默认监听本机，provider 为 `demo`。

1. 保留预填任务“修复 pricing.py 的百分比折扣 bug，并验证修改”，选择 `Agent` 并开始运行。
2. 在轨迹中展开 `repo_read`、`knowledge_search` 与 `propose_patch`。观察实际参数和返回值。
3. 等待状态变成“等待审批”。此时只有提案，任务仓库尚未写入修改。
4. 在补丁中确认唯一变化是把 `discount_percent` 改为 `discount_percent / 100`。
5. 先选择“拒绝补丁”。任务会结束，提案记录为拒绝，仓库内容保持原状。
6. 新建同样任务，这次选择“批准并继续”。随后查看 `approval`、`verify_python` 和 `sandbox_test` 的结果。

`verify_python` 的成功仅表示 AST 可解析。若 Docker 未启动或本地没有指定镜像，`sandbox_test` 会明确报告 `sandbox_unavailable`、`tests_executed=false`。最终流程完成不等于单元测试全部通过。

页面右侧可以显式保存一条跨会话笔记。记忆不会自动写入系统提示；真实模型可选择调用 `memory_get`。离线 demo 不保证使用每种辅助工具。

## 2. 用 CLI 展示审批与恢复

另开终端，从同一个仓库根目录运行：

```bash
uv run --no-sync agent-lab --state .agent-lab/learning demo
```

JSON 输出包含 `run.id`、`run.status`、`proposals[0].id` 和目标内容。复制实际 ID 后执行；下面的值只是占位符：

```bash
uv run --no-sync agent-lab --state .agent-lab/learning list
uv run --no-sync agent-lab --state .agent-lab/learning trace RUN_ID
uv run --no-sync agent-lab --state .agent-lab/learning resume RUN_ID
uv run --no-sync agent-lab --state .agent-lab/learning approve RUN_ID PROPOSAL_ID
```

等待审批时 `resume` 仍返回等待状态，不会绕过决定。`approve` 会从 SQLite 加载原提案和原工作区，应用后继续运行。如果要拒绝，使用：

```bash
uv run --no-sync agent-lab --state .agent-lab/learning approve RUN_ID PROPOSAL_ID --reject
```

`--state` 是全局选项，放在 `demo`、`approve` 等子命令之前。新终端继续使用相同 state 目录，才能找到原运行。

快速复演整个内置示例也可显式授权自动应用：

```bash
uv run --no-sync agent-lab demo --auto-approve

# 体验基于真实 Git worktree 的跨文件（checkout.py + discounts.py）原子补丁、容器套件验收与自动提交
uv run --no-sync agent-lab demo --template multi_file --auto-approve
```

这个参数仅用于 `demo` 子命令的内置示例，不是给真实模型开启通用自动审批。面试展示审批机制时仍建议手动操作。

若想演示生产形态的 **API 控制面与持久队列 Worker 分离**（含租约心跳、过期接管与单调递增 `epoch` 护栏），可在两个终端分别运行：

```bash
# 终端 1：启动 Web 工作台，将任务写入持久队列
uv run --no-sync agent-lab serve --dispatch queue

# 终端 2：启动独立 LeaseWorker 领取任务并定期续租
uv run --no-sync agent-lab worker --worker-id local-worker-1
```

随后在工作台顶栏即可看到在线 Worker、当前任务的 `epoch` 世代号、RepoMap 符号摘要与带来源校验的结构化工作记忆。

## 3. 比较三种模式

```bash
uv run --no-sync agent-lab demo --mode agent
uv run --no-sync agent-lab demo --mode workflow
uv run --no-sync agent-lab demo --mode parallel
```

三次都是新任务，仍需分别审批。观察差异：

- `agent` 直接进入工具循环。
- `workflow` 先产生 `workflow_retrieved`，再进入共享循环；不是所有步骤都由固定 DAG 控制。
- `parallel` 产生只读 worker 的工具事件，完成后再由主流程提出补丁；两种角色由代码预设。

模型轮次统计包括 worker 调用。demo 的逻辑固定，因此只能用来比较流程与开销，不能证明多 Agent 提升修复成功率。

## 4. 通过 HTTP 接口观察同一机制

先保持服务在运行。创建任务会返回 HTTP 202 与 run ID，后台执行不会因 HTTP 响应结束而停止：

```bash
curl -sS http://127.0.0.1:8765/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"prompt":"修复 pricing.py 的百分比折扣 bug，并验证修改。","mode":"agent"}'
```

把响应中的 ID 替换进下面路径：

```bash
curl -sS http://127.0.0.1:8765/api/runs/RUN_ID
curl -sS http://127.0.0.1:8765/api/runs/RUN_ID/events
curl -N http://127.0.0.1:8765/api/runs/RUN_ID/stream
```

`GET /api/runs/RUN_ID` 的 `proposals` 包含提案 ID。等状态为 `waiting_approval` 后提交具体决定：

```bash
curl -sS http://127.0.0.1:8765/api/runs/RUN_ID/proposals/PROPOSAL_ID \
  -H 'Content-Type: application/json' \
  -d '{"approved":true}'
```

审批接口也返回 202，需继续查询状态与事件。拒绝时把 JSON 布尔值改为 `false`。不能用字符串 `"true"` 代替布尔值。对不存在的提案、其他运行的提案或非等待审批状态，API 会拒绝。

显式记忆接口示例：

```bash
curl -sS http://127.0.0.1:8765/api/memory \
  -H 'Content-Type: application/json' \
  -d '{"key":"review_preference","value":"修改后分别报告语法与运行测试状态。"}'
curl -sS http://127.0.0.1:8765/api/memory
```

如设置了 `AGENT_LAB_TOKEN`，以上 `/api` 请求需要额外的 `Authorization: Bearer ...` 头；网页可以用侧栏钥匙按钮在当前页面内存中配置。不要把真实 token 写入命令示例、截图、Git 或评测报告。浏览器跨源请求会被拒绝。

## 5. 接入真实 Claude

在本机创建 `.env`，参考 [`.env.example`](../.env.example)，填写：

```dotenv
AGENT_PROVIDER=anthropic
ANTHROPIC_API_KEY=填写你自己的密钥
ANTHROPIC_MODEL=填写账户当前可用的模型ID
```

使用以下命令加载环境。程序不会自动读取 `.env`：

```bash
uv run --no-sync --env-file .env agent-lab serve
```

CLI 指定 provider：

```bash
uv run --no-sync --env-file .env agent-lab run \
  '修复 pricing.py 的百分比折扣 bug，并验证修改。' \
  --provider anthropic --mode agent
```

真实调用可能计费。模型仍只能使用已定义工具，仍在内置示例的独立副本中工作，写入仍需人工审批。当前交付未使用真实密钥完成服务联调，不能把离线结果作为模型效果报告。

## 6. 可选 Docker 测试

本项目没有宿主机执行生成代码的降级路径。需要容器验证时，先自行启动 Docker 并准备镜像：

```bash
docker pull python:3.12-slim
```

重新运行并批准内置任务后检查 `sandbox_test`。工具仅使用本地镜像，限制网络、挂载、权限、资源与时间，并校验验收测试版本。镜像下载发生在这个手动准备步骤，不由模型工具自动执行。

若本机 Docker 未启动，网页会明确显示 `sandbox_unavailable`，绝不退回宿主机执行待测代码；启动 Docker Desktop 并拉取 `python:3.12-slim` 后，本机与 GitHub Actions Linux 环境均会真实运行容器测试并在通过后对 Git worktree 执行验证门控提交。证据见 [验证记录](validation.md)。

## 7. MCP 与评测

启动可选只读 MCP stdio server：

```bash
uv run --no-sync python -m agent_lab.mcp_server --workspace data/sample_repo
```

它等待 MCP 客户端按协议通信，不是交互式终端聊天命令。不要向 stdout 打印调试日志；退出可用 Ctrl-C。服务器暴露四个只读检索工具，没有补丁、审批或 shell 工具；主应用内部目前直接调用 registry，没有经过 MCP client。

运行工程回归：

```bash
uv run --no-sync pytest -q
uv run --no-sync agent-lab eval --report reports/eval.json

# 在已准备 python:3.12-slim 镜像的环境下运行 5 任务真实仓库编码基准流水线验证
uv run --no-sync agent-lab model-eval --provider reference --report reports/model-eval.json
```

评测为每个任务复制干净工作区，检查审批前后文件、拒绝、恢复、版本冲突、重复审批、预算和不同模式。算术 grader 通过白名单 AST 验证表达式，不导入生成代码，不等于运行整个函数的单元测试。

演示结束时可以把实际报告中的四项分开说明：**流程是否完成、哪些状态约束通过、代码验证到了哪一层、真实模型或容器哪些仍待验证**。
