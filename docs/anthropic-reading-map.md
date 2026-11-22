# Anthropic 20 篇文章 → 本项目实现地图

这份地图把原书单转成可以阅读、运行和追问的工程证据。链接与文章内容核验于 **2026-11-11**。项目是受文章启发的教学实现，未复制文章代码，也未复现 Anthropic 的内部系统或报告指标。

状态含义：**实现**表示能在本仓库找到可运行路径；**简化实现**表示机制可运行，但规模、算法或保障有限；**设计延伸**表示用于面试讨论，当前没有对应产品能力。真实模型路径需要自行配置 Anthropic 凭证；离线 demo 是固定工具调用脚本，不代表模型能力。

| # | 官方文章 | 读完要能解释 | 对应代码与状态 |
|---|---|---|---|
| 1 | [Building effective agents](https://www.anthropic.com/research/building-effective-agents) · 2024-12-19 | 固定路径的 workflow 与模型动态选工具的 agent；先从满足需求的简单方案开始。 | [runtime.py](../src/agent_lab/runtime.py)：`workflow`、`agent`、`parallel` 路径，**实现**。demo 的动态性来自脚本，只有真实 provider 才由模型决策。 |
| 2 | [Building agents with the Claude Agent SDK](https://claude.com/resources/articles/building-agents-with-the-claude-agent-sdk) · 2025-09-29 | 获取上下文、采取动作、验证，再决定是否继续；搜索与工具是循环的组成部分。 | [runtime.py](../src/agent_lab/runtime.py)、[providers.py](../src/agent_lab/providers.py)：**自建循环实现**，使用 Messages API，未使用 Claude Agent SDK。 |
| 3 | [Effective context engineering for AI agents](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents) · 2025-09-29 | 上下文同时包含指令、工具、检索结果和历史；内容多不等于信息有效。 | [context.py](../src/agent_lab/context.py)：完整请求字符预算、原子工具组裁剪、固定状态与大结果分页，**简化实现**。字符预算不等于精确 token 计数；没有语义摘要模型。 |
| 4 | [Writing effective tools for agents — with agents](https://www.anthropic.com/research/writing-tools-for-agents) · 2025-09-11 | 工具是给不确定性调用者使用的接口；描述、输入约束、结果大小和错误都影响效果。 | [tools.py](../src/agent_lab/tools.py)：明确 schema、只读搜索、补丁提案、结构化错误，**实现**。 |
| 5 | [Demystifying evals for AI agents](https://www.anthropic.com/research/demystifying-evals-for-ai-agents) · 2026-01-09 | task、trial、grader、transcript、outcome 各负责什么；验收应检查环境最终状态。 | [evals.py](../src/agent_lab/evals.py)、[data/evals](../data/evals)：隔离工作区与最终文件检查，**实现**。离线得分只证明脚本和 harness 回归。 |
| 6 | [Effective harnesses for long-running agents](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents) · 2025-11-26 | 多会话工作需要可恢复状态、明确验收条件和交接材料；不能只依赖聊天历史。 | [storage.py](../src/agent_lab/storage.py)、[runtime.py](../src/agent_lab/runtime.py)：SQLite checkpoint 与恢复，**简化实现**。没有跨机器租约和分布式执行器。 |
| 7 | [Equipping agents for the real world with Agent Skills](https://www.anthropic.com/research/equipping-agents-for-the-real-world-with-agent-skills) · 2025-10-16 | 先加载技能元信息，在相关时加载操作说明，再按需读资源。 | [skills.py](../src/agent_lab/skills.py)、[skills/code-review](../skills/code-review)：有界元数据目录、章节加载/卸载和固定版本快照，**简化实现**。不执行第三方技能脚本。 |
| 8 | [Introducing the Model Context Protocol](https://www.anthropic.com/news/model-context-protocol) · 2024-11-25 | host/client/server 的边界；MCP 连接工具，不替代 Agent 的规划和权限策略。 | [mcp_server.py](../src/agent_lab/mcp_server.py)：可选 SDK 的 stdio server，**实现**。主循环内部直接调用 registry，未通过 MCP client 远程分发。 |
| 9 | [Code execution with MCP: Building more efficient agents](https://www.anthropic.com/research/code-execution-with-mcp) · 2025-11-04 | 在执行环境中组合工具和过滤中间数据，可减少数据往返模型上下文。 | [tools.py](../src/agent_lab/tools.py)：有界检索、结果投影/分页与可选容器测试，**局部实现**。任意模型生成程序组合 MCP 的机制属于**设计延伸**。 |
| 10 | [Introducing advanced tool use on the Claude Developer Platform](https://www.anthropic.com/research/advanced-tool-use) · 2025-11-24 | 工具发现、程序化调用和使用示例解决的分别是什么成本。 | [tools.py](../src/agent_lab/tools.py)、[skills.py](../src/agent_lab/skills.py)：少量工具与技能按需加载。平台 Tool Search / Programmatic Tool Calling 属于**设计延伸**。 |
| 11 | [How we built our multi-agent research system](https://www.anthropic.com/research/multi-agent-research-system) · 2025-06-13 | 独立研究方向适合并行；子任务独立上下文、输出压缩和协调成本会影响收益。 | [runtime.py](../src/agent_lab/runtime.py)：并行只读分析后汇总，**简化实现**。未证明优于单 Agent，也未复现研究产品。 |
| 12 | [Harness design for long-running application development](https://www.anthropic.com/research/harness-design-long-running-apps) · 2026-03-24 | 规划、生成与评价分离；评估者要有独立标准，避免生成者自评过高。 | [evals.py](../src/agent_lab/evals.py)：外部确定性 grader，**局部实现**。多小时规划器与独立视觉评估 Agent 属于**设计延伸**。 |
| 13 | [Building a C compiler with a team of parallel Claudes](https://www.anthropic.com/research/building-c-compiler) · 2026-02-05 | 多开发者需要任务锁、环境隔离、共享版本控制与持续回归。 | [storage.py](../src/agent_lab/storage.py)、[tools.py](../src/agent_lab/tools.py)：补丁冲突检测，**局部实现**。多容器开发者、Git 任务锁与合并队列属于**设计延伸**。 |
| 14 | [How Anthropic teams use Claude Code](https://claude.com/resources/articles/how-anthropic-teams-use-claude-code) · 2025-07-24 | 仓库理解、问题定位、测试和复盘可以形成一个开发闭环。 | [data/sample_repo](../data/sample_repo)、[web](../web)：可复演的缺陷修复，**实现**。示例是合成仓库。 |
| 15 | [The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory) · 2026-11-06 | 跨会话记忆不同于上下文裁剪；在线显著性笔记与离线 Dreaming（对照 Transcript 纠错、互链 Wiki 合成、断链修复与有界 `MEMORY.md` 预加载）形成完整生命周期。 | [memory.py](../src/agent_lab/memory.py)、[runtime.py](../src/agent_lab/runtime.py)、[storage.py](../src/agent_lab/storage.py)：用户全局 + 项目双层 `MEMORY.md`（`<=200` 行预加载）+ `[[wiki-links]]` 互链笔记 + Wiki/Transcript 混合 RRF 检索 + 每次会话结束自动四阶段 Dreaming，**实现**。 |
| 16 | [Introducing Contextual Retrieval](https://www.anthropic.com/research/contextual-retrieval) · 2024-09-19 | 分块会丢语境；可为块加入背景，再结合词法与语义检索、重排序。 | [retrieval.py](../src/agent_lab/retrieval.py)：带文档背景的块、BM25、词法向量与 RRF，**简化实现**。没有语义 embedding、LLM 生成块上下文或 cross-encoder。 |
| 17 | [Quantifying infrastructure noise in agentic coding evals](https://www.anthropic.com/research/infrastructure-noise) · 2026-02-05 | 模型和 harness 相同，资源限制也会改变失败率和可采用策略。 | [evals.py](../src/agent_lab/evals.py)：报告环境与重复次数；容器有资源上限，**局部实现**。无基础设施噪声的受控实验。 |
| 18 | [Beyond permission prompts: making Claude Code more secure and autonomous](https://www.anthropic.com/research/claude-code-sandboxing) · 2025-10-20 | 文件、网络与凭证必须共同约束；“用户点了批准”不能代替隔离。 | [tools.py](../src/agent_lab/tools.py)：路径限制、审批后写入、可选 Docker 无网络测试，**简化实现**。容器并非完整的多租户安全平台。 |
| 19 | [How we built Claude Code auto mode: a safer way to skip permissions](https://www.anthropic.com/research/claude-code-auto-mode) · 2026-03-25 | 输入注入检测和执行前动作判断是不同层；自动批准必须有明确边界。 | [runtime.py](../src/agent_lab/runtime.py)、[tools.py](../src/agent_lab/tools.py)：确定性工具白名单与人工审批，**局部实现**。没有训练动作分类器或注入检测模型。 |
| 20 | [Scaling Managed Agents: Decoupling the brain from the hands](https://www.anthropic.com/research/managed-agents) · 2026-04-08 | 持久会话、执行循环和执行环境分离，能减少故障耦合并保护凭证。 | [storage.py](../src/agent_lab/storage.py) / [runtime.py](../src/agent_lab/runtime.py) / [tools.py](../src/agent_lab/tools.py)：模块边界**实现**；独立服务部署、vault、远程执行池属于**设计延伸**。 |

## 怎样带着代码读

1. 先读 1、2、4，再跟踪 `AgentRuntime.start → execute → Provider.complete → ToolRegistry.execute`，观察一次工具结果怎样成为下一轮输入。
2. 用第 5、6 篇审视审批暂停与恢复：数据库里究竟保存了什么，恢复会不会再写一次文件？
3. 用第 3、7、15、16 篇解释四种不同的信息来源：当轮上下文、操作技能、跨会话记忆、外部知识。
4. 读 11、12、13 时画出并发边界：哪些操作只读，哪些写入必须串行，谁有权宣布验收完成？
5. 最后读 17—20，给系统补充生产化方案。把方案写成可检验的约束，再决定是否实现。

原书单里的 Claude Code 最佳实践可作为配套操作文档：[Best practices for Claude Code](https://code.claude.com/docs/en/best-practices)。这是持续更新的文档，不计入上面的 20 篇文章。
