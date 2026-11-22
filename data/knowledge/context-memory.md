# 上下文、检查点与长期记忆

本笔记为项目原创设计解释。
阅读背景：[Effective context engineering](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)、
[The components of agent memory](https://www.anthropic.com/research/components-of-agent-memory)。

## 三种状态分别解决什么问题

上下文是当前模型请求实际看到的信息。检查点用于恢复同一次任务的执行状态，
包含消息、工具结果、步骤与待审批提案。长期记忆跨任务保存经过验证的经验，
例如某项目的构建命令。三者有交集，但不能把“裁短了聊天记录”称为获得长期记忆。

## 按协议完整性裁剪

一次 assistant 消息可能请求多个工具，下一条 user 消息需要给出相应的所有
tool_result。裁剪历史时把这两条消息视为不可分割的原子组；单独保留结果会失去
对应 tool_use_id。保留最初任务与最近信息，较早工具往返从推理上下文中移除，
完整记录仍在数据库。最近一组本身超出预算时应显式失败，要求缩小工具输出。

## 字符预算与 token 预算不同

JSON 字符数只是离线可解释的近似限制。汉字、英语、代码和工具 schema 的
token 开销不同，真实计费以服务返回 usage 为准。系统提示词、工具说明、输出预算
也占窗口，不能把剩余窗口全部用来装检索文档。项目不把字符数除以常数伪装成
精确 token 计数，也不把这个裁剪策略称为 LLM 摘要压缩。

## 记忆写入需要证据与离线 Dreaming 整理

“我猜某函数有 bug”不能当作确认事实跨任务传播。系统将记忆分为两层运作：会话内模型通过 `memory_write` 记录带状态（`confirmed` / `hypothesis`）与来源指针的独立主题页，并用 `memory_tag` 为关键节点打上显著性标记；会话结束后离线 `DreamEngine` 自动对照真实 Transcript 与工作区文件 SHA-256 纠错重写漂移笔记、合成互链 Wiki、修复悬空 `[[链接]]`，并将高价值规则提升到不超过 200 行的 `MEMORY.md` 顶层索引中，兼顾跨会话复用与 KV Cache 稳定。
