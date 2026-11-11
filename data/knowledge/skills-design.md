# Skills 与渐进披露

本笔记为原创实现说明。
阅读背景：[Equipping agents for the real world with Agent Skills](https://www.anthropic.com/research/equipping-agents-for-the-real-world-with-agent-skills)。

## 先发现，再加载

启动时只给模型 Skill 的 name 与 description。模型选中 `code-review` 后再读取
全文，这样不必为每次任务预载所有领域流程。Skill 更像可版本化的操作说明，
工具是可执行接口；两者都不等于一个拥有独立目标和运行状态的子 Agent。

## 本项目的实现范围

SkillCatalog 读取 `skills/<name>/SKILL.md`，限制名称与大小、拒绝符号链接，
只解析单行 name/description 元数据。它没有执行技能脚本、解析完整 YAML 或实现
标准生态全部功能。正文是可信仓库中的应用指令，优先级仍低于系统安全策略。

## 代码审查技能应指导证据收集

先读需求与相关代码，再核验测试和输入边界，最后提出最小修改与验收清单。
当语法检查通过但测试未运行时，应如实写出这一差别。技能的价值应通过减少
遗漏或改善任务结果来评估，而不是用文件数量证明系统更强。
