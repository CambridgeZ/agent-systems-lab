# 从单机教学原型到高可用生产级 AI Agent：15 项核心缺陷与工程架构演进详解

本文档系统阐述 `agent-systems-lab` 如何从单进程本地教学原型演进为具备多节点容错、严格权限边界、真实网关韧性与全链路可观测性的生产级 AI Agent 系统。全文所有类名、函数签名、状态枚举、数据库表结构与错误码均与仓库真实实现严格一致。

---

## 1. 演进总览

### 1.1 单机原型 vs 生产级高可用架构全景对比

在早期单机原型中，API 协程、SQLite 文件锁、宿主机工作区目录与 Docker CLI 紧耦合在单一进程内；一旦遭遇并发启动、Worker 假死（GC 停顿或网络挂起）、多文件补丁中途断电、推理模型开启 Extended Thinking 或外部网关限流，系统就会暴露出锁冲突、脑裂覆盖、上下文死锁或测试伪造等致命隐患。演进后的架构通过**持久状态与计算节点解耦、双原语统一世代栅栏（Epoch Fencing）、双进程 HMAC 防伪沙箱、尾部瞬态 KV Cache 对齐**四大支柱实现高可用闭环。

```mermaid
flowchart TB
    subgraph Legacy["单机教学原型（演进前）"]
        direction TB
        L_UI["浏览器全量轮询 /events"] --> L_API["FastAPI 单进程同步阻塞读写 SQLite"]
        L_API --> L_RT["内联 AgentRuntime（仅靠本机 flock 互斥）"]
        L_RT --> L_WS["宿主机固定目录（跨节点不可见 / 多文件非原子写）"]
        L_RT --> L_LLM["裸调 SDK（无 429 退避 / 无 thinking 兼容 / 漏计缓存写倍率）"]
        L_RT --> L_SBX["单进程容器驱动（待测代码同进程可探测 marker / 容器易泄漏）"]
    end

    subgraph ProdHA["高可用生产级架构（演进后）"]
        direction TB
        P_UI["Web 增量游标轮询 (after=eventCursor) / CLI"] --> P_GW["_GuardMiddleware\nW3C Trace-ID + 多租户 RBAC + 滑动限流 + 413 拦截"]
        P_GW --> P_API["FastAPI 控制面\nasyncio.to_thread / Storage.run_async 非阻塞卸载"]
        P_API <--> P_DB[("Storage 状态层\nSQLite WAL (CAS + summary_json)\n或 PostgreSQL (FOR UPDATE SKIP LOCKED)\n+ workspace_archives 快照表")]
        P_DB <--> P_WK["无状态 LeaseWorker 集群\n独立心跳续租 + SIGTERM 优雅重入队 + max_takeovers 死信隔离"]
        P_WK --> P_FENCE["统一双层栅栏\nexclusive_run(flock) + expected_epoch(DB CAS)\n+ .agent-apply-*.json 写前恢复日志"]
        P_FENCE --> P_CTX["ContextManager + KV Cache Harness\nBPE Token 双预算 + 紧急 tail 投影 + mid_run_dynamic 尾部技能\n+ WorkingMemory bound_files 哈希防投毒 + 多语言 RepoMapIndex"]
        P_CTX --> P_PROV["韧性模型网关层\nAnthropic thinking 断点回退 & 1.25x 缓存计费\nOpenAI 429 Retry-After + 多网关 Failover + SSE 组装 + ProviderRateLimiter"]
        P_FENCE --> P_SBX["SandboxRunner / RemoteHTTPSandboxBackend\n外层 Supervisor + 内层 Child 匿名管道 HMAC 回执\n+ sys.addaudithook + reap_orphan_containers"]
    end

    Legacy ==>|"15 项核心工程演进"| ProdHA
```

### 1.2 七大演进维度与 15 项核心缺陷速查映射表

| 演进维度 | 问题编号 | 核心症状（WHY） | 解决机制（HOW） | 关键代码模块 | 验证测试文件 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **一、状态层解耦与乐观并发** | **#1** | 多进程同刻初始化 WAL 抛 `SQLITE_PROTOCOL`；异常分支连接泄漏；并发 `save_run` 静默丢更新；`list_runs` 反序列化全量消息拖垮吞吐；跨节点 Worker 看不到本地目录；同步 SQLite 阻塞事件循环 | 启动 8 次退避重试；`_connect()` 强制 `finally: con.close()`；`state_version` CAS 乐观锁；`summary_json` 窄列投影；`PostgresBackendConfig` (`FOR UPDATE SKIP LOCKED`)；`workspace_archives` 跨节点归档同步；`Storage.run_async` 与内存缓存 | `src/agent_lab/storage.py`<br>`src/agent_lab/git_repo.py`<br>`src/agent_lab/api.py` | `tests/test_storage.py`<br>`tests/test_git_ha.py`<br>`tests/test_production_api_ha.py` |
| **二、分布式租约、统一栅栏与崩溃恢复** | **#2** | Worker 收到 `SIGTERM` 取消时误标 `failed` 或忽略心跳抢占；毒丸任务致 Worker 无限崩溃接管循环；短暂 SQLite 锁繁忙误杀存活任务 | 区分 `fenced_by_heartbeat` 与外部 `CancelledError`（优雅释放回 `queued`）；`takeover_count >= max_takeovers` 自动转入 `dead_letter`；心跳 `OperationalError` 宽限至 `current_lease_deadline` | `src/agent_lab/lease.py`<br>`src/agent_lab/storage.py`<br>`src/agent_lab/cli.py` | `tests/test_lease_fencing.py` |
| **二、分布式租约、统一栅栏与崩溃恢复** | **#3** | CLI 内联执行仅靠本地 `flock`，可覆盖队列 Worker 持有的活跃租约；测试通过自动 `git commit` 前未验 epoch 且提交后 `base_commit` 停留在旧值导致二轮补丁失败 | `_bump_inline_or_busy` + `_verify_epoch_tx` 双向互锁；`commit_verified_worktree` 前置 `verify_epoch` 并在提交后同步推进 `git.base_commit` 与 `registry.base_commit` | `src/agent_lab/runtime.py`<br>`src/agent_lab/storage.py`<br>`src/agent_lab/execution_lock.py` | `tests/test_lease_fencing.py`<br>`tests/test_runtime_lock.py`<br>`tests/test_repair_loop.py` |
| **二、分布式租约、统一栅栏与崩溃恢复** | **#4** | 审批落盘与重新入队分属两事务，崩溃致任务永久卡在 `waiting_approval`；重复 resume 返回 500；缺少运行中取消接口；Uvicorn 停机留僵尸 `running` 状态 | `approve_and_enqueue` 单事务原子入队 + `release_lease` 幂等礼让；存活租约冲突统一返回 HTTP `409`；新增 `request_cancel_run` 协作取消；停机捕获 `CancelledError` 落盘 `interrupted` | `src/agent_lab/storage.py`<br>`src/agent_lab/api.py`<br>`src/agent_lab/runtime.py` | `tests/test_production_api_ha.py`<br>`tests/test_lease_fencing.py` |
| **二、分布式租约、统一栅栏与崩溃恢复** | **#5** | `propose_changeset` 跨文件依次 `os.replace` 时若中途断电，前几个文件已变更为 `new_sha256` 而后几个仍为 `old_sha256`，重启重试触发 `version_conflict` 死锁 | `_cross_process_apply_lock` 跨进程锁 + `.agent-apply-<id>.json` 写前恢复日志（Write-Ahead Journal），同时接纳 `m_pre` 与 `m_post` 自动前滚完成剩余文件替换 | `src/agent_lab/tools.py`<br>`src/agent_lab/runtime.py` | `tests/test_git_changeset.py`<br>`tests/test_recovery_integration.py` |
| **三、LLM 网关韧性与真实缓存计费** | **#6** | 开启 Anthropic Extended Thinking 时 `cache_control` 落在 `thinking` 块上触发 API 400；`cache_creation_input_tokens` (1.25x) 未计入预算导致透支；并行子代理共享预算竞态超支 | `build_anthropic_cached_payload` 自动越过 `_NON_CACHEABLE_BLOCK_TYPES` 回退断点；`compute_usage_totals` 精确折算加权计费；`_inflight_reserved_tokens` 预留锁 + `ProviderRateLimiter` (RPM/TPM) | `src/agent_lab/providers.py`<br>`src/agent_lab/kv_cache.py`<br>`src/agent_lab/runtime.py` | `tests/test_kv_cache_harness.py`<br>`tests/test_context_budget.py`<br>`tests/test_runtime.py` |
| **三、LLM 网关韧性与真实缓存计费** | **#7** | OpenAI 兼容网关遇 429/5xx 直接失败；长输出易网关超时；`finish_reason="length"` 截断工具 JSON 致解析崩溃；连续 `user` 消息或 `prompt_cache_key` 被严格网关 400/422 拒绝；畸形或重复 `tool_call.id` 破坏账本 | `_post_with_resilience` 指数退避 + `Retry-After` 解析 + `OPENAI_FALLBACK_BASE_URLS` 故障转移；`_send_sse_and_assemble` 流式拼装；截断 JSON 自愈降级；相邻 `user` 合并；400/422 自动剥离不兼容字段重试；`_normalize_tool_call_id` 清洗去重 | `src/agent_lab/providers.py` | `tests/test_model_evals.py`<br>`tests/test_production_api_ha.py`<br>`tests/test_runtime.py` |
| **四、沙箱防伪隔离、容器治理与验证闭环** | **#8** | 沙箱超时/OOM/输出爆表未被归入可修复失败或评测分母错乱；待测业务代码可通过 `sys._getframe` 或伪造 stdout 回执欺骗通过；宿主强杀后孤儿容器堆积且并发无上限；清理遇 `EBUSY` 掩盖原始结果 | 统一定义 `SANDBOX_FAILURE_CODES` 与 `SANDBOX_INFRA_CODES`；外层 Supervisor + 内层 Child 双进程匿名管道 32 字节密钥 HMAC 回执 + stdout 欺骗串替换 + `sys.addaudithook`；容器打标 + `_SANDBOX_SEMAPHORE` + `reap_orphan_containers`；`_record_rmtree_failure` 保留 `execution_code` | `src/agent_lab/sandbox.py`<br>`src/agent_lab/runtime.py`<br>`src/agent_lab/model_evals.py` | `tests/test_sandbox.py`<br>`tests/test_repair_loop.py`<br>`tests/test_model_evals.py` |
| **四、沙箱防伪隔离、容器治理与验证闭环** | **#9** | 仅识别 `unittest.TestCase` 子类，无法发现与运行顶层 `def test_*` / `async def test_*` 及 `src/` 布局项目，且忽略 `tests/` 下 JSON/YAML 测试夹具 | `discover_test_oracles` AST 双模计数 + `_SuiteLoader` 动态包装同步/异步测试函数与普通 `Test*` 类 + 自动挂载 `ws/src` 与受控夹具后缀 `_SUITE_FIXTURE_SUFFIXES` | `src/agent_lab/sandbox.py` | `tests/test_sandbox.py`<br>`tests/test_model_evals.py` |
| **四、沙箱防伪隔离、容器治理与验证闭环** | **#13a** | 容器化部署 Agent API 时若挂载宿主机 `/var/run/docker.sock` 等同交出宿主机 root 权限 | 实现 `RemoteHTTPSandboxBackend` 与 `/api/internal/sandbox/execute`，本地完成脱敏快照后经 Bearer 鉴权 HTTP 转发至独立沙箱池节点执行 | `src/agent_lab/sandbox.py`<br>`src/agent_lab/api.py` | `tests/test_sandbox.py`<br>`tests/test_production_api_ha.py` |
| **五、长程上下文、KV 保活与工作记忆防投毒** | **#10** | 模型单轮传入巨大 `propose_changeset` 参数占满 `max_chars`，因最新一组不可丢弃触发 `ContextOverflow` 永久死锁；纯字符预算在中文/紧凑 JSON 下低估真实 Token；并行只读子代理单侧报错不取消同伴 | `_project_history` 历史轮次 (>2600) 与 `emergency_tail=True` (>1200/900) 二级无损投影；校准零依赖 `estimate_bpe_tokens`（可选 `tiktoken`）双预算；`_workers` 使用 `FIRST_EXCEPTION` 立即取消同伴 | `src/agent_lab/context.py`<br>`src/agent_lab/runtime.py` | `tests/test_context_budget.py`<br>`tests/test_context.py`<br>`tests/test_runtime.py` |
| **五、长程上下文、KV 保活与工作记忆防投毒** | **#11** | 运行中途调用 `skill_load` 若插入 `pinned` 头部，直接击穿整段历史对话 KV Cache；超长轨迹超过 2048 块后 LRU 淘汰掉 `<TOOLS>` 与 `<SYSTEM_BASE>` 根块 | 中途加载技能标记 `mid_run_dynamic=True` 路由至末尾 `tail_state`；`record_kv_step` 钉扎前 `_PINNED_ROOT_BLOCKS = 128` 个根前缀块永不驱逐；`SkillCatalog.catalog_status()` 透明诊断 | `src/agent_lab/kv_cache.py`<br>`src/agent_lab/runtime.py`<br>`src/agent_lab/skills.py` | `tests/test_kv_cache_harness.py`<br>`tests/test_skill_loading.py`<br>`tests/test_skills.py` |
| **五、长程上下文、KV 保活与工作记忆防投毒** | **#12** | 模型可将失败工具调用写入 `working_memory`；绑定 `repo_read` `call_id` 的事实在文件被修改后因账本快照不变而未标 `stale`；满 12 条上限后无法删除旧条目导致写死 | `_verify_fact_source` 严拒失败调用并提取 `_extract_tool_call_paths` 记录 `bound_files` 文件哈希；修改源码后自动触发 `bound_file_hash_changed`；支持 `deleted` / `remove_*` 与 `auto_evict_stale` 自动淘汰 | `src/agent_lab/working_memory.py`<br>`src/agent_lab/runtime.py` | `tests/test_working_memory.py` |
| **六、多语言大仓索引、工具精细边界与检索扩展** | **#14, #15b** | `RepoMapIndex` 仅支持 Python、无并发锁、扫入 `node_modules` 且大仓 O(N^2) 卡顿；`repo_read` 不支持按行区间读取；`propose_changeset` 不支持增删文件或改配置；CRLF 平台测试哈希误判；修改源仓 `.git/config`；凭证黑名单不全；`0711` 父目录遍历失败；终态资源无 GC；Markdown 代码块内 `# 注释` 误切分块且无语义接口 | `RepoMapIndex` 全程 `RLock` + Python/TS/JS/Go/Rust/Java 解析 + `.gitignore` 过滤 + 非测试优先截断；`repo_read(start_line, end_line)`；`propose_changeset` 支持 `create/modify/delete` 及 `.toml/.json/.yaml` 语法校验；CRLF 兼容校验；移除源仓 config 写入 + `ensure_git_origin_atomic`；全链路敏感文件名拦截；`0711` 父目录 `lstat` 回退；`gc_expired_resources`；`_tag_fenced_lines` + 可插拔 `Embedder`/`Reranker` + 驼峰子词分词 | `src/agent_lab/repomap.py`<br>`src/agent_lab/tools.py`<br>`src/agent_lab/git_repo.py`<br>`src/agent_lab/execution_lock.py`<br>`src/agent_lab/retrieval.py` | `tests/test_repomap.py`<br>`tests/test_tools.py`<br>`tests/test_git_ha.py`<br>`tests/test_execution_lock.py`<br>`tests/test_retrieval.py` |
| **七、多租户 RBAC、安全交付与全链路可观测性** | **#13b/c, #15a** | 单一静态口令无租户隔离与角色分权，发起人可自批补丁；无请求体上限与租户限流；容器无 `HEALTHCHECK` 且非回环启动不强制鉴权；缺少跨协程 Trace-ID、日志脱敏与 Prometheus 指标；前端每次轮询全量重拉全部事件 | `Principal` 多租户 RBAC (`operator`/`approver`/`admin`) + `AGENT_LAB_REQUIRE_DISTINCT_APPROVER` 异人复核 + 租户 `memory_scope` 隔离；`_TenantRateLimiter` + 队列深度上限 + 流式 413 拦截；`Dockerfile` 加固与 CLI 非回环鉴权门禁；`observability.py` W3C `traceparent` + `redact_secrets` + `/metrics` + `/healthz` & `/readyz`；前端 `after=eventCursor` 增量拉取与单飞锁 | `src/agent_lab/api.py`<br>`src/agent_lab/observability.py`<br>`src/agent_lab/cli.py`<br>`web/app.js`<br>`Dockerfile` | `tests/test_production_api_ha.py` |

---

## 2. 维度一：状态层解耦、乐观并发与非阻塞 I/O（详解问题 #1）

### 2.1 问题根因（WHY）

在教学原型阶段，`Storage` 直接围绕单个本地 SQLite 文件做最简封装，当进入高并发 API 压测与多节点 Worker 部署时立即暴露出 7 处瓶颈：

1. **WAL 冷启动竞争崩溃**：多个进程或线程在同一时刻首次连接空库并执行 `PRAGMA journal_mode=WAL` 与 `BEGIN IMMEDIATE` 建表迁移时，SQLite 内部文件锁屏障会偶发抛出 `sqlite3.OperationalError: locking protocol` (`SQLITE_PROTOCOL`) 或 `database is locked`，导致进程启动即崩溃。
2. **异常路径连接句柄泄漏**：Python 标准库 `with sqlite3.connect(...) as con:` 仅在退出上下文时管理事务提交或回滚，**并不会调用 `con.close()`**；若 `PRAGMA` 初始化或 SQL 执行抛错，底层文件描述符会滞留直到 GC 回收，高并发下迅速耗尽句柄并加剧锁争用。
3. **盲写覆盖（Lost Update）**：`save_run()` 直接按主键 `UPDATE runs SET body=? WHERE id=?`，当控制面取消请求与执行器状态保存交错时，后写入的旧快照会静默覆盖新状态。
4. **列表查询反序列化放大**：`runs.body` 内含完整多轮对话历史、工具输出及数千个 KV 块哈希；每次请求 `GET /api/runs` 都要从磁盘读取并 `json.loads` 几百个大 JSON 文档，导致列表接口延迟随对话长度线性恶化。
5. **单机文件系统绑定**：SQLite 不支持跨物理机并发写；且当任务在节点 A 创建了工作区 Git worktree，被无共享磁盘的节点 B 上的 `LeaseWorker` 接管时，节点 B 本地根本不存在 `metadata["workspace"]` 指向的目录。
6. **同步 I/O 阻塞事件循环**：FastAPI 异步路由直接调用同步 SQLite 读写与 `SkillCatalog` 磁盘扫描，慢磁盘抖动会冻结整个事件循环（包含 `/healthz` 心跳与 SSE 推送）。

### 2.2 工程解决机制（HOW）

#### （1）WAL 启动有界退避与单事务幂等迁移（`Storage.__init__` / `_initialize_schema`）
- 在 `Storage.__init__` 中针对首次建表迁移包裹最多 8 次线性退避重试（`0.02 * (attempt + 1)` 秒），捕获包含 `"locked"`、`"busy"` 或 `"protocol"` 的 `sqlite3.OperationalError`。
- 在 `_initialize_schema` 中，先开启 `PRAGMA journal_mode=WAL`，随后在**单个 `BEGIN IMMEDIATE` 排他写事务**内原子完成所有核心表创建、`runs` 新列加法迁移（`state_version`、`status_col`、`updated_at_col`、`tenant_id`、`summary_json`）以及旧版 `run_leases` 表的无损重建迁移（补齐 `dead_letter`、`cancelled`、`interrupted` 状态约束与 `takeover_count` 列）。

#### （2）`_connect` 严格句柄防泄漏与可配置落盘级别
- `_connect()` 上下文管理器采用显式 `try ... finally: con.close()` 结构（PostgreSQL 分支同理 `finally: pg_con.close()`），确保无论 `PRAGMA busy_timeout=10000`、`PRAGMA foreign_keys=ON` 还是业务事务抛出任何异常，底层连接句柄都立即释放。
- 支持通过 `AGENT_LAB_SQLITE_SYNC`（校验枚举 `OFF | NORMAL | FULL | EXTRA`，默认 `NORMAL`）按需平衡 WAL 吞吐与掉电持久性。

#### （3）`state_version` 乐观并发控制（CAS）与内存回滚
- `runs` 表新增整型列 `state_version INTEGER NOT NULL DEFAULT 1`。
- `save_run(run, expected_epoch=..., expected_version=...)` 在 `BEGIN IMMEDIATE` 事务内先调用 `_verify_epoch_tx` 核验租约世代，再读取当前 `state_version`；若传入了 `expected_version` 且不匹配，立即抛出 `StaleEpochError("state_version_conflict")`。
- 执行 `UPDATE runs SET body=?, state_version=state_version+1, ... WHERE id=? AND state_version=?` 后检查 `changed.rowcount != 1`；若发生并发写冲突，先将传入字典 `run` 上的 `updated_at` 与 `state_version` 恢复为调用前原值，再抛出 `StaleEpochError("state_version_conflict")`，绝不污染调用方内存对象。

#### （4）`summary_json` 窄列投影加速列表查询
- `Storage._build_run_summary(run, tenant_id)` 在每次写入时预先剥离沉重的 `messages` 数组与 `kv_cache["known_blocks"]` 哈希表，仅提取顶层标量字段及精简元数据（`template`、`verification`、`tenant_id`、`actor_id`、`cancel_requested` 等），存入 `summary_json` 列并建立复合索引 `runs_tenant_created(tenant_id, created_at DESC)`。
- `list_runs(tenant_id=..., limit=200)` 直接反序列化轻量的 `summary_json`（对历史遗留行平滑回退 `body`），将列表接口开销降低一个数量级以上。

#### （5）`Storage.from_dsn` 与 PostgreSQL `FOR UPDATE SKIP LOCKED` 生产适配
- 新增 `PostgresBackendConfig` 与 `_PostgresConnectionAdapter`：通过 `_translate_qmark_to_pyformat` 在词法层面安全跳过单引号字面量，将 SQLite `?` 占位符转换为 psycopg `%s`，并将 `BEGIN IMMEDIATE` 映射为标准 PostgreSQL 事务 `BEGIN`。
- 在多 Worker 抢占队列任务时，`PostgresBackendConfig.build_claim_sql()` 自动拼装 `FOR UPDATE SKIP LOCKED`，使数十个远程 Worker 进程能够零锁等待地并发领取不同任务行。

#### （6）`workspace_archives` 跨节点可移植工作区归档同步
- 新增表 `workspace_archives(run_id, epoch, git_head, archive, updated_at)`：
  - **打包物化（`pack_workspace_archive`）**：当工作区是 Git linked worktree（根目录 `.git` 是指向源仓库的指针文件）时，`_materialize_worktree_gitdir` 自动使用 `git bundle create` + 临时独立仓库 `unbundle` 将 `.git` 物化为自包含的真实目录，随后在内存中流式构建可复现 `tar.gz`（严拒符号链接、设备文件及超过 8 MiB 或 10,000 条目的超大目录）。
  - **安全解包（`unpack_workspace_archive`）**：采用 `_BoundedGzipReader` 流式限制解压体积防御压缩炸弹，逐层拒绝 `..`、绝对路径、符号链接与硬链接，在同级临时目录解压校验完毕后 `os.replace` 原子就位。
  - **单调世代保护（`save_workspace_archive`）**：保存归档前强制对比 `run_leases.epoch` 与 `workspace_archives.epoch`，旧世代 Worker 尝试上传过期归档会立即触发 `StaleEpochError("stale_archive_epoch: ...")`。
  - **按需自动拉起（`LeaseWorker._materialize_workspace_if_needed`）**：当远程 Worker 接管任务发现本地不存在工作区路径时，自动从数据库拉取归档解压到本地专属工作区目录并更新 `metadata["workspace"]`。

#### （7）API 全链路异步卸载与热索引驻留缓存
- `Storage.run_async(fn, *args, **kwargs)` 使用 `asyncio.to_thread` 将同步数据库调用卸载至工作线程池，同时自动保留 Python `contextvars` 中的 `trace_id`。
- `create_app` 在 `app.state` 上常驻复用单实例 `KnowledgeBase`、`SkillCatalog` 以及容量上限为 `_MAX_REPOMAP_CACHE = 32` 的 LRU `repomap_cache`，消除每次请求重复扫描磁盘构建索引的开销。

---

## 3. 维度二：分布式租约容错、统一 Fencing 与崩溃恢复状态机（详解问题 #2、#3、#4、#5）

### 3.1 问题根因（WHY）

在分布式执行与随时可能断电重启的真实环境中，单靠“异常捕获”无法维持状态一致性：

- **问题 #2（Worker 生命周期与毒丸循环）**：容器滚动发布向 `LeaseWorker` 发送 `SIGTERM` 触发 `asyncio.CancelledError` 时，原逻辑要么未区分“心跳发现租约被抢占触发的内部取消”与“宿主停机触发的外部取消”，导致正在正常执行的任务被误判为 `failed`；要么遇到触发底层 C 扩展崩溃或死锁的“毒丸任务”时，在多个 Worker 间无限超时接管，耗尽整个集群算力；此外 SQLite 偶发毫秒级写锁繁忙若直接视为续租失败，会误杀正常工作的执行协程。
- **问题 #3（双锁割裂与 Git 基线漂移）**：本地 CLI/API 使用 `exclusive_run`（POSIX `flock`），而队列 `LeaseWorker` 使用数据库 `run_leases.epoch`。若用户在远程或另一模式触发内联执行，缺乏双向校验会导致两个执行器同时修改同一任务；此外，开启 `commit_on_pass=True` 后，第 1 轮补丁测试通过自动生成了新 Git commit，但若未同步推进 `git.base_commit` 或未在提交前校验 `epoch`，后续操作会因 `HEAD != base_commit` 误报 `baseline_changed` 或由过期 Worker 写入非法提交。
- **问题 #4（审批脱队竞态与控制面缺失）**：队列模式下，`approve_proposal` 与 `enqueue_run(action="resume")` 若分两次独立事务提交，一旦中间进程崩溃，或者前序 Worker 恰好在此时执行 `release_lease(..., final_state="waiting_approval")`，就会把刚排入队列的 `queued` 状态覆盖回 `waiting_approval`，造成任务永久卡死；同时 API 缺少对存活租约的 `409 Conflict` 拦截、运行中取消接口以及服务关机时的 `interrupted` 状态保护。
- **问题 #5（多文件变更集半完成崩溃死锁）**：`propose_changeset` 支持一次修改最多 6 个文件。`apply_proposal` 在对多个目标文件依次执行 `os.replace` 时不属于同一个文件系统事务：若第 1 个文件已替换为 `new_sha256`，第 2 个文件替换前进程断电，重启恢复后再次执行 `apply_proposal` 时，第 1 个文件的当前哈希既不等于 `old_sha256`（若只校验前置哈希），也不满足“全部文件已是 `new_sha256`”，导致直接报 `version_conflict` 永久卡死。

### 3.2 核心交互时序与租约状态机

#### （1）世代抢占（Epoch Fencing）、审批原子入队与多文件前滚恢复时序图

```mermaid
sequenceDiagram
    autonumber
    participant API as FastAPI 控制面
    participant DB as Storage (run_leases / runs)
    participant WA as LeaseWorker A (epoch=1)
    participant WB as LeaseWorker B (epoch=2)
    participant FS as 工作区与 Git Worktree

    API->>DB: create_run() + enqueue_run(action="execute")
    WA->>DB: claim_next_run("worker-A", ttl=15s) -> epoch=1, state=claimed
    Note over WA: 网络挂起 / GC 暂停超过 15s，心跳超时
    WB->>DB: claim_next_run("worker-B", ttl=15s) -> takeover_count=1, epoch=2
    WB->>FS: propose_changeset 生成待审批提案 P1
    WB->>DB: save_run(status="waiting_approval", expected_epoch=2)
    API->>DB: approve_and_enqueue(run_id, P1, approved=true)<br/>单事务更新 P1=approved 且置 queue_state='queued', action='resume'
    WB->>DB: release_lease(epoch=2, final_state="waiting_approval")<br/>检测到同世代已是 queued -> 幂等无操作返回（防脱队覆盖）
    WA-->>DB: 暂停恢复，尝试 save_run / finish_call / verify_epoch(expected_epoch=1)
    DB-->>WA: 拒绝写入并抛 StaleEpochError (actual_epoch=2 != 1)
    Note over WA: 记录 worker_fenced 审计事件，安全退出不污染磁盘
    WB->>DB: claim_next_run("worker-B") -> epoch=3, action="resume"
    WB->>FS: 写入 .agent-apply-P1.json 预写日志 -> 逐个临时落盘与 os.replace
    Note over WB,FS: 即使第 1 文件替换后崩溃，下次恢复凭 m_pre/m_post + 日志自动前滚完成剩余文件
    WB->>FS: sandbox_test 通过 -> verify_epoch(3) -> commit_verified_worktree()<br/>同步推进 git.base_commit = commit_sha
    WB->>DB: release_lease(epoch=3, final_state="completed")
```

#### （2）`run_leases.queue_state` 完整生命周期状态机

```mermaid
stateDiagram-v2
    [*] --> idle : create_run()
    idle --> idle : bump_inline_epoch() (内联执行递增 epoch)
    idle --> queued : enqueue_run(execute)
    queued --> claimed : claim_next_run() [epoch += 1]
    claimed --> waiting_approval : release_lease(waiting_approval)
    waiting_approval --> queued : approve_and_enqueue() [action=resume, takeover_count=0]
    claimed --> queued : 外部 SIGTERM CancelledError / RunBusy 优雅重入队
    claimed --> claimed : 租约超时被新 Worker 接管 [takeover_count < max_takeovers, epoch += 1]
    claimed --> dead_letter : 租约超时且 takeover_count >= max_takeovers (DeadLetterExceeded)
    claimed --> completed : release_lease(completed)
    claimed --> failed : release_lease(failed)
    claimed --> budget_exceeded : release_lease(budget_exceeded)
    claimed --> interrupted : 进程关机中断 release_lease(interrupted)
    queued --> cancelled : request_cancel_run() 立即终止 [epoch += 1]
    waiting_approval --> cancelled : request_cancel_run() 立即终止 [epoch += 1]
    claimed --> cancelled : 运行期检测到 is_cancel_requested() 协作退出
```

### 3.3 工程解决机制（HOW）

#### （1）问题 #2 修复：心跳取消源甄别、SIGTERM 优雅重入队、DLQ 死信隔离与锁抖动容错
- **精准区分两类 `CancelledError`（`LeaseWorker._run_claimed_under_lock`）**：
  - 引入局部布尔标志 `fenced_by_heartbeat`。当 `heartbeat_loop()` 捕获到 `StaleEpochError`（租约已被更高世代抢占）并主动调用 `exec_task.cancel()` 时，置 `fenced_by_heartbeat = True`；外层捕获 `CancelledError` 后识别该标志，累加指标 `agent_lab_worker_fenced_total` 并写入 `worker_fenced` 事件，直接返回 `{"ok": False, "code": "fenced_out"}`，绝不动数据库中的新租约。
  - 若 `fenced_by_heartbeat` 为 `False`，说明取消来自外部宿主信号（如 `cli.py` 中 `_serve_worker_gracefully` 注册的 `SIGINT`/`SIGTERM` 处理器）。此时 Worker 先同步最新工作区归档，再调用 `release_lease(..., final_state="queued")` 将任务无损放回队列并记录 `worker_shutdown_requeued`，供存活的其他 Worker 立即无缝接续。
- **心跳锁冲突宽限期**：当 `heartbeat_lease` 遇到瞬时 `sqlite3.OperationalError` 时，不立即判定失联，而是记录 `heartbeat_operational_error` 结构化告警并检查 `time.time() > current_lease_deadline`；只要仍在已获批的租约有效期内，下一拍心跳可自动恢复，仅在真实越过截止时间后才触发本地自我隔离。
- **毒丸任务死信队列（DLQ）隔离（`Storage.claim_next_run`）**：`run_leases` 表维护 `takeover_count`（正常入队或审批重入队时重置为 0，每次因 `claimed` 超时被接管时 `+1`）。在 `claim_next_run(..., max_takeovers=3)` 循环中，一旦发现候选任务 `takeover and prior_takeovers >= max_takeovers`，立即在同一事务内将其 `queue_state` 置为 `'dead_letter'`、`runs.status` 置为 `'failed'`（`error="DeadLetterExceeded"`），写入 `run_dead_lettered` 事件，并 `continue` 继续领取队列中下一个健康任务，彻底阻断雪崩。

#### （2）问题 #3 修复：本地 `flock` 与数据库 `epoch` 双向互锁及 Git 基线推进
- **内联与队列双向互锁**：
  - `LeaseWorker.process_one` 在领取数据库租约后，同时进入 `exclusive_run(self.storage.path, claimed_run_id)` 获取本机内核 `flock`；若发现本机已有 CLI/内联协程持锁抛出 `RunBusy`，立即调用 `release_lease(..., final_state="queued")` 退回队列并记录 `worker_busy_requeued`。
  - 反过来，当内联 `AgentRuntime.execute` / `resume` / `approve`（其 `expected_epoch is None`）获取 `exclusive_run` 后，立即调用 `_bump_inline_or_busy(run_id)` -> `Storage.bump_inline_epoch(run_id)`：若数据库中存在未过期的 `claimed` 租约则抛 `StaleEpochError` 并转为 `RunBusy`（API 层映射为 409）；否则将 `run_leases.epoch` 原子 `+1`（保持 `queue_state='idle'`），瞬间将任何残留在内存里的旧世代 Worker 隔离出局。同时 `_verify_epoch_tx` 对 `expected_epoch is None` 的每次写入也会核查是否存在活跃 `claimed` 租约。
- **受控 Git 提交前置栅栏与基线自动推进（`AgentRuntime._invoke`）**：
  - 当 `sandbox_test` 返回 `code == "passed"` 且 `tests_executed is True` 时，在调用 `commit_verified_worktree` 之前显式执行 `self.storage.verify_epoch(run["id"], self.expected_epoch)`，防止过期 Worker 向工作区仓库提交代码。
  - 提交成功后，保留最初基线到 `git_meta.setdefault("initial_base_commit", base_commit)`，并将 `git_meta["base_commit"]`、`git_meta["committed_sha"]`、`git_meta["head_commit"]` 以及当前 `registry.base_commit` 同步推进为新生成的 `commit_res["commit_sha"]`，确保同一任务后续轮次继续读取或追加修改时基线一致。

#### （3）问题 #4 修复：`approve_and_enqueue` 原子事务、409 冲突治理与全生命周期取消/中断控制
- **单事务审批并入队（`Storage.approve_and_enqueue` + `release_lease` 礼让）**：
  - 在队列模式下，`POST /api/runs/{run_id}/proposals/{proposal_id}` 直接调用 `Storage.approve_and_enqueue`，在单个 `BEGIN IMMEDIATE` 事务内校验提案归属、写入 `status="approved" / "rejected"` 与 `decided_by=actor_id`，并将 `run_leases` 原子更新为 `queue_state='queued', action='resume', takeover_count=0`。
  - 若上一轮使任务进入 `waiting_approval` 的 Worker 随后才执行收尾调用 `release_lease(run_id, owner_id, epoch, final_state="waiting_approval")`，`release_lease` 检测到同 `epoch` 下 `row["queue_state"] == "queued" and final_state == "waiting_approval"` 时直接幂等返回，绝不把已排队的恢复动作覆盖回等待态。
- **租约冲突统一返回 HTTP 409**：`POST /api/runs/{run_id}/resume` 与 `enqueue_run` 均基于墙钟核查 `queue_state == "claimed" and lease_until > time.time()`，遇到活跃 Worker 持有租约时明确抛出 HTTP `409 Conflict`（而非未捕获的 `ValueError` 500）。
- **取消控制面与停机 `interrupted` 保护**：
  - 新增 `Storage.request_cancel_run`、`POST /api/runs/{run_id}/cancel` 与 `agent-lab cancel <run_id>`：若任务处于 `queued` 或 `waiting_approval` 且无活跃租约持有者，立即在同一事务内将状态置为 `cancelled` 并递增 `epoch` 封死迟到写入；若任务正在运行，写入 `metadata["cancel_requested"] = True`（内联模式同时 `running_job.cancel()`），`AgentRuntime._execute_unlocked` 在模型轮次边界、工作流步骤边界与工具调用循环中轮询 `is_cancel_requested` 并在安全点调用 `_mark_cancelled` 退出。
  - FastAPI `lifespan` 退出取消后台任务或协程被非用户请求中断时，`AgentRuntime` 与 `api.dispatch` 捕获 `asyncio.CancelledError`，核实非主动取消后将仍处于 `running` 的任务持久化为 `"interrupted"` 并写入 `interrupted` 事件，前端据此展示“恢复任务”按钮。

#### （4）问题 #5 修复：跨文件 `apply_proposal` 预写恢复日志（Write-Ahead Journal）与前滚恢复
- **进程内 + 跨进程双重互斥**：`ToolRegistry.apply_proposal` 同时持有工作区 `RLock` 与 `_cross_process_apply_lock(self.storage.path, self.workspace)`（基于 `.agent-lab/locks/apply-<ws_hash>.lock` 的 `O_NOFOLLOW` + `flock` 排他锁）。
- **三态哈希巡检（Pre-image / Post-image Inspection）**：对提案中的每个目标文件计算当前状态 `cur_state`（不存在为 `None`，存在为内容 SHA-256），对比期望前置状态 `exp_pre`（`create` 为 `None`，其余为 `old_sha256`）与期望后置状态 `exp_post`（`delete` 为 `None`，其余为 `new_sha256`）：
  1. 若**所有文件**均满足 `cur_state == exp_post`：说明磁盘已完整写入，直接幂等调用 `mark_proposal_applied`、清理恢复日志并返回 `already_applied=True`。
  2. 若存在任何文件既不等于 `exp_pre` 也不等于 `exp_post`（`not (m_pre or m_post)`）：说明发生了真实的外部并发篡改，拒绝覆盖并抛出 `ToolError("version_conflict", ...)`。
  3. 若部分文件处于 `exp_post`、其余文件处于 `exp_pre`（即上次替换中途崩溃），或工作区存在 `.agent-apply-<proposal_id>.json`：判定为 `recovered_partial = True`，仅收集尚未到达 `exp_post` 的 `pending` 文件集执行前滚恢复！
- **原子落盘序列**：先通过临时文件 + `os.fsync` + `os.replace` 写入 `.agent-apply-<proposal_id>.json` 恢复日志；再将所有待写入的新内容全部写入各自目标目录的 `.agent-patch-*` 临时文件并 `fsync`；在真正执行 `os.replace` / `unlink` 之前**二次调用 `self.storage.verify_epoch(self.run_id, epoch)`** 收窄竞争窗口；全部替换成功后标记数据库 `applied=True` 并删除 `.agent-apply-*.json`。

---

## 4. 维度三：LLM 网关韧性、推理块兼容与真实缓存成本治理（详解问题 #6、#7）

### 4.1 问题根因（WHY）

真实生产环境中的商业推理网关与开源兼容网关（vLLM / SGLang / LiteLLM / OpenRouter）远比本地 Mock 复杂：

- **问题 #6（Anthropic 推理块冲突与缓存计费盲区）**：
  1. Claude 3.7 / Claude 4 系列启用 Extended Thinking（`thinking={"type": "enabled", "budget_tokens": ...}`）时，响应列表会包含 `thinking` 与 `redacted_thinking` 内容块，且多轮工具调用必须原样回传这些块及其 `signature`；但 Anthropic API **严禁在 `thinking` 或 `redacted_thinking` 块上附加 `cache_control`**（否则直接返回 HTTP 400），且要求 `budget_tokens < max_tokens`。
  2. Anthropic 的 `usage.input_tokens` **仅统计未命中缓存的尾部输入**，不包含 `cache_creation_input_tokens`（按标准输入 1.25 倍计费）与 `cache_read_input_tokens`（0.10 倍计费）。早期原型只累加 `input_tokens + output_tokens`，导致大量缓存写入时真实账单远超 `max_tokens` 预算却不触发熔断。
  3. `parallel` 模式下 `reviewer` 与 `tester` 两个协程并发发起请求时，在响应返回前各自看到的已消耗 token 相同，容易双双越过剩余预算并打爆上游 RPM/TPM 配额。
- **问题 #7（OpenAI 兼容网关脆弱性与协议方言摩擦）**：
  1. 遇到 HTTP `429` 或 `500/502/503/504` 瞬时抖动无指数退避、不读取 `Retry-After` 响应头，也不支持主备网关自动故障转移（Failover）。
  2. 非流式长回复容易触发反向代理空闲读超时；而当输出达到 `max_tokens`（`finish_reason="length"`）导致工具调用参数 JSON 半截截断时，`json.loads` 抛错会直接中断整个运行而非交由模型自我修正。
  3. 部分严格网关（如 Mistral、Groq、Together、Fireworks 等）遇到未知的顶层 `prompt_cache_key` 字段，或遇到连续两条 `role: "user"` 消息（例如 `tool_result` 后紧跟 `HARNESS_STATE`），或历史消息混入 Anthropic 的 `thinking` 块时，直接拒绝请求（HTTP 400/422）。
  4. 第三方开源网关偶发返回含冒号/空格的非法 `tool_call.id`，甚至跨轮次重复生成如 `"call_1"` 的相同 ID，破坏工具账本唯一主键约束。

### 4.2 工程解决机制（HOW）

#### （1）问题 #6 修复：Anthropic 推理块兼容、三段式断点避让、真实加权计费与并发预留限流
- **`thinking` / `redacted_thinking` 全链路兼容与断点自动回退**：
  - `AnthropicProvider.__init__` 支持 `ANTHROPIC_THINKING_BUDGET`（`>= 1024` 时启用）；在 `complete()` 中动态校验单轮 `output_cap`：若 `max_tokens <= 1024` 自动临时关闭默认 `thinking`，若 `budget_tokens >= max_tokens` 则自动收紧至 `max_tokens - 1`，消除参数越界 400。
  - 响应解析完整保留 `{"type": "thinking", "thinking": ..., "signature": ...}` 与 `{"type": "redacted_thinking", "data": ...}`，同时在 `AgentRuntime._complete` 的内容块白名单中放行这两类块。
  - 在 `kv_cache.py` 的 `_to_content_blocks` 与 `build_anthropic_cached_payload` 中，定义 `_NON_CACHEABLE_BLOCK_TYPES = frozenset({"thinking", "redacted_thinking"})`：不仅剥离历史推理块上可能残留的 `cache_control`，而且在放置第 3 个滚动历史断点（`last_stable_coord`）时，若末尾块属于不可缓存推理块，自动向前回溯定位最近一个 `text`、`tool_use` 或 `tool_result` 块挂载 `{"type": "ephemeral"}`。
- **`compute_usage_totals` 精确缓存成本核算**：
  - 区分厂商语义差异：Anthropic（`cache_usage_additive = True`）按 `total_input = uncached + cache_write + cache_read` 还原真实上下文规模，并按 `billed_input = uncached + round(1.25 * cache_write) + round(0.10 * cache_read)` 核算计费等价 token；OpenAI（`cache_usage_additive = False`）因 `prompt_tokens` 已含 `cached_tokens`，按 `uncached = total_input - cache_read`、`billed_input = uncached + round(0.25 * cache_read)` 折算。
  - `AgentRuntime._complete` 同时持久化真实 `input_tokens` 与 `metadata["billed_tokens"]`，预算门禁取 `max(input_tokens + output_tokens, billed_tokens)`，彻底杜绝 1.25x 缓存创建费用穿透预算上限。
- **并行预算预留（`_inflight_reserved_tokens`）与 `ProviderRateLimiter`**：
  - 在 `self._budget_lock` 保护下，每次调用模型前先基于 `estimate_bpe_tokens` 估算请求体积并累加 `self._inflight_reserved_tokens`，据此动态压缩单轮 `output_cap`；在 `try ... finally` 中确保响应落盘或异常退出时精确释放预留额度，杜绝并行 Worker 超支。
  - `providers.py` 实现线程与协程双安全的 60 秒滑动窗口准入限流器 `ProviderRateLimiter`（读取 `AGENT_LAB_RPM_LIMIT` 与 `AGENT_LAB_TPM_LIMIT`）：请求前通过 `reserve(estimated_tokens)` 预占窗口配额并在超限时精确等待最老条目滑出窗口，响应后通过 `record_actual(actual_tokens, reserved_tokens)` 多退少补（异常时全额退回预留），未配置环境变量时零开销直通。

#### （2）问题 #7 修复：OpenAI 多网关韧性重试、SSE 组装、截断自愈与协议清洗
- **指数退避、`Retry-After` 解析与主备网关 Failover（`_post_with_resilience`）**：
  - 支持通过 `OPENAI_FALLBACK_BASE_URLS` 配置逗号分隔的备用网关列表。
  - 针对网络异常（`TimeoutException`、`NetworkError`、`RemoteProtocolError`）及状态码 `_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}`，优先通过 `_parse_retry_after` 解析秒数或 HTTP-date 格式的 `Retry-After` 头（封顶 `max_retry_delay`），缺失时采用带 `[0.75, 1.25]` 随机抖动的指数退避；当前端点重试耗尽后自动无缝切换至下一个备用网关。
- **可选 SSE 流式接收与增量拼装（`_send_sse_and_assemble`）**：
  - 设置 `OPENAI_STREAM=1` 时自动携带 `stream=True` 与 `stream_options={"include_usage": True}`，按 `delta.tool_calls[index]` 槽位无损拼接分片 `id`、`function.name` 与 `function.arguments`，规避长思维链或大补丁生成期间的网关超时。
- **截断 JSON 自愈与最大输出续写闭环**：
  - 在 `AnthropicProvider` 与 `OpenAICompatibleProvider` 中，若工具参数 JSON 因 `max_tokens` / `length` 截断无法解析，不抛出致命异常，而是封装为合法字典 `{"__truncated_or_invalid_json__": raw_args[:240]}`，使工具层通过 JSON Schema 校验返回结构化 `invalid_arguments` 错误，引导模型下一轮缩减参数重试。
  - 当纯文本回答因 `stop_reason == "max_tokens"` 被截断且剩余步数充足时，`AgentRuntime._execute_unlocked` 自动注入最多 2 次精简续写提示（`output_truncated_continue` 事件），避免半截报告直接结束任务。
- **严格网关协议自适应与 `_normalize_tool_call_id` 清洗去重**：
  - `_to_openai_messages` 自动过滤历史中的 `thinking` / `redacted_thinking` 块，先输出紧邻 `assistant.tool_calls` 的 `role: "tool"` 消息，再通过 `_append_user_text` 将相邻的多条 `user` 文本消息以 `\n\n` 合并，满足严格角色交替约束。
  - `_should_send_prompt_cache_key` 默认跳过 `_STRICT_NO_CACHE_KEY_HOSTS` 已知严格域名；若未知网关对 `prompt_cache_key` 或 `stream_options` 返回 HTTP `400/422`，自动置 `self._supports_prompt_cache_key = False`、剥离对应字段并在当次请求立即透明重试。
  - `_normalize_tool_call_id` 将非法字符统一替换为 `_`、限制长度不超过 96 字符，并对照整段历史已用 ID 集合 `_collect_historical_tool_ids(messages)` 自动追加 `_2`、`_3` 后缀去重，保障账本主键绝对唯一。

---

## 5. 维度四：沙箱双进程防伪造隔离、容器防泄漏与验证闭环（详解问题 #8、#9、#13a）

### 5.1 问题根因（WHY）

代码 Agent 的核心可信度建立在“测试真值不可被待测代码篡改或伪造”之上，原型沙箱存在三方面深层隐患：

- **问题 #8（回执伪造面、错误分类断层与容器泄漏）**：
  1. 原型在容器内用单个 Python 进程加载并运行待测仓库：恶意或异常业务代码可通过 `print("AGENT_LAB_RECEIPT_...")` 尝试碰撞或污染标准输出回执，或通过 `sys._getframe()` / `gc.get_objects()` / `ctypes` 栈帧内省窃取父层局部变量中的随机 `marker` 直接打印虚假满分回执并以 `os._exit(0)` 退出！
  2. 沙箱返回 `sandbox_timeout`、`sandbox_output_limit`、`sandbox_incomplete` 等错误时，若 `AgentRuntime` 只把 `"tests_failed"` 视为可修复失败，模型将失去自动修复机会；而 `model_evals.py` 若将基础设施故障与模型代码引发的超时/OOM 混淆，会扭曲真实评测通过率分母。
  3. 宿主进程在 `docker run` 期间若遭遇 `SIGKILL` 崩溃，无 `--rm` 的容器会永久残留宿主机；并发突发时无信号量限制会瞬间拉起数十个容器耗尽宿主机内存；临时目录清理若遇到文件系统瞬时 `EBUSY` 抛错，还会掩盖原本已得出的容器执行结论。
- **问题 #9（测试形态与目录布局单一）**：仅调用 `unittest.defaultTestLoader.discover`，导致大量真实项目使用的 pytest 风格模块顶层 `def test_*`、`async def test_*` 协程测试函数、未继承 `unittest.TestCase` 的 `class Test*`、标准的 `src/<pkg>` 源码布局以及 `tests/` 目录下的 `.json`/`.yaml`/`.toml` 测试夹具全部无法运行或报 `ImportError`。
- **问题 #13a（Docker Socket 权限逃逸风险）**：当 Agent 控制面与 Worker 自身部署在容器内时，若为了运行 `sandbox_test` 而将宿主机 `/var/run/docker.sock` 挂载进业务容器，一旦业务容器被攻破即可直接挂载宿主机根目录提权为 root。

### 5.2 双进程 HMAC 匿名管道防伪沙箱与远程池化架构图

```mermaid
flowchart LR
    subgraph HostOrAPI["Agent Worker 节点 (本地或无特权容器)"]
        direction TB
        WS["任务工作区 Workspace"] --> SNAP["O_NOFOLLOW + O_NONBLOCK 流式快照\n二次校验复制后 test_oracles SHA-256\n剔除敏感凭证文件 / 权限锁死 0444"]
        SNAP --> MODE{"配置 AGENT_LAB_SANDBOX_URL?"}
    end

    MODE -->|"否：本地受限 Docker\n(_SANDBOX_SEMAPHORE 限流)"| SUP
    MODE -->|"是：RemoteHTTPSandboxBackend\nBearer Token + Base64 受限载荷"| RPOOL["独立沙箱工作节点\nPOST /api/internal/sandbox/execute"]
    RPOOL --> SUP

    subgraph Container["隔离容器 (network=none, read-only, cap-drop=ALL, uid=65534, pids=64, mem=128m)"]
        direction TB
        SUP["外层 Supervisor 进程 (python -I -B)\n1. 持有随机 marker 与 32B 随机密钥 key\n2. 绝不挂载或导入 /workspace\n3. 扫描子进程 stdout 拦截替换 AGENT_LAB_RECEIPT_*\n4. 校验匿名管道 HMAC-SHA256 签名后向宿主输出唯一回执"]
        CHILD["内层 Child 测试进程 (独立进程组)\n1. stdin 读取 32B key 立即关闭并 del 局部变量\n2. 闭包锁定 w_fd 单次发送 HMAC 签名回执\n3. sys.addaudithook 封禁 ctypes / gc / sys._getframe\n4. 注入 /workspace/src 并通过 _SuiteLoader 执行测试"]

        SUP -- "stdin 一次性传入 32B key" --> CHILD
        CHILD -- "匿名管道 pass_fds=(w_fd)\n32B HMAC + JSON receipt" --> SUP
        CHILD -- "stdout/stderr (欺骗前缀被实时抹除)" --> SUP
    end
```

### 5.3 工程解决机制（HOW）

#### （1）问题 #8 修复：双进程 HMAC 隔离、失败分类对齐、容器防泄漏与清理容错
- **外层 Supervisor + 内层 Child 双进程防伪架构（`sandbox.py`）**：
  - **物理隔离 `marker`**：外层 `_build_supervisor_driver` 进程持有宿主机生成的随机回执前缀 `marker = "AGENT_LAB_RECEIPT_<uuid>:"` 与 `key = os.urandom(32)`，**外层进程全程不把 `/workspace` 加入 `sys.path` 也不导入任何用户文件**。
  - **匿名管道 HMAC-SHA256 认证**：外层创建匿名管道 `r_fd, w_fd = os.pipe()`，仅通过 `pass_fds=(w_fd,)` 传给内层子进程，并通过 `stdin` 一次性写入 32 字节密钥后立即关闭管道。内层 `_CHILD_PREAMBLE` 读取密钥构造单次发送闭包 `_send` 后立即执行 `del key, _bind_sender`，并在运行完毕后向 `w_fd` 写入 `HMAC_SHA256(key, body) + body`。
  - **标准输出防欺骗与内省审计钩子**：外层使用 `selectors` 同时监听 `child.stdout` 与 `r_fd`，对跨分块边界的字节流实时扫描任何 `"AGENT_LAB_RECEIPT_"` 字样并替换为 `"[SPOOF_BLOCKED_RECEIPT_]"`，彻底封死日志伪造路径；内层在导入任何业务模块前注册 `sys.addaudithook(_audit)`，硬性拦截 `ctypes.*` 导入与调用、`gc.get_objects/get_referrers/get_referents` 对象图遍历以及非标准库发起的 `sys._getframe` 栈帧窥探。
- **失败码与基础设施码全链路对齐**：
  - 显式定义 `SANDBOX_FAILURE_CODES`（含 `tests_failed`、`no_tests_ran`、`test_count_mismatch`、`timeout`、`sandbox_timeout`、`sandbox_output_limit`、`sandbox_incomplete`、`sandbox_oom`、`sandbox_snapshot_limit`、`unsafe_workspace`、`test_oracle_modified`）与 `SANDBOX_INFRA_CODES`（含 `sandbox_unavailable`、`image_missing`、`daemon_error`、`sandbox_error`、`sandbox_cleanup_failed`）。
  - `AgentRuntime._invoke` 对所有 `SANDBOX_FAILURE_CODES` 统一更新 `verification.status = "failed"` 并返回剩余 `repair_budget`，允许模型针对死循环超时、内存溢出或输出超限进行代码修复；`model_evals.py` 仅将 `SANDBOX_INFRA_CODES` 计入 `infrastructure_failure` 从有效评测分母中剔除，其余沙箱失败如实计入 `model_failure`。
- **容器并发信号量、标签化孤儿回收与清理 `EBUSY` 保真**：
  - 引入进程级有界信号量 `_SANDBOX_SEMAPHORE = threading.BoundedSemaphore(AGENT_LAB_SANDBOX_CONCURRENCY)`（默认 4）保护宿主机资源。
  - 每个启动的容器均打上 `--label agent-lab.managed=1` 与 `--label agent-lab.expires-at=<unix_ts>`；新增 `reap_orphan_containers()`（接入 `POST /api/admin/gc` 与 `agent-lab gc`），自动扫描并 `docker rm -f` 所有已过期的孤儿容器。
  - 宿主临时快照目录删除失败（如挂载点短暂 `EBUSY`）时，`_record_rmtree_failure` 将 `ok` 置为 `False`、记录 `cleanup_error="rmtree_EBUSY"`，若原测试结果为 `passed` 则将其保存在 `execution_code="passed"` 并把顶层 `code` 降级为 `"sandbox_cleanup_failed"`，既诚实暴露资源清理异常，又不丢失原始执行证据。

#### （2）问题 #9 修复：AST 多形态测试发现、pytest 函数/异步执行器与 `src/` 布局兼容
- **宿主端零执行 AST 发现（`discover_test_oracles`）**：静态遍历 `tests/**/*.py`，在计算每个受保护测试文件 SHA-256 的同时，同时统计继承 `TestCase` 或以 `Test` 开头的类内 `test*` 方法（含 `async def`），以及模块顶层 `def test_*` 与 `async def test_*` 函数总数作为 `expected_tests` 真值门槛。
- **内层 `_SuiteLoader` 动态适配与 `src/` 路径注入**：
  - `_CHILD_PREAMBLE` 检测若存在 `/workspace/src` 目录，自动将其插入 `sys.path[0]`，原生支持现代 Python `src/` 包结构。
  - `_SUITE_CHILD_TAIL` 中的自定义 `_SuiteLoader` 扩展 `unittest.TestLoader`：自动将模块顶层同步/异步 `test_*` 函数（协程通过 `asyncio.run` 包装）以及未继承 `unittest.TestCase` 的 `Test*` 类方法封装为 `unittest.FunctionTestCase` 纳入统一回执计数。
  - `_snapshot_suite` 在复制 `.py` 之外，安全放行 `tests/` 目录下的受控静态测试夹具文件（`_SUITE_FIXTURE_SUFFIXES = {".json", ".yaml", ".yml", ".toml", ".txt", ".csv"}`）。

#### （3）问题 #13a 修复：`RemoteHTTPSandboxBackend` 无特权远程沙箱池解耦
- 当配置环境变量 `AGENT_LAB_SANDBOX_URL` 时，`SandboxRunner` 自动启用 `RemoteHTTPSandboxBackend`：
  1. 在 API / Worker 本地先完成 `O_NOFOLLOW` 防符号链接扫描、敏感文件剔除与测试真值 SHA-256 校验，生成最小化内存快照 `files_b64`。
  2. 携带 `Authorization: Bearer <AGENT_LAB_SANDBOX_TOKEN>` 将快照与 `SandboxPolicy` POST 至独立沙箱节点的 `/api/internal/sandbox/execute`（该路由在 `_GuardMiddleware` 中专门放宽请求体上限至 `AGENT_LAB_SANDBOX_MAX_BODY_BYTES`，默认 4 MiB，并强制校验沙箱专用令牌或 `admin` 角色）。
  3. 远端 `execute_remote_sandbox_payload` 二次校验路径边界、文件大小上限与快照摘要一致后在隔离宿主机执行 Docker 容器，本地再对返回的回执做矛盾性审查（如声称 `ok=True` 但 `tests_executed` 不为 `True` 则判定为 `remote_contradictory_receipt`），实现 Agent 业务容器与 Docker 守护进程的彻底物理隔离。

---

## 6. 维度五：长程上下文防死锁、KV Cache 动态技能保活、工作记忆防投毒与多语言大仓索引（详解问题 #10、#11、#12、#14、#15b）

> 注：本章统筹速查表中的“长程上下文、KV 保活与工作记忆”与“多语言大仓索引、工具精细边界与检索扩展”两大紧密协同的 Harness 内核机制。

### 6.1 问题根因（WHY）

在多轮跨文件复杂编码任务中，Agent 的认知与工具层面临五类典型退化：

- **问题 #10（大参数上下文死锁、纯字符预算失真与子代理慢失败）**：
  1. `ContextManager` 保护规则要求“最新一组完整工具往返 `tail[-1]` 绝不能被整组丢弃”。当模型在某轮调用 `propose_changeset` 传入多文件高达 15,000+ 字符的 `old_text` / `new_text` 时，即便把所有更早历史组删光，`messages[0] + pinned + tail[-1] + tail_state` 依然超过 `max_chars`，直接抛出 `ContextOverflow` 导致任务永久死锁！
  2. 原型仅用 JSON 序列化字符数限制上下文，但中文、转义符与紧凑 JSON 标点的 Token 密度远高于英文散文（字符/Token 比可低至 1:1 甚至更高），纯字符预算无法准确对齐模型窗口上限。
  3. `parallel` 模式下 `asyncio.gather` 等待全部子代理结束才抛出首个异常，若 `reviewer` 第 1 秒就触发 `BudgetExceeded` 或鉴权失败，`tester` 仍会继续空耗数轮 API 调用。
- **问题 #11（运行期动态加载技能击穿 KV Cache 与根块被驱逐）**：
  1. 若 Agent 在第 8 轮调用 `skill_load` 加载新技能并将其放入前置固定区 `pinned`（位于 `messages[0]` 之后、所有历史轮次之前），会令后续几千 Token 的历史对话位置编码整体右移，导致整段前缀 KV Cache 100% 级联失效！
  2. `record_kv_step` 维护最多 `_MAX_TRACKED_BLOCKS = 2048` 个链式块哈希；超长对话若按纯 FIFO/LRU 驱逐最老块，会把位于序列最开头的 `<TOOLS>` 与 `<SYSTEM_BASE>` 根块挤出缓存池，造成后续步骤连基础系统前缀都判定为未命中。
- **问题 #12（结构化工作记忆投毒、间接引用失察与容量写死）**：
  1. 模型可能误把 `status="failed"` 或 `ok=False` 的工具调用绑定为已核实事实（`facts`）。
  2. 当事实绑定 `source_type="tool_call"`（如某次 `repo_read` 的 `call_id`）时，虽然该历史工具调用的 `result_sha256` 在数据库中永不改变，但一旦后续补丁修改了对应的磁盘文件，这条基于旧源码得出的结论实际上已经过期，若不追踪关联文件哈希就会持续误导模型！
  3. `todos`（上限 10）与 `facts`（上限 12，总预算 2400 字符）缺乏删除与自动淘汰入口，长程任务写满配额后无法写入新结论。
- **问题 #14 & #15b（单语言索引瓶颈、工具粒度受限、路径/编码边缘缺陷与检索切块误伤）**：
  1. `RepoMapIndex` 仅支持 Python、无线程锁保护、未遵循 `.gitignore`（易扫入 `node_modules` / `target`），且高扇出同名符号跨文件引用计分呈 $O(N^2)$ 退化。
  2. `repo_read` 只能整文件读取；`propose_changeset` 只能修改已有 `.py`，不能新建/删除文件或修改 `.toml`/`.json`/`.yaml` 配置；Windows/跨平台克隆仓库产生的 CRLF 换行会导致 `test_pricing.py` 原始字节 SHA-256 误报篡改；Git 操作若写入宿主共享 `.git/config` 或并发初始化基线仓库会产生竞争污染；敏感文件过滤遗漏 `.pem`/`.key`/`id_rsa` 等后缀；`0711` 仅可执行无读权限的父目录会导致 `O_RDONLY` 逐层打开失败；终态工作区、归档与锁文件缺乏统一 GC。
  3. `retrieval.py` 按行首 `#` 切分 Markdown 标题时，会把围栏代码块（` ```python `）内部的 `# Python 注释` 误判为章节标题切碎代码；词法分词无法互通 `camelCase` 与 `snake_case`，且缺少稠密语义向量与重排器接入点。

### 6.2 工程解决机制（HOW）

#### （1）问题 #10 修复：二级无损历史投影、校准 BPE Token 双预算与 `_workers` 快速失败取消
- **历史轮次与紧急 `tail[-1]` 二级投影（`AgentRuntime._project_history` + `_complete`）**：
  - **常规投影（`emergency_tail=False`）**：对非最新 assistant 消息中超过 2600 字符的 `tool_use.input` 替换为紧凑占位符 `{"_omitted_large_input": True, "call_id": ..., "chars": ...}`（原始完整参数始终保留在持久化 `run["messages"]` 与 `tool_calls` 账本中，不改变审计记录）。
  - **紧急防死锁投影（`emergency_tail=True`）**：当 `_complete` 首次调用 `self.context.build(...)` 捕获 `ContextOverflow` 且 `len(raw_messages) > 1` 时，自动启用 `emergency_tail=True` 重建推理视图——将最新尾部组 `in_tail_group` 中超过 1200 字符的 `tool_use.input` 折叠为摘要，并将超过 900 字符的 `tool_result` 压缩为仅保留核心状态字段与 `tool_result_get` 分页取回指针的 `compact_view`，彻底消除超大补丁参数引发的上下文死锁。
- **零外部依赖校准 BPE 估算器与双预算门禁（`context.py`）**：
  - 实现 `estimate_bpe_tokens(value, use_tiktoken=...)`：默认通过正则 `_BPE_PIECE_RE` 精确区分英文单词（4 字符/tok）、数字串（3 位/tok）、CJK 汉字（1.5 tok/字）、JSON 转义序列与常见双字符标点对并计入每条消息结构开销（`_MESSAGE_BPE_OVERHEAD = 4`）；当设置 `AGENT_LAB_USE_TIKTOKEN=1` 且环境装有 `tiktoken` 时自动切换 `cl100k_base` 精确编码。
  - `ContextManager(max_chars=..., low_watermark_ratio=0.62, max_tokens=...)` 同时支持字符预算与 BPE Token 预算双约束及 62% 低水位阶梯式压缩（避免满预算后每步滑动删 1 组破坏全部前缀缓存）。
- **`_workers` `FIRST_EXCEPTION` 快速失败取消**：`AgentRuntime._workers` 使用 `asyncio.wait(pending_tasks, return_when=asyncio.FIRST_EXCEPTION)` 监控所有并行只读子代理（支持通过 `metadata["worker_roles"]` 配置 1 至 6 个角色），一旦任一协程抛错或被取消，立即 `cancel()` 并排空其余在途协程，避免无效 Token 浪费。

#### （2）问题 #11 修复：运行期动态技能尾部路由、根块钉扎 LRU 与目录诊断
- **`mid_run_dynamic` 尾部路由保活前缀缓存（`AgentRuntime._invoke` / `_complete`）**：
  - 在任务启动阶段预加载的技能保留在 `pinned` 区；而在多轮对话中途（`len(run["messages"]) > 1`）通过 `skill_load` 新加载或切换版本的技能快照会自动打标 `"mid_run_dynamic": True`。
  - `_complete` 构建请求时将 `mid_run_dynamic` 技能快照编排进末尾 `tail_state`（位于所有稳定历史消息之后），使整段历史消息前缀字节纹丝不动，KV Cache 保持 100% 连续命中！
- **根前缀块钉扎 LRU（`kv_cache.py`）**：`record_kv_step` 在已追踪块数超过 `_MAX_TRACKED_BLOCKS = 2048` 触发淘汰时，强制钉扎当前请求最前面的 `_PINNED_ROOT_BLOCKS = 128` 个根前缀块（覆盖 `<TOOLS>` 与 `<SYSTEM_BASE>` 约 8,192 字符），仅对非根历史块执行尾部 MRU 保留。
- **技能目录透明诊断（`SkillCatalog.catalog_status`）**：返回 `total_discovered`、`truncated` 与 `invalid_skipped` 列表，杜绝损坏或超上限的 `SKILL.md` 被静默吞没。

#### （3）问题 #12 修复：工作记忆来源强校验、`bound_files` 传递失效检测与弹性淘汰
- **拒绝失败工具调用与传递绑定文件哈希（`working_memory.py`）**：
  - `_verify_fact_source` 在核验 `source_type == "tool_call"` 时，不仅要求账本存在且 `result_sha256` 非空，还强制要求 `call["status"] == "succeeded"` 且返回字典 `result.get("ok") is not False`，彻底阻断拿报错调用充当事实。
  - 同时通过 `_extract_tool_call_paths` 自动提取 `repo_read`、`verify_python`、`propose_patch`、`propose_changeset`、`symbol_search`、`repo_search`（乃至一层 `tool_result_get` 溯源）所关联的工作区文件路径，逐一计算当前磁盘文件 SHA-256 存入该事实的 `bound_files` 字典。
  - 一旦后续补丁修改了对应源码，`audit_working_memory` 中的 `_fact_stale_reason` 对比 `bound_files` 发现不匹配，立即将该事实自动降级为 `status="stale"` 并标注 `stale_reason="bound_file_hash_changed"`；注入提示词时通过 `prompt_view_working_memory` 剥离 `recorded_at` 时间戳噪声以保护 KV Cache。
- **显式删除与过期事实自动驱逐（`update_working_memory`）**：支持 `remove_todo_ids`、`prune_done_todos`、条目级 `status="deleted"`、`remove_fact_ids`、`prune_stale_facts` 以及默认开启的 `auto_evict_stale=True`（当事实数超过 12 条或序列化体积超过 2400 字符时，按记录时间从早到晚自动淘汰已变为 `stale` 的旧事实腾出空间）。

#### （4）问题 #14 & #15b 修复：多语言线程安全 `RepoMapIndex`、全功能工具集、系统级安全兼容与检索增强
- **多语言、线程安全、遵循 `.gitignore` 的 `RepoMapIndex`（`repomap.py`）**：
  - 全程由 `threading.RLock` 保护，支持多线程并发刷新与查询。
  - 除 Python 标准库 `ast` 解析外，通过 `_strip_c_comments`（保持 1:1 行号剥离 `//` 与 `/* */` 注释并屏蔽字符串字面量）实现零执行风险的 **TypeScript / JavaScript (`.ts/.tsx/.js/.jsx`)、Go (`.go`)、Rust (`.rs`)、Java (`.java`)** 类/接口/结构体/函数/方法签名与导入调用静态提取。
  - `_discover_source_files` 自动跳过 `_ALWAYS_SKIP_DIRS`（`node_modules`、`.venv`、`target`、`dist` 等）并解析仓库 `.gitignore` 规则过滤生成物；超过 `max_index_files=512` 时按“业务源码优先于 `tests/`”排序截断并透明上报 `index_truncated: True`。
  - `_dependency_scores` 对每个调用方引用按 `(ref.name, ref.kind)` 去重并对超过 16 处同名定义的高频通用名跳过计分，将依赖重要度排序严格控制在近线性 $O(N)$ 开销。
- **工具粒度扩展与跨平台/宿主环境安全加固（`tools.py` / `git_repo.py` / `execution_lock.py` / `storage.py`）**：
  - `repo_read(path, start_line=..., end_line=...)` 支持按 1-based 行号区间读取大文件局部源码，同时始终返回完整文件 `sha256` 与 `total_lines` 供工作记忆绑定核验。
  - `propose_changeset` 支持 `"action": "modify" | "create" | "delete"` 三类文件操作，并放行受控配置文件后缀（`.py/.toml/.json/.yaml/.yml/.cfg/.ini`），在生成提案前通过 `_validate_content_syntax` 对 `.py`（`ast.parse`）、`.json`（`json.loads`）、`.toml`（`tomllib.loads`）执行静态语法门禁。
  - `sandbox_test` 对内置单文件测试真值同时比对原始字节 SHA-256 与 `\r\n -> \n` 归一化摘要，彻底消除 Windows/跨平台换行符差异导致的 `test_oracle_modified` 误判。
  - `git_repo.py` 绝不改写源仓库共享 `.git/config`（GPG 签名关闭改用命令行瞬态参数 `-c commit.gpgsign=false`，且注入 `GIT_CEILING_DIRECTORIES` 与独立进程组超时强杀），并通过 `ensure_git_origin_atomic`（独占 `flock` + 同级临时目录构建 + `os.replace`）保障多进程并发冷启动安全。
  - `tools.py`、`git_repo.py`、`repomap.py` 与 `sandbox.py` 统一拦截敏感文件名与密钥后缀（`credentials.*`、`secrets.*`、`.env*`、`id_rsa*`、`id_ed25519*`、`.pem`、`.key`、`p12`、`.pfx`）。
  - `execution_lock.py`（`_open_database_parent`）与 `sandbox.py`（`_open_root`）优先使用 Linux `O_PATH` 遍历父目录；在 macOS/容器内遇到权限为 `0711`（可执行但不可读）的祖先目录返回 `EACCES`/`EPERM` 时，自动回退至 `_verify_parent_prefixes_and_open` / `_verify_prefixes_and_open_root` 逐级 `os.lstat` 校验符号链接并核对目标 `st_dev/st_ino`，兼顾防符号链接逃逸与严苛容器目录兼容性。
  - 新增 `Storage.gc_expired_resources`（联动 `cleanup_task_worktree`、`git worktree prune`、`workspace_archives` 清理、`cleanup_stale_locks` 与 `prune_workspace_lock`），一键回收超过保留窗口（默认 86400s）的所有终态资源。
- **代码块感知切分、跨命名分词与可插拔语义检索（`retrieval.py`）**：
  - `_tag_fenced_lines` 精确识别 ```` ``` ```` 与 `~~~` 围栏代码块边界，`_chunks` 仅在围栏外部识别 Markdown `#` 标题切分章节，保护代码示例完整不断裂。
  - `tokenize` 通过 `_identifier_subtokens` 自动拆分 `camelCase` 与 `snake_case` 复合词根，使查询 `exponentialBackoff` 与文档中的 `exponential_backoff` 双向互通。
  - `KnowledgeBase(directory, embedder=..., reranker=...)` 支持注入稠密向量 `Embedder`（自动做有限浮点数校验与 L2 归一化，切换为 `BM25 + dense semantic cosine + RRF`）及二阶段 `Reranker`（从 `max(limit * 4, 20)` 融合候选池重排），同时诚实暴露 `semantic_embeddings: bool` 指标。

---

## 7. 维度六：多租户 RBAC、安全交付与全链路可观测性（详解问题 #13b/c、#15a）

### 7.1 问题根因（WHY）

面向生产团队多人协作与容器编排平台（Kubernetes / ECS）交付时，原型在安全治理与运维可观测性上存在明显缺口：

- **问题 #13b/c（单租户无分权、缺少防滥用护栏与容器交付薄弱）**：
  1. 仅支持单个全局 `AGENT_LAB_TOKEN`：所有调用者共享同一任务列表与长期记忆，无法按团队/租户隔离数据，更无法执行高风险编码场景必需的**“任务发起人（Operator）与补丁审批人（Approver）职责分离（双人复核）”**。
  2. 缺少按租户请求频率限制、持久队列积压上限与流式请求体大小硬限制（攻击者可发送无 `Content-Length` 的超大 chunked 请求撑爆内存）。
  3. 容器监听 `0.0.0.0` 时若忘记配置鉴权口令会直接裸奔暴露 API；`Dockerfile` 缺少 `git` 依赖与原生 `HEALTHCHECK` 探针。
- **问题 #15a（黑盒排障困难与前端全量轮询风暴）**：
  1. 缺乏跨 HTTP 网关、异步线程池与独立 `LeaseWorker` 进程的统一分布式追踪 ID；异常信息若直接写入日志或返回客户端，可能泄露内嵌在 URL 或请求头中的 `Bearer` Token / 数据库 DSN 密码；缺乏标准 Prometheus 监控指标与就绪探针。
  2. 前端 `web/app.js` 每 700ms 轮询一次 `GET /api/runs/{id}/events`（无游标重拉全量数千条事件）与 `GET /api/runs/{id}/repomap`（无条件触发后端磁盘扫描），并全量清空重建 DOM，长程任务后期造成严重的带宽与 CPU 风暴。

### 7.2 工程解决机制（HOW）

#### （1）多租户 Token 鉴权、RBAC 角色分权、异人审批与租户记忆隔离（`api.py`）
- **结构化身份模型 `Principal`**：通过 `AGENT_LAB_TOKENS_JSON` 配置多令牌映射（每个令牌绑定校验后的 `tenant_id`、`actor_id`、`roles: {"operator", "approver", "admin"}` 及可选 `global_admin`），鉴权全程使用 `hmac.compare_digest` 常量时间比对防时序侧信道。
- **细粒度 RBAC 与租户数据隔离**：
  - 创建/恢复任务、写入记忆要求 `operator` 或 `admin`；审批补丁要求 `approver` 或 `admin`；触发 `/api/admin/gc` 要求 `admin`。
  - 非全局管理员访问跨租户 `run_id` 时，`_can_access_run` 统一返回 `404 任务不存在`（防止枚举探测其他租户任务 ID）；长期记忆按 `Principal.memory_scope()` 自动隔离到 `tenant:<tenant_id>:user` 命名空间。
- **双人复核（四眼原则）强制门禁**：开启 `AGENT_LAB_REQUIRE_DISTINCT_APPROVER=1` 后，`POST /api/runs/{run_id}/proposals/{proposal_id}` 强制核对 `principal.actor_id != run["metadata"]["created_by"]`，禁止任务发起人自行批准自己触发的代码补丁（返回 HTTP `403`）。

#### （2）流式 413 体积拦截、租户滑动限流、队列背压与容器安全加固（`api.py` / `cli.py` / `Dockerfile`）
- **双阶段请求体上限拦截（`_GuardMiddleware`）**：先校验 `Content-Length` 头，再对 `POST/PUT/PATCH` 的 ASGI `receive()` 消息流做逐块累加检查；无论客户端是否谎报或省略 `Content-Length`，一旦字节数超过 `AGENT_LAB_MAX_BODY_BYTES`（默认 256 KiB，内部沙箱路由默认 4 MiB），立即中断读取并返回 HTTP `413`。同时校验 `TrustedHostMiddleware` 与同源 `Origin` 防御 DNS Rebinding 与跨站请求伪造。
- **租户限流与队列背压**：`_TenantRateLimiter` 实现按租户 60 秒滑动窗口限流（`AGENT_LAB_RATE_LIMIT_RPM`，默认 120 RPM，内置 1024 租户窗口有界淘汰防内存膨胀）；队列模式下创建任务前检查活跃 `queued + claimed` 深度，达到 `AGENT_LAB_MAX_QUEUE_DEPTH`（默认 64）立即返回 HTTP `429` 保护数据库。
- **CLI 非回环强制鉴权与 `Dockerfile` 生产化**：
  - `agent-lab serve --host 0.0.0.0` 自动检测非回环监听地址：若未配置 `AGENT_LAB_TOKEN` / `AGENT_LAB_TOKENS_JSON` 且未显式声明 `AGENT_LAB_ALLOW_INSECURE_LOCAL=1`，启动立即报错退出；配置口令后自动置 `AGENT_LAB_REQUIRE_AUTH=1`。
  - `Dockerfile` 安装最小化 `git` 包、`uv sync --locked --no-dev` 锁定依赖、切换非特权用户 `learner` 运行，并内置访问 `http://127.0.0.1:8765/healthz` 的 `HEALTHCHECK` 指令。

#### （3）W3C Trace-ID 贯穿、递归密钥脱敏、Prometheus 指标与双探针（`observability.py` / `api.py`）
- **跨进程 W3C Trace Context 传播**：
  - `_GuardMiddleware` 解析入站 `traceparent` 或 `X-Trace-Id`（缺省自动生成 32 位 hex），通过 `bind_trace_context` 绑定至 `ContextVar`，并在所有响应头回写 `X-Trace-Id`。
  - 创建任务时通过 `format_traceparent` 将标准 `00-<trace_id>-<span_id>-01` 持久化进 `run["metadata"]["traceparent"]`；独立 `LeaseWorker` 进程领取任务后在 `process_one` 中调用 `bind_trace_context(trace_ref)` 恢复同一 `trace_id`，实现跨 API 与多 Worker 节点的全链路串联。
- **结构化脱敏日志与安全错误摘要**：
  - `redact_secrets` 递归扫描字典/列表/字符串，对命中 `key|token|secret|authorization|password|credential` 的键名及字符串中内嵌的 `Bearer <tok>`、`scheme://user:pass@host` 凭据统一替换为 `[REDACTED]`。
  - `sanitize_error_detail(exc)` 仅提取 `error_type`、`error_code`、`retryable` 与 `trace_id`，绝不向客户端或事件表泄露原始 SDK 异常栈中的私有网关 URL 或请求头。
- **Prometheus `/metrics` 与 `/healthz`、`/readyz` 探针**：
  - `MetricsRegistry` 线程安全采集 HTTP 请求量与延迟直方图（`agent_lab_http_requests_total`、`agent_lab_http_request_duration_ms`）、任务生命周期计数（`created` / `completed` / `failed` / `cancelled` / `rejected`）、Worker 被隔离次数（`agent_lab_worker_fenced_total`）以及模型调用延迟与输入输出 Token 总量，通过 `GET /metrics` 输出标准 Prometheus 0.0.4 文本格式（启用鉴权时受 Bearer 保护）。
  - `GET /healthz` 提供轻量存活检查；`GET /readyz` 异步探活数据库连接、工作区目录真实可写性、沙箱可用模式（`remote_http` / `local_docker`）及已注册 Worker 数量，在配置 `AGENT_LAB_REQUIRE_SANDBOX=1` 或 `AGENT_LAB_REQUIRE_WORKER=1` 不满足时精准返回 HTTP `503`。

#### （4）前端增量事件游标与条件加载防轮询风暴（`web/app.js`）
- **单飞互斥轮询（Single-Flight Refresh）**：通过 `refreshInFlight` 与 `refreshQueued` 合并并发触发的刷新请求，杜绝慢网络下多个定时器请求重叠堆积。
- **增量游标拉取（`after=eventCursor`）**：前端在内存维护当前任务的 `cachedEvents` 与最大 `eventCursor`，每次轮询仅请求 `/api/runs/${id}/events?after=${eventCursor}` 拉取新增事件（单批满 2000 条时有界翻页追平），仅在 `appended === true` 时重绘时间线 DOM。
- **按需加载 RepoMap**：以 `${id}:${run.steps}:${ver.status}` 作为复合缓存键 `lastRepomapKey`，在任务步数与验证状态未发生变化且折叠面板未展开时跳过 `/repomap` 请求，消除后台无意义的重复索引读取。

---

## 8. 生产环境变量配置速查表 & 一键验证命令清单

### 8.1 生产环境变量配置速查表

| 分类 | 环境变量名 | 默认值 | 生产推荐值 / 格式说明 | 作用模块 |
| :--- | :--- | :--- | :--- | :--- |
| **认证与多租户 RBAC** | `AGENT_LAB_REQUIRE_AUTH` | `""` (非回环自动置 `1`) | `1` | 强制要求配置鉴权令牌，否则拒绝启动 (`api.py`, `cli.py`) |
| **认证与多租户 RBAC** | `AGENT_LAB_TOKENS_JSON` | `""` | JSON 映射：`{"tok_op":{"tenant_id":"t1","actor_id":"alice","roles":["operator"]}}` | 多租户身份、角色 (`operator`/`approver`/`admin`) 与全局管理员配置 (`api.py`) |
| **认证与多租户 RBAC** | `AGENT_LAB_TOKEN` | `""` | 高熵随机字符串 | 单租户快捷管理员 Bearer 令牌 (`api.py`) |
| **认证与多租户 RBAC** | `AGENT_LAB_REQUIRE_DISTINCT_APPROVER` | `""` | `1` | 开启双人复核：禁止任务创建者自行批准补丁 (`api.py`) |
| **认证与多租户 RBAC** | `AGENT_LAB_ALLOWED_HOSTS` | `localhost,127.0.0.1,[::1]` | 生产域名列表（逗号分隔） | `TrustedHostMiddleware` 允许的 Host 头白名单 (`api.py`) |
| **认证与多租户 RBAC** | `AGENT_LAB_ALLOW_INSECURE_LOCAL` | `""` | 生产严禁开启（仅本地容器映射调试置 `1`） | 允许非回环地址在无 Token 下监听 (`cli.py`) |
| **流量防护与背压** | `AGENT_LAB_RATE_LIMIT_RPM` | `120` | 按租户规模设置（如 `60`—`600`） | 单租户每分钟创建任务滑动窗口上限 (`api.py`) |
| **流量防护与背压** | `AGENT_LAB_MAX_QUEUE_DEPTH` | `64` | 按 Worker 并发吞吐设置（如 `128`） | `queue` 模式下待处理任务最大积压深度 (`api.py`) |
| **流量防护与背压** | `AGENT_LAB_MAX_BODY_BYTES` | `262144` (256 KiB) | `262144` | 普通 API 请求体最大字节数，超限返回 HTTP 413 (`api.py`) |
| **流量防护与背压** | `AGENT_LAB_SANDBOX_MAX_BODY_BYTES` | `4194304` (4 MiB) | `4194304` | `/api/internal/sandbox/execute` 专用载荷上限 (`api.py`) |
| **存储、调度与跨节点** | `AGENT_LAB_DISPATCH` | `inline` | `queue` | 调度模式：`inline` 单进程协程 / `queue` 独立 Worker 消费 (`api.py`) |
| **存储、调度与跨节点** | `AGENT_LAB_STORAGE_DSN` | 本地 `.agent-lab/state.db` | `postgresql://user:pass@host:5432/agent_lab` | 状态后端 DSN，支持 `sqlite:///` 与 `postgresql://` (`storage.py`, `api.py`) |
| **存储、调度与跨节点** | `AGENT_LAB_SQLITE_SYNC` | `NORMAL` | `NORMAL` 或高持久场景 `FULL` | SQLite WAL `PRAGMA synchronous` 级别 (`storage.py`) |
| **存储、调度与跨节点** | `AGENT_LAB_SYNC_WORKSPACE_ARCHIVE` | `""` | 跨机器无共享磁盘 Worker 集群置 `1` | 自动将工作区打包存入 `workspace_archives` 并在接管节点解包 (`runtime.py`, `lease.py`) |
| **存储、调度与跨节点** | `AGENT_LAB_WORKER_WORKSPACES_DIR` | `<state>/worker_workspaces/<id>` | 节点本地高速 SSD 目录路径 | 远程 Worker 解包物化工作区的本地根目录 (`lease.py`) |
| **存储、调度与跨节点** | `AGENT_LAB_REQUIRE_WORKER` | `""` | `queue` 模式下置 `1` | 无已登记 Worker 时令 `/readyz` 返回 HTTP 503 (`api.py`) |
| **模型网关与限流预算** | `AGENT_PROVIDER` | `demo` | `anthropic` 或 `openai` | 默认模型适配器类型 (`api.py`, `providers.py`) |
| **模型网关与限流预算** | `ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` | `""` | 真实密钥 + `ANTHROPIC_MODEL` | Anthropic 认证凭据与模型 ID (`providers.py`) |
| **模型网关与限流预算** | `ANTHROPIC_THINKING_BUDGET` | `""` | `>= 1024` 的整数（如 `2048`） | 开启 Extended Thinking 推理预算并自动兼容缓存断点 (`providers.py`) |
| **模型网关与限流预算** | `OPENAI_API_KEY` / `OPENAI_MODEL` | `""` | 真实密钥 + 模型名 | OpenAI 兼容网关凭据与模型 ID (`providers.py`) |
| **模型网关与限流预算** | `AGENT_ALLOW_CUSTOM_BASE_URL` | `""` | 自定义网关时置 `1` | 允许读取 `ANTHROPIC_BASE_URL` 或 `OPENAI_BASE_URL` (`providers.py`) |
| **模型网关与限流预算** | `OPENAI_FALLBACK_BASE_URLS` | `""` | 逗号分隔的备用网关 URL 列表 | 主网关 429/5xx/超时重试耗尽后自动故障转移 (`providers.py`) |
| **模型网关与限流预算** | `OPENAI_STREAM` | `""` | 长输出场景置 `1` | 启用 SSE 流式接收并在客户端增量组装文本与 `tool_calls` (`providers.py`) |
| **模型网关与限流预算** | `OPENAI_SEND_PROMPT_CACHE_KEY` | 默认按域名自适应 | 严格网关可显式置 `0` | 控制是否发送 `prompt_cache_key` 路由提示 (`providers.py`) |
| **模型网关与限流预算** | `AGENT_LAB_RPM_LIMIT` / `AGENT_LAB_TPM_LIMIT` | 未限制 | 按上游配额设置正整数 | `ProviderRateLimiter` 60 秒滑动窗口准入控制 (`providers.py`) |
| **模型网关与限流预算** | `AGENT_LAB_USE_TIKTOKEN` | `""` | 安装 `tiktoken` 时可置 `1` | 切换 `cl100k_base` 精确分词替代内置零依赖校准估算器 (`context.py`) |
| **沙箱隔离与探针** | `AGENT_LAB_SANDBOX_CONCURRENCY` | `4` | 按宿主机 CPU/内存核数设置 | 单节点同时运行 Docker 测试容器的最大并发信号量 (`sandbox.py`) |
| **沙箱隔离与探针** | `AGENT_LAB_SANDBOX_URL` | `""` | `https://sandbox-pool.internal:8765` | 启用 `RemoteHTTPSandboxBackend` 解耦本地 Docker 守护进程 (`sandbox.py`) |
| **沙箱隔离与探针** | `AGENT_LAB_SANDBOX_TOKEN` | `""` | 独立高熵内部共享密钥 | 远程沙箱 `/api/internal/sandbox/execute` 专用 Bearer 鉴权口令 (`sandbox.py`, `api.py`) |
| **沙箱隔离与探针** | `AGENT_LAB_REQUIRE_SANDBOX` | `""` | `1` | 本地 Docker 与远程沙箱均不可用时令 `/readyz` 返回 HTTP 503 (`api.py`) |

---

### 8.2 一键验证命令清单

在仓库根目录执行以下命令，可离线确定性复验上述全部 15 项生产加固特性：

```bash
# 1. 运行全套单元测试与高可用集成回归套件（涵盖存储 CAS、租约抢占、DLQ、多文件崩溃恢复、网关韧性、双进程沙箱、多语言 RepoMap、RBAC 与可观测性）
uv run pytest -q

# 2. 专项验证分布式租约世代隔离（Epoch Fencing）、毒丸死信队列、跨节点归档物化与 SIGTERM 优雅重入队
uv run pytest tests/test_lease_fencing.py tests/test_git_ha.py tests/test_production_api_ha.py -v

# 3. 专项验证双进程 HMAC 防伪沙箱、sys.addaudithook 内省拦截、远程 HTTP 沙箱后端与孤儿容器回收
uv run pytest tests/test_sandbox.py tests/test_repair_loop.py -v

# 4. 专项验证 BPE 双预算、紧急尾部大参数防死锁、动态技能尾部保活、工作记忆防投毒与多语言代码地图
uv run pytest tests/test_context_budget.py tests/test_kv_cache_harness.py tests/test_working_memory.py tests/test_repomap.py -v

# 5. 运行 Harness 控制流不变量离线评测与 KV Cache 阶梯压缩消融基准
uv run agent-lab eval
uv run agent-lab kv-benchmark --turns 16 --max-chars 6800

# 6. 运行 5 任务真实 Git Worktree 编码基准流水线自检（含跨文件原子变更集与测试失败自动修复闭环；本地需已准备 python:3.12-slim 镜像）
uv run agent-lab model-eval --provider reference

# 7. 启动多租户 RBAC 队列模式控制面 + 独立 Worker 进程并验证探针与指标
export AGENT_LAB_TOKENS_JSON='{"op_tok":{"tenant_id":"team_a","actor_id":"alice","roles":["operator"]},"ap_tok":{"tenant_id":"team_a","actor_id":"bob","roles":["approver","admin"]}}'
export AGENT_LAB_REQUIRE_DISTINCT_APPROVER=1
export AGENT_LAB_DISPATCH=queue
uv run agent-lab serve --host 127.0.0.1 --port 8765 &
uv run agent-lab worker --worker-id worker-1 --lease-ttl 15 &
curl -s http://127.0.0.1:8765/healthz
curl -s http://127.0.0.1:8765/readyz
curl -s -H "Authorization: Bearer ap_tok" http://127.0.0.1:8765/metrics | head -n 25

# 8. 执行终态过期工作区、数据库归档、陈旧锁与孤儿沙箱容器一键回收
uv run agent-lab gc --retention-seconds 86400
```

---

### 8.3 诚实工程边界说明（Engineering Honesty Boundaries）

为避免过度承诺，部署与评审时须明确本系统的真实安全与架构边界：

1. **容器隔离边界**：`SandboxRunner` 通过只读快照、禁网（`--network=none`）、丢弃全部 Linux Capabilities（`--cap-drop=ALL`）、非特权 UID `65534`、PID/内存硬限额以及双进程匿名管道 HMAC + `sys.addaudithook` 极大压缩了待测代码的逃逸与伪造空间；但它本质上仍共享宿主机 Linux 内核。面对不可信多租户恶意内核漏洞攻击时，生产环境应将远程沙箱池节点运行在 **gVisor (`runsc`)、Kata Containers 或 Firecracker MicroVM** 运行时之上。
2. **存储水平扩展边界**：SQLite WAL 模式下的 `Storage` 仅适用于**单台物理机/共享本地 POSIX 卷**上的多 Worker 进程并发，严禁将 SQLite 数据库文件挂载在 NFS / SMB 等网络文件系统上使用；跨多台物理主机部署无状态 API 与 `LeaseWorker` 集群时，必须安装 `psycopg[binary]` 并通过 `AGENT_LAB_STORAGE_DSN=postgresql://...` 切换至 PostgreSQL 后端，同时开启 `AGENT_LAB_SYNC_WORKSPACE_ARCHIVE=1` 实现跨节点工作区传递。
3. **进程内指标聚合边界**：`observability.METRICS` 为单进程线程安全注册表；当使用多进程部署多个 API 副本与独立 `LeaseWorker` 进程时，应由 Prometheus Server 分别抓取各实例端点或接入 OpenTelemetry Collector 汇总，单实例 `/metrics` 仅反映该进程自身的计数器与直方图快照。
4. **敏感信息静态过滤边界**：工具层、Git 提交层与沙箱快照层拦截了常见凭证文件名与后缀（如 `.env`、`secrets.py`、`*.pem`、`id_rsa`），日志层递归脱敏了密钥键名与 `Bearer`/DSN 凭据，但这不能替代专门的密钥扫描引擎来识别业务源码任意普通变量中硬编码的明文密码。
5. **评测真值诚实性边界**：`DemoProvider` 与 `BenchmarkReferenceProvider` 均为确定性工程状态机验证脚本，仅用于离线验证 Harness 恢复闭环、审批栅栏与评测管线本身，其通过率绝不代表任何真实大语言模型的编码智能水平。

