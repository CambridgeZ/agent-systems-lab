/* 纯浏览器客户端：只展示服务器实际返回的事件，用 textContent 避免工具输出注入 HTML。 */
let current = null,
  timer = null,
  token = "",
  busy = false;
const $ = (id) => document.getElementById(id);
const DEFAULT_PROMPTS = {
  sample: "修复 pricing.py 的百分比折扣 bug，并验证修改。",
  multi_file:
    "请检查 checkout.py 与 discounts.py 的多文件结算逻辑，修复优惠封顶与应付总额缺陷并在容器中验证。",
};
const labels = {
  queued: "已排队",
  running: "运行中",
  waiting_approval: "等待审批",
  completed: "已完成",
  failed: "失败",
  budget_exceeded: "预算已用尽",
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
  model_start: "模型开始决策",
  model_end: "模型返回",
  tool_result: "工具执行完成",
  working_memory_updated: "工作记忆已更新",
  verification_updated: "验证状态更新",
  repair_round_started: "进入自动修复轮次",
  repair_exhausted: "修复轮次已耗尽",
  git_committed: "受控 Git 已提交",
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
    if (p.owner_id)
      box.append(el("small", "租约持有者 / " + p.owner_id + " (epoch=" + p.epoch + ")"));
    if (p.commit_sha)
      box.append(el("small", "Git Commit / " + p.commit_sha.slice(0, 12)));
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
    const titleText =
      p.paths && p.paths.length > 1
        ? "原子变更集 (" + p.paths.length + " 个文件): " + p.paths.join(", ")
        : p.path;
    box.append(
      el("strong", titleText),
      ...(p.summary ? [el("div", p.summary, "muted")] : []),
      ...(p.base_commit
        ? [el("div", "绑定基线 commit: " + p.base_commit.slice(0, 12), "muted")]
        : []),
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
  if (!current) return;
  const id = current;
  try {
    const [run, events, repomap] = await Promise.all([
      api("/runs/" + id),
      api("/runs/" + id + "/events"),
      api("/runs/" + id + "/repomap").catch(() => null),
    ]);
    if (current !== id) return;
    $("status").className = "status " + run.status;
    $("status").textContent = labels[run.status] || run.status;
    $("steps").textContent = run.steps;
    const ver = run.verification || {};
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
    } else {
      $("git-label").textContent = "静态目录副本";
    }
    const tokText =
      run.provider === "demo"
        ? "0 tok"
        : (run.input_tokens + run.output_tokens).toLocaleString();
    const epochText = run.lease?.epoch ? "E" + run.lease.epoch + " · " : "";
    $("lease-tokens").textContent = epochText + run.mode + " · " + tokText;
    $("answer-card").classList.toggle("hidden", !run.answer && !run.error);
    $("answer").textContent = run.answer || "运行停止：" + run.error;
    const leaseActive =
      run.lease && ["queued", "claimed"].includes(run.lease.queue_state);
    $("resume").classList.toggle(
      "hidden",
      !["failed", "running", "queued"].includes(run.status),
    );
    renderWorkingMemory(run.working_memory);
    if (repomap?.map) {
      $("repomap-details").classList.remove("hidden");
      $("repomap-pre").textContent = repomap.map;
    }
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
    if (["queued", "running"].includes(run.status) || leaseActive)
      timer = setTimeout(refreshRun, 700);
    else await loadRuns();
  } catch (e) {
    toast(e.message);
  }
}
if ($("template")) {
  $("template").onchange = () => {
    const next = $("template").value;
    const cur = $("prompt").value.trim();
    if (cur === DEFAULT_PROMPTS.sample || cur === DEFAULT_PROMPTS.multi_file) {
      $("prompt").value = DEFAULT_PROMPTS[next];
    }
  };
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
        template: $("template") ? $("template").value : "sample",
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
