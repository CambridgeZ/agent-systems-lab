/* 纯浏览器客户端：只展示服务器实际返回的事件，用 textContent 避免工具输出注入 HTML。 */
let current = null,
  timer = null,
  token = "",
  busy = false,
  refreshInFlight = null,
  refreshQueued = false,
  eventRunId = null,
  eventCursor = 0,
  cachedEvents = [],
  lastRepomapKey = null;
const $ = (id) => document.getElementById(id);
const DEFAULT_PROMPTS = {
  sample: "修复 pricing.py 的百分比折扣 bug，并验证修改。",
  multi_file:
    "请检查 checkout.py 与 discounts.py 的多文件结算逻辑，修复优惠封顶与应付总额缺陷并在容器中验证。",
  local_cwd:
    "浏览当前项目目录结构，运行测试查看现状，并汇总模块职责。",
};
const labels = {
  queued: "已排队",
  running: "运行中",
  waiting_approval: "等待审批",
  completed: "已完成",
  failed: "失败",
  budget_exceeded: "预算已用尽",
  cancelled: "已取消",
  interrupted: "已中断",
  dead_letter: "死信隔离",
};
const verLabels = {
  not_run: "未运行",
  syntax_only: "仅语法检查",
  passed: "测试通过 ✓",
  failed: "测试未通过",
  unavailable: "容器不可用",
  unknown: "结果未知",
};
const eventNames = {
  tool_audit: "工具审计",
  patch_proposed: "补丁已提出",
  patch_applied: "补丁已应用",
  created: "任务已创建",
  lease_claimed: "Worker 领取租约",
  worker_fenced: "过期 Worker 被拦截",
  worker_shutdown_requeued: "Worker 停机重入队",
  worker_busy_requeued: "Worker 忙重入队",
  workspace_materialized: "工作区归档已解包",
  run_dead_lettered: "移入死信队列",
  cancel_requested: "已请求取消",
  cancelled: "任务已取消",
  interrupted: "任务已中断",
  model_start: "模型开始决策",
  model_end: "模型返回",
  tool_result: "工具执行完成",
  working_memory_updated: "工作记忆已更新",
  verification_updated: "验证状态更新",
  repair_round_started: "进入自动修复轮次",
  repair_exhausted: "修复轮次已耗尽",
  git_committed: "受控 Git 已提交",
  waiting_approval: "等待人工审批",
  approval: "人工审批",
  action_proposed: "待确认动作提案",
  command_executed: "终端命令已执行",
  file_written: "工作区文件已更新",
  gui_screenshot: "已截取桌面画面",
  gui_action_executed: "桌面 GUI 动作已执行",
  subagents_finished: "动态子 Agent 协同完成",
  memory_note_written: "写入长期记忆笔记",
  memory_salient_tag: "标记显著性记忆点",
  memory_dreamed: "离线 Dreaming 整理完成",
  user_followup: "用户追加多轮指令",
  permission_mode_changed: "权限档位已切换",
  context_built: "上下文预算已构建",
  completed: "任务完成",
  failure: "运行失败",
  workers_finished: "并行研究 Worker 已完成",
  worker_tool: "Worker 读取证据",
  workflow_retrieved: "固定检索阶段完成",
};

function attachArtifactImage(runId, artifactName, container) {
  const url = "/api/runs/" + encodeURIComponent(runId) + "/artifacts/" + encodeURIComponent(artifactName);
  const img = document.createElement("img");
  img.className = "gui-shot-preview";
  img.alt = artifactName;
  if (!token) {
    img.src = url;
    container.append(img);
    return;
  }
  fetch(url, { headers: { Authorization: "Bearer " + token } })
    .then((r) => (r.ok ? r.blob() : null))
    .then((blob) => {
      if (blob) {
        img.src = URL.createObjectURL(blob);
        container.append(img);
      }
    })
    .catch(() => {});
}
function toast(message) {
  $("toast").textContent = message;
  $("toast").classList.remove("hidden");
  setTimeout(() => $("toast").classList.add("hidden"), 4500);
}
// API Token 只在本页面内存中保存；服务器仍是审批与权限校验的最终边界。
async function api(path, options = {}) {
  const res = await fetch("/api" + path, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: "Bearer " + token } : {}),
      ...options.headers,
    },
  });
  if (!res.ok) {
    let detail;
    try {
      detail = (await res.json()).detail;
    } catch {
      detail = res.statusText;
    }
    throw Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}
// 列表是持久状态快照；切换任务不会重新执行模型或复制补丁。
async function loadRuns() {
  const runs = await api("/runs");
  $("runs").replaceChildren();
  if (!runs.length) $("runs").append(el("div", "还没有运行记录", "muted"));
  for (const run of runs) {
    const b = el(
      "button",
      undefined,
      "run-item" + (current === run.id ? " selected" : ""),
    );
    b.append(
      el("b", run.prompt),
      el(
        "small",
        (labels[run.status] || run.status) + " · " + run.id.slice(0, 6),
      ),
    );
    b.onclick = () => selectRun(run.id);
    $("runs").append(b);
  }
}
async function selectRun(id) {
  if (timer) {
    clearTimeout(timer);
    timer = null;
  }
  if (current !== id) {
    current = id;
    eventRunId = null;
    eventCursor = 0;
    cachedEvents = [];
    lastRepomapKey = null;
    if ($("repomap-details")) $("repomap-details").classList.add("hidden");
    if ($("repomap-pre")) $("repomap-pre").textContent = "";
  }
  await refreshRun();
  await loadRuns();
}
// 主界面折叠重复审计事件，完整原始事件始终可从 API / CLI 获取。
function renderEvents(events) {
  $("timeline").replaceChildren();
  $("empty").classList.toggle("hidden", !!events.length);
  for (const event of events.filter(
    (e) => !["tool_audit", "patch_applied", "patch_proposed"].includes(e.kind),
  )) {
    const box = el("div", undefined, "event");
    const head = el("div", undefined, "event-head");
    const p = event.payload || {};
    head.append(
      el(
        "b",
        (eventNames[event.kind] || event.kind) + (p.name ? " · " + p.name : ""),
      ),
      el(
        "time",
        new Date(event.created_at).toLocaleTimeString("zh-CN", {
          hour12: false,
        }),
      ),
    );
    box.append(head);
    if (p.worker) box.append(el("small", "执行者 / " + p.worker));
    if (p.owner_id)
      box.append(el("small", "租约持有者 / " + p.owner_id + " (epoch=" + p.epoch + ")"));
    if (p.commit_sha)
      box.append(el("small", "Git Commit / " + p.commit_sha.slice(0, 12)));
    if (p.result?.code) box.append(el("small", "结果 / " + p.result.code));
    if (event.kind === "tool_result" && p.name === "bash_exec" && p.result) {
      const cmdLine = "$ " + (p.arguments?.command || p.result.command || "");
      const outText = [cmdLine, p.result.stdout || "", p.result.stderr || ""]
        .filter(Boolean)
        .join("\n")
        .slice(0, 2000);
      box.append(el("pre", outText, "terminal-out"));
    }
    if (event.kind === "gui_screenshot" && p.artifact_name && current) {
      attachArtifactImage(current, p.artifact_name, box);
    }
    const detail = el("details");
    detail.append(
      el("summary", "查看结构化数据"),
      el("pre", JSON.stringify(p, null, 2)),
    );
    box.append(detail);
    $("timeline").append(box);
  }
}
// 必须展示具体 diff 再提交审批决定。按钮只发送提案 ID 与布尔值，不能直接写文件。
function renderProposals(proposals, status, waitingIds) {
  $("proposals").replaceChildren();
  const waitSet = Array.isArray(waitingIds) && waitingIds.length ? new Set(waitingIds) : null;
  const pending = proposals.filter((p) =>
    waitSet
      ? waitSet.has(p.id)
      : p.status === "pending" || (p.status === "approved" && !p.applied),
  );
  $("proposal-card").classList.toggle(
    "hidden",
    !pending.length || status !== "waiting_approval",
  );
  for (const p of pending) {
    const box = el("div", undefined, "proposal");
    const kind = p.kind || "single_file";
    let titleText = p.path || "待确认提案";
    let previewBody = "";
    if (kind === "command") {
      const args = p.arguments || {};
      titleText = "终端命令确认 · cwd=" + (args.cwd || ".");
      previewBody = "$ " + (args.command || p.new_content || "");
    } else if (kind === "gui_action") {
      titleText = "桌面 GUI 动作确认 · " + (p.tool || "computer");
      previewBody = JSON.stringify(p.arguments || {}, null, 2);
    } else if (kind === "mcp_call") {
      titleText = "外部 MCP 工具确认 · " + (p.tool || "mcp");
      previewBody = JSON.stringify(p.arguments || {}, null, 2);
    } else {
      titleText =
        p.paths && p.paths.length > 1
          ? "原子变更集 (" + p.paths.length + " 个文件): " + p.paths.join(", ")
          : p.path;
      previewBody =
        p.diff ||
        ["--- 原始文件", p.old_content, "+++ 提议文件", p.new_content].join("\n");
    }
    box.append(
      el("strong", titleText),
      ...(p.summary ? [el("div", p.summary, "muted")] : []),
      ...(p.risk_level ? [el("div", "风险分级: " + p.risk_level, "muted")] : []),
      ...(p.base_commit
        ? [el("div", "绑定基线 commit: " + p.base_commit.slice(0, 12), "muted")]
        : []),
      el("pre", previewBody, kind === "command" ? "terminal-out" : "diff"),
    );
    const actions = el("div", undefined, "approval-actions");
    const actionSpecs = [
      [false, "拒绝", "danger", null],
      [true, "批准并继续 ↗", "primary-button", null],
    ];
    if (["command", "gui_action", "mcp_call"].includes(kind)) {
      actionSpecs.splice(1, 0, [true, "切为 Full-Auto 并批准", "secondary", "full-auto"]);
    }
    for (const [ok, title, cls, nextMode] of actionSpecs) {
      const b = el("button", title, "button " + cls);
      b.onclick = async () => {
        b.disabled = true;
        try {
          await api("/runs/" + current + "/proposals/" + p.id, {
            method: "POST",
            body: JSON.stringify({
              approved: ok,
              ...(nextMode ? { permission_mode: nextMode } : {}),
            }),
          });
          await refreshUntilRunning();
        } catch (e) {
          toast(e.message);
        } finally {
          b.disabled = false;
        }
      };
      actions.append(b);
    }
    box.append(actions);
    $("proposals").append(box);
  }
}
async function refreshUntilRunning() {
  await new Promise((r) => setTimeout(r, 150));
  await refreshRun();
}
function renderWorkingMemory(wm) {
  const box = $("wm-box");
  if (!box) return;
  box.replaceChildren();
  if (!wm) {
    box.textContent = "本任务尚未写入结构化工作记忆。";
    return;
  }
  if (wm.goal) box.append(el("div", "目标：" + wm.goal, "wm-goal"));
  const summary =
    "有效事实 " +
    (wm.valid_facts?.length || 0) +
    " 条 · 过期(stale) " +
    (wm.stale_count || 0) +
    " 条 · 待办 " +
    (wm.todos?.length || 0) +
    " 项";
  box.append(el("div", summary, wm.stale_count ? "wm-stale" : "wm-valid"));
  for (const f of [...(wm.valid_facts || []), ...(wm.stale_facts || [])]) {
    const tag = f.status === "stale" ? "[STALE:" + f.stale_reason + "] " : "[VALID] ";
    box.append(el("div", tag + f.source_ref + " — " + f.claim, "wm-fact"));
  }
}
// 轮询只观察状态；浏览器关闭后服务器继续工作。SSE 端点提供给需要流式事件的客户端。
async function refreshRun() {
  if (timer) {
    clearTimeout(timer);
    timer = null;
  }
  if (!current) return;
  if (refreshInFlight) {
    refreshQueued = true;
    return refreshInFlight;
  }
  refreshInFlight = (async () => {
    do {
      refreshQueued = false;
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      if (!current) return;
      await refreshRunOnce();
    } while (refreshQueued);
  })().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}
async function refreshRunOnce() {
  const id = current;
  if (!id) return;
  const resetEvents = eventRunId !== id;
  try {
    const [run, newEvents] = await Promise.all([
      api("/runs/" + id),
      api("/runs/" + id + "/events?after=" + (eventRunId === id ? eventCursor : 0)),
    ]);
    if (current !== id) return;
    if (resetEvents) {
      eventRunId = id;
      eventCursor = 0;
      cachedEvents = [];
    }
    let appended = resetEvents;
    if (Array.isArray(newEvents) && newEvents.length) {
      for (const ev of newEvents) {
        if (ev.id > eventCursor) {
          cachedEvents.push(ev);
          eventCursor = ev.id;
          appended = true;
        }
      }
    }
    let batch = newEvents;
    let pages = 0;
    while (Array.isArray(batch) && batch.length === 2000 && pages < 10) {
      pages += 1;
      batch = await api("/runs/" + id + "/events?after=" + eventCursor);
      if (current !== id) return;
      for (const ev of batch) {
        if (ev.id > eventCursor) {
          cachedEvents.push(ev);
          eventCursor = ev.id;
          appended = true;
        }
      }
    }
    const ver = run.verification || {};
    const repomapKey = `${id}:${run.steps}:${ver.status}`;
    if ($("repomap-details")?.open || repomapKey !== lastRepomapKey) {
      const repomap = await api("/runs/" + id + "/repomap").catch(() => null);
      if (current !== id) return;
      lastRepomapKey = repomapKey;
      if (repomap?.map) {
        $("repomap-details").classList.remove("hidden");
        $("repomap-pre").textContent = repomap.map;
      } else if ($("repomap-details")) {
        $("repomap-details").classList.add("hidden");
      }
    }
    const displayStatus =
      run.lease?.queue_state === "dead_letter" ? "dead_letter" : run.status;
    $("status").className = "status " + displayStatus;
    $("status").textContent = labels[displayStatus] || displayStatus;
    $("steps").textContent = run.steps;
    const repairSuffix = ver.repair_rounds_used
      ? " (修复" + ver.repair_rounds_used + "轮)"
      : "";
    $("verification-label").textContent =
      (verLabels[ver.status] || ver.status || "—") + repairSuffix;
    if (run.git?.base_commit) {
      const baseShort = run.git.base_commit.slice(0, 7);
      const commitShort = (ver.committed_sha || run.git.committed_sha || "").slice(0, 7);
      $("git-label").textContent = commitShort
        ? baseShort + " → " + commitShort
        : baseShort + " (未提交)";
    } else if (run.direct_workspace) {
      $("git-label").textContent =
        "本地直连 (" + (run.permission_mode || "auto-edit") + ")";
    } else {
      $("git-label").textContent = "静态目录副本";
    }
    const tokText =
      run.provider === "demo"
        ? "0 tok"
        : (run.input_tokens + run.output_tokens).toLocaleString();
    const epochText = run.lease?.epoch ? "E" + run.lease.epoch + " · " : "";
    const kvRate = run.kv_cache?.steps_recorded
      ? "KV " + Math.round((run.kv_cache.cumulative_hit_rate || 0) * 100) + "% · "
      : "";
    $("lease-tokens").textContent =
      epochText + kvRate + run.mode + " · " + tokText;
    $("answer-card").classList.toggle("hidden", !run.answer && !run.error);
    const isErrState = ["failed", "budget_exceeded", "cancelled", "interrupted"].includes(
      displayStatus,
    );
    $("answer").textContent =
      isErrState && run.error
        ? "运行停止：" + run.error + (run.answer ? "\n\n" + run.answer : "")
        : run.answer || "运行停止：" + run.error;
    const leaseActive =
      run.lease && ["queued", "claimed"].includes(run.lease.queue_state);
    $("resume").classList.toggle(
      "hidden",
      !["failed", "interrupted", "running", "queued"].includes(run.status) ||
        run.lease?.queue_state === "dead_letter",
    );
    if ($("cancel")) {
      $("cancel").classList.toggle(
        "hidden",
        !["queued", "running", "waiting_approval"].includes(run.status),
      );
    }
    if ($("followup-card")) {
      const canFollowUp =
        ["completed", "failed", "cancelled", "budget_exceeded"].includes(run.status) &&
        run.lease?.queue_state !== "dead_letter";
      $("followup-card").classList.toggle("hidden", !canFollowUp);
      if ($("run-perm-mode") && run.permission_mode) {
        $("run-perm-mode").value = run.permission_mode;
      }
    }
    renderWorkingMemory(run.working_memory);
    if (appended) renderEvents(cachedEvents);
    renderProposals(
      run.proposals.map((p) => ({
        ...p,
        diff: cachedEvents.find(
          (e) => e.kind === "patch_proposed" && e.payload?.proposal_id === p.id,
        )?.payload?.diff,
      })),
      run.status,
      run.waiting_proposal_ids,
    );
    if (!refreshQueued) {
      if (["queued", "running"].includes(run.status) || leaseActive || run.dream_pending) {
        if (timer) clearTimeout(timer);
        timer = setTimeout(refreshRun, 700);
      } else {
        await Promise.all([loadRuns(), loadMemoryOverview()]);
      }
    }
  } catch (e) {
    toast(e.message);
  }
}
function syncTemplateControls() {
  if (!$("template")) return;
  const tpl = $("template").value;
  const isLocal = tpl === "local_cwd";
  if ($("local-cwd-row")) $("local-cwd-row").classList.toggle("hidden", !isLocal);
  if ($("perm-wrap")) $("perm-wrap").classList.toggle("hidden", !isLocal);
  if ($("gui-wrap")) $("gui-wrap").classList.toggle("hidden", !isLocal);
  if ($("template-badge")) {
    $("template-badge").textContent = isLocal
      ? "本地目录直连 / 终端命令 + 文件系统 + GUI"
      : "独立示例副本 / Git Worktree";
  }
}
if ($("template")) {
  $("template").onchange = () => {
    const next = $("template").value;
    const cur = $("prompt").value.trim();
    if (Object.values(DEFAULT_PROMPTS).includes(cur)) {
      $("prompt").value = DEFAULT_PROMPTS[next] || DEFAULT_PROMPTS.sample;
    }
    syncTemplateControls();
  };
  syncTemplateControls();
}
$("start").onclick = async () => {
  if (busy) return;
  busy = true;
  $("start").disabled = true;
  try {
    const tpl = $("template") ? $("template").value : "sample";
    const payload = {
      prompt: $("prompt").value,
      mode: $("mode").value,
      template: tpl,
    };
    if (tpl === "local_cwd") {
      const wsVal = $("workspace-path") ? $("workspace-path").value.trim() : "";
      if (wsVal) payload.workspace_path = wsVal;
      if ($("permission-mode")) payload.permission_mode = $("permission-mode").value;
      if ($("enable-gui")) payload.enable_gui = !!$("enable-gui").checked;
    }
    const run = await api("/runs", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    await selectRun(run.id);
  } catch (e) {
    toast(e.message);
  } finally {
    busy = false;
    $("start").disabled = false;
  }
};
if ($("send-followup")) {
  $("send-followup").onclick = async () => {
    if (!current || busy) return;
    const text = $("followup-prompt").value.trim();
    if (!text) return;
    busy = true;
    $("send-followup").disabled = true;
    try {
      await api("/runs/" + current + "/messages", {
        method: "POST",
        body: JSON.stringify({ prompt: text }),
      });
      $("followup-prompt").value = "";
      await refreshUntilRunning();
    } catch (e) {
      toast(e.message);
    } finally {
      busy = false;
      $("send-followup").disabled = false;
    }
  };
}
if ($("run-perm-mode")) {
  $("run-perm-mode").onchange = async () => {
    if (!current) return;
    try {
      await api("/runs/" + current + "/permissions", {
        method: "PATCH",
        body: JSON.stringify({ permission_mode: $("run-perm-mode").value }),
      });
      toast("已切换当前会话权限为 " + $("run-perm-mode").value);
      await refreshRun();
    } catch (e) {
      toast(e.message);
    }
  };
}
$("resume").onclick = async () => {
  if (!current) return;
  $("resume").disabled = true;
  try {
    await api("/runs/" + current + "/resume", { method: "POST" });
    await refreshUntilRunning();
  } catch (e) {
    toast(e.message);
  } finally {
    $("resume").disabled = false;
  }
};
if ($("cancel")) {
  $("cancel").onclick = async () => {
    if (!current) return;
    $("cancel").disabled = true;
    try {
      await api("/runs/" + current + "/cancel", { method: "POST" });
      await refreshRun();
      await loadRuns();
    } catch (e) {
      toast(e.message);
    } finally {
      $("cancel").disabled = false;
    }
  };
}
$("refresh").onclick = () => loadRuns().catch((e) => toast(e.message));
$("auth").onclick = () => {
  token =
    window.prompt("输入 AGENT_LAB_TOKEN（仅保存在当前页面内存中）", "") || "";
  init();
};
$("save-memory").onclick = async () => {
  try {
    await api("/memory", {
      method: "POST",
      body: JSON.stringify({
        key: $("memory-key").value,
        value: $("memory-value").value,
      }),
    });
    toast("笔记已保存，下次任务可通过 memory_get 读取");
    await loadMemory();
  } catch (e) {
    toast(e.message);
  }
};
// 记忆写入来自用户显式操作或模型自主沉淀；顶层索引有界预载，子页按需查阅。
async function loadMemory() {
  const memory = await api("/memory");
  $("memory-list").textContent =
    Object.entries(memory)
      .map(([k, v]) => k + "：" + (typeof v === "string" ? v : JSON.stringify(v)))
      .join("\n") || "尚未保存笔记";
  await loadMemoryOverview();
}
function memoryScopeQueryParams() {
  if (current) return "run_id=" + encodeURIComponent(current);
  if ($("template")?.value === "local_cwd" && $("workspace-path")?.value.trim()) {
    return "workspace_path=" + encodeURIComponent($("workspace-path").value.trim());
  }
  return "";
}
async function loadMemoryOverview() {
  if (!$("memory-budget-stat")) return;
  try {
    const scopeParam = memoryScopeQueryParams();
    const qs = scopeParam ? "?" + scopeParam : "";
    const ov = await api("/memory/overview" + qs);
    const gs = ov.graph?.stats || {};
    $("memory-budget-stat").textContent =
      "MEMORY.md " +
      (ov.index_budget?.used_lines || 0) +
      "/" +
      (ov.index_budget?.max_lines || 200) +
      " 行 · Wiki 页 " +
      (gs.pages || 0) +
      " · 链接边 " +
      (gs.edges || 0) +
      (gs.broken_links ? " · 断链 " + gs.broken_links : "");
    if ($("dream-stat")) {
      const lastDream = (ov.recent_dreams || [])[0];
      $("dream-stat").textContent = lastDream
        ? "最近 Dreaming (" +
          lastDream.trigger +
          ")：重放 " +
          lastDream.sessions_replayed +
          " 会话 · 纠错 " +
          lastDream.corrections_count +
          " · 更新页 " +
          lastDream.pages_written_count +
          " · 修断链 " +
          lastDream.links_repaired_count
        : "每次会话完成后默认自动运行 Dreaming 纠错与建图。";
    }
    if ($("memory-index-pre")) {
      const combinedIdx = [
        ov.global?.index_markdown ? "=== global/MEMORY.md ===\n" + ov.global.index_markdown : "",
        ov.project?.index_markdown
          ? "=== " + ov.project_slug + "/MEMORY.md ===\n" + ov.project.index_markdown
          : "",
      ]
        .filter(Boolean)
        .join("\n\n");
      $("memory-index-pre").textContent = combinedIdx || "（尚无 MEMORY.md 顶层索引，完成会话或运行 Dreaming 后自动生成）";
    }
    if ($("wiki-pages-list")) {
      $("wiki-pages-list").replaceChildren();
      const allPages = [...(ov.project?.pages || []), ...(ov.global?.pages || [])];
      for (const p of allPages) {
        const chip = el(
          "button",
          (p.layer === "global" ? "G:" : "P:") + p.key,
          "wiki-chip" + (p.status !== "confirmed" ? " hypothesis" : ""),
        );
        chip.title = p.title + " (" + p.status + ")";
        chip.onclick = async () => {
          try {
            const extraScope = memoryScopeQueryParams();
            const page = await api(
              "/memory/page?layer=" +
                encodeURIComponent(p.layer) +
                "&path=" +
                encodeURIComponent(p.path) +
                (extraScope ? "&" + extraScope : ""),
            );
            if ($("wiki-page-viewer")) {
              $("wiki-page-viewer").classList.remove("hidden");
              $("wiki-page-viewer").open = true;
              $("wiki-page-title").textContent = page.layer + ":" + page.path + " · " + page.title;
              $("wiki-page-meta").textContent =
                "状态: " +
                page.status +
                " | 出链: " +
                ((page.links || []).join(", ") || "无") +
                " | 反向入链: " +
                ((page.backlinks || []).join(", ") || "无");
              $("wiki-page-body").textContent = page.content;
            }
          } catch (e) {
            toast(e.message);
          }
        };
        $("wiki-pages-list").append(chip);
      }
    }
  } catch {
    // 忽略未授权状态下的预览刷新异常
  }
}
if ($("run-dream")) {
  $("run-dream").onclick = async () => {
    $("run-dream").disabled = true;
    try {
      const goalVal = $("dream-goal") ? $("dream-goal").value.trim() : "";
      const rep = await api("/memory/dream", {
        method: "POST",
        body: JSON.stringify({
          ...(current ? { run_id: current } : {}),
          ...(goalVal ? { focus_goal: goalVal } : {}),
        }),
      });
      toast(
        "Dreaming 完成：纠错 " +
          rep.corrections_count +
          " 项 · 更新 Wiki " +
          rep.pages_written_count +
          " 页 · 修复断链 " +
          rep.links_repaired_count +
          " 处",
      );
      await loadMemoryOverview();
    } catch (e) {
      toast(e.message);
    } finally {
      $("run-dream").disabled = false;
    }
  };
}
async function init() {
  try {
    const c = await api("/config");
    $("provider").textContent =
      c.provider === "demo"
        ? "离线 DEMO · " + (c.dispatch_mode === "queue" ? "队列模式" : "内联模式")
        : c.provider.toUpperCase() + " · 真实模型";
    if ($("worker-stat")) {
      const activeWorkers = (c.workers || []).filter((w) => w.status !== "stopped");
      $("worker-stat").textContent =
        "调度：" +
        (c.dispatch_mode || "inline") +
        " · 已登记 Worker " +
        activeWorkers.length +
        " 个";
    }
    $("knowledge").textContent =
      c.knowledge.documents +
      " 篇知识笔记 · " +
      c.knowledge.chunks +
      " 个知识块";
    $("skills").replaceChildren(
      ...c.skills.map((s) => el("span", s.name, "skill-chip")),
    );
    if (c.host_capabilities?.default_workspace && $("workspace-path") && !$("workspace-path").value) {
      $("workspace-path").value = c.host_capabilities.default_workspace;
    }
    if (c.provider && c.provider !== "demo")
      $("demo-note").textContent =
        "真实模型已就绪 (" + c.provider.toUpperCase() + ") · 支持本地操控与多轮对话 · 模型请求会计费";
    await Promise.all([loadRuns(), loadMemory()]);
  } catch (e) {
    toast(e.message);
    $("provider").textContent = "连接失败 · 点击钥匙配置 Token";
  }
}
init();
