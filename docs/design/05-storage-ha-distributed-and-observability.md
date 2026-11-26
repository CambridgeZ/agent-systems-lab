# 05 — 持久化存储、高可用分布式队列、触发调度与可观测性架构设计

本文档深度剖析 Agent Systems Lab 的**双后端事务存储与版本化迁移体系**、**跨进程 POSIX 内核文件互斥锁**、**分布式持久队列与世代栅栏 Worker**、**零依赖自主 Cron/Webhook 触发引擎**以及**W3C 分布式追踪 / 零信任脱敏 / Prometheus 可观测性基础设施**。文内所有数据表、列定义、索引、迁移版本号、常量、配额、状态枚举及源码行号均逐行核对自代码仓真实实现。

覆盖源码范围（共 5 个核心文件，总计 7,313 行）：
- 事务存储与分布式状态账本：[src/agent_lab/storage.py](../../src/agent_lab/storage.py)（L1–L4234，4,234 行）
- 跨进程本机互斥锁：[src/agent_lab/execution_lock.py](../../src/agent_lab/execution_lock.py)（L1–L273，273 行）
- 分布式持久队列与租约 Worker：[src/agent_lab/lease.py](../../src/agent_lab/lease.py)（L1–L872，872 行）
- 自主 Cron / Webhook 触发调度引擎：[src/agent_lab/triggers.py](../../src/agent_lab/triggers.py)（L1–L1116，1,116 行）
- 链路追踪、凭据脱敏与 Prometheus 指标：[src/agent_lab/observability.py](../../src/agent_lab/observability.py)（L1–L818，818 行）

---

## 0. 全局分层架构与并发协调总览

Agent Systems Lab 在单机开发态（SQLite WAL + 本机 POSIX `flock`）与多节点生产态（PostgreSQL + `psycopg_pool` + `FOR UPDATE SKIP LOCKED` + `LISTEN/NOTIFY`）之间保持**同一套事务语义、同一套 8 步版本化 Schema 迁移与同一套 Kleppmann Monotonic Fencing Token (`epoch`) 防脑裂协议**。

```mermaid
flowchart TB
    subgraph ENTRY["调用入口与触发源"]
        API["FastAPI Server<br/>/api/runs /api/triggers<br/>/api/webhooks/{id} /metrics"]
        CLI["CLI & Worker Daemon<br/>worker / gc / trigger"]
        TRIG["TriggerScheduler (triggers.py)<br/>Cron 15s 周期扫描 + Webhook HMAC-SHA256"]
    end

    subgraph OBS["可观测性与安全脱敏底座 (observability.py)"]
        TRACE["W3C traceparent 校验<br/>ContextVars: trace_id / span_id / parent_span_id"]
        OTLP["_BackgroundOTLPExporter<br/>有界队列(256) + SSRF/IMDS/重定向拦截"]
        REDACT["redact_secrets & sanitize_error_detail<br/>键名/Bearer/DSN 递归脱敏"]
        PROM["MetricsRegistry (METRICS)<br/>短临界区快照 + Prometheus 0.0.4"]
    end

    subgraph DIST["高可用分布式执行层 (lease.py + execution_lock.py)"]
        WORKER["LeaseWorker<br/>claim_next_run + 1/3 TTL 心跳自熔断<br/>跨节点工作区物化 + 出带 auto_dream"]
        WAKE["Push 唤醒通道<br/>_wakeup_event + PG LISTEN agent_lab_queue<br/>+ 自适应轮询回退"]
        WMS["WorkerMetricsServer<br/>纯标准库 HTTP /metrics & /healthz"]
        FLOCK["exclusive_run (execution_lock.py)<br/>O_NOFOLLOW + inode 校验 + 非阻塞 flock"]
    end

    subgraph STORE["双后端事务存储层 (storage.py)"]
        DIALECT["_translate_qmark_to_pyformat<br/>词法级 ? -> %s & % -> %% 翻译"]
        MIG["8 步幂等版本化 Schema 迁移<br/>schema_migrations (v1..v8)"]
        SQLITE["SQLite 后端<br/>WAL + busy_timeout=10000 + 8次退避启动"]
        PG["PostgreSQL 后端<br/>PostgresBackendConfig + psycopg_pool<br/>pg_advisory_xact_lock + pg_notify"]
        TABLES["16 张核心数据表<br/>runs / run_leases / proposals / tool_calls<br/>events / run_steering_messages / trigger_* ..."]
    end

    API --> STORE
    CLI --> STORE
    TRIG -->|"record_trigger_fire 原子去重"| STORE
    TRIG -->|"dispatch_fn"| API

    API -.->|"pg_notify / notify_wakeup"| WAKE
    WAKE --> WORKER
    WORKER --> FLOCK
    WORKER -->|"epoch fencing 读写"| STORE
    WORKER --> WMS

    DIALECT --> PG
    MIG --> SQLITE
    MIG --> PG
    SQLITE --> TABLES
    PG --> TABLES

    API -.-> OBS
    WORKER -.-> OBS
    TRIG -.-> OBS
    STORE -.->|"trace_id/span_id 落盘<br/>审计日志 redact_secrets"| OBS
```

---

## 1. 双后端事务存储引擎（`src/agent_lab/storage.py`）

### 1.1 模块定位与核心职责

- **源码路径**：[src/agent_lab/storage.py](../../src/agent_lab/storage.py)（L1–L4234）
- **核心职责**：
  1. **双后端统一抽象**：通过 `Storage`（L383）与 `Storage.from_dsn`（L438–L475）同时支持单机 SQLite（WAL 模式）与多节点 PostgreSQL（`PostgresBackendConfig`，L176–L228；`_PostgresConnectionAdapter`，L231–L272；惰性 `psycopg_pool.ConnectionPool`，L477–L515）。
  2. **跨方言版本化 Schema 迁移**：维护 `schema_migrations` 元数据表与 8 个幂等迁移步骤（L542–L1018），冷启动通过 SQLite `BEGIN IMMEDIATE` + 8 次指数退避（L427–L435）或 PostgreSQL `pg_advisory_xact_lock(7281946510293847)`（L1036–L1040）串行化 DDL。
  3. **词法安全 SQL 翻译**：`_translate_qmark_to_pyformat`（L71–L172）将 SQLite 风格 `?` 参数翻译为 psycopg `%s`，并完整处理单行注释、嵌套块注释、`E'...'` 字符串、双引号标识符、`$tag$...$tag$` dollar-quote 与字面 `% -> %%` 转义。
  4. **分布式租约与双层乐观并发**：提供 Kleppmann 语义单调递增 `epoch` 世代栅栏（`_verify_epoch_tx`，L1191–L1214）、`runs.state_version` CAS 写保护（`save_run`，L1875–L1943）、`FOR UPDATE SKIP LOCKED` 无锁竞争领取（`claim_next_run`，L1280–L1400）、毒丸死信隔离（`dead_letter`，L1329–L1367）与 `pg_notify('agent_lab_queue', ?)` 推送唤醒（L1028–L1034）。
  5. **全生命周期业务账本**：覆盖会话 CRUD / 键集游标分页 / 重命名 / 归档 / 删除 / 分叉自愈（`fork_run`）、统一文件/动作审批状态机（`proposals` + `approve_and_enqueue`）、工具调用幂等账本（`tool_calls`）、零 CAS 冲突运行期转向收件箱（`run_steering_messages`）、多轮对话接续与预算延展（`append_user_turn`）、工作区跨节点归档与 `direct_workspace` 豁免 GC（`workspace_archives` / `gc_expired_resources`）、会话检查点（`run_checkpoints`）、仅追加脱敏安全审计日志（`security_audit_logs`）、A2A 上下文索引与 Push Webhook 配置（`a2a_push_configs`）以及自主触发器定义与去重记录（`trigger_definitions` / `trigger_fires`）。

---

### 1.2 完整实体关系图（ER Diagram）

共包含 **16 张物理表**（15 张业务表 + 1 张 `schema_migrations` 迁移注册表）。所有逻辑/物理外键关系均经源码逐表核验：

```mermaid
erDiagram
    schema_migrations {
        INTEGER version PK
        TEXT name
        TEXT applied_at
    }

    runs {
        TEXT id PK
        TEXT created_at
        TEXT body
        INTEGER state_version
        TEXT status_col
        TEXT updated_at_col
        TEXT tenant_id
        TEXT summary_json
        TEXT title
        INTEGER archived
        TEXT a2a_context_id
    }

    run_leases {
        TEXT run_id PK,FK
        TEXT queue_state
        TEXT action
        TEXT owner_id
        INTEGER epoch
        INTEGER takeover_count
        REAL lease_until
        TEXT heartbeat_at
        TEXT updated_at
    }

    events {
        INTEGER id PK
        TEXT run_id FK
        TEXT kind
        TEXT created_at
        TEXT payload
        TEXT trace_id
        TEXT span_id
    }

    proposals {
        TEXT id PK
        TEXT run_id FK
        TEXT body
        TEXT operation_id
        INTEGER seq
    }

    tool_calls {
        TEXT run_id PK,FK
        TEXT call_id PK
        TEXT name
        TEXT arguments_sha256
        TEXT status
        TEXT created_at
        TEXT body
    }

    workspace_archives {
        TEXT run_id PK,FK
        INTEGER epoch
        TEXT git_head
        BLOB archive
        TEXT updated_at
    }

    run_steering_messages {
        INTEGER id PK
        TEXT run_id FK
        TEXT prompt
        TEXT actor_id
        TEXT created_at
        TEXT consumed_at
    }

    run_checkpoints {
        TEXT checkpoint_id PK
        TEXT run_id
        TEXT tenant_id
        INTEGER turn_index
        INTEGER message_idx
        TEXT label
        TEXT file_manifest_json
        TEXT git_tree
        TEXT created_at
    }

    a2a_push_configs {
        TEXT task_id PK
        TEXT tenant_id
        TEXT url
        TEXT token_hash
        TEXT secret
        TEXT events_json
        TEXT created_at
        TEXT updated_at
    }

    worker_nodes {
        TEXT worker_id PK
        INTEGER pid
        TEXT host
        TEXT started_at
        TEXT last_seen_at
        TEXT active_run_id
        INTEGER active_epoch
        TEXT status
    }

    memories {
        TEXT scope PK
        TEXT key PK
        TEXT value
        TEXT updated_at
    }

    dream_runs {
        TEXT id PK
        TEXT tenant_id
        TEXT project_slug
        TEXT created_at
        TEXT body
    }

    security_audit_logs {
        INTEGER id PK
        TEXT tenant_id
        TEXT actor
        TEXT action
        TEXT resource
        TEXT outcome
        TEXT detail_json
        TEXT trace_id
        TEXT created_at
    }

    trigger_definitions {
        TEXT id PK
        TEXT tenant_id
        TEXT name
        TEXT kind
        TEXT cron_expr
        TEXT webhook_secret
        TEXT prompt_template
        TEXT workspace_path
        INTEGER enabled
        TEXT last_fired_at
        TEXT metadata_json
        TEXT created_at
        TEXT updated_at
    }

    trigger_fires {
        INTEGER id PK
        TEXT trigger_id
        TEXT dedup_key UK
        TEXT run_id
        TEXT status
        TEXT created_at
    }

    runs ||--o| run_leases : "FK run_id"
    runs ||--o{ events : "FK run_id"
    runs ||--o{ proposals : "FK run_id"
    runs ||--o{ tool_calls : "FK run_id"
    runs ||--o| workspace_archives : "FK run_id"
    runs ||--o{ run_steering_messages : "FK run_id"
    runs ||--o{ run_checkpoints : "logical run_id"
    runs ||--o| a2a_push_configs : "logical task_id"
    worker_nodes }o--o| runs : "logical active_run_id"
    trigger_definitions ||--o{ trigger_fires : "logical trigger_id"
    trigger_fires }o--o| runs : "logical run_id"
```

---

### 1.3 版本化 Schema 迁移清单（`schema_migrations` v1–v8）

`_migrations()`（L542–L984）定义跨 SQLite 与 PostgreSQL 共享的 8 个有序幂等迁移，由 `_run_migrations()`（L986–L1010）在排他事务中按版本号升序执行，并以 `INSERT ... ON CONFLICT(version) DO NOTHING` 登记至 `schema_migrations`。

| 版本 | 迁移名称（源码字面量） | 实现函数与行号 | SQLite 与 PostgreSQL 差异及增量变更细节 |
|---:|---|---|---|
| 1 | `baseline_core_schema` | `_m1_baseline_core_schema` (L545–L790) | 建立核心 10 张业务表：`runs`、`events`、`memories`、`proposals`、`tool_calls`、`run_leases`、`worker_nodes`、`workspace_archives`、`dream_runs`、`run_steering_messages`。PG 用 `BIGSERIAL` / `DOUBLE PRECISION` / `BYTEA`；SQLite 用 `INTEGER PRIMARY KEY AUTOINCREMENT` / `REAL` / `BLOB`，并兼容旧库无损升级：自动补齐 `runs` 的 5 个扩展列、`proposals.operation_id` 与部分唯一索引；若旧版 `run_leases` CHECK 约束缺失 `dead_letter`/`cancelled`/`interrupted`，通过重命名 `_run_leases_legacy` -> 重建 -> 迁回 -> 删旧表完成无损重建（L768–L787）。 |
| 2 | `proposals_fifo_seq` | `_m2_proposals_fifo_seq` (L791–L799) | 为 `proposals` 增加严格 FIFO 顺序号列 `seq`（PG：`BIGSERIAL`；SQLite：`INTEGER`，并以 `UPDATE proposals SET seq = rowid WHERE seq IS NULL` 回填历史行），建索引 `proposals_run_seq(run_id, seq)`。 |
| 3 | `run_checkpoints_table` | `_m3_run_checkpoints_table` (L801–L823) | 创建 `run_checkpoints` 表及索引 `run_checkpoints_run_turn(run_id, turn_index, created_at)`、`run_checkpoints_tenant(tenant_id, run_id)`。 |
| 4 | `security_audit_logs_table` | `_m4_security_audit_logs_table` (L825–L845) | 创建仅追加表 `security_audit_logs`（`id` 在 PG 为 `BIGSERIAL`，SQLite 为 `INTEGER PRIMARY KEY AUTOINCREMENT`）及索引 `security_audit_logs_tenant_created(tenant_id, created_at DESC, id DESC)`。 |
| 5 | `events_trace_span_columns` | `_m5_events_trace_span_columns` (L847–L859) | 为 `events` 增加 `trace_id TEXT` 与 `span_id TEXT` 列，并建部分索引 `events_trace(trace_id) WHERE trace_id IS NOT NULL`。 |
| 6 | `runs_session_management_columns` | `_m6_runs_session_management_columns` (L861–L885) | 为 `runs` 增加 `title TEXT` 与 `archived INTEGER NOT NULL DEFAULT 0`，建索引 `runs_tenant_archived_created(tenant_id, archived, created_at DESC, id DESC)`，并扫描 `status_col IS NULL` 的历史行从 `body` JSON 回填 `status_col`。注：`run_steering_messages` 已在 v1 基线中随核心表一并创建。 |
| 7 | `a2a_context_and_push_configs` | `_m7_a2a_context_and_push_configs` (L887–L931) | 为 `runs` 增加 `a2a_context_id TEXT` 列与部分索引 `idx_runs_a2a_context_id(a2a_context_id, tenant_id, created_at DESC) WHERE a2a_context_id IS NOT NULL`（自动从历史 `body.metadata.a2a_context_id` 截断 256 字符回填）；创建 `a2a_push_configs` 表及索引 `idx_a2a_push_configs_tenant(tenant_id, task_id)`。 |
| 8 | `trigger_definitions_and_fires` | `_m8_trigger_definitions_and_fires` (L933–L973) | 创建 `trigger_definitions` 与 `trigger_fires` 两张表，及索引 `idx_trigger_definitions_tenant_enabled(tenant_id, enabled, created_at DESC)`、`idx_trigger_fires_trigger_created(trigger_id, id DESC)`。 |

---

### 1.4 全量 16 张数据表逐列 Schema 参考手册

#### 表 1：`schema_migrations`（L990–L996）
| 列名 | 类型 (SQLite / PG) | 约束 / 默认值 | 说明 |
|---|---|---|---|
| `version` | `INTEGER` | `PRIMARY KEY` | 单调递增迁移版本号（1..8） |
| `name` | `TEXT` | `NOT NULL` | 迁移名称标识符 |
| `applied_at` | `TEXT` | `NOT NULL` | UTC ISO-8601 执行时间戳 |

#### 表 2：`runs`（v1 L548–L553 / L631–L640；v6 L861–L874；v7 L887–L898）
| 列名 | 类型 (SQLite / PG) | 约束 / 默认值 | 索引 / 说明 |
|---|---|---|---|
| `id` | `TEXT` | `PRIMARY KEY` | 32 位十六进制 `uuid4().hex` |
| `created_at` | `TEXT` | `NOT NULL` | UTC ISO-8601 创建时间 |
| `body` | `TEXT` | `NOT NULL` | 完整会话 JSON（含 `messages`、`metadata` 等，`_dump` 序列化且 `allow_nan=False`） |
| `state_version` | `INTEGER` | `NOT NULL DEFAULT 1` | 乐观锁版本号，每次 `save_run` 执行 `WHERE id=? AND state_version=?` CAS 递增 |
| `status_col` | `TEXT` | 可空 | 状态投影列（如 `queued`/`running`/`waiting_approval`/`completed`/`failed`/`budget_exceeded`/`cancelled`/`interrupted`），用于列表 SQL 过滤与 `is_cancel_requested` 快速路径 |
| `updated_at_col` | `TEXT` | 可空 | 最近更新时间戳投影列，用于 GC 保留窗口判定 |
| `tenant_id` | `TEXT` | `NOT NULL DEFAULT 'local'` | 多租户逻辑隔离键 |
| `summary_json` | `TEXT` | 可空 | 不含完整 `messages` 与 `kv_cache.known_blocks` 的轻量摘要 JSON（由 `_build_run_summary` 生成，L1079–L1120），避免 `list_runs` 反序列化超大轨迹 |
| `title` | `TEXT` | 可空（v6） | 会话标题（1..200 字符，`rename_run`/`fork_run` 维护） |
| `archived` | `INTEGER` | `NOT NULL DEFAULT 0`（v6） | 归档标志位（`0`/`1`），默认 `list_runs` 过滤 `COALESCE(archived, 0) = 0` |
| `a2a_context_id` | `TEXT` | 可空（v7） | A2A 协议上下文 ID（最大 256 字符），支撑 `list_runs_by_a2a_context` O(log N) 检索 |

- **索引**：
  - `runs_tenant_created ON runs(tenant_id, created_at DESC)`
  - `runs_tenant_archived_created ON runs(tenant_id, archived, created_at DESC, id DESC)`
  - `idx_runs_a2a_context_id ON runs(a2a_context_id, tenant_id, created_at DESC) WHERE a2a_context_id IS NOT NULL`

#### 表 3：`run_leases`（v1 L578–L588 / L614–L627）
| 列名 | 类型 (SQLite / PG) | 约束 / 默认值 | 说明 |
|---|---|---|---|
| `run_id` | `TEXT` | `PRIMARY KEY REFERENCES runs(id)` | 关联任务 ID（1:1 映射） |
| `queue_state` | `TEXT` | `NOT NULL CHECK(...)` | 10 态枚举：`idle`、`queued`、`claimed`、`waiting_approval`、`completed`、`failed`、`budget_exceeded`、`dead_letter`、`cancelled`、`interrupted` |
| `action` | `TEXT` | `NOT NULL DEFAULT 'execute'` | 队列动作：`execute` 或 `resume` |
| `owner_id` | `TEXT` | 可空 | 当前持租 Worker ID（仅 `claimed` 时非空） |
| `epoch` | `INTEGER` | `NOT NULL DEFAULT 0` | **单调递增 Fencing Token**；领取、内联启动、追问、立即取消均原子 +1 |
| `takeover_count` | `INTEGER` | `NOT NULL DEFAULT 0` | 租约超时被其他 Worker 接管次数；`>= max_takeovers`（默认 3）触发毒丸转入 `dead_letter`；重新入队时归零 |
| `lease_until` | `REAL` / `DOUBLE PRECISION` | 可空 | Unix 秒级浮点数租约到期截止时间 |
| `heartbeat_at` | `TEXT` | 可空 | 最近续租 UTC 时间戳 |
| `updated_at` | `TEXT` | `NOT NULL` | 最近状态变更 UTC 时间戳 |

- **索引**：`run_leases_claimable ON run_leases(queue_state, lease_until)`

#### 表 4：`events`（v1 L554–L558 / L645–L652；v5 L847–L859）
| 列名 | 类型 (SQLite / PG) | 约束 / 默认值 | 说明 |
|---|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` / `BIGSERIAL PRIMARY KEY` | 主键 | 全局自增事件序号（用于 `events(run_id, after)` 增量拉取与 SSE 断线重连） |
| `run_id` | `TEXT` | `NOT NULL REFERENCES runs(id)` | 所属任务 ID |
| `kind` | `TEXT` | `NOT NULL` | 事件类型（如 `lease_claimed`、`run_dead_lettered`、`user_steering_queued`、`user_followup`、`run_forked`、`worker_fenced` 等） |
| `created_at` | `TEXT` | `NOT NULL` | UTC 时间戳 |
| `payload` | `TEXT` | `NOT NULL` | JSON 事件载荷 |
| `trace_id` | `TEXT` | 可空（v5） | 自动从 `current_trace_id()` 注入 |
| `span_id` | `TEXT` | 可空（v5） | 自动从 `current_span_id()` 注入 |

- **索引**：`events_run ON events(run_id, id)`；`events_trace ON events(trace_id) WHERE trace_id IS NOT NULL`

#### 表 5：`proposals`（v1 L563–L568 / L664–L671, L760–L766；v2 L791–L799）
| 列名 | 类型 (SQLite / PG) | 约束 / 默认值 | 说明 |
|---|---|---|---|
| `id` | `TEXT` | `PRIMARY KEY` | 提案唯一 ID（`uuid4().hex`） |
| `run_id` | `TEXT` | `NOT NULL REFERENCES runs(id)` | 所属任务 ID |
| `body` | `TEXT` | `NOT NULL` | 提案 JSON：统一承载文件补丁（`single_file`/`changeset`，最多 8 文件，带 `old_sha256`/`new_sha256`）与非文件动作（`command`/`gui_action`/`mcp_call`/`a2a_call`/`user_question`/`plan_approval`，带 `execution_result` 与 `answer_text`） |
| `operation_id` | `TEXT` | 可空 | 工具 `call_id` 幂等键，配合部分唯一索引防崩溃重放重复建单与参数篡改冲突（`proposal_operation_conflict`） |
| `seq` | `INTEGER` / `BIGSERIAL` | 可空（SQLite 写入时 `MAX(seq)+1`；PG 自动生成）（v2） | 严格 FIFO 展示与处理顺序号 |

- **索引**：`proposals_run ON proposals(run_id)`；`proposals_operation UNIQUE ON proposals(run_id, operation_id) WHERE operation_id IS NOT NULL`；`proposals_run_seq ON proposals(run_id, seq)`

#### 表 6：`tool_calls`（v1 L569–L577 / L674–L688）
| 列名 | 类型 (SQLite / PG) | 约束 / 默认值 | 说明 |
|---|---|---|---|
| `run_id` | `TEXT` | `NOT NULL REFERENCES runs(id)`，联合主键 | 所属任务 ID |
| `call_id` | `TEXT` | `NOT NULL`，联合主键 | 模型发出的工具调用 ID（1..128 字符） |
| `name` | `TEXT` | `NOT NULL` | 工具名；相同 `(run_id, call_id)` 重调度时必须匹配（否则抛 `tool_call_identity_conflict`） |
| `arguments_sha256` | `TEXT` | `NOT NULL` | `canonical_json(arguments)` 的 SHA-256 指纹，阻断参数变更伪造重放 |
| `status` | `TEXT` | `NOT NULL CHECK(...)` | 6 态枚举：`scheduled`、`running`、`succeeded`、`failed`、`waiting_approval`、`unknown` |
| `created_at` | `TEXT` | `NOT NULL` | 调度登记时间 |
| `body` | `TEXT` | `NOT NULL` | 完整调用记录 JSON（含 `attempts`、`attempt_history`、`result`、`result_sha256`），支持 `get_call_result(offset, limit)` 1..8000 字符分页取回 |

- **索引**：`PRIMARY KEY(run_id, call_id)`；`tool_calls_run_status ON tool_calls(run_id, status)`

#### 表 7：`run_steering_messages`（v1 L603–L607 / L732–L745）
| 列名 | 类型 (SQLite / PG) | 约束 / 默认值 | 说明 |
|---|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` / `BIGSERIAL PRIMARY KEY` | 主键 | FIFO 消费排序键 |
| `run_id` | `TEXT` | `NOT NULL REFERENCES runs(id)` | 所属任务 ID |
| `prompt` | `TEXT` | `NOT NULL` | 运行期实时人类转向指令（1..8000 字符） |
| `actor_id` | `TEXT` | 可空 | 操作者标识 |
| `created_at` | `TEXT` | `NOT NULL` | 入队时间 |
| `consumed_at` | `TEXT` | 可空 | 消费时间（`NULL` 表示待消费） |

- **索引**：`run_steering_messages_pending ON run_steering_messages(run_id, consumed_at, id)`

#### 表 8：`workspace_archives`（v1 L594–L597 / L721–L727）
| 列名 | 类型 (SQLite / PG) | 约束 / 默认值 | 说明 |
|---|---|---|---|
| `run_id` | `TEXT` | `PRIMARY KEY REFERENCES runs(id)` | 所属任务 ID（1:1 覆盖最新世代快照） |
| `epoch` | `INTEGER` | `NOT NULL` | 归档时的执行世代号；写入时强校验 `epoch >= run_leases.epoch` 且 `epoch >= existing.epoch` |
| `git_head` | `TEXT` | 可空 | 归档时的 Git `HEAD` commit hash |
| `archive` | `BLOB` / `BYTEA` | `NOT NULL` | `pack_workspace_archive` 生成的压缩归档二进制流 |
| `updated_at` | `TEXT` | `NOT NULL` | 归档写入时间（参与 GC 过期判断） |

#### 表 9：`worker_nodes`（v1 L589–L593 / L693–L702）
| 列名 | 类型 (SQLite / PG) | 约束 / 默认值 | 说明 |
|---|---|---|---|
| `worker_id` | `TEXT` | `PRIMARY KEY` | Worker 节点唯一标识（默认 `worker-{pid}-{uuid8}`） |
| `pid` | `INTEGER` | `NOT NULL` | 操作系统进程 ID |
| `host` | `TEXT` | `NOT NULL` | 主机名（截断至 120 字符） |
| `started_at` | `TEXT` | `NOT NULL` | 首次注册时间 |
| `last_seen_at` | `TEXT` | `NOT NULL` | 最近心跳/状态刷新时间 |
| `active_run_id` | `TEXT` | 可空 | 当前正在执行的任务 ID |
| `active_epoch` | `INTEGER` | 可空 | 当前执行世代号 |
| `status` | `TEXT` | `NOT NULL` | 节点状态（`idle`/`busy`/`stopped`，截断至 40 字符） |

#### 表 10：`memories`（v1 L559–L562 / L655–L660）
| 列名 | 类型 (SQLite / PG) | 约束 / 默认值 | 说明 |
|---|---|---|---|
| `scope` | `TEXT` | `NOT NULL`，联合主键 | 记忆命名空间（1..200 字符） |
| `key` | `TEXT` | `NOT NULL`，联合主键 | 键名（1..200 字符） |
| `value` | `TEXT` | `NOT NULL` | JSON 序列化值（编码后上限 16,000 字符，超限抛 `memory_too_large`） |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

#### 表 11：`dream_runs`（v1 L598–L602 / L707–L718）
| 列名 | 类型 (SQLite / PG) | 约束 / 默认值 | 说明 |
|---|---|---|---|
| `id` | `TEXT` | `PRIMARY KEY` | Dreaming 报告 ID |
| `tenant_id` | `TEXT` | `NOT NULL` | 租户 ID |
| `project_slug` | `TEXT` | `NOT NULL` | 项目标识 |
| `created_at` | `TEXT` | `NOT NULL` | 生成时间 |
| `body` | `TEXT` | `NOT NULL` | Dreaming 整理报告 JSON |

- **索引**：`dream_runs_scope ON dream_runs(tenant_id, project_slug, created_at DESC)`

#### 表 12：`run_checkpoints`（v3 L801–L823）
| 列名 | 类型 (SQLite / PG) | 约束 / 默认值 | 说明 |
|---|---|---|---|
| `checkpoint_id` | `TEXT` | `PRIMARY KEY` | 检查点 ID（`uuid4().hex` 或外部指定，1..128 字符） |
| `run_id` | `TEXT` | `NOT NULL` | 所属会话 ID |
| `tenant_id` | `TEXT` | `NOT NULL DEFAULT 'local'` | 租户 ID |
| `turn_index` | `INTEGER` | `NOT NULL DEFAULT 0` | 轮次编号（`>= 0`） |
| `message_idx` | `INTEGER` | `NOT NULL DEFAULT 0` | 对话消息下标（`>= 0`） |
| `label` | `TEXT` | `NOT NULL DEFAULT ''` | 检查点标签（最多 200 字符） |
| `file_manifest_json` | `TEXT` | `NOT NULL DEFAULT '{}'` | 文件快照清单 JSON |
| `git_tree` | `TEXT` | 可空 | 可选 Git tree SHA（最多 128 字符） |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |

- **索引**：`run_checkpoints_run_turn ON run_checkpoints(run_id, turn_index, created_at)`；`run_checkpoints_tenant ON run_checkpoints(tenant_id, run_id)`

#### 表 13：`security_audit_logs`（v4 L825–L845）
| 列名 | 类型 (SQLite / PG) | 约束 / 默认值 | 说明 |
|---|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` / `BIGSERIAL PRIMARY KEY` | 主键 | 仅追加审计序号 |
| `tenant_id` | `TEXT` | `NOT NULL` | 租户 ID（最多 128 字符） |
| `actor` | `TEXT` | `NOT NULL` | 操作主体（最多 128 字符） |
| `action` | `TEXT` | `NOT NULL` | 审计动作名（非空，最多 128 字符） |
| `resource` | `TEXT` | `NOT NULL` | 受控资源标识（非空，最多 256 字符） |
| `outcome` | `TEXT` | `NOT NULL` | 判定结果（非空，最多 64 字符，默认 `allowed`） |
| `detail_json` | `TEXT` | `NOT NULL DEFAULT '{}'` | 经 `redact_secrets()` 递归脱敏后的详情 JSON |
| `trace_id` | `TEXT` | `NOT NULL DEFAULT ''` | 关联 W3C `trace_id`（最多 128 字符） |
| `created_at` | `TEXT` | `NOT NULL` | UTC 写入时间 |

- **索引**：`security_audit_logs_tenant_created ON security_audit_logs(tenant_id, created_at DESC, id DESC)`；注意 `delete_run()`（L1680–L1715）**严禁删除本表**，维持不可篡改仅追加审计合规性。

#### 表 14：`a2a_push_configs`（v7 L914–L931）
| 列名 | 类型 (SQLite / PG) | 约束 / 默认值 | 说明 |
|---|---|---|---|
| `task_id` | `TEXT` | `PRIMARY KEY` | A2A 任务 ID（对应 `runs.id`） |
| `tenant_id` | `TEXT` | `NOT NULL` | 租户 ID；UPSERT 时校验已有记录归属，跨租户覆写抛 `KeyError("task_not_found")` |
| `url` | `TEXT` | `NOT NULL` | Webhook 回调地址（1..2048 字符，拒绝 `\r`/`\n`/`\x00`） |
| `token_hash` | `TEXT` | 可空 | `secret` 的 SHA-256 摘要（通过 `digest()` 计算） |
| `secret` | `TEXT` | 可空 | 签名密钥（最多 512 字符，拒绝控制字符） |
| `events_json` | `TEXT` | `NOT NULL DEFAULT '[]'` | 订阅事件白名单 JSON（最多 32 项，每项最多 64 字符，自动去重） |
| `created_at` | `TEXT` | `NOT NULL` | 首次创建时间（更新时保留原值） |
| `updated_at` | `TEXT` | `NOT NULL` | 最近更新时间 |

- **索引**：`idx_a2a_push_configs_tenant ON a2a_push_configs(tenant_id, task_id)`

#### 表 15：`trigger_definitions`（v8 L935–L957）
| 列名 | 类型 (SQLite / PG) | 约束 / 默认值 | 说明 |
|---|---|---|---|
| `id` | `TEXT` | `PRIMARY KEY` | 触发器唯一 ID（`uuid4().hex` 或外部指定） |
| `tenant_id` | `TEXT` | `NOT NULL DEFAULT 'local'` | 租户 ID（最多 128 字符） |
| `name` | `TEXT` | `NOT NULL` | 触发器名称（1..200 字符） |
| `kind` | `TEXT` | `NOT NULL` | 类型（如 `cron`/`webhook`，1..64 字符） |
| `cron_expr` | `TEXT` | 可空 | 5 字段 Cron 表达式（最多 128 字符，禁 `\r`/`\n`/`\x00`） |
| `webhook_secret` | `TEXT` | 可空 | Webhook HMAC 密钥（最多 512 字符，禁 `\r`/`\n`/`\x00`） |
| `prompt_template` | `TEXT` | `NOT NULL` | 触发提示词模板（1..16,000 字符） |
| `workspace_path` | `TEXT` | 可空 | 绑定工作目录（最多 1,024 字符，禁 `\r`/`\n`/`\x00`） |
| `enabled` | `INTEGER` | `NOT NULL DEFAULT 1` | 启用标志（`0`/`1`） |
| `last_fired_at` | `TEXT` | 可空 | 最近一次成功去重触发时间（由 `record_trigger_fire` 同步更新） |
| `metadata_json` | `TEXT` | `NOT NULL DEFAULT '{}'` | 扩展配置 JSON（如 `timezone`） |
| `created_at` | `TEXT` | `NOT NULL` | 创建时间 |
| `updated_at` | `TEXT` | `NOT NULL` | 更新时间 |

- **索引**：`idx_trigger_definitions_tenant_enabled ON trigger_definitions(tenant_id, enabled, created_at DESC)`

#### 表 16：`trigger_fires`（v8 L958–L973）
| 列名 | 类型 (SQLite / PG) | 约束 / 默认值 | 说明 |
|---|---|---|---|
| `id` | `INTEGER PRIMARY KEY AUTOINCREMENT` / `BIGSERIAL PRIMARY KEY` | 主键 | 触发历史自增 ID |
| `trigger_id` | `TEXT` | `NOT NULL` | 关联触发器 ID |
| `dedup_key` | `TEXT` | `NOT NULL UNIQUE` | 全局唯一幂等去重键（1..256 字符），支撑 `ON CONFLICT(dedup_key) DO NOTHING` 原子至多一次语义 |
| `run_id` | `TEXT` | 可空 | 派发生成的 `runs.id`（最多 128 字符） |
| `status` | `TEXT` | `NOT NULL DEFAULT 'dispatched'` | 触发记录状态（1..64 字符） |
| `created_at` | `TEXT` | `NOT NULL` | 触发落库时间 |

- **索引**：`UNIQUE(dedup_key)`；`idx_trigger_fires_trigger_created ON trigger_fires(trigger_id, id DESC)`

---

### 1.5 双后端连接架构与词法安全 SQL 翻译

#### 1.5.1 SQLite WAL 并发冷启动与连接策略
- **禁用 `:memory:`**（L418–L419、L449–L454）：每次 `_connect()` 打开独立短生命周期连接，内存库无法跨连接共享状态。
- **父目录规范化、末端保留**（L420–L424）：`self.path = configured.parent.resolve() / configured.name`——既消除 macOS `/var -> /private/var` 别名歧义，又保留末端文件名，使 `execution_lock.py` 仍可捕获数据库文件自身的符号链接替换攻击。
- **Barrier 并发 WAL 切换退避重试**（L427–L435）：多进程/多线程在同一瞬间首次打开数据库执行 `PRAGMA journal_mode=WAL` 与 `BEGIN IMMEDIATE` 迁移时，SQLite 可能抛出 `database is locked` / `busy` / `protocol error`；启动循环执行最多 **8 次** 线性退避重试（延迟 `0.02 * (attempt + 1)` 秒，即 20ms..140ms）。
- **每连接 PRAGMA 配置**（L1067–L1076）：`timeout=10`、`row_factory=sqlite3.Row`、`PRAGMA busy_timeout=10000`、`PRAGMA foreign_keys=ON`、`PRAGMA synchronous={self.synchronous}`（由参数或环境变量 `AGENT_LAB_SQLITE_SYNC` 控制，白名单 `OFF` / `NORMAL` / `FULL` / `EXTRA`，默认 `NORMAL`）。

#### 1.5.2 PostgreSQL 后端配置、惰性连接池与冷启动 DDL 串行化
- **`PostgresBackendConfig`（L175–L228）**：不可变 dataclass，关键默认参数：
  - `connect_timeout = 10`（秒）
  - `application_name = "agent-systems-lab"`
  - `claim_lock_clause = "FOR UPDATE SKIP LOCKED"`
  - `pool_size = 4`（可通过 `AGENT_LAB_PG_POOL_SIZE` 环境变量覆盖，L459–L466；校验拒绝 `bool` 或 `< 0`）
  - `pool_max_size = None`（惰性建池时默认为 `max(min_size, min_size * 2)`，L495–L499）
  - `pool_timeout = 30.0`（秒）
- **惰性双检锁连接池 `_get_or_create_pg_pool`（L477–L515）**：
  - 使用 `self._pg_pool_lock` 双检锁惰性初始化 `psycopg_pool.ConnectionPool(open=True)`，并在连接 kwargs 中设置 `row_factory=psycopg.rows.dict_row`、`autocommit=True`。
  - 若环境未安装 `psycopg_pool` 或 `pool_size <= 0`，置位 `self._pg_pool_unavailable = True` 并无感降级为每请求直连 `pg_config.connect()`（L1059–L1064）。
  - 池连接包装为 `_PostgresConnectionAdapter(raw_con, self.pg_config, close_raw=False)`，退出上下文时不关闭底层连接而是安全归还连接池；`Storage.close()` / `__exit__()` 幂等关闭连接池（L517–L531）。
- **多副本冷启动 DDL 串行化（L1036–L1040）**：
  ```sql
  BEGIN;
  SELECT pg_advisory_xact_lock(7281946510293847);
  -- 执行 _run_migrations(con)
  ```
  事务级咨询锁 `7281946510293847` 在多个无状态 API/Worker 容器同时冷启动时强制串行化迁移事务，彻底消除 PostgreSQL `CREATE TABLE IF NOT EXISTS` 并发争抢 `pg_type_typname_nsp_index` 的经典竞态异常，事务提交/回滚时自动释放锁。

#### 1.5.3 加固词法级 SQL 翻译器 `_translate_qmark_to_pyformat`（L53–L172）
朴素 `sql.replace("?", "%s")` 会在字面量包含 `?`（例如 JSON 内容、URL、注释）或含 `%`（例如 `LIKE '%foo%'`）时导致 psycopg 参数错位或 `IndexError`。本模块手写**单遍状态机词法扫描器**：
1. **`BEGIN IMMEDIATE` 映射**：整体去空白大写等于 `"BEGIN IMMEDIATE"` 时直接映射为 `"BEGIN"`（L73–L75），配合 `_PostgresConnectionAdapter.execute()` 置位 `self._in_tx = True` 以启用显式事务提交/回滚（L248–L268）。
2. **5 类词法保护区（内部 `?` 原样保留，所有 `%` 转义为 `%%`）**：
   - 单行注释 `-- ... \n`（L82–L89）
   - 块注释 `/* ... */`：**支持 PostgreSQL 规范的嵌套块注释计数 `depth`**（L91–L108）
   - 单引号字符串 `'...'`：兼容 `''` 转义，且通过前驱字符检测精确识别 PostgreSQL `E'...'` / `e'...'` 转义串（要求 `e`/`E` 前面非字母数字/下划线，避免误判标识符末尾字母），内部正确跳过 `\.` 双字符转义对（L110–L134）
   - 双引号标识符 `"..."`：兼容 `""` 转义（L136–L151）
   - PostgreSQL dollar-quote `$$...$$` 与 `$tag$...$tag$`：经 `_match_dollar_quote_tag()`（L53–L68）严格校验标签首字符 `[A-Za-z_]` 与后续 `[A-Za-z0-9_]`，查找到配对闭合标签前保护全部内容（L153–L164）
3. **非保护区替换**：`? -> %s`，`% -> %%`（L165–L170）。此外 `_PostgresConnectionAdapter.execute()` 在无参数但 SQL 包含 `%`（已转义为 `%%`）时显式传入空元组 `()`（L254–L255），确保 psycopg 始终执行 pyformat 反转义。

---

### 1.6 全局 PostgreSQL 锁序、双层乐观并发与毒丸死信机制

#### 1.6.1 全局锁序不变量（Deadlock Prevention）
在 PostgreSQL 多行事务中，所有涉及多表的写操作严格遵守固定全序锁获取链，杜绝 AB-BA 死锁：

$$\texttt{run\_leases} \longrightarrow \texttt{runs} \longrightarrow \texttt{child tables}\;(\texttt{proposals} / \texttt{tool\_calls} / \texttt{run\_steering\_messages} / \texttt{workspace\_archives})$$

| 事务方法 | `run_leases` 锁级别 | `runs` 锁级别 | 子表锁级别 | 源码行号 |
|---|---|---|---|---|
| `save_run` | `FOR KEY SHARE`（经 `_verify_epoch_tx`） | `FOR UPDATE` | — | L1889–L1893 |
| `claim_next_run` | `FOR UPDATE SKIP LOCKED` | `FOR UPDATE`（仅毒丸转死信时） | — | L1302–L1338 |
| `heartbeat_lease` | `FOR UPDATE`（`_verify_epoch_tx(..., for_update=True)`） | — | — | L1415 |
| `approve_and_enqueue` | `FOR UPDATE` | `FOR UPDATE` | `proposals FOR UPDATE` | L2315–L2328 |
| `append_user_turn` | `FOR UPDATE` | `FOR UPDATE` | `proposals FOR UPDATE`（接管待审批时） | L2683–L2722 |
| `request_cancel_run` | `FOR UPDATE` | `FOR UPDATE` | — | L3182–L3189 |
| `delete_run` | `FOR UPDATE` | `FOR UPDATE` | 顺序删除 8 张从属表（保留审计表） | L1684–L1715 |
| `schedule_call` / `start_call` / `finish_call` | `FOR KEY SHARE` | — | `tool_calls FOR UPDATE` | L2950–L2954, L3003, L3019, L3066 |
| `create_changeset_proposal` / `create_action_proposal` / `mark_*_proposal_*` | `FOR KEY SHARE` | — | `proposals FOR UPDATE` | L2194–L2199, L2430–L2434, L2498–L2503, L2544–L2548 |
| `drain_steering_messages` | `FOR KEY SHARE` | — | `run_steering_messages FOR UPDATE` | L2609–L2614 |
| `save_workspace_archive` | `FOR KEY SHARE` | 只读存在校验 | `workspace_archives FOR UPDATE` | L3118–L3131 |

**Why `FOR KEY SHARE` vs `FOR UPDATE`**：校验 `epoch` 的高频写操作（工具调用、事件、提案、转向消费、归档）仅需确保事务期间 `run_leases` 行的 `epoch`/`queue_state` 不被并发抢占更新，使用 `FOR KEY SHARE` 允许同一任务内只读外键引用与同级校验并发，同时仍与抢占/续租的 `FOR UPDATE` 互斥；而任何修改 `run_leases` 自身状态的操作一律先持 `run_leases FOR UPDATE` 再进入 `runs FOR UPDATE`。

#### 1.6.2 Kleppmann Monotonic Epoch Fencing（`_verify_epoch_tx`，L1191–L1214）
```mermaid
sequenceDiagram
    participant WA as Worker A (epoch=1)
    participant DB as Storage (run_leases + runs)
    participant WB as Worker B (epoch=2)

    WA->>DB: claim_next_run (epoch: 0 -> 1, lease_until=T+15s)
    Note over WA: GC 停顿 / I/O 假死超过 T+15s
    WB->>DB: claim_next_run (lease_until <= now)<br/>FOR UPDATE SKIP LOCKED -> epoch: 1 -> 2, owner=WB
    Note over WA: Worker A 苏醒，尝试写库或写盘
    WA->>DB: save_run(expected_epoch=1) / finish_call(expected_epoch=1)
    DB->>DB: _verify_epoch_tx: actual_epoch(2) != expected_epoch(1)
    DB-->>WA: 抛出 StaleEpochError (fencing_token_rejected)
    WB->>DB: save_run(expected_epoch=2, expected_version=V)
    DB->>DB: epoch=2 匹配 & state_version=V CAS 成功 -> V+1
    DB-->>WB: 提交成功
```
- **两种校验模式**：
  - **Worker 模式（`expected_epoch is not None`）**：严格要求 `row is not None and row["epoch"] == expected_epoch and row["queue_state"] == "claimed"`，否则立即抛出 `StaleEpochError("fencing_token_rejected: ...")`。
  - **内联模式（`expected_epoch is None`）**：若当前存在未过期的活跃租约（`queue_state == "claimed" and (lease_until or 0.0) > time.time()`），抛出 `StaleEpochError("fencing_inline_blocked_by_claimed_lease: ...")`，防止 CLI/API 内联执行穿透正在运行中的分布式 Worker；内联执行启动前调用 `bump_inline_epoch()`（L1221–L1247）递增 `epoch` 保持 `idle`，直接把任何过期僵尸 Worker 永久隔离出局。
- **同世代乐观 CAS（`save_run`，L1875–L1943）**：除 `_verify_epoch_tx` 外，`save_run` 先锁定 `runs` 行比对 `expected_version`（若传入且不等则抛 `StaleEpochError("state_version_conflict")`），再以 `UPDATE runs SET ..., state_version=? WHERE id=? AND state_version=?` 执行原子 CAS 并校验 `changed.rowcount == 1`。

#### 1.6.3 毒丸任务隔离（Dead-Letter Queue，`claim_next_run` L1280–L1400）
- 参数边界：`ttl_seconds` 必须为非 bool 数值且在 `[0.2, 600]`；`max_takeovers` 为非负整数（默认 `3`）。
- 循环扫描逻辑（`while True`，L1301）：
  1. PG 使用 `PostgresBackendConfig.build_claim_sql()` 带 `FOR UPDATE SKIP LOCKED` 跳过其他 Worker 正在锁定的行，实现多 Worker 零锁等待吞吐。
  2. 当命中已过期租约（`queue_state == 'claimed' AND lease_until <= ts`，即 `takeover=True`）且历史 `takeover_count >= max_takeovers` 时：
     - 将 `run_leases.queue_state` 置为 `'dead_letter'`、`owner_id=NULL`、`lease_until=NULL`；
     - 同步更新 `runs` 状态为 `'failed'`、`error='DeadLetterExceeded'`、`state_version + 1`；
     - 写入 `run_dead_lettered` 审计事件；
     - 若按特定 `run_id` 领取则返回 `None`，否则 `continue` 立即跳过毒丸继续领取队列中的下一个健康任务，**避免单条崩溃任务堵死整个集群**。
  3. 健康任务被领取时原子递增 `epoch += 1`、若是接管则 `takeover_count += 1`，写入 `lease_claimed` 事件并返回领取凭证。

---

### 1.7 关键业务原语与算法逻辑详解

#### 1.7.1 零 CAS 冲突运行期转向收件箱（`run_steering_messages`）
- **Why**：当任务正在由 Worker 高速推进（每步 `save_run` 递增 `runs.state_version`）时，如果用户在前端发送中途纠偏指令（Steering）直接修改 `runs.body`，极易引发 `state_version_conflict` 导致 Worker 回滚或丢消息。
- **How**：
  - `enqueue_steering_message(run_id, prompt, actor_id)`（L2560–L2599）：校验 `prompt` 长度 `1..8000`，仅校验 `runs` 存在后向独立表 `run_steering_messages` 追加行（`consumed_at=NULL`），**完全不写 `runs.body` 与 `runs.state_version`**，随后写入 `user_steering_queued` 事件。
  - `pending_steering_messages(run_id)`（L2636–L2653）：只读查询 `consumed_at IS NULL ORDER BY id ASC`。
  - `drain_steering_messages(run_id, expected_epoch)`（L2601–L2634）：在单事务内先通过 `_verify_epoch_tx` 校验世代栅栏，再以 `FOR UPDATE` 锁定未消费行，按每批 **500 条** 分片执行 `UPDATE ... SET consumed_at=? WHERE id IN (...) AND consumed_at IS NULL`（规避 SQLite 绑定变量上限 999），确保每条转向指令仅被当前合法世代原子消费一次。

#### 1.7.2 多轮追问与等待审批接管（`append_user_turn`，L2655–L2914）
单事务内按锁序（`run_leases FOR UPDATE` -> `runs FOR UPDATE` -> 可选 `proposals FOR UPDATE`）完成 5 项关键操作：
1. **状态门禁**：拒绝 `running`/`queued`、拒绝 `dead_letter`、拒绝存在未过期 `claimed` 租约；当状态为 `waiting_approval` 时，仅当 `supersede_waiting_approval=True` 允许接管。
2. **等待审批自动作废接管（`superseded_waiting`）**：将 `metadata.waiting_proposal_ids` 中所有 `pending` 提案原子置为 `rejected`（标记 `decision_reason="superseded_by_user_steering"`），记录至 `metadata.decisions`，并在对话历史中注入接管提示语。
3. **悬空 `tool_use` 1:1 协议自愈（L2749–L2830）**：
   - 若历史末尾是含 `tool_use` 的 `assistant` 消息，优先复用 `metadata.pending_results` 中已完成工具的真实回执，其余补齐 `aborted_before_followup`（或接管时的 `superseded_by_user_steering`）合成 `tool_result` 块。
   - 若接管时末尾已是部分配对的 `user` 消息，补齐缺失的 `tool_use_id` 闭合块，保证 Anthropic/OpenAI 协议层不报 400 且 `ContextManager` 原子消息组配对不变量成立。
4. **KV-Cache 感知的线性预算延展与记忆刷新标志（L2860–L2874）**：
   - 置位 `meta["refresh_pinned_memory"] = True`，指示 Runtime 在新轮次开始时刷新固定长期记忆快照。
   - 步数上限：`max(meta["max_steps"], steps + max(1, base_max_steps or step_extension))`（默认 `step_extension=24`）。
   - Token 上限：**锚定 KV-Cache 折算计费量 `billed_tokens`**（若 `billed_tokens > 0` 则取 `billed_tokens`，否则回退 `input_tokens + output_tokens`），计算 `max(meta["max_tokens"], used_tokens + max(2048, base_max_tokens or token_extension))`（默认 `token_extension=120_000`）。使用 `billed_tokens` 作为锚点既避免高缓存命中长会话被原始未折算输入虚高耗尽配额，又防止累加模式囤积失控预算。
5. **世代推进与队列推送（L2897–L2913）**：递增 `run_leases.epoch += 1`、重置 `takeover_count=0`，若 `enqueue=True` 则置 `queue_state='queued'` 并调用 `_notify_queue_tx(con, run_id, action="execute")` 发送 `pg_notify`。

#### 1.7.3 会话分叉与悬空工具自愈（`fork_run` & `_heal_dangling_tool_uses`）
- **`_heal_dangling_tool_uses`（L293–L380）**：两步修复截断对话尾部：
  1. 循环弹出末尾孤立或配对不完整的 `user.tool_result`（校验前一条 `assistant` 的 `tool_use` ID 集合与末尾 `tool_result` 数量及集合完全一致）。
  2. 若修复后末尾为含 `tool_use` 的 `assistant` 消息，按序补齐 1:1 合成 `tool_result`（`ok=False, code="forked_branch", is_error=True`）。
- **`fork_run`（L1717–L1873）**：
  - 支持 `up_to_message_idx`（当传入 `0` 且原消息非空时，自动提升为 `effective_up_to_idx = 1` 保留首条消息，L1753–L1754）。
  - **瞬态元数据清零**（L1761–L1784）：剥离 22 个瞬态/计费/审批/取消键（`pending_calls`、`pending_results`、`waiting_proposal_ids`、`executing_proposal_id`、`approval_failure`、`cancel_requested`、`cancel_reason`、`cancel_requested_at`、`cancelled_by`、`error_detail`、`archived`、`archived_at`、`billed_tokens`、`cost_usd`、`cost_breakdown`、`kv_cache`、`active_checkpoint_id`、`turn_skills_snapshot`、`stop_veto_count`、`truncation_continuations`、`context_stats`）。
  - **条件清理压缩游标**（L1787–L1794）：仅当截断或发生悬空修复时清除 `compaction_cursors`、`compaction_summaries`、`compaction_summary`、`micro_compaction_cursors`；完整无损分叉则保留原摘要与游标以及 `answer`。
  - **零步计数器重置**（L1824–L1827）：`steps=0`、`input_tokens=0`、`output_tokens=0`、`state_version=1`，写入血缘字段（`forked_from_run_id`、`forked_at_message_idx`、`forked_at`）并初始化 `run_leases(queue_state='idle', epoch=0)` 与 `run_forked` 事件。

#### 1.7.4 原子审批入队（`approve_and_enqueue`，L2287–L2422）
单事务内完成补丁/动作审批、可选权限升级与恢复入队，彻底消除“审批落库成功但入队前进程崩溃导致任务永久卡死在 `waiting_approval`”的双写一致性问题：
- 按全局锁序锁定 `run_leases` -> `runs` -> `proposals`，拒绝已处于终态（`completed`/`cancelled`/`dead_letter`）的任务与不可逆翻转（`approval_already_decided`）。
- 支持原子写入：
  - `permission_mode`：更新 `runs.metadata.permission_mode`；
  - `grant_pattern`（仅 `approved=True` 时生效，拒 `\x00`/`\n`/`\r`，截断至 200 字符，每会话最多 **64** 条不重复规则）：追加至 `runs.metadata.session_grants` 并写入 `session_grant_added` 事件；
  - `answer_text`（截断至 **4,000** 字符）：写入 `proposal["answer_text"]` 承载 `user_question` / `plan_approval` 的人工回复内容。
- 将 `run_leases` 设为 `queue_state='queued', action='resume', takeover_count=0`，并执行 `_notify_queue_tx(con, run_id, action="resume")` 立即推送 `pg_notify('agent_lab_queue', ...)`。
- **与 Worker `release_lease` 竞态协调（L1452–L1453）**：若人工审批抢在 Worker 调用 `release_lease(..., final_state='waiting_approval')` 前将同世代租约原子切为 `queued`，`release_lease` 识别到 `row["queue_state"] == "queued" and final_state == "waiting_approval"` 后直接幂等返回，不抛 `StaleEpochError` 也不回退 `queued` 状态。

#### 1.7.5 工作区归档快照与 `direct_workspace` 豁免 GC
- **`save_workspace_archive` / `get_workspace_archive`（L3102–L3169）**：
  - 写入前先对 `run_leases` 加 `FOR KEY SHARE`、对 `workspace_archives` 加 `FOR UPDATE`，双重校验 `lease_epoch <= epoch` 与 `stored_epoch <= epoch`，阻断迟到僵尸 Worker 用旧代码快照覆盖新世代归档。
- **`gc_expired_resources`（L3288–L3431）**：
  - 仅回收状态属于终态集合 `{"completed", "failed", "budget_exceeded", "cancelled", "dead_letter"}` 且 `run_leases.queue_state` 不在活跃集合 `{"queued", "claimed", "waiting_approval"}`、且距今超过 `retention_seconds`（默认 `86400.0` 秒 = 24 小时）的资源。
  - **`direct_workspace` 绝对豁免铁律（L3348–L3351）**：只要 `metadata.get("direct_workspace")` 为真，无条件跳过磁盘目录删除（哪怕管理员误把父目录传作 `workspaces_dir`），仅释放进程内工作区锁与过期截图产物。
  - **路径逃逸保护**：待删工作区与产物目录均经 `.resolve()` 与 `is_relative_to(root)` 及 `!= root` 校验，且跳过符号链接。
  - **五类回收动作**：托管工作区/Git worktree 清理（`cleanup_task_worktree`）、`git worktree prune`、删除过期 `workspace_archives` 行、删除 `artifacts/<run_id>` 目录、以及 SQLite 模式下调用 `cleanup_stale_locks(..., active_run_ids=active_run_ids)` 清理陈旧 `.lock` 文件。

---

### 1.8 核心配额、常量与键集分页摘要

| 常量 / 边界 | 精确数值 | 源码位置 | 约束说明 |
|---|---|---|---|
| `PG_QUEUE_NOTIFY_CHANNEL` | `"agent_lab_queue"` | L42 | PostgreSQL `LISTEN/NOTIFY` 推送频道名 |
| PG 冷启动咨询锁 ID | `7281946510293847` | L1039 | `pg_advisory_xact_lock` 全局固定 64-bit 键 |
| SQLite 冷启动重试 | 8 次，退避 `0.02*(attempt+1)` s | L427–L435 | 应对 Barrier 并发 WAL 切换争抢 |
| SQLite 连接超时 / busy_timeout | `10` s / `10000` ms | L1067, L1070 | 限制单次写锁等待，防无限挂起 |
| `claim_next_run` TTL 范围 | `[0.2, 600]` s（默认 `15.0`） | L1283, L1293 | 非 bool 合法浮点/整数 |
| `max_takeovers` 默认值 | `3` | L1286 | 达到阈值转 `dead_letter` |
| `list_runs` 游标格式与上限 | `<created_at>\|<id>`，limit `1..1000` | L1504–L1519 | 支持复合游标、裸 `run_id` 与 ISO 时间戳回退 |
| `events()` 单次读取上限 | `2000` 条 | L1985 | 防止大轨迹单次拉取 OOM |
| `remember()` 键/值上限 | scope/key `<= 200`，JSON `<= 16000` | L1991–L1995 | 控制 KV 记忆体积 |
| `create_changeset_proposal` 文件数上限 | `1..8` 个文件，路径不可重复 | L2114–L2125 | 单提案原子补丁集边界 |
| `session_grants` 规则上限 | 单会话 `64` 条，每条 `<= 200` 字符 | L2311, L2356 | 审批持久化会话级放行规则配额 |
| `answer_text` 长度上限 | `4000` 字符 | L2283, L2396 | 人工审批/问答回复截断长度 |
| `drain_steering_messages` 批大小 | `500` 条/批 | L2618 | 规避 SQLite 绑定参数数量上限 |
| `enqueue_steering_message` / `append_user_turn` prompt 上限 | `1..8000` 字符 | L2567, L2678 | 单条转向/追问消息长度约束 |
| `append_user_turn` 默认预算延展 | `step_extension=24`, `token_extension=120_000`（下限 `2048`） | L2660–L2661, L2863 | 锚定 `billed_tokens` 线性延展 |
| `get_call_result` 分页上限 | `limit` `1..8000`（默认 `4000`） | L3076–L3079 | 完整工具结果分页取回边界 |

---

### 1.9 `storage.py` 关键源码符号与精确行号索引

| 符号 / 方法 | 精确行号 | 职责摘要 |
|---|---|---|
| `PG_QUEUE_NOTIFY_CHANNEL` / `StaleEpochError` / `StorageConfigError` | L42–L50 | 通知频道常量与核心存储异常定义 |
| `_match_dollar_quote_tag` / `_translate_qmark_to_pyformat` | L53–L172 | 词法级 SQLite `?` -> PG `%s` 与 `% -> %%` 翻译状态机 |
| `PostgresBackendConfig` / `_PostgresConnectionAdapter` | L175–L272 | PG 连接/池配置、`build_claim_sql`、统一上下文适配器 |
| `canonical_json` / `digest` / `_heal_dangling_tool_uses` | L283–L380 | 确定性规范化 JSON、SHA-256 摘要、悬空 `tool_use` 修复 |
| `Storage.__init__` / `from_dsn` / `_get_or_create_pg_pool` / `close` / `run_async` | L386–L536 | 双后端初始化、DSN 解析、惰性 PG 连接池与线程池桥接 |
| `_migrations` / `_run_migrations` / `applied_migrations` | L542–L1018 | 8 步版本化 Schema 迁移实现与查询 |
| `_pg_lock` / `_notify_queue_tx` / `_initialize_postgres_schema` / `_initialize_schema` / `_connect` | L1020–L1076 | 方言行锁拼接、`pg_notify`、冷启动迁移与连接上下文 |
| `create_run` / `_verify_epoch_tx` / `verify_epoch` / `bump_inline_epoch` | L1131–L1247 | 任务创建与 Kleppmann `epoch` 世代校验/内联递进 |
| `enqueue_run` / `claim_next_run` / `heartbeat_lease` / `release_lease` / `get_lease` | L1249–L1468 | 队列入队、无锁领取、毒丸转死信、心跳续租与租约释放 |
| `update_worker_node` / `list_workers` | L1470–L1496 | Worker 节点注册表与存活状态查询 |
| `get_run` / `make_run_cursor` / `list_runs` / `rename_run` / `archive_run` / `delete_run` / `fork_run` / `save_run` | L1498–L1943 | 会话生命周期管理、键集分页、原子分叉与 `state_version` CAS |
| `add_event` / `events` / `remember` / `memories` / `full_runs_for_tenant` / `record_dream_run` / `list_dream_runs` | L1945–L2069 | 事件流、KV 记忆、跨会话 Transcript 拉取与 Dreaming 记录 |
| `create_proposal` / `create_changeset_proposal` / `approve_proposal` / `approve_and_enqueue` / `mark_proposal_applied` | L2071–L2443 | 文件补丁提案状态机与单事务审批入队 |
| `create_action_proposal` / `mark_action_proposal_executed` | L2445–L2558 | 非文件高风险动作提案与回执封存防重放 |
| `enqueue_steering_message` / `drain_steering_messages` / `pending_steering_messages` | L2560–L2653 | 零 CAS 冲突运行期转向收件箱（入队/原子消费/只读查询） |
| `append_user_turn` | L2655–L2914 | 多轮续聊、等待审批接管、悬空闭合修复与线性预算延展 |
| `schedule_call` / `get_call` / `list_calls` / `call_summaries` / `start_call` / `finish_call` / `get_call_result` | L2921–L3100 | 工具调用幂等账本与分页结果读取 |
| `save_workspace_archive` / `get_workspace_archive` | L3102–L3169 | 世代受保护的跨节点工作区归档快照读写 |
| `request_cancel_run` / `is_cancel_requested` | L3171–L3274 | 立即取消 vs 运行期协作取消打标 |
| `gc_expired_resources` | L3288–L3431 | 终态过期资源回收与 `direct_workspace` 绝对保护 |
| `save_checkpoint` / `list_checkpoints` / `get_checkpoint` / `delete_checkpoints_after` | L3453–L3599 | 会话检查点持久化与回滚裁剪 |
| `record_audit_log` / `list_audit_logs` | L3621–L3742 | 仅追加、自动脱敏的安全审计日志 |
| `set_a2a_push_config` / `get_a2a_push_config` / `delete_a2a_push_config` / `list_runs_by_a2a_context` | L3767–L3918 | A2A Push Webhook 配置与 `context_id` 索引查询 |
| `create_trigger` / `list_triggers` / `get_trigger` / `delete_trigger` / `record_trigger_fire` / `list_trigger_fires` | L3959–L4233 | 自主触发器定义 CRUD 与 `dedup_key` 原子去重落库 |

---

## 2. 跨进程本机互斥锁（`src/agent_lab/execution_lock.py`）

### 2.1 模块定位与核心职责

- **源码路径**：[src/agent_lab/execution_lock.py](../../src/agent_lab/execution_lock.py)（L1–L273）
- **定位边界**：提供**同一 POSIX 主机上、跨独立进程（API / CLI / Worker）**的非阻塞任务互斥锁。正如文件头声明（L1–L6），它不替代分布式租约或 Fencing Token（网络文件系统或跨主机仍依赖 `storage.py` 的 `run_leases.epoch`），而是作为同机双保险——防止“上一轮同步子线程仍在收尾尚未释放文件句柄、下一进程同时写工作区”或本地并发 CLI 与后台 Worker 重叠执行。
- **核心能力**：
  1. `exclusive_run(storage_path, run_id)`（L136–L203）：防路径遍历与符号链接替换的逐层 `dir_fd` + `O_NOFOLLOW` + 非阻塞 `fcntl.flock(LOCK_EX | LOCK_NB)` + 加锁后 `(st_dev, st_ino)` 双重校验上下文管理器。
  2. `cleanup_stale_locks(storage_path, active_run_ids, max_age_seconds=3600.0)`（L206–L273）：仅在持有非阻塞独占锁并复核 `mtime` 与目录项 inode 一致后安全回收陈旧 `.lock` 文件。

---

### 2.2 加锁与防竞争状态机流程

```mermaid
flowchart TD
    START["exclusive_run(storage_path, run_id)"] --> VAL_ID["正则校验 run_id<br/>^[A-Za-z0-9_-]{1,128}$<br/>计算 sha256(run_id).lock"]
    VAL_ID --> WALK["_database_parent(storage_path)<br/>拒绝 '..'，从根目录逐层 dir_fd + O_NOFOLLOW 打开父目录<br/>校验数据库路径非 symlink"]
    WALK --> MKDIR["os.mkdir('locks', 0o700, dir_fd)<br/>_open_directory('locks', database_fd)"]
    MKDIR --> LOOP["最多 4 轮重试 (for _ in range(4))"]
    LOOP --> OPEN["os.open(filename, O_RDWR|O_CREAT|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC, 0o600)"]
    OPEN -->|"ELOOP / EISDIR"| ERR_VAL["raise ValueError (拒绝符号链接/目录)"]
    OPEN --> FSTAT1["fstat(candidate_fd)"]
    FSTAT1 -->|"非普通文件 或 st_nlink > 1"| ERR_HL["raise ValueError (防硬链接欺骗)"]
    FSTAT1 -->|"st_nlink == 0 (已被 GC 删链)"| RETRY1["close(fd) -> continue 重试"]
    FSTAT1 -->|"st_nlink == 1"| FLOCK["fcntl.flock(fd, LOCK_EX | LOCK_NB)"]
    FLOCK -->|"EACCES / EAGAIN"| CHK_UNLINK{"fstat(fd).st_nlink == 0 ?"}
    CHK_UNLINK -->|"是 (GC 持锁删链中)"| RETRY1
    CHK_UNLINK -->|"否 (另一活跃进程持锁)"| BUSY["raise RunBusy"]
    FLOCK -->|"持锁成功"| FSTAT2["加锁后复核 fstat(fd)<br/>校验 st_nlink == 1 且 _same_dir_entry(st_dev, st_ino)"]
    FSTAT2 -->|"st_nlink > 1"| ERR_HL
    FSTAT2 -->|"已删链或同名文件已被替换"| RETRY2["flock(LOCK_UN) + close(fd) -> continue 重试"]
    FSTAT2 -->|"inode 完全一致"| UTIME["os.utime(fd, None) 刷新 mtime<br/>yield 执行临界区"]
    UTIME --> RELEASE["finally: flock(LOCK_UN) + close(fd)<br/>严禁常规释放时 unlink 锁文件"]
    RETRY1 --> LOOP
    RETRY2 --> LOOP
    LOOP -->|"4 轮耗尽仍未获锁"| BUSY
```

---

### 2.3 核心安全与并发不变量（Why + How）

| 不变量 | Why（防御的漏洞 / 竞态） | How（精确源码机制） |
|---|---|---|
| **禁止 `Path.resolve()` 静默穿越符号链接** | 若直接调用 `resolve()`，攻击者可把父目录或 `locks/` 或数据库文件替换为指向系统敏感文件的符号链接，导致 `O_CREAT` / `flock` 落点逃逸。 | `_database_parent`（L99–L121）拒绝含 `".."` 的路径；`_open_database_parent`（L71–L96）从 `parent.anchor` 起逐层使用 `dir_fd` 与 `_traversal_flags()`（Linux 优先 `O_PATH \| O_DIRECTORY \| O_NOFOLLOW \| O_CLOEXEC`，macOS 退回 `O_RDONLY \| O_DIRECTORY \| O_NOFOLLOW \| O_CLOEXEC`）打开各级目录，并对数据库文件自身调用 `os.stat(..., follow_symlinks=False)` 校验 `S_ISREG`。 |
| **无读权限可穿越父目录降级校验** | 在无 `O_PATH` 的系统（如 macOS）上，若某级中间目录权限为 `0o111`（仅可穿越 `+x` 不可读），`O_RDONLY` 打开会报 `EACCES`/`EPERM`。 | `_open_database_parent`（L86–L87）捕获 `EACCES`/`EPERM` 后降级到 `_verify_parent_prefixes_and_open`（L53–L68），对每一级前缀调用 `os.lstat` 确认非符号链接且为目录，再以 `_DIRECTORY_FLAGS` 打开最终父目录。 |
| **硬链接注入防护（`st_nlink > 1`）** | 攻击者可在同文件系统为锁文件创建硬链接，即使锁被释放/清理仍保留引用或篡改目标文件。 | 打开后（L165）与加锁后（L180）两次校验 `info.st_nlink > 1`，发现异常立即解锁并抛 `ValueError`；`cleanup_stale_locks` 亦要求 `st_nlink == 1`（L243、L257）。 |
| **消除 Lockfile Unlink/Recreate ABA 竞态** | 经典陷阱：进程 A 持锁期间进程 B 打开旧 inode 等待，A 释放时顺手 `unlink` 锁文件，进程 C 创建同名新 inode 并同时获锁，导致 B 与 C **各自锁住不同 inode 而并发进入临界区**。 | 1. 常规释放（L198–L201）**永不删除锁文件**，仅 `LOCK_UN` + `close`；<br>2. 获取 `flock` 后立即执行 `_same_dir_entry`（L124–L133）核对当前目录项与已锁 fd 的 `(st_dev, st_ino)` 完全一致，不一致则释放重试（最多 4 轮，L156–L189）；<br>3. 锁获取成功后调用 `os.utime(candidate_fd, None)`（L187）刷新 `mtime`，配合 `cleanup_stale_locks` 加锁后的二次 `st_mtime <= time.time() - max_age_seconds` 校验（L258），防止 GC 误删刚刚被激活的锁文件。 |
| **进程崩溃零僵尸锁 & 子进程不继承** | 进程被 `SIGKILL` 或 OOM Killer 终止时，无法执行 `finally` 块；若 `fork+exec` 出子进程继承锁 fd，则父进程退出后锁仍被子进程意外持有。 | POSIX `flock` 绑定内核 open file description，进程终止即由内核自动释放；全部 `open` 均附带 `_CLOEXEC`（`O_CLOEXEC`，L26–L27、L154、L217）确保子进程不继承锁句柄。 |

---

### 2.4 关键常量与失败模式边界

- **`run_id` 合法字符集**：`^[A-Za-z0-9_-]{1,128}$`（L144），文件名一律经 SHA-256 哈希为 `<64hex>.lock`（L146、L213），杜绝路径分隔符注入与大小写不敏感文件系统（macOS APFS/HFS+）下的文件名碰撞。
- **权限最小化**：`locks` 目录权限 `0o700`（L149），锁文件权限 `0o600`（L158）。
- **`cleanup_stale_locks` 安全容错**（L230–L240）：打开扫描文件时显式不传 `O_CREAT`，忽略 `ENOENT`、`ELOOP`、`EISDIR`、`ENOTDIR`、`EACCES`、`EPERM`、`ENXIO`（防止 FIFO/特殊设备或并发删除阻断整轮清理）；目录不存在返回 `0`（L221–L222）。
- **4 次重试耗尽**（L193–L194）：在极端高频 GC 删链争抢下有界退出并抛出 `RunBusy`，绝不死循环自旋。

---

### 2.5 `execution_lock.py` 关键源码符号与精确行号索引

| 符号 / 函数 | 精确行号 | 职责摘要 |
|---|---|---|
| `RunBusy` | L22–L23 | 本机并发互斥冲突异常（上层应返回冲突或重新入队） |
| `_CLOEXEC` / `_DIRECTORY_FLAGS` / `_traversal_flags` | L26–L34 | 安全打开标志位与 Linux `O_PATH` 优先策略 |
| `_open_directory` | L37–L50 | `dir_fd` 相对打开目录并校验 `S_ISDIR` |
| `_verify_parent_prefixes_and_open` | L53–L68 | 不可读中间父目录的 `lstat` 前缀校验回退路径 |
| `_open_database_parent` | L71–L96 | 从根目录逐段无符号链接遍历至数据库父目录 |
| `_database_parent` | L99–L121 | 拒绝 `..` 与数据库文件符号链接的上下文管理器 |
| `_same_dir_entry` | L124–L133 | `(st_dev, st_ino)` 目录项一致性校验原语 |
| `exclusive_run` | L136–L203 | 跨进程非阻塞 POSIX `flock` 独占锁上下文管理器 |
| `cleanup_stale_locks` | L206–L273 | 持锁复核 `mtime` 与 inode 后安全回收过期锁文件 |

---

## 3. 分布式持久队列与租约 Worker（`src/agent_lab/lease.py`）

### 3.1 模块定位与核心职责

- **源码路径**：[src/agent_lab/lease.py](../../src/agent_lab/lease.py)（L1–L872）
- **关联入口**：CLI `worker` 子命令（`--worker-id`、`--lease-ttl`、`--poll-interval`、`--once`、`--metrics-port`、`--metrics-host`，见 [src/agent_lab/cli.py](../../src/agent_lab/cli.py) L2946–L2963）。
- **核心职责**：
  1. **无状态持久队列消费者 `LeaseWorker`（L209–L837）**：完全依赖数据库账本与工作区归档重建现场，随时可被 `SIGKILL` 并由任意存活节点接管。
  2. **心跳续租与主动自熔断（Self-Fencing）**：后台以 `1/3 TTL` 周期续租，区分被抢占/超时自熔断（`fenced_by_heartbeat`）与 `SIGTERM` 优雅重入队（`worker_shutdown_requeued`）。
  3. **跨节点工作区物化与归档同步**：故障漂移至新节点时自动解压 `workspace_archives` 并重绑路径；严禁归档 `direct_workspace`。
  4. **出带 Post-Lease Dreaming**：严格按“归档同步 -> `release_lease` -> 退出 `exclusive_run` 锁 -> 新 Runtime 执行 `dream()`”推进，杜绝与用户多轮续聊互锁或污染主任务终态。
  5. **推送唤醒与自适应轮询**：整合线程安全 `_wakeup_event` + `notify_wakeup()` + PostgreSQL `LISTEN agent_lab_queue` + 自适应退避轮询回退。
  6. **纯标准库独立监控服务 `WorkerMetricsServer`（L72–L206）**：零第三方 Web 框架依赖暴露 `/metrics`（可选 Bearer Token 鉴权）与 `/healthz`。

---

### 3.2 `LeaseWorker` 全生命周期时序与状态流转

```mermaid
sequenceDiagram
    participant Q as Storage Queue (SQLite / PG)
    participant PG as PG LISTEN Loop
    participant W as LeaseWorker (serve_forever)
    participant HB as Heartbeat Coroutine
    participant RT as AgentRuntime (defer_auto_dream=True)

    PG-->>W: NOTIFY agent_lab_queue -> notify_wakeup()
    W->>W: _wakeup_event.clear() (查库前清零防丢失)
    W->>Q: claim_next_run(worker_id, ttl_seconds)
    Q-->>W: claim {run_id, epoch, action, lease_until}
    W->>Q: update_worker_node(status='busy', active_run_id, active_epoch)

    rect rgb(240, 248, 255)
        Note over W,RT: bind_trace_context(trace_ref) + start_span("lease_worker.process_run") + exclusive_run(flock)
        W->>HB: create_task(heartbeat_loop, interval=TTL/3)
        W->>Q: _materialize_workspace_if_needed (本地目录缺失时解压归档)
        W->>RT: _execute_unlocked / _resume_unlocked (expected_epoch=epoch)
        loop 每 TTL/3 秒
            HB->>Q: heartbeat_lease(run_id, worker_id, epoch, ttl)
            alt StaleEpochError 或 OperationalError 且 now > lease_deadline
                HB->>W: fenced_by_heartbeat = True; exec_task.cancel()
            end
        end
        RT-->>W: result {status: completed / waiting_approval / ...}
        opt AGENT_LAB_SYNC_WORKSPACE_ARCHIVE=="1" 或 had_archive (且非 direct_workspace)
            W->>Q: save_workspace_archive(run_id, epoch, archive_bytes, git_head)
        end
        W->>Q: release_lease(run_id, worker_id, epoch, final_state)
        W->>HB: stop_heartbeat.set() + cancel/await
        W->>RT: await runtime.aclose()
    end
    Note over W: 已退出 exclusive_run 文件锁且租约已释放

    opt finished_run.status == "completed" 且 auto_dream == True
        W->>RT: start_span("lease_worker.auto_dream")<br/>新建 dream_rt -> await dream_rt.dream(trigger="auto_post_session")
        Note over W,RT: 异常 suppress(BaseException)，绝不覆写主任务状态
    end
```

---

### 3.3 核心机制深度拆解

#### 3.3.1 心跳主动自熔断 vs. `SIGTERM` 优雅重入队（L532–L642）
`_run_claimed_under_lock` 同时面临两种截然不同的 `asyncio.CancelledError` 来源，处理策略截然相反：

| 场景 | 触发条件 | 标志位 | 归档与租约动作 | 异常与 Worker 主循环行为 | 指标与事件 |
|---|---|---|---|---|---|
| **被新节点抢占（Fenced）** | `heartbeat_lease` 抛 `StaleEpochError`；或遭遇 `sqlite3.OperationalError` 且本地时钟 `time.time() > current_lease_deadline`（保守自杀策略，L571–L575） | `fenced_by_heartbeat = True` | **严禁**调用 `save_workspace_archive` 或 `release_lease`（避免干扰新 Owner） | 吞掉 `CancelledError`，返回 `{"ok": False, "code": "fenced_out"}`，**Worker 主循环保持存活继续消费其他任务** | `agent_lab_worker_fenced_total += 1`；写入 `worker_fenced` 事件 |
| **进程优雅关机（`SIGTERM`）** | 外部信号取消当前任务且 `fenced_by_heartbeat == False` | `fenced_by_heartbeat = False` | 取消并等待 `exec_task` 结束后，最佳努力同步工作区归档（L631–L633），再调用 `release_lease(..., final_state="queued")`（L635）立即归还队列供其他节点零等待接管 | 写入 `worker_shutdown_requeued` 事件后**向上重抛 `CancelledError`** 终止消费循环 | 写入 `worker_shutdown_requeued` 事件 |
| **本机文件锁冲突（`RunBusy`）** | 进入 `with lock_cm` 时同机另一进程仍持 `flock`（L389–L406） | — | **绝不标为 `failed`**，调用 `release_lease(..., final_state="queued")` 退回队列 | 返回 `{"ok": False, "code": "run_busy"}`，下一轮安全重试 | 写入 `worker_busy_requeued` 事件 |
| **启动或执行异常（`Exception`）** | `runtime_factory` 或执行期抛出未预期异常（L643–L664） | — | `sanitize_error_detail(exc)` 脱敏后以 `expected_epoch=epoch` 将 run 标为 `failed` 并 `release_lease(..., final_state="failed")` | 返回 `{"ok": False, "code": "worker_bootstrap_failed"}` | 写入 `failure` 事件 |

#### 3.3.2 为什么 Post-Lease Dreaming 必须出带执行（L13–L20、L429–L454、L869）
`build_worker_runtime_factory`（L839–L872）固定传入 `defer_auto_dream=True`。如果在 `_execute_unlocked` 内部（仍持有 `claimed` 租约与 `exclusive_run` 内核锁期间）同步运行耗时数秒的 LLM 记忆整理：
1. 前端看到任务 `completed` 后立即发起第 2 轮追问（`append_user_turn`），会直接撞上 `run_busy:claimed` / `RunBusy`，或推进 `epoch` 导致随后 `release_lease` 触发误报 `StaleEpochError`；
2. 若 Dreaming 期间 LLM 超时或网络波动，外层 `except Exception`（L643）会把已成功交付的任务错误覆写为 `failed`。

因此 `process_one()` 在退出 `with lock_cm`（且已完成归档与 `release_lease`）后，保留同一 `bind_trace_context(trace_ref)` 上下文，另起 `lease_worker.auto_dream` 子 Span 与独立 `dream_rt` 实例执行 `dream(run_id=claimed_run_id, trigger="auto_post_session")`，外包 `contextlib.suppress(BaseException)` 并确保 `dream_rt.aclose()` 释放资源。

#### 3.3.3 推送驱动唤醒与 PostgreSQL `LISTEN/NOTIFY` 自适应降级（L274–L288、L676–L819）
- **线程安全唤醒 `notify_wakeup()`（L274–L288）**：支持同事件循环直接 `_wakeup_event.set()`，以及跨线程/跨事件循环经 `target_loop.call_soon_threadsafe(self._wakeup_event.set)` 安全投递。
- **消除“空查 -> 等待”丢唤醒窗口（L805–L806）**：每轮循环**先 `self._wakeup_event.clear()` 再查库 `process_one()`**；如果在查库期间有新任务入队并触发 `notify_wakeup()`，则查库返回 `None` 后进入 `_wait_for_wakeup_or_stop` 时 `_wakeup_event.is_set()` 已为真（L685–L687），立即返回 `True` 零延迟重试。
- **PostgreSQL 后台监听协程 `_pg_listen_loop`（L711–L779）**：
  - 频道名先经 `_validate_pg_channel`（L60–L64，匹配 `^[A-Za-z_][A-Za-z0-9_]{0,62}$`）防 SQL 注入；
  - 建连参数 `autocommit=True`、`application_name=f"{pg_config.application_name}-listen"`，执行 `LISTEN {channel}` 后立即置位 `_pg_listen_active = True` 并主动触发一次 `notify_wakeup()`，弥合建连前已提交入队的通知空洞（L739–L740）；
  - 兼容新旧版本 psycopg `aconn.notifies(timeout=1.0)` 与无参 `aconn.notifies()`（L742–L747），对零通知快速返回增加 50ms 保护休眠防空转（L759–L761）；
  - 断连或异常时记录 `pg_listen_degraded` 告警，按 `0.5s -> 1.0s -> 2.0s -> ... -> 10.0s` 指数退避重连（L764–L774），期间 `_pg_listen_active` 自动归 `False`。
- **自适应轮询策略（L785–L818）**：
  - `base_interval = max(0.01, poll_interval)`（默认 `0.8s`），`fast_interval = min(base_interval, max(0.05, base_interval * 0.25))`（默认 `0.2s`）；
  - 有任务处理或被推送唤醒时，立即将 `current_wait` 重置为 `fast_interval`（且处理完任务直接 `continue` 零延迟连领）；
  - PG LISTEN 活跃时以 `base_interval` 作为兜底扫描周期（捕获租约超时接管）；PG LISTEN 未启用或降级时，空闲每拍按 `min(base_interval, current_wait * 1.5)` 渐进回退，兼顾低延迟响应与低空载 DB QPS。

#### 3.3.4 轻量级监控服务 `WorkerMetricsServer`（L72–L206）
- 基于纯标准库 `ThreadingHTTPServer`（`allow_reuse_address=True`、`daemon_threads=True`），无 FastAPI/uvicorn 启动开销。
- **路由契约**：
  - `GET /healthz`：返回 `200 application/json`（含 `ok`、`status`、`worker_id`、`host`、`pid`、`active_run_id`、`active_epoch`、`pg_listen_active`），响应头附带 `Cache-Control: no-store`。
  - `GET /metrics`：若配置了环境变量 `AGENT_LAB_WORKER_METRICS_TOKEN`，强制使用 `hmac.compare_digest(auth_header, f"Bearer {expected_tok}")` 常量时间鉴权（失败返回 `401`）；通过后返回 `200 text/plain; version=0.0.4; charset=utf-8`。
  - 其他路径：`404 {"ok": false, "error": "not_found"}`。

---

### 3.4 关键常量、环境变量与指标清单

| 配置项 / 指标 | 默认值 / 标签 | 源码行号 | 说明 |
|---|---|---|---|
| `ttl_seconds` | `15.0` s | L220 | 单次租约有效期 |
| `heartbeat_interval` | `max(0.1, round(ttl / 3.0, 3))`（默认 `5.0` s） | L235–L239 | 允许单次锁抖动而不触发误抢占 |
| `poll_interval` | `0.8` s（下限 `0.01` s） | L226, L240 | 空闲最大兜底轮询周期 |
| `AGENT_LAB_WORKER_WORKSPACES_DIR` | `<db_parent>/worker_workspaces/<worker_id>` | L472–L473 | 跨节点工作区物化根目录 |
| `AGENT_LAB_SYNC_WORKSPACE_ARCHIVE` | `"1"` 时启用 | L589, L631 | 强制每轮完成后同步归档快照至数据库 |
| `AGENT_LAB_WORKER_METRICS_TOKEN` | 空（不鉴权） | L153 | 开启 `/metrics` 常量时间 Bearer 鉴权 |
| `agent_lab_worker_busy` (Gauge) | `{worker_id}` | L316, L327, L411 | Worker 忙闲状态（`0` / `1`） |
| `agent_lab_worker_claims_total` (Counter) | `{worker_id, action, takeover}` | L328–L335 | 任务领取及故障接管计数 |
| `agent_lab_worker_runs_total` (Counter) | `{worker_id, status}` | L419–L422 | Worker 完成任务状态分布 |
| `agent_lab_worker_run_duration_ms` (Histogram) | `{worker_id, action}` | L423–L427 | 单次任务执行端到端耗时 |
| `agent_lab_worker_fenced_total` (Counter) | `{worker_id}` | L607, L623 | 世代栅栏拦截/自熔断次数 |

---

### 3.5 `lease.py` 关键源码符号与精确行号索引

| 符号 / 方法 | 精确行号 | 职责摘要 |
|---|---|---|
| `_validate_pg_channel` | L60–L64 | 校验 PG `LISTEN` 频道标识符防注入 |
| `_ReusableThreadingHTTPServer` / `WorkerMetricsServer` | L67–L206 | 纯标准库 `/metrics` 与 `/healthz` 独立监控服务器 |
| `LeaseWorker.__init__` / `_health_payload` / `notify_wakeup` | L215–L288 | Worker 初始化、健康载荷与线程安全推送唤醒 |
| `LeaseWorker.process_one` | L309–L455 | 原子领取、Trace 绑定、flock 保护、`RunBusy` 重入队与出带 `auto_dream` |
| `LeaseWorker._materialize_workspace_if_needed` | L457–L491 | 跨节点漂移时从 `workspace_archives` 还原工作区 |
| `LeaseWorker._sync_workspace_archive` | L493–L513 | 打包托管工作区快照写库（严格跳过 `direct_workspace`） |
| `LeaseWorker._run_claimed_under_lock` | L514–L674 | 1/3 TTL 心跳协程、自熔断 vs. 优雅退出的核心执行引擎 |
| `LeaseWorker._wait_for_wakeup_or_stop` | L676–L709 | 事件唤醒与停止信号的双路 `FIRST_COMPLETED` 等待原语 |
| `LeaseWorker._pg_listen_loop` | L711–L779 | PostgreSQL `LISTEN agent_lab_queue` 异步监听与指数退避降级 |
| `LeaseWorker.serve_forever` | L781–L836 | 常驻消费主循环（清零防丢唤醒 + 自适应退避轮询） |
| `build_worker_runtime_factory` | L839–L872 | 基于持久化元数据重建绑定 `expected_epoch` 且 `defer_auto_dream=True` 的 Runtime |

---

## 4. 自主 Cron 与 Webhook 触发引擎（`src/agent_lab/triggers.py`）

### 4.1 模块定位与核心职责

- **源码路径**：[src/agent_lab/triggers.py](../../src/agent_lab/triggers.py)（L1–L1116）
- **关联入口**：
  - HTTP API（[src/agent_lab/api.py](../../src/agent_lab/api.py)）：`GET/POST /api/triggers`、`DELETE /api/triggers/{trigger_id}`、`GET /api/triggers/{trigger_id}/fires`、`POST /api/webhooks/{trigger_id}`（`202 Accepted`）；
  - CLI（[src/agent_lab/cli.py](../../src/agent_lab/cli.py) L3129–L3215）：`trigger create` / `trigger list` / `trigger delete` / `trigger tick`（支持 `--now`、`--enqueue-only`、`--timezone`、`--secret-env`）。
- **核心职责**：
  1. **零依赖 5 字段 `CronSchedule` 解析器（L199–L345）**：支持 `*`、`*/n`、`a-b`、`a-b/n`、`a/n`、逗号列表、月份/星期英文缩写、`@` 宏、周日 `0`/`7` 统一映射、Vixie DOM/DOW 组合匹配语义与跳跃加速 `next_fire_time()`。
  2. **常量时间 Webhook 验签（L348–L392）**：`compute_webhook_signature` / `verify_webhook_signature` 严格校验 HMAC-SHA256，阻断伪造回调、算法降级与时序侧信道。
  3. **不可信外部载荷安全渲染 `render_webhook_prompt`（L507–L788）**：针对 `pull_request`、`workflow_run`、`issues` 及通用事件执行双重脱敏、控制字符清洗、围栏闭合转义、单遍白名单模板替换与确定性幂等 `dedup_key` 生成。
  4. **异步定时调度循环 `TriggerScheduler`（L833–L1116）**：支持触发器级 IANA 时区解析、UTC 分钟槽确定性去重 `cron:{trigger_id}:{minute_iso}` 与至多一次（at-most-once）安全派发。

---

### 4.2 触发与防重数据流架构图

```mermaid
flowchart LR
    subgraph CRON_PATH["自主定时路径 (Cron)"]
        TS["TriggerScheduler.tick_once<br/>每 15s (asyncio.Lock 串行)"]
        TZ["slot_utc (YYYY-MM-DDTHH:MMZ)<br/>-> ZoneInfo(timezone) 本地评估"]
        MATCH["CronSchedule.matches(eval_dt)<br/>(缓存上限 512 条)"]
        CKEY["dedup_key =<br/>cron:{trigger_id}:{minute_iso}"]
        TS --> TZ --> MATCH --> CKEY
    end

    subgraph HOOK_PATH["外部回调路径 (Webhook)"]
        REQ["POST /api/webhooks/{trigger_id}<br/>raw body + X-Hub-Signature-256"]
        VER["verify_webhook_signature<br/>拒绝非 sha256= 前缀<br/>hmac.compare_digest 常量比对"]
        REN["render_webhook_prompt<br/>双重脱敏 + 闭合标签转义<br/>单遍白名单正则替换"]
        WKEY["确定性 dedup_key (<=200 chars)<br/>PR / workflow_run / issues / generic"]
        REQ --> VER -->|"合法"| REN --> WKEY
        VER -->|"失败"| REJ["401/403 拒绝"]
    end

    subgraph DEDUP["原子至多一次屏障 (storage.py)"]
        REC["storage.record_trigger_fire<br/>INSERT INTO trigger_fires ...<br/>ON CONFLICT(dedup_key) DO NOTHING"]
    end

    subgraph EXEC["派发与遥测"]
        DISP["dispatch_fn(trigger, dedup_key, fired_at)<br/>创建 Run / 入队 LeaseWorker"]
        MET["METRICS.inc<br/>agent_lab_triggers_fired_total<br/>{kind, status}"]
    end

    CKEY --> REC
    WKEY --> REC
    REC -->|"首次插入成功"| DISP --> MET
    REC -->|"冲突返回 None (重复)"| SKIP["幂等跳过，绝不重复派发"]
```

---

### 4.3 核心算法与安全不变量详解

#### 4.3.1 `CronSchedule` 语法解析、Vixie DOM/DOW 语义与跳跃搜索（L64–L345）
- **7 个 `@` 宏展开（`_CRON_ALIASES`，L64–L72）**：`@hourly` (`0 * * * *`)、`@daily`/`@midnight` (`0 0 * * *`)、`@weekly` (`0 0 * * 0`)、`@monthly` (`0 0 1 * *`)、`@yearly`/`@annually` (`0 0 1 1 *`)。
- **字段范围与特殊归一化（L225–L236）**：
  - `minute`: `0..59`；`hour`: `0..23`；`day_of_month`: `1..31`；`month`: `1..12`（支持 `jan`..`dec`）；
  - `day_of_week`: `0..7`（支持 `sun`..`sat`），显式传入 `wildcard_max=6, wrap_seven_to_zero=True`——因此 `0` 和 `7` 均归一化为周日 `0`，而 `*` 或 `*/n` 仅在 `0..6` 内展开，避免出现重复 `7`。
  - 所有数字原子强制 `clean.isascii() and clean.isdigit()`（L127、L162），防止全角 Unicode 数字绕过校验；显式拒绝倒序范围（`start > end`）、非正步长（`step <= 0`）、多余 `/` 或 `-`。
- **标准 Vixie Cron DOM/DOW 组合匹配（`_day_matches`，L248–L260）**：
  - Python `dt.weekday()`（周一=0..周日=6）经 `(dt.weekday() + 1) % 7` 转为 Cron 星期（周日=0..周六=6）。
  - `dom_wildcard` / `dow_wildcard` 仅当原始字段精确等于 `"*"` 时为 `True`（`*/2` 视为显式受限）。
  - 当 DOM 与 DOW **同时显式受限**时，遵循 Vixie 规范采用**逻辑或（`dom_match or dow_match`）**；仅其一受限时匹配受限字段；均为 `*` 时匹配每日。
- **跳跃加速 `next_fire_time` 与闰年/跨年处理（L273–L342）**：
  - 从 `after.replace(second=0, microsecond=0) + timedelta(minutes=1)` 起至 `base + timedelta(minutes=max_search_minutes)`（默认 `525960` 分钟 = 365.25 天）：
    1. 月份不匹配：直接跳至下一月 1 日 `00:00`（12 月跨年进位 `year+1, month=1, day=1`），由于始终复位至 `day=1`，永不在 2 月 29/30/31 日产生 `ValueError`；
    2. 日期不匹配：经 `cur.date() + timedelta(days=1)` 自动由标准库日历推进（精准跨越平年 2/28->3/1 与闰年 2/28->2/29->3/1），重设 `00:00`；
    3. 小时/分钟不匹配：使用 `bisect_right(sorted_hours / sorted_minutes, cur.hour / cur.minute)` O(log N) 直接跳到下一个合法时/分，无需逐分钟枚举；捕获 `ValueError` / `OverflowError` 防止触及 `datetime.max` 崩溃。

#### 4.3.2 常量时间 HMAC-SHA256 验签（`verify_webhook_signature`，L362–L392）
- 类型与非空校验：拒绝非字节串 `body`、非字符串或空 `secret`/`signature_header`。
- 算法前缀防降级：仅接受 `sha256=<64hex>`（不区分大小写）或裸 `<64hex>`；**凡包含 `=` 但不以 `sha256=` 开头（如 `sha1=`、`md5=`）直接返回 `False`**（L378–L380）。
- 格式预检与常量比对：校验 `_HEX64_RE.fullmatch(provided_hex)` 后计算 `hmac.new(clean_secret.encode("utf-8"), bytes(body), hashlib.sha256).hexdigest().lower()`，严格调用 `hmac.compare_digest(provided_hex, expected_hex)` 消除时序侧信道。

#### 4.3.3 不可信 Webhook 载荷清洗、单遍模板渲染与确定性去重（L399–L788）
- **双重凭据脱敏 `_scrub_text_secrets`（L399–L405）**：串联 `observability.redact_secrets` 与 `egress_dlp.redact_secrets`，防止外部 Webhook 误夹带的密钥/Token 流入 LLM 上下文与持久化轨迹。
- **字段清洗与围栏逃逸防御 `_sanitize_field`（L408–L425）**：
  - 统一换行符，单行字段将 `[\x00-\x1f\x7f]+` 与连续空白折叠为单空格，多行字段保留 `\t`/`\n` 并剥离其余控制字符；
  - 将任何大小写/带空格变体的 `</untrusted_webhook_data>` 替换为 `<\/untrusted_webhook_data>`（`_CLOSING_FENCE_RE`，L104、L420），阻断 Prompt Injection 伪造围栏闭合逃逸数据区；
  - 超长截断附加 `...[truncated]`。
- **单遍白名单正则替换 `_safe_render_template`（L492–L504）**：
  - 严禁使用 `str.format()`（防 `{repo.__class__.__init__.__globals__}` 属性遍历攻击与变量值内再含 `{body}` 触发二次展开）；
  - 仅识别 `_PLACEHOLDER_RE`（`\{([a-zA-Z_][a-zA-Z0-9_]*)\}`）中属于 29 个白名单变量槽的占位符；若用户自定义 `prompt_template` 未显式引用任何已知占位符，自动在末尾追加 `\n\n{context_block}`（L779–L783），确保事件数据不丢失。
- **4 类事件专化与确定性 `dedup_key` 构造**（全部经 `_build_bounded_dedup_key` 限制在 `<= 200` 字符，超长截断并拼 `:{fallback_digest[:16]}`）：

| 事件类型 | 提取核心字段 | 确定性 `dedup_key` 模板 | 源码行号 |
|---|---|---|---|
| `pull_request` | `repo`, `number`, `title`, `body`, `author`, `head_ref -> base_ref`, `head_sha`, `url` | `webhook:pull_request:{repo}:pr-{number}:{action}:{head_sha[:12]\|digest[:12]}` | L548–L589 |
| `workflow_run` | `workflow_name`, `run_id`, `run_attempt`, `status`, `conclusion`, `head_branch`, `head_sha`, `head_commit.message`（失败结论 `failure`/`timed_out`/`startup_failure`/`action_required` 启用故障排查提示头） | `webhook:workflow_run:{repo}:run-{run_id\|digest[:12]}:attempt-{attempt}:{action\|status}:{conclusion\|pending}` | L591–L648 |
| `issues` | `number`, `title`, `labels`（最多 16 个、每个 48 字符）, `body`, `author`, `state`, `updated_at` | `webhook:issues:{repo}:issue-{number}:{action}:{updated_at\|digest[:12]}` | L650–L693 |
| 通用 / 其他 | `title`/`summary`/`subject`, `body`/`description`/`message`, `url`, `branch`/`sha`, 递归脱敏截断 JSON 树（深度 `< 4`、每层最多 24 键 / 16 列表项、字符串上限 240 字符） | `webhook:{norm_event}[:{delivery_id\|event_id\|id}]:{digest[:16]}` | L695–L728 |

#### 4.3.4 `TriggerScheduler` 调度循环与至多一次语义（L833–L1116）
- **并发防重入**：`tick_once` 全程持 `self._tick_lock`（`asyncio.Lock`），避免慢速派发导致相邻 15s tick 重叠扫描。
- **时区评估与 UTC 去重解耦（L1000–L1044）**：
  - 优先从 `trigger["timezone"]` 或 `trigger["metadata"]["timezone"]` 解析 IANA 时区（`ZoneInfo`）用于 `schedule.matches(eval_dt)`；非法时区记录 `cron_trigger_invalid_timezone` 警告并跳过该触发器，不影响其他触发器。
  - 去重键始终基于 **UTC 分钟槽** `cron:{trigger_id}:{YYYY-MM-DDTHH:MMZ}`，确保夏令时（DST）回拨出现重复本地墙钟时间时，两个不同 UTC 瞬间不会错误碰撞。
- **存储原子去重屏障（`_record_fire`，L913–L956）**：仅当 `storage.record_trigger_fire` 成功插入新行且 `_is_fire_accepted` 为 `True`（捕获 `sqlite3.IntegrityError` 视为重复返回 `False`）才执行 `_invoke_dispatch`；派发失败记录 `cron_trigger_dispatch_failed` 与 `agent_lab_triggers_fired_total{kind="cron",status="error"}`，遵循**至多一次（at-most-once）语义**，不会在同一分钟内因异常重试引发副作用风暴。

---

### 4.4 `triggers.py` 关键常量与源码符号索引

| 常量 | 精确数值 | 源码行号 | 说明 |
|---|---|---|---|
| `_MAX_PROMPT_CHARS` | `8000` | L106 | 渲染后完整提示词硬上限 |
| `_MAX_SHORT_FIELD_CHARS` / `_MAX_TITLE_CHARS` / `_MAX_URL_CHARS` / `_MAX_BODY_CHARS` | `180` / `300` / `500` / `2000` | L107–L110 | 外部不可信字段分档截断上限 |
| `_MAX_DEDUP_KEY_CHARS` | `200` | L111 | 幂等去重键长度上限 |
| `_MAX_SCHEDULE_CACHE` | `512` | L112 | `TriggerScheduler` 已解析 `CronSchedule` 内存缓存上限 |
| `next_fire_time.max_search_minutes` | `525960`（约 1 年） | L277 | 防止不可满足表达式（如 2 月 31 日）无限循环 |
| `TriggerScheduler.tick_interval_seconds` | `15.0` s | L841 | 默认扫描周期（1 分钟内最多扫描 4 次，由 DB 去重保证仅触发 1 次） |

| 符号 / 类 / 方法 | 精确行号 | 职责摘要 |
|---|---|---|
| `_parse_cron_atom` / `_parse_cron_field` | L115–L196 | Cron 原子值、区间、步长、名称映射与周日 `7 -> 0` 展开 |
| `CronSchedule` (`parse`, `_day_matches`, `matches`, `next_fire_time`) | L199–L345 | 零依赖 5 字段 Vixie Cron 解析与跳跃搜索 |
| `compute_webhook_signature` / `verify_webhook_signature` | L348–L392 | HMAC-SHA256 签名计算与常量时间防降级验签 |
| `_scrub_text_secrets` / `_sanitize_field` / `_sanitize_payload_tree` / `_safe_render_template` | L399–L504 | 双重脱敏、控制字符与闭合围栏清洗、有界树裁剪、单遍安全渲染 |
| `render_webhook_prompt` | L507–L788 | PR / CI `workflow_run` / `issues` / 通用事件提示词与 `dedup_key` 渲染 |
| `TriggerScheduler` (`tick_once`, `run_forever`, `start`, `stop`) | L833–L1116 | 异步周期 Cron 调度、时区评估、原子去重与签名自适应派发 |

---

## 5. 可观测性、分布式追踪与零信任脱敏（`src/agent_lab/observability.py`）

### 5.1 模块定位与核心职责

- **源码路径**：[src/agent_lab/observability.py](../../src/agent_lab/observability.py)（L1–L818）
- **核心职责**：
  1. **W3C Trace Context 1.0 规范校验与层级传播（L46–L209）**：通过 3 个 `ContextVar`（`_TRACE_ID_VAR`、`_SPAN_ID_VAR`、`_PARENT_SPAN_ID_VAR`）在 FastAPI 协程、`asyncio.to_thread` 线程池与 `LeaseWorker` 异步任务间无损贯通追踪树。
  2. **层级 Span 管理与非阻塞 OTLP/HTTP JSON 异步导出（L211–L511）**：`start_span()` 记录纳秒时间戳与毫秒耗时，配置 `AGENT_LAB_OTLP_ENDPOINT` 时经有界队列后台导出标准 `ExportTraceServiceRequest`，内建严格 SSRF / IMDS / 内网 IP / 重定向与 DNS 重绑定防护。
  3. **零信任递归凭据脱敏与安全错误摘要（L514–L577）**：`redact_secrets` 递归屏蔽敏感键名与内嵌 `Bearer` / URL DSN 凭据；`log_structured` 输出单行脱敏 JSON 日志；`sanitize_error_detail` 彻底屏蔽原始异常文本防止凭据泄露。
  4. **零依赖线程安全 Prometheus 0.0.4 指标注册表（L580–L818）**：`MetricsRegistry` 与全局单例 `METRICS` 提供 Counter / Gauge / 12 桶累积 Histogram、统一工具埋点 `record_tool_metric` 及短临界区快照渲染 `render_prometheus()`。

---

### 5.2 全链路追踪与 OTLP 安全导出架构图

```mermaid
flowchart TB
    IN["上游请求 / Run 元数据<br/>W3C traceparent 或自定义 X-Request-ID"] --> PARSE["_parse_trace_and_span<br/>拒绝 version=ff / 全0 trace-id / 全0 span-id<br/>非安全字符 -> SHA-256[:32] 定长哈希"]
    PARSE --> BIND["bind_trace_context<br/>同 trace_id 重入保留活跃 Span 链<br/>跨 trace_id 切换隔离重置 Span"]
    BIND --> CV["ContextVars 传播作用域<br/>_TRACE_ID_VAR<br/>_SPAN_ID_VAR<br/>_PARENT_SPAN_ID_VAR"]

    CV --> SPAN["start_span(name, attributes)<br/>生成 16-hex 非全0 span_id<br/>关联 parent_span_id + time.time_ns()"]
    CV --> LOG["log_structured<br/>自动注入 trace_id / span_id<br/>+ redact_secrets 脱敏"]
    CV --> EVT["storage.add_event<br/>自动落盘 events.trace_id / span_id"]
    CV --> FMT["format_traceparent<br/>00-{32hex}-{16hex}-01 透传下游"]

    SPAN -->|"退出 finally & AGENT_LAB_OTLP_ENDPOINT 非空"| FAST["_parse_otlp_endpoint_fast (调用线程无阻塞)<br/>校验 http/https、无凭据/fragment/CRLF<br/>拦云元数据域名/*.internal 与字面量非法 IP"]
    FAST -->|"通过"| Q["_BackgroundOTLPExporter<br/>queue.Queue(maxsize=256) put_nowait<br/>队满静默丢弃，永不阻塞业务"]
    Q --> BG["守护线程 agent-lab-otlp-exporter<br/>发送前二次 validate_otlp_endpoint (DNS 解析复核防重绑定)<br/>_NoRedirectHandler 禁用 3xx 跳转<br/>POST application/json (timeout=2.0s)"]
```

---

### 5.3 核心算法、安全不变量与边界处理

#### 5.3.1 W3C `traceparent` 严格校验与重入保护（L92–L208）
- **规范校验规则（L115–L130）**：
  - 正则 `^\s*([0-9a-fA-F]{2})-([0-9a-fA-F]{32})-([0-9a-fA-F]{16})-([0-9a-fA-F]{2})\s*$`；
  - 严格拒绝保留版本 `version == "ff"`、全零 32-hex `trace-id`（`"0"*32`）与全零 16-hex `parent-id`（`"0"*16`），任一违规则自动回退新建随机 `uuid.uuid4().hex`；
  - 非 `traceparent` 输入若命中 32-hex 非全零或 `_SAFE_TRACE_ID_RE`（`^[A-Za-z0-9_.:-]{1,128}$`）则沿用；含空白、换行或控制字符的不可信外部头部统一经 `sha256(raw).hexdigest()[:32]` 哈希定长化，**杜绝 HTTP 响应头拆分与日志注入（Log Injection）**。
- **重入保留不变量（`bind_trace_context`，L175–L184）**：当 `LeaseWorker` 已开启 `lease_worker.process_run` Span 后，内部 `AgentRuntime` 再次调用 `bind_trace_context(same_traceparent)` 时，因 `prev_trace == trace_id`，保留当前活跃 `_SPAN_ID_VAR` 与 `_PARENT_SPAN_ID_VAR` 不被清零，保证 Worker Span -> Runtime Span -> Tool Span 形成完整父子树。
- **16-hex Span ID 非全零保证（`_new_span_id`，L92–L97）**：`os.urandom(8).hex()` 若极小概率全零则强制置为 `"0"*15 + "1"`。

#### 5.3.2 OTLP/HTTP JSON 后台导出器与纵深 SSRF 防御（L246–L458）
1. **零空闲开销**：未设置 `AGENT_LAB_OTLP_ENDPOINT` 时，`_BackgroundOTLPExporter` 不创建任何后台线程（`_ensure_worker` 仅在首次合法 `enqueue` 时双检锁惰性启动守护线程 `"agent-lab-otlp-exporter"`，L408–L420）。
2. **双阶段无阻塞 SSRF 校验**：
   - **阶段 1（主线程，无 DNS 阻塞）`_parse_otlp_endpoint_fast`（L279–L310）**：
     - 仅允许 `http` / `https`；
     - 禁止 `\r`/`\n`/`\x00`、URL 内嵌 `username`/`password`/`fragment`；
     - 强制拦截云元数据主机名黑名单 `_BLOCKED_OTLP_HOSTS`（`metadata.google.internal`、`metadata.goog`、`metadata`、`instance-data`、`metadata.azure.internal`）及所有 `*.internal` 后缀域名；
     - 端口须在 `1..65535`；若是字面量 IP 立即校验 `_is_otlp_ip_allowed`。
   - **阶段 2（后台发送线程，发送前即时复核）`validate_otlp_endpoint`（L313–L330、L427）**：
     - 调用 `socket.getaddrinfo(host, port, SOCK_STREAM)` 解析全部 A/AAAA 记录，要求**每一条解析 IP** 均通过 `_is_otlp_ip_allowed`（先解包 IPv6 `ipv4_mapped` 映射地址，再无条件拦截 `is_link_local`（含 AWS/GCP `169.254.169.254`）、`is_multicast`、`is_unspecified`、`is_reserved`、AWS IPv6 IMDS `fd00:ec2::254/128`、CGNAT `100.64.0.0/10`；默认同时拦截 `is_private`、`is_loopback` 与 `not is_global`，仅当显式配置 `AGENT_LAB_OTLP_ALLOW_PRIVATE` 为 `1`/`true`/`yes` 时允许私有/回环内网 Collector）。
     - 在发送前实时重做 DNS 校验，有效压缩 DNS Rebinding 攻击窗口。
3. **禁用 HTTP 重定向（`_NoRedirectHandler`，L333–L337）**：通过自定义 `HTTPRedirectHandler` 拦截所有 `301`/`302`/`307`/`308` 跳转并抛出 `otlp_redirects_disabled`，彻底封死通过公网中转站 302 跳转探测内网/IMDS 的 SSRF 绕过路径。
4. **导出前属性脱敏与无损降级**：`_build_otlp_span_payload`（L352–L395）先对 `span["attributes"]` 执行 `redact_secrets()` 再组装 OTLP JSON（`service.name` 取自 `AGENT_LAB_SERVICE_NAME`，默认 `"agent-systems-lab"`）；队列满（`maxsize=256`）或远端超时（`timeout=2.0s`）一律静默丢弃，绝不拖慢或打断主任务执行。

#### 5.3.3 递归脱敏、结构化日志与安全错误摘要（L514–L577）
- **`redact_secrets(value)`（L519–L536）**：
  - 字典键名命中 `_SECRET_KEY_RE`（`key|token|secret|authorization|password|credential`，大小写不敏感）时，整个字段值直接替换为 `"[REDACTED]"`；
  - 列表/元组与普通键递归下钻；
  - 字符串执行 `_redact_string`（L514–L516）：屏蔽 `Bearer <value>`（`_BEARER_VALUE_RE`）与 `scheme://user:password@host` 连接串明文密码（`_URL_CRED_RE`）。
  - 与 `egress_dlp.redact_secrets` 的分工：本模块负责通用结构化字段与 Bearer/DSN 清洗（零外部模块依赖，供 `storage.py` 审计日志与 `observability.py` 自身日志/OTLP 导出安全调用）；针对非结构化外部文本中各厂商前缀密钥的深度 DLP 扫描，由 `triggers.py` 等上层串联调用 `egress_dlp.redact_secrets`。
- **`sanitize_error_detail(exc)`（L561–L577）**：
  - 严格**丢弃 `str(exc)` 原始异常消息**（第三方 SDK 报错文本常夹带完整请求头或 DSN），仅提取白名单字段：`{"error_type": type(exc).__name__, "error_code": ..., "retryable": bool, "trace_id": get_trace_id()}`。

#### 5.3.4 零依赖 Prometheus `MetricsRegistry`（L580–L818）
- **Python `bool` 子类陷阱防御（L615、L642、L653、L664、L700）**：在 Python 中 `isinstance(True, int) is True`；所有数值埋点（`inc`、`set_gauge`、`observe_ms`、`record_tool_metric`）均显式拦截 `isinstance(value, bool)` 抛出 `ValueError`，并校验 Counter 增量与耗时 `>= 0`。
- **累积直方图（Cumulative Histogram）与默认桶边界（L74–L87、L687–L690）**：
  - 12 档默认毫秒上界：`(1.0, 5.0, 10.0, 25.0, 50.0, 100.0, 250.0, 500.0, 1000.0, 2500.0, 5000.0, 10000.0)`；
  - 写入时对所有满足 `val <= bound` 的桶递增计数，导出时补充 `le="+Inf"`、`_sum` 与 `_count`，完全符合 Prometheus 0.0.4 规范。
- **统一工具遥测 `record_tool_metric`（L692–L740）**：
  - 归一化 `category` 别名（`tool`/`tools` -> `tool`；`bash`/`bash_exec`/`shell` -> `bash_exec`；`subagent`/`subagents` -> `subagent`；`sandbox`；`mcp`），`tool_name` 截断至 120 字符；
  - 上报通用指标：`agent_lab_tool_calls_total{tool,category,status}`、`agent_lab_tool_duration_ms{tool,category}`，失败时额外上报 `agent_lab_tool_errors_total{tool,category}`；
  - 当 `clean_category != "tool"` 时，同步镜像上报分类专属指标 `agent_lab_{category}_calls_total{tool,status}` 与 `agent_lab_{category}_duration_ms{tool}`。
- **短临界区快照渲染（`render_prometheus`，L774–L815）**：仅在 `with self._lock:` 内执行 3 次字典浅拷贝，把全部 `sorted()`、标签转义（`\` / `\n` / `"`）与字符串拼接移至锁外执行，保证高频 `/metrics` 抓取不阻塞业务线程的指标埋点。

---

### 5.4 `observability.py` 关键源码符号与精确行号索引

| 符号 / 类 / 方法 | 精确行号 | 职责摘要 |
|---|---|---|
| `_TRACE_ID_VAR` / `_SPAN_ID_VAR` / `_PARENT_SPAN_ID_VAR` | L46–L48 | 协程/线程隔离的层级追踪 ContextVars |
| `_DEFAULT_MS_BUCKETS` / `_CGNAT_NET` / `_AWS_IPV6_IMDS_NET` / `_BLOCKED_OTLP_HOSTS` | L62–L87 | 直方图默认 12 桶与 OTLP SSRF 阻断网段/域名常量 |
| `_new_span_id` / `_normalize_trace_id_hex32` / `_parse_trace_and_span` | L92–L130 | W3C `traceparent` 严格校验、非全零保护与注入哈希定长化 |
| `current_trace_id` / `current_span_id` / `current_parent_span_id` / `get_trace_id` / `bind_trace_context` / `format_traceparent` | L139–L208 | 追踪上下文读取、重入安全绑定与 W3C 头格式化 |
| `SpanRecord` | L211–L243 | 兼容字典与属性访问的轻量级 Span 结构 |
| `_is_otlp_ip_allowed` / `_parse_otlp_endpoint_fast` / `validate_otlp_endpoint` / `_NoRedirectHandler` | L246–L337 | OTLP 端点双阶段 SSRF / IMDS / 内网 / 重定向防护 |
| `_to_otlp_attr_value` / `_build_otlp_span_payload` / `_BackgroundOTLPExporter` / `_OTLP_EXPORTER` | L340–L458 | 标准 OTLP/HTTP JSON 构造与有界(256)非阻塞后台导出线程 |
| `start_span` | L461–L511 | 层级 Span 上下文管理器、异常打标与按需异步 OTLP 投递 |
| `_redact_string` / `redact_secrets` / `log_structured` / `sanitize_error_detail` | L514–L577 | 递归凭据脱敏、结构化 JSON 日志与无泄露错误摘要 |
| `MetricsRegistry` (`inc`, `set_gauge`, `observe_ms`, `record_tool_metric`, `snapshot`, `render_prometheus`) / `METRICS` | L623–L818 | 零依赖线程安全指标注册表与 Prometheus 0.0.4 短锁快照导出 |

---

## 6. 跨模块环境变量与端到端高可用协同速查表

### 6.1 环境变量配置总表（均已逐行核验）

| 环境变量 | 所属模块与行号 | 默认值 | 作用与安全边界 |
|---|---|---|---|
| `AGENT_LAB_SQLITE_SYNC` | [storage.py](../../src/agent_lab/storage.py) L393 | `NORMAL` | SQLite `PRAGMA synchronous` 级别（仅允许 `OFF` / `NORMAL` / `FULL` / `EXTRA`） |
| `AGENT_LAB_PG_POOL_SIZE` | [storage.py](../../src/agent_lab/storage.py) L459 | `4` | `Storage.from_dsn("postgresql://...")` 默认连接池最小连接数（`<= 0` 回退直连） |
| `AGENT_LAB_WORKER_WORKSPACES_DIR` | [lease.py](../../src/agent_lab/lease.py) L472 | `<db_parent>/worker_workspaces/<worker_id>` | Worker 跨节点物化工作区根目录 |
| `AGENT_LAB_SYNC_WORKSPACE_ARCHIVE` | [lease.py](../../src/agent_lab/lease.py) L589, L631 | 未设置（按需） | 置 `"1"` 时每轮完成/优雅关机均打包同步托管工作区归档（`direct_workspace` 恒豁免） |
| `AGENT_LAB_WORKER_METRICS_TOKEN` | [lease.py](../../src/agent_lab/lease.py) L153 | 空（不鉴权） | 启用 `WorkerMetricsServer` `/metrics` 常量时间 Bearer 鉴权 |
| `AGENT_LAB_OTLP_ENDPOINT` | [observability.py](../../src/agent_lab/observability.py) L509 | 空（零线程） | 配置后开启 `_BackgroundOTLPExporter` 异步导出 OTLP/HTTP JSON Span |
| `AGENT_LAB_OTLP_ALLOW_PRIVATE` | [observability.py](../../src/agent_lab/observability.py) L272 | 关闭 | 置 `1`/`true`/`yes` 允许 OTLP 导出至内网/回环 Collector（仍强制阻断云 IMDS/链路本地/CGNAT） |
| `AGENT_LAB_SERVICE_NAME` | [observability.py](../../src/agent_lab/observability.py) L380 | `agent-systems-lab` | OTLP Resource `service.name` 属性值 |

### 6.2 端到端故障恢复与一致性协同矩阵

| 故障模式 | 第一道防线 | 第二道防线 | 最终系统状态 |
|---|---|---|---|
| **多副本同时冷启动建表** | SQLite `BEGIN IMMEDIATE` + 8 次线性退避 / PG `pg_advisory_xact_lock(7281946510293847)` | `schema_migrations` 幂等版本号检查 + `ON CONFLICT DO NOTHING` | 所有副本零报错收敛至 v8 Schema |
| **Worker 遭遇 GC 停顿或网络分区后苏醒写库** | `heartbeat_loop` 检测到 `StaleEpochError` 或 `now > lease_deadline` 主动 `fenced_by_heartbeat` 取消执行 | `_verify_epoch_tx` 在每笔 `save_run`/`finish_call`/`drain_steering_messages`/`save_workspace_archive` 拦截旧 `epoch` | 僵尸写入 100% 拒绝，新世代 Worker 数据完整无污染 |
| **单条毒丸任务反复令 Worker 进程 OOM/崩溃** | `claim_next_run` 接管时原子递增 `takeover_count` | `takeover_count >= max_takeovers`（默认 3）自动隔离至 `dead_letter` 并跳过继续领取下一任务 | 毒丸隔离审计，队列整体吞吐不受阻断 |
| **审批瞬间与 Worker 释放租约并发竞态** | `approve_and_enqueue` 全局锁序 `run_leases -> runs -> proposals` 单事务原子置 `queued/resume` + `pg_notify` | `release_lease` 识别同世代 `queued + waiting_approval` 幂等无操作返回 | 零卡死、零误报 `StaleEpochError`，立即推送唤醒恢复 |
| **同机 CLI / Worker 并发重叠操作同一任务** | `exclusive_run` 逐层 `O_NOFOLLOW` + 非阻塞 `flock` + `(st_dev, st_ino)` 复核 | `LeaseWorker` 捕获 `RunBusy` 后将租约退回 `queued` 重试，而非误标 `failed` | 工作区零双写破坏，锁释放后自动平滑恢复 |
| **Webhook 重放 / Cron 周期抖动重复扫描** | `verify_webhook_signature` 常量时间验签 + `CronSchedule` 分钟槽确定性去重键 | `trigger_fires.dedup_key UNIQUE` + `ON CONFLICT DO NOTHING` | 严格保证外部事件与定时任务至多一次派发 |
| **直连宿主代码库误入过期 GC 清理** | `gc_expired_resources` 显式检查 `metadata.direct_workspace` 无条件跳过目录删除 | 所有可删路径均强制 `.resolve()` + `is_relative_to(root)` + 非 symlink 校验 | 用户真实代码库绝对安全，仅清理锁文件与过期截图产物 |
