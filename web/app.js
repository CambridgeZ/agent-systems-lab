/* 纯浏览器客户端：只展示服务器实际返回的事件，用 textContent 避免工具输出注入 HTML。 */
let current = null,
  timer = null,
  token = "",
  busy = false;
const $ = (id) => document.getElementById(id);
const labels = {
  queued: "已排队",
  running: "运行中",
  waiting_approval: "等待审批",
  completed: "已完成",
  failed: "失败",
  budget_exceeded: "预算已用尽",
};
const eventNames = {
  tool_audit: "工具审计",
  patch_proposed: "补丁已提出",
  patch_applied: "补丁已应用",
  created: "任务已创建",
  model_start: "模型开始决策",
  model_end: "模型返回",
  tool_result: "工具执行完成",
  waiting_approval: "补丁等待审批",
  approval: "人工审批",
  completed: "任务完成",
  failure: "运行失败",
  workers_finished: "两个研究 Worker 已完成",
  worker_tool: "Worker 读取证据",
  workflow_retrieved: "固定检索阶段完成",
};
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
  current = id;
  clearTimeout(timer);
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
    if (p.result?.code) box.append(el("small", "结果 / " + p.result.code));
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
function renderProposals(proposals, status) {
  $("proposals").replaceChildren();
  const pending = proposals.filter(
    (p) => p.status === "pending" || (p.status === "approved" && !p.applied),
  );
  $("proposal-card").classList.toggle(
    "hidden",
    !pending.length || status !== "waiting_approval",
  );
  for (const p of pending) {
    const box = el("div", undefined, "proposal");
    box.append(
      el("strong", p.path),
      el(
        "pre",
        p.diff ||
          ["--- 原始文件", p.old_content, "+++ 提议文件", p.new_content].join(
            "\n",
          ),
        "diff",
      ),
    );
    const actions = el("div", undefined, "approval-actions");
    for (const [ok, title, cls] of [
      [false, "拒绝补丁", "danger"],
      [true, "批准并继续 ↗", "primary-button"],
    ]) {
      const b = el("button", title, "button " + cls);
      b.onclick = async () => {
        b.disabled = true;
        try {
          await api("/runs/" + current + "/proposals/" + p.id, {
            method: "POST",
            body: JSON.stringify({ approved: ok }),
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
// 轮询只观察状态；浏览器关闭后服务器继续工作。SSE 端点提供给需要流式事件的客户端。
async function refreshRun() {
  if (!current) return;
  const id = current;
  try {
    const [run, events] = await Promise.all([
      api("/runs/" + id),
      api("/runs/" + id + "/events"),
    ]);
    if (current !== id) return;
    $("status").className = "status " + run.status;
    $("status").textContent = labels[run.status] || run.status;
    $("steps").textContent = run.steps;
    $("tokens").textContent =
      run.provider === "demo"
        ? "DEMO / 0"
        : (run.input_tokens + run.output_tokens).toLocaleString();
    $("mode-label").textContent = run.mode;
    $("answer-card").classList.toggle("hidden", !run.answer && !run.error);
    $("answer").textContent = run.answer || "运行停止：" + run.error;
    $("resume").classList.toggle(
      "hidden",
      !["failed", "running", "queued"].includes(run.status),
    );
    renderEvents(events);
    renderProposals(
      run.proposals.map((p) => ({
        ...p,
        diff: events.find(
          (e) => e.kind === "patch_proposed" && e.payload?.proposal_id === p.id,
        )?.payload?.diff,
      })),
      run.status,
    );
    if (["queued", "running"].includes(run.status))
      timer = setTimeout(refreshRun, 700);
    else await loadRuns();
  } catch (e) {
    toast(e.message);
  }
}
$("start").onclick = async () => {
  if (busy) return;
  busy = true;
  $("start").disabled = true;
  try {
    const run = await api("/runs", {
      method: "POST",
      body: JSON.stringify({
        prompt: $("prompt").value,
        mode: $("mode").value,
      }),
    });
    await selectRun(run.id);
  } catch (e) {
    toast(e.message);
  } finally {
    busy = false;
    $("start").disabled = false;
  }
};
$("resume").onclick = async () => {
  try {
    await api("/runs/" + current + "/resume", { method: "POST" });
    await refreshUntilRunning();
  } catch (e) {
    toast(e.message);
  }
};
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
// 记忆写入来自用户显式操作；模型只拥有读取笔记的工具。
async function loadMemory() {
  const memory = await api("/memory");
  $("memory-list").textContent =
    Object.entries(memory)
      .map(([k, v]) => k + "：" + v)
      .join("\n") || "尚未保存笔记";
}
async function init() {
  try {
    const c = await api("/config");
    $("provider").textContent =
      c.provider === "demo" ? "离线 DEMO · 本机运行" : "Claude · 真实模型";
    $("knowledge").textContent =
      c.knowledge.documents +
      " 篇知识笔记 · " +
      c.knowledge.chunks +
      " 个知识块";
    $("skills").replaceChildren(
      ...c.skills.map((s) => el("span", s.name, "skill-chip")),
    );
    if (c.provider === "anthropic")
      $("demo-note").textContent =
        "真实 Claude 模型 · 使用服务端密钥 · 模型请求会计费";
    await Promise.all([loadRuns(), loadMemory()]);
  } catch (e) {
    toast(e.message);
    $("provider").textContent = "连接失败 · 点击钥匙配置 Token";
  }
}
init();
