/* 纯浏览器客户端：只展示服务器实际返回的事件，全程零 innerHTML，杜绝工具输出注入 HTML。 */
let current = null,
  timer = null,
  sidebarTimer = null,
  searchTimer = null,
  token = "",
  busy = false,
  followupBusy = false,
  compactBusy = false,
  refreshInFlight = null,
  refreshQueued = false,
  eventRunId = null,
  eventCursor = 0,
  cachedEvents = [],
  timelineRunId = null,
  lastRepomapKey = null,
  lastRunsSignature = "",
  latestMemoryOverview = null,
  latestConfigSnapshot = null,
  latestGatewaysData = null,
  gatewayEditorDirty = false,
  latestA2AData = null,
  a2aEditorDirty = false,
  coreDocDirty = false,
  lastCoreDocScope = "",
  activeEventSource = null,
  activeStreamRunId = null,
  streamFallbackPolling = false,
  streamAnswerText = "",
  streamThinkingText = "",
  streamBashText = "",
  streamSubagents = new Map(),
  renameTargetRunId = null,
  pendingDeleteRunId = null;
const decidedProposalIds = new Set();
const renderedEventIds = new Set();
const artifactBlobCache = new Map();
const streamAfterSeqByRun = new Map();
const composerAttachments = [];
const followupAttachments = [];
let streamTurnStartSeq = 0;
let liveStepNodes = [];
let currentLiveStepText = "";
let liveToolPills = new Map();
let lastChatThreadSig = "";
let lastProposalsSig = "";
let lastWmSig = "";
let lastCheckpointsSig = "";
let lastBgProcsSig = "";
let lastStreamRenderAt = 0;
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
  subagent_started: "✦ 子 Agent 已启动",
  subagents_started: "✦ 并行子 Agent 批次已启动",
  subagent_finished: "✦ 子 Agent 完成汇报",
  subagents_finished: "✦ 动态子 Agent 协同完成",
  memory_note_written: "写入长期记忆笔记",
  memory_page_written: "长期记忆页已保存",
  memory_updated: "长期记忆已更新",
  memory_salient_tag: "标记显著性记忆点",
  memory_dreamed: "离线 Dreaming 整理完成",
  user_steering_queued: "⚡ 用户实时补充指令已入队",
  user_steering_injected: "⚡ 实时指令已注入当前会话",
  user_followup: "↳ 用户追加多轮指令",
  proposals_superseded: "待审提案已由新指令替代",
  waiting_approval_superseded: "待审提案已由新指令替代",
  permission_mode_changed: "权限档位已切换",
  plan_mode_entered: "📋 已进入只读规划模式",
  egress_dlp_blocked: "🛡 出站安全 DLP 已拦截敏感凭据",
  session_grant_added: "已添加本会话模式放行规则",
  run_rewound: "↶ 已回退至检查点",
  checkpoint_created: "⏱ 已创建回合检查点",
  hook_executed: "钩子已执行",
  context_built: "上下文预算已构建",
  context_compacted: "🗜 上下文自动/滚动压缩完成",
  skill_routed: "◈ 多通道技能路由命中",
  skills_routed: "◈ 多通道技能路由命中",
  mcp_tools_deferred: "⌕ MCP 工具目录延迟按需挂载",
  mcp_tool_search: "⌕ MCP 工具按需检索完成",
  mcp_tool_searched: "⌕ MCP 工具按需检索完成",
  a2a_task_delegated: "⇄ A2A 远端任务已委托",
  completed: "任务完成",
  failure: "运行失败",
  workers_finished: "并行研究 Worker 已完成",
  worker_tool: "Worker 读取证据",
  workflow_retrieved: "固定检索阶段完成",
};

function revokeArtifactBlobs(exceptRunId = null) {
  for (const [key, blobUrl] of artifactBlobCache.entries()) {
    if (exceptRunId && key.startsWith(exceptRunId + ":")) continue;
    try {
      URL.revokeObjectURL(blobUrl);
    } catch {}
    artifactBlobCache.delete(key);
  }
}

function attachArtifactImage(runId, artifactName, container) {
  const url =
    "/api/runs/" + encodeURIComponent(runId) + "/artifacts/" + encodeURIComponent(artifactName);
  const cacheKey = runId + ":" + artifactName;
  const img = document.createElement("img");
  img.className = "gui-shot-preview";
  img.alt = artifactName;
  if (!token) {
    img.src = url;
    container.append(img);
    return;
  }
  if (artifactBlobCache.has(cacheKey)) {
    img.src = artifactBlobCache.get(cacheKey);
    container.append(img);
    return;
  }
  fetch(url, {
    credentials: "same-origin",
    headers: { Authorization: "Bearer " + token },
  })
    .then((r) => (r.ok ? r.blob() : null))
    .then((blob) => {
      if (!blob) return;
      if (artifactBlobCache.size > 40) revokeArtifactBlobs(current);
      const blobUrl = URL.createObjectURL(blob);
      artifactBlobCache.set(cacheKey, blobUrl);
      img.src = blobUrl;
      container.append(img);
    })
    .catch(() => {});
}

window.addEventListener("beforeunload", () => {
  disconnectRunStream();
  revokeArtifactBlobs(null);
});

function toast(message, level = "info") {
  const text = String(message || "");
  if ($("toast")) $("toast").textContent = text;
  const stack = $("toast-stack");
  if (!stack) {
    if ($("toast")) {
      $("toast").classList.remove("hidden");
      setTimeout(() => $("toast")?.classList.add("hidden"), 4500);
    }
    return;
  }
  const item = el("div", undefined, "toast-item " + (level || "info"));
  const body = el("span", text);
  const closeBtn = el("button", "✕", "toast-close");
  closeBtn.type = "button";
  closeBtn.setAttribute("aria-label", "关闭提示");
  let hideTimer = null;
  const dismiss = () => {
    if (hideTimer) clearTimeout(hideTimer);
    item.remove();
  };
  const armTimer = (ms) => {
    if (hideTimer) clearTimeout(hideTimer);
    hideTimer = setTimeout(dismiss, ms);
  };
  closeBtn.onclick = dismiss;
  item.onmouseenter = () => {
    if (hideTimer) clearTimeout(hideTimer);
  };
  item.onmouseleave = () => armTimer(2200);
  item.append(body, closeBtn);
  stack.append(item);
  while (stack.children.length > 5) stack.firstElementChild?.remove();
  armTimer(level === "error" ? 6500 : 4200);
}

function copyTextToClipboard(text) {
  const val = String(text || "");
  if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
    return navigator.clipboard.writeText(val).catch(() => fallbackCopyText(val));
  }
  return Promise.resolve(fallbackCopyText(val));
}

function fallbackCopyText(val) {
  const ta = document.createElement("textarea");
  ta.value = val;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.append(ta);
  ta.select();
  try {
    document.execCommand("copy");
  } catch {}
  ta.remove();
}

function makeCopyButton(getText, label = "复制") {
  const btn = el("button", label, "copy-btn");
  btn.type = "button";
  btn.onclick = async (ev) => {
    ev.stopPropagation();
    await copyTextToClipboard(typeof getText === "function" ? getText() : getText);
    btn.textContent = "已复制 ✓";
    btn.classList.add("copied");
    setTimeout(() => {
      btn.textContent = label;
      btn.classList.remove("copied");
    }, 1800);
  };
  return btn;
}

function isNearBottom(node, threshold = 48) {
  if (!node) return true;
  if (node === document.documentElement || node === window) {
    const doc = document.documentElement;
    return doc.scrollHeight - (window.scrollY + window.innerHeight) <= threshold;
  }
  return node.scrollHeight - (node.scrollTop + node.clientHeight) <= threshold;
}

function maybeStickBottom(node, { force = false, markJumpBtn = false } = {}) {
  if (!node) return;
  if (force || isNearBottom(node, 56)) {
    if (node === document.documentElement || node === window) {
      window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "auto" });
    } else {
      node.scrollTop = node.scrollHeight;
    }
    if (markJumpBtn && $("jump-bottom-btn")) {
      $("jump-bottom-btn").classList.add("hidden");
      $("jump-bottom-btn").classList.remove("has-new");
    }
  } else if (markJumpBtn && $("jump-bottom-btn")) {
    $("jump-bottom-btn").classList.remove("hidden");
    $("jump-bottom-btn").classList.add("has-new");
  }
}

let streamRafId = null;
function ensureCurrentLiveStepNode() {
  const ans = $("live-stream-answer");
  if (!ans) return null;
  if (!liveStepNodes.length) {
    const node = el("div", undefined, "stream-step-block");
    ans.append(node);
    liveStepNodes.push(node);
  }
  return liveStepNodes[liveStepNodes.length - 1];
}
function finalizeCurrentLiveStepNode() {
  if (currentLiveStepText.trim()) {
    const node = ensureCurrentLiveStepNode();
    if (node) renderSafeMarkdown(node, currentLiveStepText);
    currentLiveStepText = "";
    const nextNode = el("div", undefined, "stream-step-block");
    if ($("live-stream-answer")) $("live-stream-answer").append(nextNode);
    liveStepNodes.push(nextNode);
  }
}
function upsertLiveToolPill(key, name, state = "running") {
  const strip = $("live-tool-strip");
  if (!strip) return;
  strip.classList.remove("hidden");
  let pill = liveToolPills.get(key);
  if (!pill) {
    pill = el("span", "", "live-tool-pill " + state);
    liveToolPills.set(key, pill);
    strip.append(pill);
    while (strip.children.length > 10) {
      const first = strip.firstElementChild;
      if (first) first.remove();
    }
  } else {
    pill.className = "live-tool-pill " + state;
  }
  const icon = state === "running" ? "⚙ " : state === "error" ? "✕ " : "✓ ";
  pill.textContent = icon + name;
}
function scheduleStreamMarkdownRender() {
  if (streamRafId !== null) return;
  const delay = Math.max(0, 60 - (Date.now() - lastStreamRenderAt));
  streamRafId = setTimeout(() => {
    streamRafId = null;
    lastStreamRenderAt = Date.now();
    const node = ensureCurrentLiveStepNode();
    if (node && currentLiveStepText) {
      renderSafeMarkdown(node, currentLiveStepText);
      maybeStickBottom(document.documentElement, { markJumpBtn: true });
    }
  }, delay);
}
// 优先使用服务端签发的 HttpOnly 会话 Cookie，兼容显式内存 Bearer Token。
async function api(path, options = {}) {
  const res = await fetch("/api" + path, {
    credentials: "same-origin",
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

// 零 innerHTML 彩色行号统一 Diff 渲染器（支持审批提案、自动编辑时间线与 Dreaming 预演）
function renderUnifiedDiff(diffText, { maxLines = 220 } = {}) {
  const viewer = el("div", undefined, "diff-viewer");
  const rawLines = String(diffText || "").replace(/\r\n/g, "\n").split("\n");
  let oldLn = 0;
  let newLn = 0;
  let shown = 0;
  for (const line of rawLines) {
    if (shown >= maxLines) {
      const moreRow = el("div", undefined, "diff-line diff-meta");
      moreRow.append(
        el("span", "…", "diff-ln"),
        el("span", " ", "diff-sign"),
        el("span", "（剩余 " + (rawLines.length - shown) + " 行已省略）", "diff-code"),
      );
      viewer.append(moreRow);
      break;
    }
    shown += 1;
    let cls = "diff-line";
    let lnStr = "";
    let sign = " ";
    let code = line;
    const hunkMatch = line.match(/^@@\s+-(\d+)(?:,\d+)?\s+\+(\d+)(?:,\d+)?\s+@@/);
    if (hunkMatch) {
      cls += " diff-hunk";
      oldLn = Number(hunkMatch[1]);
      newLn = Number(hunkMatch[2]);
      lnStr = "@@";
      sign = "";
    } else if (
      line.startsWith("--- ") ||
      line.startsWith("+++ ") ||
      line.startsWith("diff --git") ||
      line.startsWith("index ")
    ) {
      cls += " diff-meta";
      sign = "";
    } else if (line.startsWith("+")) {
      cls += " diff-add";
      lnStr = newLn > 0 ? String(newLn) : "+";
      if (newLn > 0) newLn += 1;
      sign = "+";
      code = line.slice(1);
    } else if (line.startsWith("-")) {
      cls += " diff-del";
      lnStr = oldLn > 0 ? String(oldLn) : "-";
      if (oldLn > 0) oldLn += 1;
      sign = "-";
      code = line.slice(1);
    } else {
      lnStr = newLn > 0 ? String(newLn) : oldLn > 0 ? String(oldLn) : "";
      if (oldLn > 0) oldLn += 1;
      if (newLn > 0) newLn += 1;
      if (line.startsWith(" ")) code = line.slice(1);
    }
    const row = el("div", undefined, cls);
    row.append(el("span", lnStr, "diff-ln"), el("span", sign, "diff-sign"), el("span", code, "diff-code"));
    viewer.append(row);
  }
  return viewer;
}

function resetLiveStreamView({ hideCard = true } = {}) {
  if (streamRafId !== null) {
    clearTimeout(streamRafId);
    streamRafId = null;
  }
  streamAnswerText = "";
  currentLiveStepText = "";
  liveStepNodes = [];
  liveToolPills = new Map();
  streamThinkingText = "";
  streamBashText = "";
  streamSubagents = new Map();
  if ($("live-stream-answer")) $("live-stream-answer").replaceChildren();
  if ($("live-tool-strip")) {
    $("live-tool-strip").replaceChildren();
    $("live-tool-strip").classList.add("hidden");
  }
  if ($("live-thinking-pre")) $("live-thinking-pre").textContent = "";
  if ($("live-thinking-details")) $("live-thinking-details").classList.add("hidden");
  if ($("live-bash-pre")) $("live-bash-pre").textContent = "";
  if ($("live-bash-details")) $("live-bash-details").classList.add("hidden");
  if ($("live-subagent-bar")) {
    $("live-subagent-bar").replaceChildren();
    $("live-subagent-bar").classList.add("hidden");
  }
  if (hideCard && $("live-stream-card")) {
    $("live-stream-card").classList.add("hidden");
  }
}

function renderLiveSubagentBadges() {
  const bar = $("live-subagent-bar");
  if (!bar) return;
  bar.replaceChildren();
  if (!streamSubagents.size) {
    bar.classList.add("hidden");
    return;
  }
  bar.classList.remove("hidden");
  for (const [role, st] of streamSubagents.entries()) {
    const stateCls = st === "error" ? "error" : st === "completed" ? "completed" : "running";
    bar.append(el("span", role + " · " + st, "subagent-live-badge " + stateCls));
  }
}

function disconnectRunStream() {
  if (activeEventSource) {
    try {
      activeEventSource.close();
    } catch {}
    activeEventSource = null;
  }
  activeStreamRunId = null;
  if ($("stream-mode-badge")) {
    $("stream-mode-badge").classList.add("hidden");
    $("stream-mode-badge").classList.remove("fallback");
  }
}

function _recordStreamSeq(runId, ev, parsed) {
  const candidates = [
    Number(ev?.lastEventId || 0),
    Number(parsed?.seq || 0),
  ].filter((n) => Number.isFinite(n) && n > 0);
  if (!candidates.length) return 0;
  const maxSeq = Math.max(...candidates);
  const prev = streamAfterSeqByRun.get(runId) || 0;
  if (maxSeq > prev) streamAfterSeqByRun.set(runId, maxSeq);
  return maxSeq;
}

function connectRunStream(runId) {
  if (!runId || typeof EventSource === "undefined") {
    streamFallbackPolling = true;
    return;
  }
  if (activeEventSource && activeStreamRunId === runId && activeEventSource.readyState !== 2) {
    return;
  }
  disconnectRunStream();
  streamFallbackPolling = false;
  activeStreamRunId = runId;
  const afterSeq = streamAfterSeqByRun.get(runId) || 0;
  streamTurnStartSeq = afterSeq;
  if ($("stream-mode-badge")) {
    $("stream-mode-badge").classList.remove("hidden", "fallback");
    $("stream-mode-badge").textContent = "SSE 实时流";
  }
  if ($("live-stream-card")) $("live-stream-card").classList.remove("hidden");
  if ($("live-stream-badge")) {
    $("live-stream-badge").className = "status running";
    $("live-stream-badge").textContent = "思考与流式推送中…";
  }

  const es = new EventSource(
    "/api/runs/" + encodeURIComponent(runId) + "/stream?after_seq=" + encodeURIComponent(String(afterSeq)),
    { withCredentials: true },
  );
  activeEventSource = es;

  es.onopen = () => {
    if (activeEventSource !== es) return;
    if ($("live-stream-badge") && $("live-stream-badge").textContent === "流式连接中…") {
      $("live-stream-badge").textContent = "实时推送中";
    }
  };

  es.addEventListener("thinking_delta", (ev) => {
    if (current !== runId) return;
    try {
      const parsed = JSON.parse(ev.data || "{}");
      _recordStreamSeq(runId, ev, parsed);
      const data =
        parsed && typeof parsed.payload === "object" && parsed.payload !== null
          ? parsed.payload
          : parsed;
      const delta = String(data.delta ?? data.text ?? "");
      if (!delta) return;
      streamThinkingText += delta;
      if ($("live-stream-card")) $("live-stream-card").classList.remove("hidden");
      if ($("live-thinking-details")) $("live-thinking-details").classList.remove("hidden");
      if ($("live-thinking-pre")) {
        const stick = isNearBottom($("live-thinking-pre"));
        $("live-thinking-pre").textContent = streamThinkingText.slice(-8000);
        if (stick) $("live-thinking-pre").scrollTop = $("live-thinking-pre").scrollHeight;
      }
      if ($("live-thinking-summary")) {
        $("live-thinking-summary").textContent =
          "Extended Thinking 推理过程（" + streamThinkingText.length + " 字符）";
      }
    } catch {}
  });

  es.addEventListener("text_delta", (ev) => {
    if (current !== runId) return;
    try {
      const parsed = JSON.parse(ev.data || "{}");
      _recordStreamSeq(runId, ev, parsed);
      const data =
        parsed && typeof parsed.payload === "object" && parsed.payload !== null
          ? parsed.payload
          : parsed;
      const delta = String(data.delta ?? data.text ?? "");
      if (!delta) return;
      streamAnswerText += delta;
      currentLiveStepText += delta;
      if ($("live-stream-card")) $("live-stream-card").classList.remove("hidden");
      if ($("live-stream-badge") && $("live-stream-badge").textContent !== "正在输出回答…") {
        $("live-stream-badge").textContent = "正在输出回答…";
      }
      scheduleStreamMarkdownRender();
    } catch {}
  });

  es.addEventListener("tool_use_start", (ev) => {
    if (current !== runId) return;
    try {
      const parsed = JSON.parse(ev.data || "{}");
      _recordStreamSeq(runId, ev, parsed);
      const data =
        parsed && typeof parsed.payload === "object" && parsed.payload !== null
          ? parsed.payload
          : parsed;
      finalizeCurrentLiveStepNode();
      const tName = String(data.name || "tool");
      const tKey = String(data.call_id || data.id || tName + ":" + liveToolPills.size);
      upsertLiveToolPill(tKey, tName, "running");
      if ($("live-stream-badge")) {
        $("live-stream-badge").textContent = "调用工具：" + tName;
      }
    } catch {}
  });

  es.addEventListener("model_step_done", (ev) => {
    if (current !== runId) return;
    try {
      const parsed = JSON.parse(ev.data || "{}");
      _recordStreamSeq(runId, ev, parsed);
    } catch {}
    finalizeCurrentLiveStepNode();
  });

  es.addEventListener("bash_output_delta", (ev) => {
    if (current !== runId) return;
    try {
      const parsed = JSON.parse(ev.data || "{}");
      _recordStreamSeq(runId, ev, parsed);
      const data =
        parsed && typeof parsed.payload === "object" && parsed.payload !== null
          ? parsed.payload
          : parsed;
      const chunk = String(data.delta ?? data.text ?? data.chunk ?? "");
      if (!chunk) return;
      streamBashText = (streamBashText + chunk).slice(-8000);
      if ($("live-stream-card")) $("live-stream-card").classList.remove("hidden");
      if ($("live-bash-details")) $("live-bash-details").classList.remove("hidden");
      if ($("live-bash-pre")) {
        const stick = isNearBottom($("live-bash-pre"));
        $("live-bash-pre").textContent = streamBashText;
        if (stick) $("live-bash-pre").scrollTop = $("live-bash-pre").scrollHeight;
      }
    } catch {}
  });

  es.addEventListener("subagent_progress", (ev) => {
    if (current !== runId) return;
    try {
      const parsed = JSON.parse(ev.data || "{}");
      _recordStreamSeq(runId, ev, parsed);
      const data =
        parsed && typeof parsed.payload === "object" && parsed.payload !== null
          ? parsed.payload
          : parsed;
      const role = String(data.role || data.job_id || "subagent");
      const st = String(data.status || "running");
      streamSubagents.set(role, st);
      if ($("live-stream-card")) $("live-stream-card").classList.remove("hidden");
      renderLiveSubagentBadges();
    } catch {}
  });

  es.addEventListener("event", (ev) => {
    if (current !== runId) return;
    try {
      const parsed = JSON.parse(ev.data || "{}");
      _recordStreamSeq(runId, ev, parsed);
      const item =
        parsed && typeof parsed.id !== "number" && parsed.payload && typeof parsed.payload.id === "number"
          ? parsed.payload
          : parsed;
      if (item && item.kind === "tool_result" && item.payload?.name) {
        const p = item.payload;
        const tKey = String(p.call_id || p.name);
        const ok = p.result?.ok !== false;
        upsertLiveToolPill(tKey, p.name, ok ? "done" : "error");
      }
      if (item && typeof item.id === "number" && item.id > eventCursor) {
        const stickTl = isNearBottom($("timeline"));
        cachedEvents.push(item);
        eventCursor = item.id;
        renderEvents(cachedEvents);
        if (stickTl && $("timeline")) $("timeline").scrollTop = $("timeline").scrollHeight;
      }
    } catch {}
  });

  es.addEventListener("run_status", (ev) => {
    if (current !== runId) return;
    try {
      const parsed = JSON.parse(ev.data || "{}");
      const seq = _recordStreamSeq(runId, ev, parsed);
      // 忽略来自上一轮缓冲的过期终态帧，杜绝 Turn 2 连接→立即关闭→重连的闪烁死循环
      if (seq > 0 && seq <= streamTurnStartSeq) return;
      const data =
        parsed && typeof parsed.payload === "object" && parsed.payload !== null
          ? parsed.payload
          : parsed;
      const st = String(data.status || "");
      if (["completed", "failed", "cancelled", "waiting_approval", "dead_letter", "budget_exceeded"].includes(st)) {
        disconnectRunStream();
        refreshRun();
      }
    } catch {}
  });

  es.addEventListener("done", () => {
    if (activeEventSource === es) {
      disconnectRunStream();
      refreshRun();
    }
  });

  es.onerror = () => {
    if (activeEventSource !== es) return;
    disconnectRunStream();
    streamFallbackPolling = true;
    if ($("stream-mode-badge")) {
      $("stream-mode-badge").classList.remove("hidden");
      $("stream-mode-badge").classList.add("fallback");
      $("stream-mode-badge").textContent = "轮询回退";
    }
    if (!timer && current === runId) {
      timer = setTimeout(refreshRun, 1200);
    }
  };
}

// 零 innerHTML 的纯 DOM 安全 Markdown 渲染器：支持标题、代码块、表格、引用、列表、粗斜体、行内代码与安全链接，杜绝 XSS 注入。
function appendInlineMarkdown(parent, text) {
  const pattern =
    /(`[^`\n]+`)|(\*\*\*[^*\n]+\*\*\*)|(\*\*[^*\n]+\*\*)|(\[\[[^\]\n]+\]\])|(\[([^\]\n]+)\]\(((?:https?:\/\/|\/)[^\s)]+)\))|(\*[^*\s][^*\n]*\*)/g;
  let lastIndex = 0;
  let match;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      parent.append(document.createTextNode(text.slice(lastIndex, match.index)));
    }
    if (match[1]) {
      parent.append(el("code", match[1].slice(1, -1), "md-inline-code"));
    } else if (match[2]) {
      const strong = el("strong");
      strong.append(el("em", match[2].slice(3, -3)));
      parent.append(strong);
    } else if (match[3]) {
      parent.append(el("strong", match[3].slice(2, -2)));
    } else if (match[4]) {
      parent.append(el("span", match[4], "wiki-chip"));
    } else if (match[5]) {
      const link = el("a", match[6]);
      link.href = match[7];
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      parent.append(link);
    } else if (match[8]) {
      parent.append(el("em", match[8].slice(1, -1)));
    }
    lastIndex = pattern.lastIndex;
  }
  if (lastIndex < text.length) {
    parent.append(document.createTextNode(text.slice(lastIndex)));
  }
}

function _parseTableCells(line) {
  let trimmed = line.trim();
  if (trimmed.startsWith("|")) trimmed = trimmed.slice(1);
  if (trimmed.endsWith("|")) trimmed = trimmed.slice(0, -1);
  return trimmed.split("|").map((c) => c.trim());
}

function _isTableSeparator(line) {
  const t = line.trim();
  if (!t.includes("|")) return false;
  const cells = _parseTableCells(t);
  return cells.length >= 1 && cells.every((c) => /^:?-{3,}:?$/.test(c));
}

function renderSafeMarkdown(container, rawMarkdown) {
  container.replaceChildren();
  const text = String(rawMarkdown || "").replace(/\r\n/g, "\n");
  if (!text.trim()) return;
  const lines = text.split("\n");
  let idx = 0;

  while (idx < lines.length) {
    const line = lines[idx];
    const trimmed = line.trim();

    if (!trimmed) {
      idx += 1;
      continue;
    }

    // 1. 围栏代码块 ```lang
    const fenceMatch = line.match(/^ {0,3}(`{3,}|~{3,})([^\n]*)$/);
    if (fenceMatch) {
      const marker = fenceMatch[1];
      const lang = fenceMatch[2].trim();
      const codeLines = [];
      idx += 1;
      while (idx < lines.length) {
        const closeMatch = lines[idx].match(/^ {0,3}(`{3,}|~{3,})\s*$/);
        if (closeMatch && closeMatch[1][0] === marker[0] && closeMatch[1].length >= marker.length) {
          idx += 1;
          break;
        }
        codeLines.push(lines[idx]);
        idx += 1;
      }
      const codeText = codeLines.join("\n");
      const wrap = el("div", undefined, "md-code-wrap");
      const codeHdr = el("div", undefined, "md-code-header");
      codeHdr.append(el("span", lang || "code", "md-code-lang"), makeCopyButton(codeText, "复制代码"));
      const pre = el("pre", codeText, "md-code");
      wrap.append(codeHdr, pre);
      container.append(wrap);
      continue;
    }

    // 2. 分割线 --- / ***
    if (/^ {0,3}([-*_])(\s*\1){2,}\s*$/.test(line)) {
      container.append(document.createElement("hr"));
      idx += 1;
      continue;
    }

    // 3. ATX 标题 # .. ######
    const headMatch = line.match(/^ {0,3}(#{1,6})\s+(.+?)\s*#*$/);
    if (headMatch) {
      const level = Math.min(4, headMatch[1].length);
      const h = document.createElement("h" + level);
      appendInlineMarkdown(h, headMatch[2]);
      container.append(h);
      idx += 1;
      continue;
    }

    // 4. GFM 表格
    if (line.includes("|") && idx + 1 < lines.length && _isTableSeparator(lines[idx + 1])) {
      const headers = _parseTableCells(line);
      idx += 2;
      const tblWrap = el("div", undefined, "md-table-wrap");
      const table = el("table", undefined, "md-table");
      const thead = document.createElement("thead");
      const trHead = document.createElement("tr");
      for (const hText of headers) {
        const th = document.createElement("th");
        appendInlineMarkdown(th, hText);
        trHead.append(th);
      }
      thead.append(trHead);
      table.append(thead);
      const tbody = document.createElement("tbody");
      while (idx < lines.length && lines[idx].trim().includes("|") && lines[idx].trim() !== "") {
        const rowCells = _parseTableCells(lines[idx]);
        const tr = document.createElement("tr");
        for (let c = 0; c < headers.length; c += 1) {
          const td = document.createElement("td");
          appendInlineMarkdown(td, rowCells[c] || "");
          tr.append(td);
        }
        tbody.append(tr);
        idx += 1;
      }
      table.append(tbody);
      tblWrap.append(table);
      container.append(tblWrap);
      continue;
    }

    // 5. 引用块 > ...
    if (/^ {0,3}>\s?/.test(line)) {
      const quoteLines = [];
      while (idx < lines.length && /^ {0,3}>\s?/.test(lines[idx])) {
        quoteLines.push(lines[idx].replace(/^ {0,3}>\s?/, ""));
        idx += 1;
      }
      const bq = document.createElement("blockquote");
      renderSafeMarkdown(bq, quoteLines.join("\n"));
      container.append(bq);
      continue;
    }

    // 6. 无序 / 有序列表
    const ulMatch = line.match(/^(\s*)[-*+]\s+(.+)$/);
    const olMatch = line.match(/^(\s*)\d+\.\s+(.+)$/);
    if (ulMatch || olMatch) {
      const isOrdered = Boolean(olMatch);
      const listEl = document.createElement(isOrdered ? "ol" : "ul");
      while (idx < lines.length) {
        const m = isOrdered
          ? lines[idx].match(/^(\s*)\d+\.\s+(.+)$/)
          : lines[idx].match(/^(\s*)[-*+]\s+(.+)$/);
        if (!m) break;
        const li = document.createElement("li");
        appendInlineMarkdown(li, m[2]);
        listEl.append(li);
        idx += 1;
      }
      container.append(listEl);
      continue;
    }

    // 7. 普通段落（合并连续普通文本行）
    const paraLines = [];
    while (
      idx < lines.length &&
      lines[idx].trim() !== "" &&
      !/^ {0,3}(`{3,}|~{3,}|#{1,6}\s+|>\s?|[-*+]\s+|\d+\.\s+|([-*_])(\s*\2){2,}\s*$)/.test(
        lines[idx],
      ) &&
      !(lines[idx].includes("|") && idx + 1 < lines.length && _isTableSeparator(lines[idx + 1]))
    ) {
      paraLines.push(lines[idx].trim());
      idx += 1;
    }
    const pEl = document.createElement("p");
    paraLines.forEach((pLine, pIdx) => {
      if (pIdx > 0) pEl.append(document.createElement("br"));
      appendInlineMarkdown(pEl, pLine);
    });
    container.append(pEl);
  }
}

function setSidebarOpen(open) {
  const sb = $("sidebar");
  const bd = $("sidebar-backdrop");
  const toggleBtn = $("sidebar-toggle-btn");
  if (!sb) return;
  sb.classList.toggle("open", Boolean(open));
  if (bd) bd.classList.toggle("hidden", !open);
  if (toggleBtn) toggleBtn.setAttribute("aria-expanded", open ? "true" : "false");
}

function openRenameModal(run) {
  renameTargetRunId = run.id;
  if ($("rename-title-input")) {
    $("rename-title-input").value = run.title || run.prompt || "";
  }
  if ($("rename-modal")) {
    $("rename-modal").classList.remove("hidden");
    $("rename-title-input")?.focus();
  }
}

function closeRenameModal() {
  renameTargetRunId = null;
  if ($("rename-modal")) $("rename-modal").classList.add("hidden");
}

// 列表是持久状态快照；支持按关键词/状态/归档筛选与重命名/归档/删除操作。
async function loadRuns({ force = false } = {}) {
  if (sidebarTimer) {
    clearTimeout(sidebarTimer);
    sidebarTimer = null;
  }
  const qVal = $("run-search-input") ? $("run-search-input").value.trim() : "";
  const stVal = $("run-status-filter") ? $("run-status-filter").value : "";
  const params = new URLSearchParams();
  if (qVal) params.set("q", qVal);
  if (stVal === "archived" || stVal === "all_with_archived") {
    params.set("include_archived", "true");
  } else if (stVal) {
    params.set("status", stVal);
  }
  const qs = params.toString() ? "?" + params.toString() : "";
  const rawRuns = await api("/runs" + qs);
  if (!qVal && !stVal && Array.isArray(rawRuns)) cachedRunsForCmd = rawRuns;
  const runs =
    stVal === "archived"
      ? rawRuns.filter((r) => Boolean(r.archived))
      : rawRuns;
  const sig =
    (current || "") +
    "|" +
    qVal +
    "|" +
    stVal +
    "|" +
    (pendingDeleteRunId || "") +
    "|" +
    runs.map((r) => `${r.id}:${r.status}:${r.archived ? 1 : 0}:${r.title || r.prompt}`).join(",");
  let hasBackgroundActive = false;
  for (const run of runs) {
    if (["running", "queued"].includes(run.status)) {
      hasBackgroundActive = true;
    }
  }
  if (force || sig !== lastRunsSignature) {
    lastRunsSignature = sig;
    $("runs").replaceChildren();
    if (!runs.length) $("runs").append(el("div", "没有匹配的会话记录", "muted"));
    for (const run of runs) {
      const row = el(
        "div",
        undefined,
        "run-row" +
          (current === run.id ? " selected" : "") +
          (run.archived ? " archived" : ""),
      );
      const b = el("button", undefined, "run-item");
      b.type = "button";
      const metaLine = el("small");
      const pillText =
        run.status === "waiting_approval"
          ? "⚠ 待审批"
          : run.status === "running"
            ? "● 运行中"
            : labels[run.status] || run.status;
      metaLine.append(
        el("span", pillText, "run-pill " + run.status),
        document.createTextNode(
          (run.archived ? "已归档 · " : "") +
            (run.direct_workspace ? "本地 · " : "") +
            run.id.slice(0, 6),
        ),
      );
      b.append(el("b", run.title || run.prompt), metaLine);
      b.onclick = () => selectRun(run.id);

      const actBox = el("div", undefined, "run-row-actions");
      const renameBtn = el("button", "✎", "run-act-btn");
      renameBtn.type = "button";
      renameBtn.title = "重命名会话";
      renameBtn.onclick = (ev) => {
        ev.stopPropagation();
        openRenameModal(run);
      };

      const archBtn = el("button", run.archived ? "↥" : "▤", "run-act-btn");
      archBtn.type = "button";
      archBtn.title = run.archived ? "取消归档" : "归档会话";
      archBtn.onclick = async (ev) => {
        ev.stopPropagation();
        try {
          await api("/runs/" + encodeURIComponent(run.id), {
            method: "PATCH",
            body: JSON.stringify({ archived: !run.archived }),
          });
          toast(run.archived ? "已恢复会话" : "已归档会话");
          await loadRuns({ force: true });
        } catch (e) {
          toast(e.message);
        }
      };

      const isConfirmDel = pendingDeleteRunId === run.id;
      const delBtn = el("button", isConfirmDel ? "确认?" : "✕", "run-act-btn" + (isConfirmDel ? " confirm-del" : ""));
      delBtn.type = "button";
      delBtn.title = isConfirmDel ? "再次点击永久删除此会话" : "删除会话";
      delBtn.onclick = async (ev) => {
        ev.stopPropagation();
        if (pendingDeleteRunId !== run.id) {
          pendingDeleteRunId = run.id;
          await loadRuns({ force: true });
          setTimeout(() => {
            if (pendingDeleteRunId === run.id) {
              pendingDeleteRunId = null;
              loadRuns({ force: true }).catch(() => {});
            }
          }, 4000);
          return;
        }
        pendingDeleteRunId = null;
        try {
          await api("/runs/" + encodeURIComponent(run.id), { method: "DELETE" });
          if (current === run.id) {
            disconnectRunStream();
            streamFallbackPolling = false;
            current = null;
            timelineRunId = null;
            renderedEventIds.clear();
            $("timeline").replaceChildren();
            $("empty").classList.remove("hidden");
          }
          toast("会话已删除");
          await loadRuns({ force: true });
        } catch (e) {
          toast(e.message);
        }
      };

      actBox.append(renameBtn, archBtn, delBtn);
      row.append(b, actBox);
      $("runs").append(row);
    }
  }
  if (hasBackgroundActive) {
    sidebarTimer = setTimeout(() => loadRuns().catch(() => {}), 2000);
  }
}
async function selectRun(id) {
  setSidebarOpen(false);
  if (timer) {
    clearTimeout(timer);
    timer = null;
  }
  if (current !== id) {
    disconnectRunStream();
    streamFallbackPolling = false;
    resetLiveStreamView({ hideCard: true });
    revokeArtifactBlobs(id);
    current = id;
    eventRunId = null;
    eventCursor = 0;
    cachedEvents = [];
    timelineRunId = null;
    renderedEventIds.clear();
    lastChatThreadSig = "";
    lastProposalsSig = "";
    lastWmSig = "";
    lastCheckpointsSig = "";
    lastBgProcsSig = "";
    lastRepomapKey = null;
    if ($("repomap-details")) $("repomap-details").classList.add("hidden");
    if ($("repomap-pre")) $("repomap-pre").textContent = "";
    if ($("compaction-details")) {
      $("compaction-details").classList.add("hidden");
      $("compaction-details").open = false;
    }
    if ($("compaction-pre")) $("compaction-pre").textContent = "";
  }
  await refreshRun();
  await Promise.all([loadRuns(), loadMemoryOverview()]);
}
function appendCollapsibleTerminalOutput(container, rawCommand, resObj) {
  const cmdText = String(rawCommand || resObj?.command || "").trim();
  const oneLineCmd = cmdText.replace(/\s+/g, " ").slice(0, 72) + (cmdText.length > 72 ? "…" : "");
  const exitLabel =
    typeof resObj?.exit_code === "number"
      ? " · 退出码 " + resObj.exit_code
      : resObj?.code
        ? " · " + resObj.code
        : "";
  const termDetails = el("details", undefined, "terminal-details");
  const summaryEl = el("summary", "展开终端输出" + exitLabel + " · ");
  summaryEl.append(el("code", "$ " + (oneLineCmd || "bash_exec"), "terminal-summary-cmd"));
  const bodyParts = ["$ " + cmdText];
  if (resObj?.stdout || resObj?.stderr) {
    if (resObj.stdout) bodyParts.push(resObj.stdout);
    if (resObj.stderr) bodyParts.push(resObj.stderr);
  } else if (resObj?.excerpt) {
    bodyParts.push(resObj.excerpt);
  } else {
    bodyParts.push("（命令已执行完毕，无标准输出内容）");
  }
  const outText = bodyParts.filter(Boolean).join("\n").slice(0, 3000);
  const termHdr = el("div", undefined, "terminal-header");
  termHdr.append(el("span", "$ " + (oneLineCmd || "bash_exec"), "md-code-lang"), makeCopyButton(outText, "复制输出"));
  termDetails.append(summaryEl, termHdr, el("pre", outText, "terminal-out"));
  container.append(termDetails);
}

function buildSubagentReportsDom(reports, tasksSpec) {
  const listWrap = el("div", undefined, "subagent-reports-list");
  const specs = Array.isArray(tasksSpec) ? tasksSpec : [];
  reports.forEach((rep, idx) => {
    const spec =
      specs[idx] || specs.find((t) => t && rep && t.role === rep.role) || {};
    const card = el("div", undefined, "subagent-report-item");
    const head = el("div", undefined, "subagent-report-head");
    const roleName = String(rep?.role || spec?.role || "subagent-" + (idx + 1));
    head.append(el("span", roleName, "subagent-role-pill"));
    const metaBadges = [
      rep?.mode || spec?.mode || "readonly",
      typeof rep?.rounds === "number" ? rep.rounds + " 轮" : "",
      rep?.effort_tier ? "effort=" + rep.effort_tier : "",
      rep?.ok === false ? "异常" : "",
    ].filter(Boolean);
    if (metaBadges.length) {
      head.append(el("span", metaBadges.join(" · "), "muted"));
    }
    card.append(head);

    const scopeText = String(rep?.scope_boundaries || spec?.scope_boundaries || "").trim();
    const expectedText = String(rep?.expected_output || spec?.expected_output || "").trim();
    if (scopeText || expectedText) {
      const contractParts = [
        scopeText ? "边界约束：" + scopeText : "",
        expectedText ? "期望产出：" + expectedText : "",
      ].filter(Boolean);
      card.append(el("div", contractParts.join(" ｜ "), "subagent-contract-meta"));
    }

    const bodyDiv = el("div", undefined, "subagent-report-body md-body");
    const rawFindings = String(
      rep?.condensed_report ??
        rep?.summary ??
        rep?.answer ??
        rep?.report ??
        rep?.output ??
        "（无文本结论）",
    );
    renderSafeMarkdown(bodyDiv, rawFindings);
    card.append(bodyDiv);
    listWrap.append(card);
  });
  return listWrap;
}

function appendCollapsibleSubagentPreview(container, argsObj, resObj, callId) {
  const inlineReports = Array.isArray(resObj?.reports)
    ? resObj.reports
    : Array.isArray(resObj?.results)
      ? resObj.results
      : null;
  const tasksSpec = Array.isArray(argsObj?.tasks) ? argsObj.tasks : [];
  const roleNames = (inlineReports || tasksSpec)
    .map((item) => item?.role)
    .filter(Boolean);
  const countNum = Number(
    resObj?.count ?? (inlineReports ? inlineReports.length : tasksSpec.length || 1),
  );
  const detailsEl = el("details", undefined, "subagent-details");
  const summaryLabel =
    "展开子 Agent 调查发现（" +
    countNum +
    " 个角色" +
    (roleNames.length ? "：" + roleNames.join(", ") : "") +
    "）";
  detailsEl.append(el("summary", summaryLabel));

  if (inlineReports && inlineReports.length > 0) {
    detailsEl.append(buildSubagentReportsDom(inlineReports, tasksSpec));
    container.append(detailsEl);
    return;
  }

  const bodySlot = el("div");
  if (resObj?.content_omitted && callId && current) {
    const runIdSnapshot = current;
    bodySlot.append(
      el(
        "pre",
        String(resObj.excerpt || "报告较大已折叠，展开时自动拉取完整内容…"),
        "terminal-out",
      ),
    );
    let loadedFull = false;
    detailsEl.ontoggle = async () => {
      if (!detailsEl.open || loadedFull) return;
      loadedFull = true;
      try {
        const full = await api(
          "/runs/" +
            encodeURIComponent(runIdSnapshot) +
            "/tool-result?call_id=" +
            encodeURIComponent(callId) +
            "&offset=0&limit=8000",
        );
        const parsed = JSON.parse(String(full?.content || ""));
        const fullReports = Array.isArray(parsed?.reports)
          ? parsed.reports
          : Array.isArray(parsed?.results)
            ? parsed.results
            : null;
        if (fullReports && fullReports.length > 0) {
          bodySlot.replaceChildren(buildSubagentReportsDom(fullReports, tasksSpec));
          return;
        }
        if (full?.content) {
          bodySlot.replaceChildren(el("pre", String(full.content), "terminal-out"));
        }
      } catch {
        // 超过单页 8000 字符或非完整 JSON 片段时保留预览摘要
      }
    };
  } else if (resObj?.condensed_handoff || resObj?.summary) {
    const mdSlot = el("div", undefined, "subagent-report-body md-body");
    renderSafeMarkdown(mdSlot, String(resObj.condensed_handoff || resObj.summary));
    bodySlot.append(mdSlot);
  } else {
    bodySlot.append(
      el("pre", JSON.stringify(resObj || {}, null, 2).slice(0, 2400), "terminal-out"),
    );
  }
  detailsEl.append(bodySlot);
  container.append(detailsEl);
}

function appendCollapsibleMcpSearchPreview(container, argsObj, resObj) {
  const matched = Array.isArray(resObj?.tools)
    ? resObj.tools
    : Array.isArray(resObj?.matches)
      ? resObj.matches
      : Array.isArray(resObj?.results)
        ? resObj.results
        : Array.isArray(resObj?.activated_tools)
          ? resObj.activated_tools
          : [];
  const qStr = String(argsObj?.query ?? resObj?.query ?? "").trim();
  const detailsEl = el("details", undefined, "subagent-details mcp-search-details");
  detailsEl.append(
    el(
      "summary",
      "展开 MCP 工具按需搜索结果" +
        (qStr ? "（\"" + qStr.slice(0, 44) + "\"）" : "") +
        " · 命中 " +
        matched.length +
        " 项",
    ),
  );
  const listWrap = el("div", undefined, "subagent-reports-list");
  if (!matched.length) {
    listWrap.append(
      el("div", String(resObj?.message || resObj?.note || "未命中匹配的外部 MCP 工具"), "muted"),
    );
  } else {
    for (const item of matched) {
      const row = el("div", undefined, "mcp-match-item");
      const head = el("div", undefined, "mcp-match-head");
      const toolName =
        typeof item === "string"
          ? item
          : String(item?.name || item?.tool_name || item?.remote_name || "mcp_tool");
      head.append(el("code", toolName, "terminal-summary-cmd"));
      if (item && typeof item === "object") {
        const srv = item.server || item.mcp_server;
        const roLabel =
          item.read_only === true
            ? "readOnly"
            : item.read_only === false
              ? "mutating"
              : "";
        const rightMeta = [srv ? "MCP:" + srv : "", roLabel].filter(Boolean).join(" · ");
        if (rightMeta) head.append(el("span", rightMeta, "muted"));
      }
      row.append(head);
      if (item && typeof item === "object" && item.description) {
        row.append(el("div", String(item.description), "muted"));
      }
      listWrap.append(row);
    }
  }
  detailsEl.append(listWrap);
  container.append(detailsEl);
}

function appendCollapsibleSkillSearchPreview(container, argsObj, resObj) {
  const skills = Array.isArray(resObj?.skills)
    ? resObj.skills
    : Array.isArray(resObj?.matches)
      ? resObj.matches
      : [];
  const qStr = String(argsObj?.query ?? resObj?.query ?? "").trim();
  const detailsEl = el("details", undefined, "subagent-details");
  detailsEl.append(
    el(
      "summary",
      "展开多通道技能路由结果" +
        (qStr ? "（\"" + qStr.slice(0, 40) + "\"）" : "") +
        " · 命中 " +
        skills.length +
        " 项",
    ),
  );
  const listWrap = el("div", undefined, "subagent-reports-list");
  if (!skills.length) {
    listWrap.append(el("div", "未命中相关技能元数据", "muted"));
  } else {
    for (const sk of skills) {
      const row = el("div", undefined, "mcp-match-item");
      const head = el("div", undefined, "mcp-match-head");
      head.append(el("code", String(sk?.name || "skill"), "terminal-summary-cmd"));
      const channel = sk?.channel || sk?.matched_by || sk?.source || "";
      const pathGlobs = Array.isArray(sk?.paths || sk?.globs)
        ? (sk.paths || sk.globs).join(", ")
        : sk?.paths || sk?.globs || "";
      const metaStr = [channel ? "路由:" + channel : "", pathGlobs ? "paths:" + pathGlobs : ""]
        .filter(Boolean)
        .join(" · ");
      if (metaStr) head.append(el("span", metaStr, "muted"));
      row.append(head);
      if (sk?.description) {
        row.append(el("div", String(sk.description), "muted"));
      }
      listWrap.append(row);
    }
  }
  detailsEl.append(listWrap);
  container.append(detailsEl);
}

// 增量事件渲染器：按 data-event-id 仅追加新增节点，切换 runId 时才清空，彻底保留用户已展开的 <details> 状态。
function buildEventNode(event) {
  const p = event.payload || {};
  const box = el("div", undefined, "event");
  box.dataset.eventId = String(event.id);
  if (["user_steering_queued", "user_steering_injected"].includes(event.kind)) {
    box.classList.add("event-steering");
  } else if (event.kind === "context_compacted") {
    box.classList.add("event-compaction");
  } else if (["run_rewound", "checkpoint_created"].includes(event.kind)) {
    box.classList.add("event-checkpoint");
  } else if (
    [
      "subagent_started",
      "subagents_started",
      "subagent_finished",
      "subagents_finished",
    ].includes(event.kind) ||
    (event.kind === "tool_result" && p.name === "subagent_run")
  ) {
    box.classList.add("event-subagent");
  }

  const head = el("div", undefined, "event-head");
  let eventTitle = (eventNames[event.kind] || event.kind) + (p.name ? " · " + p.name : "");
  if (event.kind === "approval") {
    const toolLabel = p.result?.command
      ? " · bash_exec"
      : p.result?.path
        ? " · " + p.result.path
        : "";
    eventTitle = (p.approved ? "已批准并执行" : "已拒绝提案") + toolLabel;
  } else if (event.kind === "user_steering_queued") {
    eventTitle = "⚡ 用户实时补充指令已入队";
  } else if (event.kind === "user_steering_injected") {
    const injCnt = Number(
      p.count ??
        (Array.isArray(p.prompts)
          ? p.prompts.length
          : Array.isArray(p.messages)
            ? p.messages.length
            : 1),
    );
    eventTitle = "⚡ 实时指令已注入当前会话" + (injCnt > 1 ? " (" + injCnt + " 条)" : "");
  } else if (event.kind === "user_followup") {
    const supCnt = Array.isArray(p.superseded_proposal_ids)
      ? p.superseded_proposal_ids.length
      : Number(p.superseded_proposals || 0);
    if (supCnt > 0 || p.takeover) {
      eventTitle = "↳ 用户转向指令已接管（自动撤销待审提案）";
    }
  } else if (event.kind === "context_compacted") {
    const foldedGroups =
      p.newly_compacted_groups ??
      p.compacted_groups ??
      p.dropped_groups ??
      p.compaction_cursor ??
      0;
    eventTitle =
      "🗜 上下文自动/滚动压缩完成：折叠 " + foldedGroups + " 组早期对话 → COMPACTION_SUMMARY";
  } else if (event.kind === "subagent_started" || event.kind === "subagents_started") {
    if (p.role) {
      eventTitle = "✦ 子 Agent 已启动 · " + p.role + (p.mode ? " (" + p.mode + ")" : "");
    } else {
      const startRoles = Array.isArray(p.roles)
        ? p.roles
        : Array.isArray(p.tasks)
          ? p.tasks.map((t) => t?.role).filter(Boolean)
          : [];
      eventTitle =
        "✦ 并行子 Agent 批次已启动 (" +
        (p.count || startRoles.length || 1) +
        " 个)" +
        (startRoles.length ? " · " + startRoles.join(", ") : "");
    }
  } else if (event.kind === "subagents_finished" || event.kind === "subagent_finished") {
    const finRoles = Array.isArray(p.roles) ? p.roles : p.role ? [p.role] : [];
    eventTitle =
      "✦ 动态子 Agent 协同完成 (" +
      (p.count ?? (finRoles.length || 1)) +
      " 个)" +
      (finRoles.length ? " · " + finRoles.join(", ") : "");
  } else if (event.kind === "run_rewound") {
    eventTitle = "↶ 已回退至检查点 " + String(p.checkpoint_id || "").slice(0, 10) + " (" + (p.scope || "both") + ")";
  } else if (event.kind === "a2a_task_delegated") {
    const peerLabel = String(p.peer || p.agent || "");
    const stLabel = p.state ? " (" + p.state + ")" : "";
    eventTitle = "⇄ A2A 远端任务已委托" + (peerLabel ? " · " + peerLabel : "") + stLabel;
  }

  head.append(
    el("b", eventTitle),
    el("time", new Date(event.created_at).toLocaleTimeString("zh-CN", { hour12: false })),
  );
  box.append(head);
  if (p.worker) box.append(el("small", "执行者 / " + p.worker));
  if (p.owner_id) box.append(el("small", "租约持有者 / " + p.owner_id + " (epoch=" + p.epoch + ")"));
  if (p.commit_sha) box.append(el("small", "Git Commit / " + p.commit_sha.slice(0, 12)));
  if (p.result?.code) box.append(el("small", "结果 / " + p.result.code));

  if (event.kind === "user_steering_queued") {
    const qText = String(p.prompt || p.message || p.text || "").trim();
    if (qText) box.append(el("div", qText, "event-callout steering-callout"));
    const pendingInfo =
      typeof p.pending_count === "number" ? " · 当前排队 " + p.pending_count + " 条" : "";
    box.append(
      el(
        "small",
        "零锁争用收件箱 (run_steering_messages) 已接收" + pendingInfo + "，将在当前工具步返回后立即合并",
      ),
    );
  } else if (event.kind === "user_steering_injected") {
    const injectedPrompts = Array.isArray(p.prompts)
      ? p.prompts
      : Array.isArray(p.messages)
        ? p.messages
            .map((m) => (typeof m === "string" ? m : m?.prompt || m?.content || ""))
            .filter(Boolean)
        : p.prompt
          ? [String(p.prompt)]
          : [];
    for (const itemTxt of injectedPrompts) {
      box.append(el("div", String(itemTxt), "event-callout steering-callout"));
    }
    box.append(el("small", "已在工具步安全边界无损合并至当前会话上下文，保持前缀缓存连续"));
  } else if (event.kind === "user_followup") {
    if (p.prompt) box.append(el("div", String(p.prompt), "event-callout"));
    const supList = Array.isArray(p.superseded_proposal_ids) ? p.superseded_proposal_ids : [];
    if (supList.length > 0 || p.superseded_proposals || p.takeover) {
      box.append(el("small", "已自动撤销挂起审批提案并补齐协议闭合回执，直接按新指令继续执行"));
    }
  } else if (event.kind === "context_compacted") {
    const triggerDesc =
      p.trigger === "manual" || p.manual
        ? "手动整理 (/compact)"
        : "80% 高水位自动滞回压缩 → 55% 低水位";
    const compMeta = [
      triggerDesc,
      typeof p.dropped_groups === "number" ? "累计归档 " + p.dropped_groups + " 组" : "",
      typeof p.summary_chars === "number" ? "摘要 " + p.summary_chars + " 字符" : "",
    ].filter(Boolean);
    box.append(el("small", compMeta.join(" · ")));
    const snapSummary = String(p.summary || p.compaction_summary || "").trim();
    if (snapSummary) {
      const sumDetails = el("details", undefined, "subagent-details");
      sumDetails.append(
        el("summary", "展开原位注入的 COMPACTION_SUMMARY 摘要"),
        el("pre", snapSummary, "terminal-out"),
      );
      box.append(sumDetails);
    }
  } else if (event.kind === "subagent_started" || event.kind === "subagents_started") {
    const subMetaParts = [
      p.effort_tier ? "规模档位: " + p.effort_tier : "",
      p.max_rounds ? "轮次上限 " + p.max_rounds : "",
      p.isolation ? "隔离: " + p.isolation : "",
      p.model_tier ? "模型档位: " + p.model_tier : "",
      p.scope_boundaries ? "边界: " + String(p.scope_boundaries).slice(0, 96) : "",
      p.expected_output ? "产出契约: " + String(p.expected_output).slice(0, 96) : "",
    ].filter(Boolean);
    if (subMetaParts.length) box.append(el("small", subMetaParts.join(" · ")));
    if (p.prompt) {
      const promptPreview = String(p.prompt);
      box.append(
        el("div", promptPreview.slice(0, 260) + (promptPreview.length > 260 ? "…" : ""), "event-callout"),
      );
    }
  } else if (
    (event.kind === "subagents_finished" || event.kind === "subagent_finished") &&
    Array.isArray(p.reports) &&
    p.reports.length > 0
  ) {
    appendCollapsibleSubagentPreview(box, p.arguments || {}, p, p.parent_call_id);
  } else if (event.kind === "skill_routed" || event.kind === "skills_routed") {
    const routedNames = Array.isArray(p.skills)
      ? p.skills.map((s) => (typeof s === "string" ? s : s?.name)).filter(Boolean)
      : p.name
        ? [p.name]
        : [];
    if (routedNames.length) {
      box.append(
        el("small", "命中技能：" + routedNames.join(", ") + (p.channel ? " · 通道：" + p.channel : "")),
      );
    }
  } else if (event.kind === "file_written" && p.diff) {
    const diffDetails = el("details", undefined, "diff-details");
    diffDetails.append(
      el("summary", "展开文件修改差异 · " + (p.path || "")),
      renderUnifiedDiff(p.diff, { maxLines: 140 }),
    );
    box.append(diffDetails);
  } else if (event.kind === "egress_dlp_blocked") {
    const dlpMeta = [
      p.tool ? "工具: " + p.tool : "",
      p.code ? "规则: " + p.code : "",
      p.reason ? String(p.reason) : "",
    ].filter(Boolean);
    if (dlpMeta.length) {
      box.append(el("div", dlpMeta.join(" · "), "event-callout"));
    }
  } else if (event.kind === "plan_mode_entered") {
    const planMeta = [
      p.plan_file ? "计划文件: " + p.plan_file : "",
      p.reason ? "原因: " + String(p.reason) : "",
    ].filter(Boolean);
    if (planMeta.length) {
      box.append(el("small", planMeta.join(" · ")));
    }
  }

  if (event.kind === "tool_result" && p.result && !p.result.requires_approval) {
    if (p.name === "bash_exec") {
      appendCollapsibleTerminalOutput(box, p.arguments?.command, p.result);
    } else if ((p.name === "fs_write" || p.name === "fs_edit") && p.result.diff) {
      const diffDetails = el("details", undefined, "diff-details");
      const targetPath = p.result.path || p.arguments?.path || "file";
      diffDetails.append(
        el("summary", "展开自动编辑差异 (" + p.name + ") · " + targetPath),
        renderUnifiedDiff(p.result.diff, { maxLines: 140 }),
      );
      box.append(diffDetails);
    } else if (p.name === "subagent_run") {
      appendCollapsibleSubagentPreview(box, p.arguments, p.result, p.call_id);
    } else if (p.name === "mcp_tool_search") {
      appendCollapsibleMcpSearchPreview(box, p.arguments, p.result);
    } else if (p.name === "skill_search") {
      appendCollapsibleSkillSearchPreview(box, p.arguments, p.result);
    } else if (p.name === "skill_load" && p.result.ok) {
      box.append(
        el(
          "small",
          "已挂载技能快照：" +
            (p.result.name || "") +
            (p.result.section ? " § " + p.result.section : "") +
            (p.result.content_chars ? " (" + p.result.content_chars + " 字符)" : ""),
        ),
      );
    }
  }
  if (event.kind === "approval" && p.approved && p.result?.command) {
    appendCollapsibleTerminalOutput(box, p.result.command, p.result);
  }
  if (event.kind === "gui_screenshot" && p.artifact_name && current) {
    attachArtifactImage(current, p.artifact_name, box);
  }
  const detail = el("details");
  detail.append(el("summary", "查看结构化数据"), el("pre", JSON.stringify(p, null, 2)));
  box.append(detail);
  return box;
}

function renderEvents(events) {
  const tl = $("timeline");
  if (!tl) return;
  const visible = events.filter((e) => {
    if (
      [
        "tool_audit",
        "patch_applied",
        "patch_proposed",
        "action_proposed",
        "waiting_approval",
        "command_executed",
        "context_built",
      ].includes(e.kind)
    ) {
      return false;
    }
    if (e.kind === "tool_result" && e.payload?.result?.requires_approval) return false;
    return true;
  });
  $("empty").classList.toggle("hidden", !!visible.length);
  if (timelineRunId !== current) {
    timelineRunId = current;
    renderedEventIds.clear();
    tl.replaceChildren();
  }
  for (const event of visible) {
    const key = String(event.id);
    if (renderedEventIds.has(key)) continue;
    renderedEventIds.add(key);
    tl.append(buildEventNode(event));
  }
}

// 必须展示彩色行号 diff 再提交审批决定；支持多文件选择性勾选与会话模式授权。
function renderProposals(proposals, status, waitingIds) {
  const waitSet = Array.isArray(waitingIds) && waitingIds.length ? new Set(waitingIds) : null;
  const pending = proposals.filter((p) => {
    if (decidedProposalIds.has(p.id) || p.applied || p.status !== "pending") {
      return false;
    }
    return waitSet ? waitSet.has(p.id) : true;
  });
  const shouldHide = !pending.length || status !== "waiting_approval";
  const sig =
    (current || "") +
    "|" +
    status +
    "|" +
    (shouldHide ? "H" : "S") +
    "|" +
    pending.map((p) => p.id + ":" + (p.diff ? p.diff.length : 0)).join(",");
  $("proposal-card").classList.toggle("hidden", shouldHide);
  if (sig === lastProposalsSig) return;
  lastProposalsSig = sig;
  $("proposals").replaceChildren();
  if (shouldHide) return;
  for (const p of pending) {
    const box = el("div", undefined, "proposal");
    const kind = p.kind || "single_file";
    let titleText = p.path || "待确认提案";
    const fileCheckboxes = [];
    const submitDecision = async ({
      approved,
      nextMode = null,
      grantPattern = null,
      answerText = null,
    }) => {
      let selectedFiles = null;
      if (approved && fileCheckboxes.length > 0) {
        selectedFiles = fileCheckboxes.filter((c) => c.checked).map((c) => c.value);
        if (!selectedFiles.length) {
          toast("请至少勾选 1 个文件，或点击拒绝");
          return;
        }
        if (selectedFiles.length === fileCheckboxes.length) {
          selectedFiles = null;
        }
      }
      const cleanAnswer =
        typeof answerText === "string" && answerText.trim() ? answerText.trim() : null;
      decidedProposalIds.add(p.id);
      box.remove();
      if (!$("proposals").children.length) $("proposal-card").classList.add("hidden");
      $("status").className = "status running";
      $("status").textContent = approved ? "已批准 · 运行中" : "已拒绝 · 继续中";
      try {
        await api("/runs/" + encodeURIComponent(current) + "/proposals/" + encodeURIComponent(p.id), {
          method: "POST",
          body: JSON.stringify({
            approved,
            ...(nextMode ? { permission_mode: nextMode } : {}),
            ...(grantPattern ? { grant_pattern: grantPattern } : {}),
            ...(selectedFiles ? { selected_files: selectedFiles } : {}),
            ...(cleanAnswer !== null ? { answer_text: cleanAnswer } : {}),
          }),
        });
        streamFallbackPolling = false;
        await refreshUntilRunning();
      } catch (e) {
        decidedProposalIds.delete(p.id);
        toast(e.message);
        await refreshRun();
      }
    };

    if (kind === "user_question") {
      const qArgs = p.arguments || {};
      const questionText = String(p.question || qArgs.question || p.summary || "请确认以下问题").trim();
      const questionContext = String(p.context || qArgs.context || "").trim();
      const rawOptions = Array.isArray(p.options)
        ? p.options
        : Array.isArray(qArgs.options)
          ? qArgs.options
          : [];
      const defaultOpt = String(p.default_option || qArgs.default_option || "").trim();
      const multiSelect = Boolean(
        p.multi_select !== undefined ? p.multi_select : qArgs.multi_select,
      );
      const optionsList = rawOptions
        .map((opt) => {
          if (typeof opt === "string") return opt.trim();
          if (opt && typeof opt === "object") {
            const label = String(opt.label || opt.value || opt.text || "").trim();
            const desc = String(opt.description || "").trim();
            return label ? (desc ? label + " — " + desc : label) : "";
          }
          return "";
        })
        .filter(Boolean);

      box.append(el("strong", "❓ 智能体澄清提问"));
      const qBox = el("div", undefined, "event-callout md-body");
      renderSafeMarkdown(qBox, questionText);
      box.append(qBox);
      if (questionContext) {
        box.append(el("div", questionContext, "muted"));
      }

      const selectedMulti = new Set();
      if (optionsList.length > 0) {
        const optWrap = el("div", undefined, "approval-actions");
        optWrap.style.justifyContent = "flex-start";
        optionsList.forEach((optText, idx) => {
          const isRec = Boolean(defaultOpt && optText === defaultOpt);
          const optBtn = el(
            "button",
            idx + 1 + ". " + optText + (isRec ? "（推荐）" : ""),
            "button secondary",
          );
          optBtn.type = "button";
          optBtn.onclick = () => {
            if (multiSelect) {
              if (selectedMulti.has(optText)) {
                selectedMulti.delete(optText);
                optBtn.className = "button secondary";
              } else {
                selectedMulti.add(optText);
                optBtn.className = "button primary-button";
              }
              return;
            }
            submitDecision({ approved: true, answerText: optText });
          };
          optWrap.append(optBtn);
        });
        box.append(optWrap);
      }

      const replyRow = el("div", undefined, "gateway-row");
      replyRow.style.marginTop = "10px";
      const answerInput = document.createElement("input");
      answerInput.type = "text";
      answerInput.maxLength = 4000;
      answerInput.setAttribute("aria-label", "回复智能体提问");
      answerInput.placeholder = optionsList.length
        ? multiSelect
          ? "可点选多个选项，或输入补充回复后点击提交…"
          : "点击上方选项直接回复，或在此输入自定义答案…"
        : "请输入你的回复后按 Enter 或点击提交…";

      const submitAnswer = () => {
        const typed = answerInput.value.trim();
        const picked = Array.from(selectedMulti);
        const combined = [...picked, ...(typed ? [typed] : [])].join("；").trim();
        if (!combined) {
          toast("请选择一个选项或输入回复内容");
          answerInput.focus();
          return;
        }
        submitDecision({ approved: true, answerText: combined });
      };

      answerInput.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
          ev.preventDefault();
          submitAnswer();
        }
      });
      replyRow.append(answerInput);
      box.append(replyRow);

      const qActions = el("div", undefined, "approval-actions");
      const skipBtn = el("button", "取消 / 跳过", "button danger");
      skipBtn.type = "button";
      skipBtn.onclick = () => submitDecision({ approved: false });
      const sendAnswerBtn = el("button", "提交回复 ↗", "button primary-button");
      sendAnswerBtn.type = "button";
      sendAnswerBtn.onclick = submitAnswer;
      qActions.append(skipBtn, sendAnswerBtn);
      box.append(qActions);
      $("proposals").append(box);
      continue;
    }

    if (kind === "plan_approval") {
      const pArgs = p.arguments || {};
      const planSummary = String(p.plan_summary || p.summary || pArgs.summary || "").trim();
      const planFile = String(p.plan_file || pArgs.plan_file || p.path || "").trim();
      const planContent = String(p.plan_content || p.new_content || pArgs.plan_content || "").trim();
      const targetPerm = String(p.target_permission_mode || pArgs.target_permission_mode || "").trim();

      box.append(el("strong", "📋 实施计划确认" + (planFile ? " · " + planFile : "")));
      if (targetPerm) {
        box.append(el("div", "批准后目标权限模式：" + targetPerm, "grant-hint-pill"));
      }
      if (planSummary) {
        const planBox = el("div", undefined, "event-callout md-body");
        renderSafeMarkdown(planBox, planSummary);
        box.append(planBox);
      }
      if (planContent && planContent !== planSummary) {
        const planDetails = el("details", undefined, "subagent-details");
        planDetails.open = !planSummary;
        planDetails.append(
          el("summary", "查看完整实施计划文档" + (planFile ? " (" + planFile + ")" : "")),
        );
        const contentDiv = el("div", undefined, "md-body");
        renderSafeMarkdown(contentDiv, planContent);
        planDetails.append(contentDiv);
        box.append(planDetails);
      }
      if (!planSummary && !planContent) {
        box.append(
          el("div", "智能体已完成规划，请求批准退出只读规划模式并开始执行。", "muted"),
        );
      }

      const planActions = el("div", undefined, "approval-actions");
      const rejectPlanBtn = el("button", "拒绝计划", "button danger");
      rejectPlanBtn.type = "button";
      rejectPlanBtn.onclick = () => submitDecision({ approved: false });
      const approvePlanBtn = el("button", "批准计划并开始执行", "button primary-button");
      approvePlanBtn.type = "button";
      approvePlanBtn.onclick = () => submitDecision({ approved: true });
      planActions.append(rejectPlanBtn, approvePlanBtn);
      box.append(planActions);
      $("proposals").append(box);
      continue;
    }

    box.append(
      el(
        "strong",
        kind === "command"
          ? "终端命令确认 · cwd=" + ((p.arguments || {}).cwd || ".")
          : kind === "gui_action"
            ? "桌面 GUI 动作确认 · " + (p.tool || "computer")
            : kind === "mcp_call"
              ? "外部 MCP 工具确认 · " + (p.tool || "mcp")
              : kind === "a2a_call"
                ? "A2A 远端智能体委托确认 · " +
                  ((p.arguments || {}).peer || p.peer || p.tool || "a2a")
                : p.paths && p.paths.length > 1
                  ? "原子变更集 (" + p.paths.length + " 个文件): " + p.paths.join(", ")
                  : titleText,
      ),
    );
    if (p.summary) box.append(el("div", p.summary, "muted"));
    if (p.risk_level) box.append(el("div", "风险分级: " + p.risk_level, "muted"));
    if (p.base_commit) box.append(el("div", "绑定基线 commit: " + p.base_commit.slice(0, 12), "muted"));
    if (p.suggested_grant_pattern) {
      box.append(el("div", "建议会话模式放行：" + p.suggested_grant_pattern, "grant-hint-pill"));
    }

    if (kind === "command") {
      box.append(el("pre", "$ " + ((p.arguments || {}).command || p.new_content || ""), "terminal-out"));
    } else if (kind === "gui_action" || kind === "mcp_call" || kind === "a2a_call") {
      if (kind === "a2a_call") {
        const a2aArgs = p.arguments || {};
        const a2aMeta = [
          p.tool ? "工具: " + p.tool : "",
          a2aArgs.peer ? "目标对端: " + a2aArgs.peer : "",
          a2aArgs.task_id ? "Task ID: " + a2aArgs.task_id : "",
          typeof a2aArgs.blocking === "boolean"
            ? a2aArgs.blocking
              ? "模式: 同步等待"
              : "模式: 异步提交"
            : "",
        ].filter(Boolean);
        if (a2aMeta.length) {
          box.append(el("div", a2aMeta.join(" · "), "muted"));
        }
      }
      box.append(el("pre", JSON.stringify(p.arguments || {}, null, 2), "terminal-out"));
    } else {
      // 多文件变更集勾选器
      const multiFiles = Array.isArray(p.files) && p.files.length > 1 ? p.files : null;
      if (multiFiles) {
        const picker = el("div", undefined, "changeset-file-picker");
        const pickerHead = el("div", undefined, "changeset-picker-head");
        pickerHead.append(el("span", "选择要批准应用的文件（" + multiFiles.length + " 个）："));
        const toggleAllBtn = el("button", "全选 / 反选", "changeset-toggle-btn");
        toggleAllBtn.type = "button";
        pickerHead.append(toggleAllBtn);
        picker.append(pickerHead);
        const listEl = el("div", undefined, "changeset-file-list");
        for (const fItem of multiFiles) {
          const fPath = typeof fItem === "string" ? fItem : fItem.path;
          if (!fPath) continue;
          const optLabel = el("label", undefined, "changeset-file-opt");
          const cb = document.createElement("input");
          cb.type = "checkbox";
          cb.checked = true;
          cb.value = fPath;
          fileCheckboxes.push(cb);
          optLabel.append(cb, el("span", fPath));
          if (typeof fItem === "object" && fItem.action) {
            optLabel.append(el("span", fItem.action, "changeset-action-tag"));
          }
          listEl.append(optLabel);
        }
        toggleAllBtn.onclick = () => {
          const allChecked = fileCheckboxes.every((c) => c.checked);
          for (const c of fileCheckboxes) c.checked = !allChecked;
        };
        picker.append(listEl);
        box.append(picker);
      }

      const diffBody =
        p.diff ||
        ["--- 原始文件", p.old_content || "", "+++ 提议文件", p.new_content || ""].join("\n");
      box.append(renderUnifiedDiff(diffBody, { maxLines: 220 }));
    }

    const actions = el("div", undefined, "approval-actions");

    const rejectBtn = el("button", "拒绝", "button danger");
    rejectBtn.type = "button";
    rejectBtn.onclick = () => submitDecision({ approved: false });
    actions.append(rejectBtn);

    if (p.suggested_grant_pattern) {
      const grantBtn = el("button", "本会话允许此模式", "button secondary");
      grantBtn.type = "button";
      grantBtn.title = "批准并向本会话添加模式放行：" + p.suggested_grant_pattern;
      grantBtn.onclick = () =>
        submitDecision({ approved: true, grantPattern: p.suggested_grant_pattern });
      actions.append(grantBtn);
    }

    if (["command", "gui_action", "mcp_call", "a2a_call"].includes(kind)) {
      const fullBtn = el("button", "切为 Full-Auto 并批准", "button secondary");
      fullBtn.type = "button";
      fullBtn.onclick = () => submitDecision({ approved: true, nextMode: "full-auto" });
      actions.append(fullBtn);
    }

    const approveBtn = el("button", "批准并继续 ↗", "button primary-button");
    approveBtn.type = "button";
    approveBtn.onclick = () => submitDecision({ approved: true });
    actions.append(approveBtn);

    box.append(actions);
    $("proposals").append(box);
  }
}
async function refreshUntilRunning() {
  await new Promise((r) => setTimeout(r, 80));
  await refreshRun();
  if (!timer && current) {
    timer = setTimeout(refreshRun, 500);
  }
}
function renderContextStats(run) {
  const meterEl = $("context-meter");
  const fillEl = $("context-meter-fill");
  const lowMarkEl = $("context-low-marker");
  const highMarkEl = $("context-high-marker");
  const wmBadgeEl = $("context-watermark-badge");
  const lowTickEl = $("context-low-tick");
  const highTickEl = $("context-high-tick");
  const statEl = $("context-stat-text");
  const compactBtn = $("compact-btn");
  const compDetails = $("compaction-details");
  const compPre = $("compaction-pre");
  if (!fillEl || !statEl) return;

  if (!run) {
    fillEl.style.width = "0%";
    fillEl.classList.remove("warning");
    if (meterEl) meterEl.setAttribute("aria-valuenow", "0");
    if (lowMarkEl) lowMarkEl.style.left = "55%";
    if (highMarkEl) highMarkEl.style.left = "80%";
    if (wmBadgeEl) wmBadgeEl.textContent = "自动压缩水位 80% → 55%";
    if (lowTickEl) lowTickEl.textContent = "↙ 55% 回落";
    if (highTickEl) highTickEl.textContent = "⚡ 80% 自动阈值";
    statEl.textContent =
      "达到自动压缩阈值 80% 时自动折叠早期原子组回落至 55% 并原位注入 COMPACTION_SUMMARY；选择任务后显示实时使用率与 KV 命中率。";
    if (compactBtn) compactBtn.classList.add("hidden");
    if (compDetails) {
      compDetails.classList.add("hidden");
      compDetails.open = false;
    }
    if (compPre) compPre.textContent = "";
    return;
  }

  const ctx = run.context || {};
  const highRatioRaw = Number(
    ctx.high_watermark_ratio ?? ctx.auto_compact_ratio ?? ctx.compact_threshold_ratio ?? 0.8,
  );
  const lowRatioRaw = Number(ctx.low_watermark_ratio ?? 0.55);
  const highPct = Math.round(
    (Number.isFinite(highRatioRaw) && highRatioRaw > 0 && highRatioRaw <= 1
      ? highRatioRaw
      : 0.8) * 100,
  );
  const lowPct = Math.round(
    (Number.isFinite(lowRatioRaw) && lowRatioRaw > 0 && lowRatioRaw <= 1
      ? lowRatioRaw
      : 0.55) * 100,
  );

  if (lowMarkEl) lowMarkEl.style.left = lowPct + "%";
  if (highMarkEl) highMarkEl.style.left = highPct + "%";
  if (wmBadgeEl) {
    wmBadgeEl.textContent = "自动压缩水位 " + highPct + "% → " + lowPct + "%";
  }
  if (lowTickEl) lowTickEl.textContent = "↙ " + lowPct + "% 回落";
  if (highTickEl) highTickEl.textContent = "⚡ " + highPct + "% 自动阈值";

  const usedChars = Number(ctx.used_chars ?? ctx.kept_chars ?? ctx.request_chars ?? 0);
  const budgetChars = Number(
    ctx.effective_budget ?? ctx.effective_message_budget ?? ctx.max_chars ?? 0,
  );
  const estTokens = Number(ctx.estimated_tokens ?? ctx.request_tokens ?? 0);
  const budgetTokens = Number(ctx.effective_token_budget ?? ctx.max_tokens ?? 0);
  const droppedGroups = Number(ctx.dropped_groups ?? ctx.compaction_cursor ?? 0);

  const charPct = budgetChars > 0 ? (usedChars / budgetChars) * 100 : 0;
  const tokPct = budgetTokens > 0 ? (estTokens / budgetTokens) * 100 : 0;
  const explicitPct =
    typeof ctx.utilization_ratio === "number" ? ctx.utilization_ratio * 100 : 0;
  const rawPct = Math.max(charPct, tokPct, explicitPct);
  const clampedPct = Math.max(0, Math.min(100, Math.round(rawPct)));
  const displayPct = budgetChars > 0 || budgetTokens > 0 ? Math.round(rawPct) : 0;

  fillEl.style.width = clampedPct + "%";
  fillEl.classList.toggle("warning", rawPct >= Math.max(65, highPct - 8));
  if (meterEl) meterEl.setAttribute("aria-valuenow", String(clampedPct));

  const kvText = run.kv_cache?.steps_recorded
    ? Math.round((run.kv_cache.cumulative_hit_rate || 0) * 100) + "%"
    : "—";
  const fmtTok = (n) =>
    n >= 1_000_000
      ? (n / 1_000_000).toFixed(n % 1_000_000 === 0 ? 0 : 2) + "M"
      : n >= 10_000
        ? (n / 1_000).toFixed(1) + "k"
        : n.toLocaleString();
  const winTokens = Number(run.metadata?.context_window_tokens ?? ctx.context_window_tokens ?? 0);
  const winTag = winTokens >= 100_000 ? " (" + fmtTok(winTokens) + " 窗口)" : "";
  const budgetDesc =
    budgetTokens >= 32_000
      ? fmtTok(estTokens) + " / " + fmtTok(budgetTokens) + " tok" + winTag
      : (budgetChars > 0
          ? usedChars.toLocaleString() + "/" + budgetChars.toLocaleString() + " 字符"
          : "待构建") +
        " · 约 " +
        estTokens.toLocaleString() +
        " tok";
  statEl.textContent =
    "使用率 " +
    displayPct +
    "% · " +
    budgetDesc +
    " · 自动压缩阈值 " +
    highPct +
    "% (回落 " +
    lowPct +
    "%) · 已压缩 " +
    droppedGroups +
    " 组 · KV 命中 " +
    kvText;

  const leaseActive =
    run.lease && ["queued", "claimed"].includes(run.lease.queue_state);
  const isActivelyRunning =
    ["queued", "running"].includes(run.status) ||
    Boolean(run.job_active) ||
    (Boolean(leaseActive) && run.status !== "waiting_approval");
  if (compactBtn) {
    compactBtn.classList.toggle(
      "hidden",
      isActivelyRunning || run.lease?.queue_state === "dead_letter",
    );
    if (!compactBusy) compactBtn.disabled = false;
  }

  const summaryText =
    typeof run.compaction_summary === "string"
      ? run.compaction_summary.trim()
      : typeof ctx.compaction_summary === "string"
        ? ctx.compaction_summary.trim()
        : "";
  if (compDetails && compPre) {
    if (summaryText) {
      compDetails.classList.remove("hidden");
      compPre.textContent = summaryText;
    } else {
      compDetails.classList.add("hidden");
      compPre.textContent = "";
    }
  }
}

function renderFollowupCard(run, isActivelyRunning) {
  const card = $("followup-card");
  if (!card) return;
  if (!run || run.lease?.queue_state === "dead_letter") {
    card.classList.add("hidden");
    card.classList.remove("steering-active", "takeover-active");
    if ($("followup-live-badge")) {
      $("followup-live-badge").classList.add("hidden");
      $("followup-live-badge").textContent = "";
    }
    return;
  }
  card.classList.remove("hidden");

  const headingEl = $("followup-heading");
  const badgeEl = $("followup-live-badge");
  const promptEl = $("followup-prompt");
  const hintEl = $("followup-hint");
  const btnEl = $("send-followup");
  const permEl = $("run-perm-mode");

  if (permEl && run.permission_mode && document.activeElement !== permEl) {
    permEl.value = run.permission_mode;
  }

  const pendingSteering = Array.isArray(run.pending_steering) ? run.pending_steering : [];
  if (badgeEl) {
    if (pendingSteering.length > 0) {
      badgeEl.classList.remove("hidden");
      badgeEl.textContent =
        "已有 " + pendingSteering.length + " 条补充指令排队等待当前工具步结束注入…";
    } else {
      badgeEl.classList.add("hidden");
      badgeEl.textContent = "";
    }
  }

  let headingText = "↳ 继续多轮对话（共享当前工作区与上下文）";
  let btnText = "发送后续指令 ↗";
  let hintText = "沿用当前会话历史、结构化工作记忆与 KV Cache 前缀";
  let placeholderText =
    "继续输入下一条指令（例如：运行测试并把报错修掉 / 截取当前屏幕查看效果）…";

  card.classList.toggle("steering-active", Boolean(isActivelyRunning));
  card.classList.toggle(
    "takeover-active",
    !isActivelyRunning && run.status === "waiting_approval",
  );

  if (isActivelyRunning) {
    headingText = "⚡ 运行中实时引导（无需中断，当前工具步完成后立即注入新指令）";
    btnText = "实时注入指令 ↗";
    hintText =
      "写入零 CAS 争用收件箱 (run_steering_messages)，当前工具步返回后立即合并至会话";
    placeholderText =
      "输入实时补充要求或方向修正（无需中断当前运行，将在当前工具步完成后立即注入）…";
  } else if (run.status === "waiting_approval") {
    headingText = "↳ 发送新指令替代当前待审动作（自动撤销挂起提案并按新方向继续）";
    btnText = "发送转向指令 ↗";
    hintText = "无需先手动点拒绝：发送后自动撤销挂起审批提案、闭合调用并按新方向继续";
    placeholderText =
      "输入新方向指令替代上方待审动作（将自动撤销挂起提案并按新要求继续执行）…";
  }

  if (headingEl && headingEl.textContent !== headingText) {
    headingEl.textContent = headingText;
  }
  if (btnEl && btnEl.textContent !== btnText) {
    btnEl.textContent = btnText;
  }
  if (hintEl && hintEl.textContent !== hintText) {
    hintEl.textContent = hintText;
  }
  // 严格保护用户正在键入的文本：只更新 placeholder，绝不改写 promptEl.value
  if (promptEl && promptEl.placeholder !== placeholderText) {
    promptEl.placeholder = placeholderText;
  }
}

function renderWorkingMemory(wm) {
  const box = $("wm-box");
  if (!box) return;
  const sig = wm ? JSON.stringify(wm) : "__empty__";
  if (sig === lastWmSig) return;
  lastWmSig = sig;
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
function renderCostMetric(run) {
  const costEl = $("cost-usd-label");
  if (!costEl) return;
  costEl.classList.remove("cost-warn", "cost-over");
  if (!run) {
    costEl.textContent = "—";
    costEl.title = "";
    return;
  }
  const cost = Number(run.cost_usd || 0);
  const maxCost = run.max_cost_usd ? Number(run.max_cost_usd) : null;
  const formatted = "$" + (cost >= 0.01 ? cost.toFixed(4) : cost.toFixed(6));
  costEl.textContent = maxCost ? formatted + " / $" + maxCost.toFixed(2) : formatted;
  if (maxCost && cost >= maxCost) {
    costEl.classList.add("cost-over");
  } else if (maxCost && cost >= maxCost * 0.75) {
    costEl.classList.add("cost-warn");
  }
  const cb = run.cost_breakdown || {};
  costEl.title =
    "模型: " +
    (run.model || cb.model || run.provider || "default") +
    " | in=$" +
    Number(cb.input_cost_usd || 0).toFixed(6) +
    " cache_w=$" +
    Number(cb.cache_write_cost_usd || 0).toFixed(6) +
    " cache_r=$" +
    Number(cb.cache_read_cost_usd || 0).toFixed(6) +
    " out=$" +
    Number(cb.output_cost_usd || 0).toFixed(6);
}

function renderCheckpointsAndExtras(run, isActivelyRunning) {
  const cpBar = $("checkpoint-fork-bar");
  const cpCnt = $("checkpoint-count");
  const cpSel = $("checkpoint-select");
  const rwBtn = $("rewind-btn");
  const grantsBar = $("session-grants-bar");
  const runAttBar = $("run-attachments-bar");

  if (!run) {
    lastCheckpointsSig = "";
    if (cpBar) cpBar.classList.add("hidden");
    if (grantsBar) {
      grantsBar.replaceChildren();
      grantsBar.classList.add("hidden");
    }
    if (runAttBar) {
      runAttBar.replaceChildren();
      runAttBar.classList.add("hidden");
    }
    return;
  }

  const cps = Array.isArray(run.checkpoints) ? run.checkpoints : [];
  const grants = Array.isArray(run.session_grants) ? run.session_grants : [];
  const atts = Array.isArray(run.user_attachments) ? run.user_attachments : [];
  const sig =
    run.id +
    "|" +
    (isActivelyRunning ? "1" : "0") +
    "|" +
    cps.map((c) => c.checkpoint_id + ":" + (c.file_count ?? 0)).join(",") +
    "|" +
    grants.join(",") +
    "|" +
    atts.map((a) => a.artifact_name || a.filename || "").join(",");
  if (sig === lastCheckpointsSig) return;
  lastCheckpointsSig = sig;

  if (cpBar) cpBar.classList.remove("hidden");
  if (cpCnt) cpCnt.textContent = String(cps.length);
  if (cpSel) {
    const prevVal = cpSel.value;
    cpSel.replaceChildren();
    if (!cps.length) {
      cpSel.append(el("option", "暂无检查点", ""));
      cpSel.value = "";
    } else {
      for (const cp of cps) {
        const opt = document.createElement("option");
        opt.value = cp.checkpoint_id;
        const fCount = cp.file_count ?? Object.keys(cp.file_manifest || {}).length;
        opt.textContent =
          "Turn " +
          (cp.turn_index ?? 0) +
          " · " +
          String(cp.checkpoint_id || "").slice(0, 8) +
          " (" +
          fCount +
          " 文件)";
        cpSel.append(opt);
      }
      if (cps.some((c) => c.checkpoint_id === prevVal)) {
        cpSel.value = prevVal;
      } else {
        cpSel.value = cps[cps.length - 1].checkpoint_id;
      }
    }
  }
  if (rwBtn) rwBtn.disabled = isActivelyRunning || !cps.length;

  if (grantsBar) {
    const grants = Array.isArray(run.session_grants) ? run.session_grants : [];
    grantsBar.replaceChildren();
    if (grants.length) {
      grantsBar.classList.remove("hidden");
      grantsBar.append(el("span", "本会话放行规则：", "muted"));
      for (const g of grants) {
        grantsBar.append(el("span", String(g), "grant-chip"));
      }
    } else {
      grantsBar.classList.add("hidden");
    }
  }

  if (runAttBar) {
    const atts = Array.isArray(run.user_attachments) ? run.user_attachments : [];
    runAttBar.replaceChildren();
    if (atts.length) {
      runAttBar.classList.remove("hidden");
      runAttBar.append(el("span", "已附加文件：", "muted"));
      for (const a of atts) {
        const chip = el("span", undefined, "att-chip");
        const label = a.filename || a.artifact_name || "attachment";
        if (a.artifact_name && current) {
          const link = el("a", "📎 " + label, "att-chip-link");
          link.href =
            "/api/runs/" +
            encodeURIComponent(current) +
            "/artifacts/" +
            encodeURIComponent(a.artifact_name);
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          chip.append(link);
        } else {
          chip.append(el("span", "📎 " + label, "att-chip-name"));
        }
        runAttBar.append(chip);
      }
    } else {
      runAttBar.classList.add("hidden");
    }
  }
}

function renderPendingAttachmentChips(list, containerId) {
  const box = $(containerId);
  if (!box) return;
  box.replaceChildren();
  list.forEach((item, idx) => {
    const chip = el("span", undefined, "att-chip");
    if (item.previewUrl) {
      const thumb = document.createElement("img");
      thumb.src = item.previewUrl;
      thumb.alt = item.filename;
      thumb.className = "att-chip-thumb";
      chip.append(thumb);
    }
    chip.append(
      el("span", item.filename, "att-chip-name"),
      el("span", Math.max(1, Math.round(item.size / 1024)) + " KB", "att-chip-size"),
    );
    const rm = el("button", "✕", "att-chip-remove");
    rm.type = "button";
    rm.title = "移除附件";
    rm.onclick = () => {
      list.splice(idx, 1);
      renderPendingAttachmentChips(list, containerId);
    };
    chip.append(rm);
    box.append(chip);
  });
}

function readFilesAsAttachments(fileList, targetList, containerId) {
  const MAX_BYTES = 6 * 1024 * 1024;
  for (const file of Array.from(fileList || [])) {
    if (targetList.length >= 8) {
      toast("单次最多附加 8 个文件");
      break;
    }
    if (file.size > MAX_BYTES) {
      toast("附件超过 6 MiB 上限：" + file.name);
      continue;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result || "");
      const commaIdx = dataUrl.indexOf(",");
      const b64 = commaIdx >= 0 ? dataUrl.slice(commaIdx + 1) : dataUrl;
      const isImg = (file.type || "").startsWith("image/");
      targetList.push({
        filename: file.name,
        media_type: file.type || "application/octet-stream",
        content_base64: b64,
        size: file.size,
        previewUrl: isImg ? dataUrl : null,
      });
      renderPendingAttachmentChips(targetList, containerId);
    };
    reader.readAsDataURL(file);
  }
}

function stripAttachmentPayloads(list) {
  return list.map(({ filename, media_type, content_base64 }) => ({
    filename,
    media_type,
    content_base64,
  }));
}

// 轮询只观察状态；活跃任务优先通过 EventSource SSE 实时推送增量帧。
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
      run.lease?.queue_state === "dead_letter"
        ? "dead_letter"
        : run.job_active && run.status === "waiting_approval"
          ? "running"
          : run.status;
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
    renderCostMetric(run);
    const leaseActive =
      run.lease && ["queued", "claimed"].includes(run.lease.queue_state);
    const hasPendingApproval =
      run.status === "waiting_approval" &&
      Array.isArray(run.proposals) &&
      run.proposals.some(
        (p) => p.status === "pending" && !p.applied && !decidedProposalIds.has(p.id),
      );
    const isActivelyRunning =
      ["queued", "running"].includes(run.status) ||
      Boolean(run.job_active) ||
      (Boolean(leaseActive) && !hasPendingApproval);
    renderCheckpointsAndExtras(run, isActivelyRunning);
    if (typeof run.stream_latest_seq === "number" && run.stream_latest_seq > 0) {
      const prevSeq = streamAfterSeqByRun.get(id) || 0;
      if (!isActivelyRunning && run.stream_latest_seq > prevSeq) {
        // 回合静止时把游标推到服务端最新序号，保证下一轮发消息不会重放上一轮的 completed 帧
        streamAfterSeqByRun.set(id, run.stream_latest_seq);
      }
    }
    if (isActivelyRunning) {
      if (!streamFallbackPolling) {
        connectRunStream(id);
      }
    } else {
      disconnectRunStream();
      resetLiveStreamView({ hideCard: true });
    }
    setSessionMode(run);
    const isErrState = ["failed", "budget_exceeded", "cancelled", "interrupted"].includes(
      displayStatus,
    );
    const answerMarkdown =
      isErrState && run.error
        ? "**运行停止：" + run.error + "**" + (run.answer ? "\n\n" + run.answer : "")
        : run.answer || (run.error ? "**运行停止：" + run.error + "**" : "");
    const hasRichThread = renderChatThread(run, cachedEvents, isActivelyRunning);
    const hideAnswerCard = hasRichThread || isActivelyRunning || (!run.answer && !run.error);
    $("answer-card").classList.toggle("hidden", hideAnswerCard);
    if (!hideAnswerCard) {
      renderSafeMarkdown($("answer"), answerMarkdown);
    }
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
    renderFollowupCard(run, isActivelyRunning);
    renderContextStats(run);
    renderWorkingMemory(run.working_memory);
    if (appended) {
      const stickTl = isNearBottom($("timeline"));
      renderEvents(cachedEvents);
      if (stickTl && $("timeline")) $("timeline").scrollTop = $("timeline").scrollHeight;
    }
    const enrichedProposals = run.proposals.map((p) => ({
      ...p,
      diff:
        p.diff ||
        cachedEvents.find(
          (e) => e.kind === "patch_proposed" && e.payload?.proposal_id === p.id,
        )?.payload?.diff,
    }));
    renderProposals(enrichedProposals, run.status, run.waiting_proposal_ids);
    syncStickyApprovalBanner(run, enrichedProposals);
    updateTabTitleAndNotify(run);
    if (resetEvents || !$("tab-panel-run")?.classList.contains("hidden")) {
      loadBackgroundProcesses(id).catch(() => {});
    }
    if (resetEvents) loadWorkspaceFilesForRun(id).catch(() => {});
    if (!refreshQueued) {
      if (
        ["queued", "running"].includes(run.status) ||
        (leaseActive && !hasPendingApproval) ||
        run.job_active ||
        run.dream_pending ||
        (isActivelyRunning &&
          Array.isArray(run.pending_steering) &&
          run.pending_steering.length > 0 &&
          !hasPendingApproval)
      ) {
        if (timer) clearTimeout(timer);
        const pollMs = activeEventSource && !streamFallbackPolling ? 4500 : 1200;
        timer = setTimeout(refreshRun, pollMs);
      } else {
        await Promise.all([loadRuns(), loadMemoryOverview()]);
      }
    }
  } catch (e) {
    toast(e.message, "error");
  }
}
function syncTemplateControls() {
  if (!$("template")) return;
  const tpl = $("template").value;
  const isLocal = tpl === "local_cwd";
  if ($("local-cwd-row")) $("local-cwd-row").classList.toggle("hidden", !isLocal);
  if ($("perm-wrap")) $("perm-wrap").classList.toggle("hidden", !isLocal);
  if ($("gui-wrap")) $("gui-wrap").classList.toggle("hidden", !isLocal);
  if ($("wt-wrap")) $("wt-wrap").classList.toggle("hidden", !isLocal);
  if ($("template-badge")) {
    $("template-badge").textContent = isLocal
      ? "本地目录直连 / 支持多 Agent 并行操控"
      : "独立示例副本 / Git Worktree";
  }
  updateComposerAdvancedSummary();
}
function enterNewSessionMode() {
  setSidebarOpen(false);
  disconnectRunStream();
  streamFallbackPolling = false;
  resetLiveStreamView({ hideCard: true });
  revokeArtifactBlobs(null);
  if (timer) {
    clearTimeout(timer);
    timer = null;
  }
  current = null;
  eventRunId = null;
  eventCursor = 0;
  cachedEvents = [];
  timelineRunId = null;
  renderedEventIds.clear();
  lastChatThreadSig = "";
  lastProposalsSig = "";
  lastWmSig = "";
  lastCheckpointsSig = "";
  lastBgProcsSig = "";
  setSessionMode(null);
  if ($("chat-thread")) $("chat-thread").replaceChildren();
  $("timeline").replaceChildren();
  $("timeline").classList.remove("hidden");
  if ($("toggle-raw-timeline-btn")) $("toggle-raw-timeline-btn").classList.add("hidden");
  $("empty").classList.remove("hidden");
  $("proposal-card").classList.add("hidden");
  if ($("sticky-approval-bar")) $("sticky-approval-bar").classList.add("hidden");
  $("answer-card").classList.add("hidden");
  renderFollowupCard(null, false);
  renderCostMetric(null);
  renderCheckpointsAndExtras(null, false);
  $("status").className = "status idle";
  $("status").textContent = "等待新建任务";
  $("steps").textContent = "—";
  $("verification-label").textContent = "—";
  $("git-label").textContent = "—";
  $("lease-tokens").textContent = "—";
  renderContextStats(null);
  if ($("wm-box")) {
    $("wm-box").textContent = "选择任务后显示带来源哈希校验的工作记忆。";
  }
  if ($("bg-processes-box")) {
    $("bg-processes-box").textContent = "选择任务后查看活跃的后台 PTY / Shell 进程及异步子 Agent 任务。";
  }
  if ($("repomap-details")) $("repomap-details").classList.add("hidden");
  if ($("repomap-pre")) $("repomap-pre").textContent = "";
  document.title = "Agent Systems Lab · 代码开发工作台";
  loadRuns().catch(() => {});
  loadMemoryOverview().catch(() => {});
  $("prompt")?.focus();
  toast("已打开新并行会话面板，原后台 Agent 会继续运行", "info");
}
if ($("new-agent-btn")) {
  $("new-agent-btn").onclick = enterNewSessionMode;
}
if ($("header-new-agent-btn")) {
  $("header-new-agent-btn").onclick = enterNewSessionMode;
}
if ($("sidebar-toggle-btn")) {
  $("sidebar-toggle-btn").onclick = () => {
    const isOpen = $("sidebar")?.classList.contains("open");
    setSidebarOpen(!isOpen);
  };
}
if ($("sidebar-close-btn")) {
  $("sidebar-close-btn").onclick = () => setSidebarOpen(false);
}
if ($("sidebar-backdrop")) {
  $("sidebar-backdrop").onclick = () => setSidebarOpen(false);
}
if ($("run-search-input")) {
  $("run-search-input").oninput = () => {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(() => loadRuns({ force: true }).catch(() => {}), 220);
  };
}
if ($("run-status-filter")) {
  $("run-status-filter").onchange = () => loadRuns({ force: true }).catch(() => {});
}
if ($("attach-btn") && $("attachment-input")) {
  $("attach-btn").onclick = () => $("attachment-input").click();
  $("attachment-input").onchange = (ev) => {
    readFilesAsAttachments(ev.target.files, composerAttachments, "composer-attachments");
    ev.target.value = "";
  };
}
if ($("followup-attach-btn") && $("followup-attachment-input")) {
  $("followup-attach-btn").onclick = () => $("followup-attachment-input").click();
  $("followup-attachment-input").onchange = (ev) => {
    readFilesAsAttachments(ev.target.files, followupAttachments, "followup-attachments");
    ev.target.value = "";
  };
}
if ($("rewind-btn")) {
  $("rewind-btn").onclick = () => {
    if (!current) return;
    const cpId = $("checkpoint-select") ? $("checkpoint-select").value : "";
    if (!cpId) return;
    const scope = $("rewind-scope") ? $("rewind-scope").value : "both";
    const cpLabel = $("checkpoint-select")?.selectedOptions?.[0]?.textContent || cpId.slice(0, 8);
    const scopeDesc =
      scope === "both"
        ? "同时恢复工作区文件并截断对话历史（该检查点后新建的文件将被删除）"
        : scope === "files"
          ? "仅恢复工作区文件到该检查点前的快照"
          : "仅回退对话消息历史，不改动磁盘文件";
    openConfirmModal({
      title: "确认回退至检查点",
      body: "目标检查点：" + cpLabel + "\n回退范围：" + scopeDesc + "\n此操作不可撤销，确定继续？",
      okLabel: "确认 Rewind",
      onConfirm: async () => {
        $("rewind-btn").disabled = true;
        try {
          const res = await api("/runs/" + encodeURIComponent(current) + "/rewind", {
            method: "POST",
            body: JSON.stringify({ checkpoint_id: cpId, scope }),
          });
          toast(
            "已回退至检查点 " +
              cpId.slice(0, 8) +
              "（恢复 " +
              (res.restored_files?.length || 0) +
              " 文件，清理 " +
              (res.deleted_files?.length || 0) +
              " 新增文件）",
            "success",
          );
          eventRunId = null;
          eventCursor = 0;
          cachedEvents = [];
          timelineRunId = null;
          renderedEventIds.clear();
          await refreshRun();
        } catch (e) {
          toast(e.message, "error");
        } finally {
          if ($("rewind-btn")) $("rewind-btn").disabled = false;
        }
      },
    });
  };
}
if ($("fork-run-btn")) {
  $("fork-run-btn").onclick = async () => {
    if (!current) return;
    $("fork-run-btn").disabled = true;
    try {
      const forked = await api("/runs/" + encodeURIComponent(current) + "/fork", {
        method: "POST",
        body: JSON.stringify({}),
      });
      toast("已分叉创建新会话：" + forked.id.slice(0, 8));
      await selectRun(forked.id);
    } catch (e) {
      toast(e.message);
    } finally {
      if ($("fork-run-btn")) $("fork-run-btn").disabled = false;
    }
  };
}
if ($("rename-close-btn")) $("rename-close-btn").onclick = closeRenameModal;
if ($("rename-cancel-btn")) $("rename-cancel-btn").onclick = closeRenameModal;
if ($("rename-save-btn")) {
  $("rename-save-btn").onclick = async () => {
    if (!renameTargetRunId) return;
    const nextTitle = $("rename-title-input") ? $("rename-title-input").value.trim() : "";
    if (!nextTitle) return;
    $("rename-save-btn").disabled = true;
    try {
      await api("/runs/" + encodeURIComponent(renameTargetRunId), {
        method: "PATCH",
        body: JSON.stringify({ title: nextTitle }),
      });
      toast("会话标题已更新");
      closeRenameModal();
      await loadRuns({ force: true });
    } catch (e) {
      toast(e.message);
    } finally {
      if ($("rename-save-btn")) $("rename-save-btn").disabled = false;
    }
  };
}
if ($("compact-btn")) {
  $("compact-btn").onclick = async () => {
    if (!current || compactBusy) return;
    compactBusy = true;
    $("compact-btn").disabled = true;
    try {
      const res = await api("/runs/" + encodeURIComponent(current) + "/compact", {
        method: "POST",
        body: JSON.stringify({}),
      });
      const compactedGroups =
        res.compacted_groups ??
        res.newly_compacted_groups ??
        res.context?.dropped_groups ??
        res.dropped_groups;
      if (res.compacted === false || res.skipped) {
        toast(res.reason || res.message || "当前对话较短，暂无可压缩的早期历史组");
      } else if (typeof compactedGroups === "number") {
        toast("历史上下文已压缩（累计压缩 " + compactedGroups + " 组）");
      } else {
        toast("历史上下文已压缩至 COMPACTION_SUMMARY");
      }
      await refreshRun();
    } catch (e) {
      toast(e.message);
    } finally {
      compactBusy = false;
      if ($("compact-btn")) $("compact-btn").disabled = false;
    }
  };
}
if ($("template")) {
  $("template").onchange = () => {
    const next = $("template").value;
    const cur = $("prompt").value.trim();
    if (Object.values(DEFAULT_PROMPTS).includes(cur)) {
      $("prompt").value = DEFAULT_PROMPTS[next] || DEFAULT_PROMPTS.sample;
    }
    syncTemplateControls();
    if (!current) loadMemoryOverview().catch(() => {});
  };
  syncTemplateControls();
}
if ($("workspace-path")) {
  $("workspace-path").onchange = () => {
    if (!current) loadMemoryOverview().catch(() => {});
  };
}
async function prepareAttachmentsForSubmit(list) {
  const out = [];
  for (const item of list) {
    // 超过 ~120 KiB 或始终通过 8 MiB 分阶段 /api/uploads 上传，彻底消除 POST /api/runs 与 /messages 的 256 KiB HTTP 413 错误
    if ((item.content_base64 || "").length > 140000 || item.size > 120000) {
      const uploaded = await api("/uploads", {
        method: "POST",
        body: JSON.stringify({
          filename: item.filename,
          media_type: item.media_type,
          content_base64: item.content_base64,
        }),
      });
      out.push({
        upload_id: uploaded.upload_id,
        filename: uploaded.filename || item.filename,
        media_type: uploaded.media_type || item.media_type,
      });
    } else {
      out.push({
        filename: item.filename,
        media_type: item.media_type,
        content_base64: item.content_base64,
      });
    }
  }
  return out;
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
    const chosenModel = $("model-select") ? $("model-select").value.trim() : "";
    if (chosenModel) payload.model = chosenModel;
    const maxCostRaw = $("max-cost-usd") ? $("max-cost-usd").value.trim() : "";
    if (maxCostRaw) {
      const parsedCost = Number(maxCostRaw);
      if (Number.isFinite(parsedCost) && parsedCost > 0) {
        payload.max_cost_usd = parsedCost;
      }
    }
    if (composerAttachments.length > 0) {
      payload.attachments = await prepareAttachmentsForSubmit(composerAttachments);
    }
    if (tpl === "local_cwd") {
      const wsVal = $("workspace-path") ? $("workspace-path").value.trim() : "";
      if (wsVal) payload.workspace_path = wsVal;
      if ($("permission-mode")) payload.permission_mode = $("permission-mode").value;
      if ($("enable-gui")) payload.enable_gui = !!$("enable-gui").checked;
      if ($("isolate-worktree")) payload.isolate_worktree = !!$("isolate-worktree").checked;
    }
    const run = await api("/runs", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    composerAttachments.length = 0;
    renderPendingAttachmentChips(composerAttachments, "composer-attachments");
    streamFallbackPolling = false;
    await selectRun(run.id);
  } catch (e) {
    toast(e.message, "error");
  } finally {
    busy = false;
    $("start").disabled = false;
  }
};
if ($("prompt")) {
  $("prompt").addEventListener("keydown", (ev) => {
    if (handleAutocompleteKeydown(ev, $("prompt"))) return;
    if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter" && !ev.isComposing) {
      ev.preventDefault();
      $("start")?.click();
    }
  });
}
if ($("send-followup")) {
  $("send-followup").onclick = async () => {
    if (!current || followupBusy) return;
    const text = $("followup-prompt").value.trim();
    if (!text) return;
    // Web / CLI 斜杠命令对等：本地拦截 /compact、/rewind、/fork 避免白发给模型烧 Token
    if (text === "/compact" || text.startsWith("/compact ")) {
      const focus = text.slice(8).trim();
      $("followup-prompt").value = "";
      try {
        await api("/runs/" + encodeURIComponent(current) + "/compact", {
          method: "POST",
          body: JSON.stringify({ ...(focus ? { focus } : {}) }),
        });
        toast("上下文压缩已完成" + (focus ? "（聚焦：" + focus + "）" : ""), "success");
        await refreshRun();
      } catch (e) {
        toast(e.message, "error");
      }
      return;
    }
    if (text === "/fork" || text.startsWith("/fork ")) {
      const title = text.slice(5).trim();
      $("followup-prompt").value = "";
      try {
        const forked = await api("/runs/" + encodeURIComponent(current) + "/fork", {
          method: "POST",
          body: JSON.stringify(title ? { title } : {}),
        });
        toast("已分叉创建新会话：" + forked.id.slice(0, 8), "success");
        await selectRun(forked.id);
      } catch (e) {
        toast(e.message, "error");
      }
      return;
    }
    if (text === "/rewind") {
      $("followup-prompt").value = "";
      $("rewind-btn")?.click();
      return;
    }
    if (text === "/plan" || text.startsWith("/plan ")) {
      const planArg = text.slice(5).trim().toLowerCase();
      const curMode = $("run-perm-mode") ? $("run-perm-mode").value : "auto-edit";
      let nextMode = curMode === "plan" ? "auto-edit" : "plan";
      if (["on", "enter", "plan"].includes(planArg)) nextMode = "plan";
      else if (["off", "exit"].includes(planArg)) nextMode = "auto-edit";
      else if (["ask", "auto-edit", "full-auto"].includes(planArg)) nextMode = planArg;
      $("followup-prompt").value = "";
      try {
        await api("/runs/" + encodeURIComponent(current) + "/permissions", {
          method: "PATCH",
          body: JSON.stringify({ permission_mode: nextMode }),
        });
        toast(
          nextMode === "plan"
            ? "已进入只读规划模式 (Plan)"
            : "已退出规划模式，权限切换为 " + nextMode,
          "success",
        );
        await refreshRun();
      } catch (e) {
        toast(e.message, "error");
      }
      return;
    }
    followupBusy = true;
    $("send-followup").disabled = true;
    try {
      const msgPayload = { prompt: text };
      if (followupAttachments.length > 0) {
        msgPayload.attachments = await prepareAttachmentsForSubmit(followupAttachments);
      }
      const res = await api("/runs/" + encodeURIComponent(current) + "/messages", {
        method: "POST",
        body: JSON.stringify(msgPayload),
      });
      $("followup-prompt").value = "";
      followupAttachments.length = 0;
      renderPendingAttachmentChips(followupAttachments, "followup-attachments");
      if (res && res.queued_steering === true) {
        toast("实时指令已入队，将在当前工具步完成后立即注入", "info");
        await refreshRun();
      } else {
        streamFallbackPolling = false;
        decidedProposalIds.clear();
        await refreshUntilRunning();
      }
    } catch (e) {
      toast(e.message, "error");
    } finally {
      followupBusy = false;
      if ($("send-followup")) $("send-followup").disabled = false;
    }
  };
}
if ($("followup-prompt")) {
  $("followup-prompt").addEventListener("keydown", (ev) => {
    if (handleAutocompleteKeydown(ev, $("followup-prompt"))) return;
    if ((ev.metaKey || ev.ctrlKey) && ev.key === "Enter" && !ev.isComposing) {
      ev.preventDefault();
      $("send-followup")?.click();
    }
  });
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
    streamFallbackPolling = false;
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
$("refresh").onclick = () => loadRuns({ force: true }).catch((e) => toast(e.message));

async function openAuthModal() {
  if (!$("auth-modal")) return;
  $("auth-modal").classList.remove("hidden");
  if ($("auth-token-input")) {
    $("auth-token-input").value = "";
    $("auth-token-input").type = "password";
    if ($("auth-toggle-vis")) $("auth-toggle-vis").textContent = "显示";
  }
  try {
    const sess = await api("/auth/session");
    if ($("auth-status-text")) {
      $("auth-status-text").textContent = sess.authenticated
        ? "当前已认证 · 租户=" + (sess.tenant_id || "local") + " · 角色=" + (sess.roles || []).join(",")
        : sess.auth_enabled
          ? "当前未登录（服务端已启用身份校验）"
          : "本地开放模式（服务端未强制令牌校验）";
    }
  } catch (e) {
    if ($("auth-status-text")) $("auth-status-text").textContent = "认证状态：" + e.message;
  }
  $("auth-token-input")?.focus();
}
function closeAuthModal() {
  if ($("auth-modal")) $("auth-modal").classList.add("hidden");
}
$("auth").onclick = () => openAuthModal();
if ($("auth-close-btn")) $("auth-close-btn").onclick = closeAuthModal;
if ($("auth-cancel-btn")) $("auth-cancel-btn").onclick = closeAuthModal;
if ($("auth-toggle-vis") && $("auth-token-input")) {
  $("auth-toggle-vis").onclick = () => {
    const isPwd = $("auth-token-input").type === "password";
    $("auth-token-input").type = isPwd ? "text" : "password";
    $("auth-toggle-vis").textContent = isPwd ? "隐藏" : "显示";
  };
}
if ($("auth-login-btn")) {
  $("auth-login-btn").onclick = async () => {
    const rawTok = $("auth-token-input") ? $("auth-token-input").value.trim() : "";
    if (!rawTok) {
      toast("请输入 API Token");
      return;
    }
    $("auth-login-btn").disabled = true;
    try {
      await api("/auth/session", {
        method: "POST",
        body: JSON.stringify({ token: rawTok }),
      });
      token = "";
      if ($("auth-token-input")) $("auth-token-input").value = "";
      toast("已通过 HttpOnly Cookie 建立安全会话");
      closeAuthModal();
      await init();
    } catch (e) {
      toast(e.message);
    } finally {
      if ($("auth-login-btn")) $("auth-login-btn").disabled = false;
    }
  };
}
if ($("auth-logout-btn")) {
  $("auth-logout-btn").onclick = async () => {
    disconnectRunStream();
    resetLiveStreamView({ hideCard: true });
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
    if (sidebarTimer) {
      clearTimeout(sidebarTimer);
      sidebarTimer = null;
    }
    revokeArtifactBlobs(null);
    current = null;
    eventRunId = null;
    eventCursor = 0;
    cachedEvents = [];
    timelineRunId = null;
    lastRepomapKey = null;
    lastRunsSignature = "";
    latestMemoryOverview = null;
    latestConfigSnapshot = null;
    latestGatewaysData = null;
    gatewayEditorDirty = false;
    latestA2AData = null;
    a2aEditorDirty = false;
    streamFallbackPolling = false;
    renderedEventIds.clear();
    decidedProposalIds.clear();
    if ($("auth-token-input")) $("auth-token-input").value = "";
    if ($("gateway-json-editor")) $("gateway-json-editor").value = "";
    if ($("a2a-json-editor")) $("a2a-json-editor").value = "";
    if ($("timeline")) $("timeline").replaceChildren();
    if ($("empty")) $("empty").classList.remove("hidden");
    if ($("answer")) $("answer").replaceChildren();
    if ($("answer-card")) $("answer-card").classList.add("hidden");
    if ($("proposals")) $("proposals").replaceChildren();
    if ($("proposal-card")) $("proposal-card").classList.add("hidden");
    if ($("wm-box")) {
      $("wm-box").replaceChildren();
      $("wm-box").textContent = "选择任务后显示带来源哈希校验的工作记忆。";
    }
    if ($("memory-index-pre")) $("memory-index-pre").textContent = "";
    if ($("wiki-pages-list")) $("wiki-pages-list").replaceChildren();
    if ($("wiki-page-body")) $("wiki-page-body").replaceChildren();
    if ($("wiki-page-viewer")) $("wiki-page-viewer").classList.add("hidden");
    if ($("core-doc-content")) $("core-doc-content").value = "";
    if ($("memory-list")) $("memory-list").textContent = "";
    if ($("runs")) $("runs").replaceChildren();
    if ($("repomap-pre")) $("repomap-pre").textContent = "";
    if ($("repomap-details")) $("repomap-details").classList.add("hidden");
    renderFollowupCard(null, false);
    renderCostMetric(null);
    renderContextStats(null);
    renderCheckpointsAndExtras(null, false);
    try {
      await api("/auth/session", { method: "DELETE" });
      token = "";
      toast("已注销认证会话");
      closeAuthModal();
      await init();
    } catch (e) {
      token = "";
      toast(e.message);
    }
  };
}
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
function memoryScopePayload() {
  if (current) return { run_id: current };
  if ($("template")?.value === "local_cwd" && $("workspace-path")?.value.trim()) {
    return { workspace_path: $("workspace-path").value.trim() };
  }
  return {};
}
function parseCoreDocTarget() {
  const raw = $("core-doc-target") ? $("core-doc-target").value : "global:CLAUDE.md";
  const sep = raw.indexOf(":");
  if (sep < 0) return { layer: "global", path: "CLAUDE.md" };
  return { layer: raw.slice(0, sep), path: raw.slice(sep + 1) };
}
function getCoreDocContentFromOverview(ov, layer, path) {
  if (!ov) return "";
  const bucket = layer === "project" ? ov.project : ov.global;
  if (!bucket) return "";
  if (path === "CLAUDE.md") return String(bucket.claude_markdown ?? "");
  if (path === "MEMORY.md") return String(bucket.index_markdown ?? "");
  return "";
}
function syncCoreDocEditor({ forceContent = false } = {}) {
  const { layer, path } = parseCoreDocTarget();
  const isProject = layer === "project";
  if ($("sync-ws-doc-wrap")) {
    $("sync-ws-doc-wrap").classList.toggle("hidden", !isProject);
  }
  if ($("sync-ws-doc")) {
    $("sync-ws-doc").disabled = !isProject;
    if (!isProject) $("sync-ws-doc").checked = false;
  }
  const editorEl = $("core-doc-content");
  if (!editorEl || !latestMemoryOverview) return;
  if (document.activeElement === editorEl) return;
  if (!forceContent && coreDocDirty) return;
  editorEl.value = getCoreDocContentFromOverview(latestMemoryOverview, layer, path);
  coreDocDirty = false;
}
function formatWorkspaceFilesSections(wsFiles) {
  const out = [];
  if (Array.isArray(wsFiles)) {
    for (const item of wsFiles) {
      if (typeof item === "string" && item.trim()) {
        out.push("=== workspace/" + item.trim() + " ===");
      } else if (item && typeof item === "object" && item.exists !== false) {
        const label = item.path || item.name || "file";
        const body =
          typeof item.content === "string"
            ? item.content
            : typeof item.markdown === "string"
              ? item.markdown
              : "";
        if (body.trim()) {
          out.push("=== workspace/" + label + " ===\n" + body);
        } else if (item.exists === true || item.chars > 0) {
          out.push(
            "=== workspace/" + label + (item.chars ? " (" + item.chars + " chars)" : "") + " ===",
          );
        }
      }
    }
  } else if (wsFiles && typeof wsFiles === "object") {
    for (const [k, v] of Object.entries(wsFiles)) {
      if (typeof v === "string" && v.trim()) {
        out.push("=== workspace/" + k + " ===\n" + v);
      } else if (v && typeof v === "object" && v.exists !== false) {
        const label = v.path || k;
        const body =
          typeof v.content === "string"
            ? v.content
            : typeof v.markdown === "string"
              ? v.markdown
              : "";
        if (body.trim()) {
          out.push("=== workspace/" + label + " ===\n" + body);
        } else if (v.exists === true || v.chars > 0) {
          out.push(
            "=== workspace/" + label + (v.chars ? " (" + v.chars + " chars)" : "") + " ===",
          );
        }
      }
    }
  }
  return out;
}
async function loadMemoryOverview() {
  if (!$("memory-budget-stat")) return;
  try {
    const scopeParam = memoryScopeQueryParams();
    const qs = scopeParam ? "?" + scopeParam : "";
    const ov = await api("/memory/overview" + qs);
    latestMemoryOverview = ov;
    if (scopeParam !== lastCoreDocScope) {
      lastCoreDocScope = scopeParam;
      if (document.activeElement !== $("core-doc-content")) {
        coreDocDirty = false;
      }
    }
    syncCoreDocEditor();
    const gs = ov.graph?.stats || {};
    const hasClaudeMd = Boolean(
      ov.global?.claude_markdown?.trim() ||
        ov.project?.claude_markdown?.trim() ||
        (Array.isArray(ov.workspace_files) &&
          ov.workspace_files.some((f) => f.name === "CLAUDE.md" || f.name === "AGENTS.md")),
    );
    $("memory-budget-stat").textContent =
      "MEMORY.md " +
      (ov.index_budget?.used_lines || 0) +
      "/" +
      (ov.index_budget?.max_lines || 200) +
      " 行 · Wiki 页 " +
      (gs.pages || 0) +
      " · 链接边 " +
      (gs.edges || 0) +
      (gs.broken_links ? " · 断链 " + gs.broken_links : "") +
      (hasClaudeMd ? " · CLAUDE.md 已启用" : "");
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
      const projSlug = ov.project_slug || "project";
      const wsFiles = ov.workspace_files ?? ov.project?.workspace_files;
      const wsMarkdown = ov.project?.workspace_markdown?.trim();
      const combinedIdx = [
        ov.global?.claude_markdown?.trim()
          ? "=== global/CLAUDE.md ===\n" + ov.global.claude_markdown
          : "",
        ov.global?.index_markdown?.trim()
          ? "=== global/MEMORY.md ===\n" + ov.global.index_markdown
          : "",
        ov.project?.claude_markdown?.trim()
          ? "=== " + projSlug + "/CLAUDE.md ===\n" + ov.project.claude_markdown
          : "",
        ov.project?.index_markdown?.trim()
          ? "=== " + projSlug + "/MEMORY.md ===\n" + ov.project.index_markdown
          : "",
        ...(wsMarkdown
          ? ["=== workspace 自动发现 ===\n" + wsMarkdown]
          : formatWorkspaceFilesSections(wsFiles)),
      ]
        .filter(Boolean)
        .join("\n\n");
      $("memory-index-pre").textContent =
        combinedIdx ||
        "（尚无 CLAUDE.md / MEMORY.md 顶层索引，可在上方配置器编写或运行 Dreaming 后自动生成）";
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
              renderSafeMarkdown($("wiki-page-body"), page.content);
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
if ($("core-doc-target")) {
  $("core-doc-target").onchange = () => {
    coreDocDirty = false;
    const { layer, path } = parseCoreDocTarget();
    const isProject = layer === "project";
    if ($("sync-ws-doc-wrap")) {
      $("sync-ws-doc-wrap").classList.toggle("hidden", !isProject);
    }
    if ($("sync-ws-doc")) {
      $("sync-ws-doc").disabled = !isProject;
      if (!isProject) $("sync-ws-doc").checked = false;
    }
    if ($("core-doc-content") && latestMemoryOverview) {
      $("core-doc-content").value = getCoreDocContentFromOverview(
        latestMemoryOverview,
        layer,
        path,
      );
    }
  };
  syncCoreDocEditor();
}
if ($("core-doc-content")) {
  $("core-doc-content").oninput = () => {
    coreDocDirty = true;
  };
}
if ($("save-core-doc")) {
  $("save-core-doc").onclick = async () => {
    const { layer, path } = parseCoreDocTarget();
    const isProject = layer === "project";
    const content = $("core-doc-content") ? $("core-doc-content").value : "";
    const syncWs = Boolean(isProject && $("sync-ws-doc")?.checked);
    $("save-core-doc").disabled = true;
    try {
      await api("/memory/page", {
        method: "PUT",
        body: JSON.stringify({
          layer,
          path,
          content,
          sync_workspace_file: syncWs,
          ...memoryScopePayload(),
        }),
      });
      coreDocDirty = false;
      toast("已保存 " + layer + "/" + path);
      await loadMemoryOverview();
    } catch (e) {
      toast(e.message);
    } finally {
      $("save-core-doc").disabled = false;
    }
  };
}
if ($("preview-dream")) {
  $("preview-dream").onclick = async () => {
    $("preview-dream").disabled = true;
    try {
      const goalVal = $("dream-goal") ? $("dream-goal").value.trim() : "";
      const rep = await api("/memory/dream", {
        method: "POST",
        body: JSON.stringify({
          ...memoryScopePayload(),
          ...(goalVal ? { focus_goal: goalVal } : {}),
          dry_run: true,
        }),
      });
      if ($("dream-preview-box") && $("dream-preview-diffs") && $("dream-preview-summary")) {
        $("dream-preview-box").classList.remove("hidden");
        const diffs = Array.isArray(rep.planned_diffs) ? rep.planned_diffs : [];
        latestDreamPreviewDiffs = diffs;
        $("dream-preview-summary").textContent =
          "预演 Diff（零落盘）：纠错 " +
          (rep.corrections_count || 0) +
          " · 更新 " +
          (rep.pages_written_count || 0) +
          " 页 · 修断链 " +
          (rep.links_repaired_count || 0);
        $("dream-preview-diffs").replaceChildren();
        if (!diffs.length) {
          $("dream-preview-diffs").append(el("div", "当前无待变更记忆页面，无需落盘。", "muted"));
        } else {
          for (const d of diffs) {
            const item = el("div", undefined, "dream-diff-item");
            item.append(
              el(
                "div",
                "[" + (d.action || "update") + "] " + (d.layer || "project") + ":" + (d.path || ""),
                "dream-diff-item-title",
              ),
            );
            if (d.diff) {
              item.append(renderUnifiedDiff(d.diff, { maxLines: 80 }));
            }
            $("dream-preview-diffs").append(item);
          }
        }
      }
      toast("Dreaming 预演完成（未改动任何磁盘文件）");
    } catch (e) {
      toast(e.message);
    } finally {
      $("preview-dream").disabled = false;
    }
  };
}
if ($("dream-preview-close")) {
  $("dream-preview-close").onclick = () => {
    if ($("dream-preview-box")) $("dream-preview-box").classList.add("hidden");
  };
}
let latestDreamPreviewDiffs = [];
if ($("rollback-dream")) {
  $("rollback-dream").onclick = () => {
    openConfirmModal({
      title: "确认回滚上次 Dreaming 整理",
      body: "此操作将把长期记忆（CLAUDE.md / MEMORY.md / Wiki 页面）恢复到最近一次 Dreaming 执行前的原子快照。\n确定要回滚吗？",
      okLabel: "确认回滚",
      onConfirm: async () => {
        $("rollback-dream").disabled = true;
        try {
          const rb = await api("/memory/dream/rollback", {
            method: "POST",
            body: JSON.stringify({ ...memoryScopePayload() }),
          });
          toast(
            "已回滚至快照 " +
              String(rb.snapshot_id || "latest").slice(0, 12) +
              "（恢复 " +
              (rb.restored_files?.length || 0) +
              " 文件）",
            "success",
          );
          if ($("dream-preview-box")) $("dream-preview-box").classList.add("hidden");
          await loadMemoryOverview();
        } catch (e) {
          toast(e.message, "error");
        } finally {
          $("rollback-dream").disabled = false;
        }
      },
    });
  };
}
if ($("run-dream")) {
  $("run-dream").onclick = async () => {
    $("run-dream").disabled = true;
    try {
      const goalVal = $("dream-goal") ? $("dream-goal").value.trim() : "";
      const rep = await api("/memory/dream", {
        method: "POST",
        body: JSON.stringify({
          ...memoryScopePayload(),
          ...(goalVal ? { focus_goal: goalVal } : {}),
        }),
      });
      if ($("dream-preview-box")) $("dream-preview-box").classList.add("hidden");
      toast(
        "Dreaming 完成：纠错 " +
          rep.corrections_count +
          " 项 · 更新 Wiki " +
          rep.pages_written_count +
          " 页 · 修复断链 " +
          rep.links_repaired_count +
          " 处",
        "success",
      );
      await loadMemoryOverview();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      $("run-dream").disabled = false;
    }
  };
}

// ============================================================================
// UX Power Module (Pure DOM API Only · Zero HTML String Sinks):
// 双态会话布局、统一多轮对话流 + 只读工具折叠组、吸顶审批条 + 标签页/桌面提醒、
// 3-Tab 可折叠检查器（PTY 进程 / 声明式规则 / 安全审计 / 文件树）、宽屏编辑器、
// 目录选择器、剪贴板贴图 + 拖拽、/ 与 @ 行内自动补全、⌘K 命令面板与全局快捷键。
// ============================================================================
let cachedSkillsCatalog = [];
let cachedWsFiles = [];
let cachedRunsForCmd = [];
let lastRunStatusById = new Map();
let desktopNotifyEnabled = false;
let rawTimelineExpanded = false;
let confirmModalAction = null;
let proposalCardInView = true;
let activeProposalForSticky = null;
let lastNotifiedStatus = null;
let activeAcTextarea = null;
let activeAcItems = [];
let activeAcIndex = 0;
let activeAcTokenStart = 0;
let activeAcTokenEnd = 0;
let browseCurrentPath = "";
let browseParentPath = null;

const READONLY_GROUP_TOOLS = new Set([
  "fs_read",
  "fs_grep",
  "fs_glob",
  "repo_read",
  "repo_search",
  "repo_list",
  "repo_map",
  "symbol_search",
  "knowledge_search",
  "memory_read",
  "memory_search",
  "skill_search",
  "skill_inspect",
  "lsp_query",
  "mcp_tool_search",
  "mcp_list_resources",
  "mcp_read_resource",
  "a2a_list_agents",
  "a2a_discover_agent",
  "a2a_get_task",
]);

function updateComposerAdvancedSummary() {
  const sumEl = $("composer-advanced-summary");
  if (!sumEl) return;
  const parts = [];
  const costVal = $("max-cost-usd") ? $("max-cost-usd").value.trim() : "";
  parts.push(costVal ? "上限 $" + costVal : "预算不限");
  if ($("template")?.value === "local_cwd") {
    const pm = $("permission-mode")?.value || "auto-edit";
    parts.push(pm);
    if ($("enable-gui")?.checked) parts.push("GUI");
    if ($("isolate-worktree")?.checked) parts.push("Worktree");
  }
  sumEl.textContent = parts.join(" · ");
}

function setSessionMode(run) {
  const isActive = Boolean(run && run.id);
  document.body.classList.toggle("session-active", isActive);
  const bar = $("active-session-bar");
  if (bar) bar.classList.toggle("is-visible", isActive);
  if (!isActive) return;
  if ($("active-session-title")) {
    $("active-session-title").textContent = run.title || run.prompt || "当前会话";
  }
  if ($("active-workspace-pill")) {
    const wsLabel = run.direct_workspace
      ? "本地直连 · " + (run.permission_mode || "auto-edit")
      : run.template === "multi_file"
        ? "多文件 Worktree"
        : "隔离示例副本";
    $("active-workspace-pill").textContent = wsLabel;
  }
  if ($("active-model-pill")) {
    $("active-model-pill").textContent = run.model || run.provider || "默认模型";
  }
}

function insertTextIntoActivePrompt(snippet) {
  const target =
    document.body.classList.contains("session-active") && $("followup-prompt")
      ? $("followup-prompt")
      : $("prompt");
  if (!target) return;
  const start = target.selectionStart ?? target.value.length;
  const end = target.selectionEnd ?? target.value.length;
  const prefix = start > 0 && !/\s$/.test(target.value.slice(0, start)) ? " " : "";
  target.value = target.value.slice(0, start) + prefix + snippet + target.value.slice(end);
  const nextPos = start + prefix.length + snippet.length;
  target.focus();
  target.setSelectionRange(nextPos, nextPos);
}

function buildToolGroupNode(toolEvents) {
  const reads = toolEvents.filter((e) =>
    ["fs_read", "repo_read", "memory_read", "mcp_read_resource"].includes(e.payload?.name),
  ).length;
  const searches = toolEvents.length - reads;
  const summaryParts = [];
  if (reads) summaryParts.push("读取 " + reads + " 个文件/资源");
  if (searches) summaryParts.push("检索/分析 " + searches + " 次");
  const det = el("details", undefined, "tool-group-card");
  const sum = el("summary", undefined, "tool-group-summary");
  const left = el("span", undefined, "tool-group-summary-left");
  left.append(
    el("span", "⌕ " + (summaryParts.join(" · ") || "只读工具步骤")),
    el("span", toolEvents.length + " 步", "tool-group-count"),
  );
  sum.append(left, el("span", "展开详情 ▾", "muted"));
  det.append(sum);
  const body = el("div", undefined, "tool-group-body");
  for (const ev of toolEvents) {
    const p = ev.payload || {};
    const args = p.arguments || {};
    const argPreview =
      args.path || args.query || args.pattern || args.symbol || args.name || "";
    const ok = p.result?.ok !== false;
    const row = el("div", undefined, "tool-step-row");
    const head = el("div", undefined, "tool-step-head");
    head.append(
      el("span", String(p.name || "tool"), "tool-step-name"),
      el("span", ok ? "OK" : String(p.result?.code || "ERR"), "tool-step-status " + (ok ? "ok" : "error")),
    );
    row.append(head);
    if (argPreview) {
      row.append(el("div", String(argPreview).slice(0, 140), "tool-step-meta"));
    }
    body.append(row);
  }
  det.append(body);
  return det;
}

function renderChatThread(run, events, isActivelyRunning = false) {
  const threadEl = $("chat-thread");
  const rawToggle = $("toggle-raw-timeline-btn");
  const tl = $("timeline");
  if (!threadEl || !run) return false;

  const turns = Array.isArray(run.conversation_thread) && run.conversation_thread.length
    ? run.conversation_thread
    : run.prompt
      ? [
          {
            turn_index: 0,
            user_prompt: run.prompt,
            assistant_answer: isActivelyRunning ? "" : run.answer || "",
            thinking: run.thinking || "",
            tool_calls_count: 0,
            attachments: run.user_attachments || [],
            started_at: run.created_at,
            completed_at: run.updated_at,
          },
        ]
      : [];

  if (!turns.length) {
    if (lastChatThreadSig !== "") {
      lastChatThreadSig = "";
      threadEl.replaceChildren();
    }
    if (rawToggle) rawToggle.classList.add("hidden");
    if (tl) tl.classList.remove("hidden");
    return false;
  }

  // 把工具执行事件按时间分配到对应 Turn，并自动将连续只读工具折叠为单个步骤组
  const toolEvs = (events || []).filter(
    (e) =>
      (e.kind === "tool_result" && e.payload?.result && !e.payload.result.requires_approval) ||
      e.kind === "gui_screenshot" ||
      e.kind === "user_steering_injected" ||
      e.kind === "context_compacted",
  );
  const lastToolEvId = toolEvs.length ? toolEvs[toolEvs.length - 1].id : 0;
  const sig =
    run.id +
    "|" +
    (isActivelyRunning ? "1" : "0") +
    "|" +
    (run.error || "") +
    "|" +
    toolEvs.length +
    ":" +
    lastToolEvId +
    "|" +
    turns
      .map(
        (t, i) =>
          i +
          ":" +
          String(t.user_prompt || "").length +
          ":" +
          (isActivelyRunning && i === turns.length - 1 ? 0 : String(t.assistant_answer || "").length) +
          ":" +
          (isActivelyRunning && i === turns.length - 1 ? 0 : String(t.thinking || "").length),
      )
      .join(",");

  if (rawToggle) {
    rawToggle.classList.remove("hidden");
    rawToggle.setAttribute("aria-expanded", rawTimelineExpanded ? "true" : "false");
    rawToggle.textContent = rawTimelineExpanded
      ? "收起原始事件流 ▴"
      : "原始事件流 (" + (events?.length || 0) + ") ▾";
  }
  if (tl) tl.classList.toggle("hidden", !rawTimelineExpanded);

  // 签名完全未变时零 DOM 重绘，彻底消除轮询引起的画面闪烁！
  if (sig === lastChatThreadSig && threadEl.children.length > 0) {
    return true;
  }
  lastChatThreadSig = sig;

  // 保存用户已展开的 <details> 状态（按稳定 data-open-key），更新后原地还原不闪烁折叠
  const openKeys = new Set();
  for (const d of threadEl.querySelectorAll("details[data-open-key]")) {
    if (d.open) openKeys.add(d.dataset.openKey);
  }

  const turnBuckets = turns.map(() => []);
  const turnTimes = turns.map((t) => (t.started_at ? Date.parse(t.started_at) || 0 : 0));
  for (const ev of toolEvs) {
    const evTs = ev.created_at ? Date.parse(ev.created_at) || 0 : 0;
    let targetIdx = 0;
    for (let i = 1; i < turnTimes.length; i += 1) {
      if (turnTimes[i] > 0 && evTs >= turnTimes[i]) targetIdx = i;
    }
    turnBuckets[targetIdx].push(ev);
  }

  const prevScroll = isNearBottom(document.documentElement);
  const frag = document.createDocumentFragment();
  $("empty").classList.add("hidden");

  turns.forEach((turn, idx) => {
    const isCurrentActiveTurn = isActivelyRunning && idx === turns.length - 1;
    const turnBox = el("div", undefined, "chat-turn");

    // 1. User Bubble
    const userWrap = el("div", undefined, "chat-msg chat-msg-user");
    const userBubble = el("div", undefined, "chat-bubble chat-bubble-user");
    const uHead = el("div", undefined, "chat-bubble-head");
    uHead.append(
      el("span", "第 " + (idx + 1) + " 轮 · 用户", "chat-role-badge"),
      turn.started_at
        ? el("span", new Date(turn.started_at).toLocaleTimeString("zh-CN", { hour12: false }), "chat-time")
        : el("span", "", "chat-time"),
    );
    userBubble.append(uHead, el("div", String(turn.user_prompt || ""), "chat-user-text"));
    const turnAtts = Array.isArray(turn.attachments) ? turn.attachments : [];
    if (turnAtts.length) {
      const attRow = el("div", undefined, "chat-attachments");
      for (const a of turnAtts) {
        attRow.append(el("span", "📎 " + (a.filename || a.artifact_name || "file"), "att-chip"));
      }
      userBubble.append(attRow);
    }
    userWrap.append(userBubble);
    turnBox.append(userWrap);

    // 2. Grouped Tool Steps for this turn
    const bucket = turnBuckets[idx] || [];
    let roBatch = [];
    let groupSeq = 0;
    const flushRo = () => {
      if (!roBatch.length) return;
      const gKey = "t" + idx + ":ro:" + groupSeq;
      groupSeq += 1;
      const node = buildToolGroupNode(roBatch);
      node.dataset.openKey = gKey;
      if (openKeys.has(gKey)) node.open = true;
      turnBox.append(node);
      roBatch = [];
    };
    for (const ev of bucket) {
      const p = ev.payload || {};
      if (ev.kind === "tool_result" && READONLY_GROUP_TOOLS.has(p.name)) {
        roBatch.push(ev);
        continue;
      }
      flushRo();
      if (ev.kind === "user_steering_injected") {
        const sWrap = el("div", undefined, "chat-msg chat-msg-steering");
        const sBub = el("div", undefined, "chat-bubble chat-bubble-steering");
        sBub.append(
          el("span", "⚡ 实时插话已注入", "chat-role-badge"),
          el("div", Array.isArray(p.prompts) ? p.prompts.join("\n") : String(p.prompt || ""), "chat-user-text"),
        );
        sWrap.append(sBub);
        turnBox.append(sWrap);
      } else if (ev.kind === "context_compacted") {
        turnBox.append(
          el(
            "div",
            "🗜 上下文已自动压缩（累计归档 " + (p.dropped_groups ?? 0) + " 组 → COMPACTION_SUMMARY）",
            "event-callout",
          ),
        );
      } else if (ev.kind === "gui_screenshot" && p.artifact_name && current) {
        const imgBox = el("div", undefined, "tool-group-card");
        attachArtifactImage(current, p.artifact_name, imgBox);
        turnBox.append(imgBox);
      } else if (ev.kind === "tool_result") {
        if (p.name === "bash_exec") {
          appendCollapsibleTerminalOutput(turnBox, p.arguments?.command, p.result);
          const lastDet = turnBox.lastElementChild;
          if (lastDet && lastDet.tagName === "DETAILS") {
            const dKey = "t" + idx + ":ev:" + ev.id;
            lastDet.dataset.openKey = dKey;
            if (openKeys.has(dKey)) lastDet.open = true;
          }
        } else if ((p.name === "fs_write" || p.name === "fs_edit") && p.result?.diff) {
          const dKey = "t" + idx + ":diff:" + ev.id;
          const dd = el("details", undefined, "diff-details");
          dd.dataset.openKey = dKey;
          if (openKeys.has(dKey)) dd.open = true;
          dd.append(
            el("summary", "修改文件 (" + p.name + ") · " + (p.result.path || p.arguments?.path || "")),
            renderUnifiedDiff(p.result.diff, { maxLines: 120 }),
          );
          turnBox.append(dd);
        } else if (p.name === "subagent_run") {
          appendCollapsibleSubagentPreview(turnBox, p.arguments, p.result, p.call_id);
        } else {
          roBatch.push(ev);
        }
      }
    }
    flushRo();

    // 3. Assistant Bubble：当前活跃回合正在由 #live-stream-card 独占流式展示，不提前双写造成反复切换闪烁
    const ansText = isCurrentActiveTurn
      ? ""
      : idx === turns.length - 1 && !turn.assistant_answer && run.error
        ? "**运行停止：" + run.error + "**"
        : String(turn.assistant_answer || "");
    const thinkText = isCurrentActiveTurn ? "" : String(turn.thinking || "");
    if (ansText || thinkText) {
      const aWrap = el("div", undefined, "chat-msg chat-msg-assistant");
      const aBub = el("div", undefined, "chat-bubble chat-bubble-assistant");
      const aHead = el("div", undefined, "chat-bubble-head");
      aHead.append(
        el("span", "Agent · 回答", "chat-role-badge"),
        makeCopyButton(ansText || thinkText, "复制 Markdown"),
      );
      aBub.append(aHead);
      if (thinkText) {
        const thKey = "t" + idx + ":thinking";
        const tDet = el("details", undefined, "chat-thinking-details");
        tDet.dataset.openKey = thKey;
        if (openKeys.has(thKey)) tDet.open = true;
        tDet.append(
          el("summary", "查看 Extended Thinking 推理过程（" + thinkText.length + " 字符）"),
          el("pre", thinkText, "chat-thinking-pre"),
        );
        aBub.append(tDet);
      }
      if (ansText) {
        const mdBody = el("div", undefined, "answer md-body");
        renderSafeMarkdown(mdBody, ansText);
        aBub.append(mdBody);
      }
      aWrap.append(aBub);
      turnBox.append(aWrap);
    }

    frag.append(turnBox);
  });

  threadEl.replaceChildren(frag);
  if (prevScroll) maybeStickBottom(document.documentElement);
  return true;
}

if ($("toggle-raw-timeline-btn")) {
  $("toggle-raw-timeline-btn").onclick = () => {
    rawTimelineExpanded = !rawTimelineExpanded;
    if ($("timeline")) $("timeline").classList.toggle("hidden", !rawTimelineExpanded);
    $("toggle-raw-timeline-btn").setAttribute("aria-expanded", rawTimelineExpanded ? "true" : "false");
    $("toggle-raw-timeline-btn").textContent = rawTimelineExpanded ? "收起原始事件流 ▴" : "原始事件流 ▾";
  };
}

// 吸顶待审批快速条 + IntersectionObserver 防止审批卡滚出视野上方被漏看
if (typeof IntersectionObserver !== "undefined" && $("proposal-card")) {
  const obs = new IntersectionObserver(
    (entries) => {
      proposalCardInView = Boolean(entries[0]?.isIntersecting);
      updateStickyApprovalVisibility();
    },
    { threshold: 0.08 },
  );
  obs.observe($("proposal-card"));
}

function updateStickyApprovalVisibility() {
  const bar = $("sticky-approval-bar");
  if (!bar) return;
  const shouldShow = Boolean(activeProposalForSticky && !proposalCardInView);
  bar.classList.toggle("hidden", !shouldShow);
}

function syncStickyApprovalBanner(run, proposals) {
  const waitSet =
    Array.isArray(run.waiting_proposal_ids) && run.waiting_proposal_ids.length
      ? new Set(run.waiting_proposal_ids)
      : null;
  const pending = (proposals || []).filter(
    (p) =>
      p.status === "pending" &&
      !p.applied &&
      !decidedProposalIds.has(p.id) &&
      (!waitSet || waitSet.has(p.id)),
  );
  const firstPending = run.status === "waiting_approval" && pending.length ? pending[0] : null;
  const transitionedToApproval =
    firstPending && (!activeProposalForSticky || activeProposalForSticky.id !== firstPending.id);
  activeProposalForSticky = firstPending;
  if (firstPending && $("sticky-approval-text")) {
    const label =
      firstPending.kind === "command"
        ? "待确认终端命令：" + ((firstPending.arguments || {}).command || "").slice(0, 64)
        : firstPending.kind === "a2a_call"
          ? "待确认 A2A 远端委托：" +
            ((firstPending.arguments || {}).peer || firstPending.tool || firstPending.id.slice(0, 8))
          : firstPending.kind === "user_question"
            ? "智能体等待你的回复：" +
              String(firstPending.question || (firstPending.arguments || {}).question || "").slice(0, 64)
            : firstPending.kind === "plan_approval"
              ? "实施计划待批准：" +
                String(
                  firstPending.plan_file ||
                    (firstPending.arguments || {}).plan_file ||
                    firstPending.summary ||
                    "查看计划摘要",
                ).slice(0, 64)
              : "待确认动作 / 补丁：" + (firstPending.path || firstPending.tool || firstPending.id.slice(0, 8));
    $("sticky-approval-text").textContent = label + (pending.length > 1 ? "（共 " + pending.length + " 项）" : "");
  }
  if ($("sticky-approve-btn")) {
    $("sticky-approve-btn").textContent =
      firstPending?.kind === "plan_approval"
        ? "批准计划并开始执行"
        : firstPending?.kind === "user_question"
          ? "前往回复 ↗"
          : "批准并继续 ↗";
  }
  updateStickyApprovalVisibility();
  if (transitionedToApproval && $("proposal-card")) {
    $("proposal-card").scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
}

if ($("sticky-jump-approval")) {
  $("sticky-jump-approval").onclick = () => {
    $("proposal-card")?.scrollIntoView({ behavior: "smooth", block: "start" });
  };
}
if ($("sticky-approve-btn")) {
  $("sticky-approve-btn").onclick = async () => {
    if (!current || !activeProposalForSticky) return;
    if (activeProposalForSticky.kind === "user_question") {
      $("proposal-card")?.scrollIntoView({ behavior: "smooth", block: "start" });
      return;
    }
    const pid = activeProposalForSticky.id;
    decidedProposalIds.add(pid);
    activeProposalForSticky = null;
    updateStickyApprovalVisibility();
    try {
      await api("/runs/" + encodeURIComponent(current) + "/proposals/" + encodeURIComponent(pid), {
        method: "POST",
        body: JSON.stringify({ approved: true }),
      });
      await refreshUntilRunning();
    } catch (e) {
      decidedProposalIds.delete(pid);
      toast(e.message, "error");
      await refreshRun();
    }
  };
}
if ($("sticky-reject-btn")) {
  $("sticky-reject-btn").onclick = async () => {
    if (!current || !activeProposalForSticky) return;
    const pid = activeProposalForSticky.id;
    decidedProposalIds.add(pid);
    activeProposalForSticky = null;
    updateStickyApprovalVisibility();
    try {
      await api("/runs/" + encodeURIComponent(current) + "/proposals/" + encodeURIComponent(pid), {
        method: "POST",
        body: JSON.stringify({ approved: false }),
      });
      await refreshUntilRunning();
    } catch (e) {
      decidedProposalIds.delete(pid);
      toast(e.message, "error");
      await refreshRun();
    }
  };
}

// 动态浏览器标签标题与可选桌面 Notification 提醒
function updateTabTitleAndNotify(run) {
  if (!run) return;
  const baseTitle = "Agent Systems Lab · 代码开发工作台";
  if (run.status === "waiting_approval") {
    const cnt = (run.waiting_proposal_ids || []).length || 1;
    document.title = "⚠ (" + cnt + " 待审批) · " + baseTitle;
  } else if (["running", "queued"].includes(run.status)) {
    document.title = "● 运行中 · " + baseTitle;
  } else if (run.status === "completed") {
    document.title = "✓ 已完成 · " + baseTitle;
  } else {
    document.title = baseTitle;
  }

  const prev = lastRunStatusById.get(run.id);
  lastRunStatusById.set(run.id, run.status);
  if (
    desktopNotifyEnabled &&
    document.hidden &&
    prev &&
    prev !== run.status &&
    ["waiting_approval", "completed", "failed"].includes(run.status) &&
    lastNotifiedStatus !== run.id + ":" + run.status
  ) {
    lastNotifiedStatus = run.id + ":" + run.status;
    if (typeof Notification !== "undefined" && Notification.permission === "granted") {
      const msg =
        run.status === "waiting_approval"
          ? "任务暂停等待你确认审批：" + (run.title || run.prompt || "").slice(0, 60)
          : run.status === "completed"
            ? "任务已完成：" + (run.title || run.prompt || "").slice(0, 60)
            : "任务运行异常：" + (run.error || run.status);
      try {
        new Notification("Agent Systems Lab", { body: msg });
      } catch {}
    }
  }
}

if ($("notify-toggle-btn")) {
  $("notify-toggle-btn").onclick = async () => {
    if (typeof Notification === "undefined") {
      toast("当前浏览器不支持桌面通知", "warning");
      return;
    }
    if (!desktopNotifyEnabled) {
      const perm = Notification.permission === "granted" ? "granted" : await Notification.requestPermission();
      if (perm !== "granted") {
        toast("未授予浏览器通知权限", "warning");
        return;
      }
      desktopNotifyEnabled = true;
      toast("已开启待审批 / 任务完成桌面通知", "success");
    } else {
      desktopNotifyEnabled = false;
      toast("已关闭桌面通知", "info");
    }
    $("notify-toggle-btn").setAttribute("aria-pressed", desktopNotifyEnabled ? "true" : "false");
    $("notify-toggle-btn").classList.toggle("active", desktopNotifyEnabled);
  };
}

// 通用高风险确认弹窗（保护 Rewind / Dreaming Rollback）
function openConfirmModal({ title, body, okLabel = "确认执行", onConfirm }) {
  confirmModalAction = onConfirm;
  if ($("confirm-modal-title")) $("confirm-modal-title").textContent = title;
  if ($("confirm-modal-body")) $("confirm-modal-body").textContent = body;
  if ($("confirm-ok-btn")) $("confirm-ok-btn").textContent = okLabel;
  if ($("confirm-modal")) $("confirm-modal").classList.remove("hidden");
  $("confirm-ok-btn")?.focus();
}
function closeConfirmModal() {
  confirmModalAction = null;
  if ($("confirm-modal")) $("confirm-modal").classList.add("hidden");
}
if ($("confirm-close-btn")) $("confirm-close-btn").onclick = closeConfirmModal;
if ($("confirm-cancel-btn")) $("confirm-cancel-btn").onclick = closeConfirmModal;
if ($("confirm-ok-btn")) {
  $("confirm-ok-btn").onclick = async () => {
    const fn = confirmModalAction;
    closeConfirmModal();
    if (typeof fn === "function") await fn();
  };
}

// 宽屏记忆编辑 & Dreaming Diff 弹窗
function closeWideModal() {
  if ($("wide-modal")) $("wide-modal").classList.add("hidden");
  if ($("wide-modal-body")) $("wide-modal-body").replaceChildren();
}
if ($("wide-modal-close")) $("wide-modal-close").onclick = closeWideModal;

if ($("open-wide-memory-modal")) {
  $("open-wide-memory-modal").onclick = () => {
    if (!$("wide-modal") || !$("wide-modal-body")) return;
    const { layer, path } = parseCoreDocTarget();
    $("wide-modal-title").textContent = "⤢ 宽屏编辑 · " + layer + "/" + path;
    const ta = document.createElement("textarea");
    ta.className = "wide-editor-textarea";
    ta.rows = 18;
    ta.value = $("core-doc-content") ? $("core-doc-content").value : "";
    const actions = el("div", undefined, "wide-modal-actions");
    const cancelB = el("button", "取消", "button secondary");
    cancelB.type = "button";
    cancelB.onclick = closeWideModal;
    const saveB = el("button", "保存 " + layer + "/" + path, "button primary-button");
    saveB.type = "button";
    saveB.onclick = async () => {
      if ($("core-doc-content")) $("core-doc-content").value = ta.value;
      closeWideModal();
      $("save-core-doc")?.click();
    };
    actions.append(cancelB, saveB);
    $("wide-modal-body").replaceChildren(ta, actions);
    $("wide-modal").classList.remove("hidden");
    ta.focus();
  };
}
if ($("open-wide-dream-modal")) {
  $("open-wide-dream-modal").onclick = () => {
    if (!$("wide-modal") || !$("wide-modal-body")) return;
    $("wide-modal-title").textContent = "⤢ Dreaming 预演差异全景（零落盘）";
    const wrap = el("div");
    if (!latestDreamPreviewDiffs.length) {
      wrap.append(el("div", "当前暂无预演差异，请先点击「预演 Diff」。", "muted"));
    } else {
      for (const d of latestDreamPreviewDiffs) {
        const box = el("div", undefined, "dream-diff-item");
        box.append(
          el("div", "[" + (d.action || "update") + "] " + (d.layer || "project") + ":" + (d.path || ""), "dream-diff-item-title"),
        );
        if (d.diff) box.append(renderUnifiedDiff(d.diff, { maxLines: 240 }));
        wrap.append(box);
      }
    }
    $("wide-modal-body").replaceChildren(wrap);
    $("wide-modal").classList.remove("hidden");
  };
}

// Intro 横幅关闭 + 能力面板 + 高级选项折叠
if ($("dismiss-intro-btn")) {
  $("dismiss-intro-btn").onclick = () => {
    if ($("intro-hero")) $("intro-hero").classList.add("hidden");
    try {
      localStorage.setItem("agent_lab_intro_dismissed", "1");
    } catch {}
  };
}
try {
  if (localStorage.getItem("agent_lab_intro_dismissed") === "1" && $("intro-hero")) {
    $("intro-hero").classList.add("hidden");
  }
} catch {}
if ($("cap-toggle-btn") && $("cap-panel")) {
  $("cap-toggle-btn").onclick = () => {
    const willOpen = $("cap-panel").classList.contains("hidden");
    $("cap-panel").classList.toggle("hidden", !willOpen);
    $("cap-toggle-btn").setAttribute("aria-expanded", willOpen ? "true" : "false");
    $("cap-toggle-btn").textContent = willOpen ? "系统能力一览 ▴" : "系统能力一览 ▾";
  };
}
if ($("composer-advanced-toggle") && $("composer-advanced-panel")) {
  $("composer-advanced-toggle").onclick = () => {
    const willOpen = $("composer-advanced-panel").classList.contains("hidden");
    $("composer-advanced-panel").classList.toggle("hidden", !willOpen);
    $("composer-advanced-toggle").setAttribute("aria-expanded", willOpen ? "true" : "false");
  };
  for (const id of ["max-cost-usd", "permission-mode", "enable-gui", "isolate-worktree"]) {
    if ($(id)) $(id).addEventListener("change", updateComposerAdvancedSummary);
  }
  updateComposerAdvancedSummary();
}

// 桌面左右双栏折叠 + 右侧 3-Tab 检查器切换
function toggleDesktopSidebar() {
  const collapsed = document.body.classList.toggle("sidebar-collapsed");
  if ($("desktop-sidebar-toggle")) {
    $("desktop-sidebar-toggle").setAttribute("aria-expanded", collapsed ? "false" : "true");
  }
  try {
    localStorage.setItem("agent_lab_sidebar_collapsed", collapsed ? "1" : "0");
  } catch {}
}
function toggleRightInspector() {
  const collapsed = document.body.classList.toggle("inspector-collapsed");
  for (const btnId of ["inspector-toggle-btn", "session-inspector-toggle-btn"]) {
    if ($(btnId)) $(btnId).setAttribute("aria-expanded", collapsed ? "false" : "true");
  }
  try {
    localStorage.setItem("agent_lab_inspector_collapsed", collapsed ? "1" : "0");
  } catch {}
}
if ($("desktop-sidebar-toggle")) $("desktop-sidebar-toggle").onclick = toggleDesktopSidebar;
if ($("inspector-toggle-btn")) $("inspector-toggle-btn").onclick = toggleRightInspector;
if ($("session-inspector-toggle-btn")) $("session-inspector-toggle-btn").onclick = toggleRightInspector;
try {
  if (localStorage.getItem("agent_lab_sidebar_collapsed") === "1") toggleDesktopSidebar();
  if (localStorage.getItem("agent_lab_inspector_collapsed") === "1") toggleRightInspector();
} catch {}

function switchInspectorTab(tabName) {
  for (const name of ["run", "memory", "tools"]) {
    const btn = $("tab-btn-" + name);
    const panel = $("tab-panel-" + name);
    const active = name === tabName;
    if (btn) {
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", active ? "true" : "false");
    }
    if (panel) panel.classList.toggle("hidden", !active);
  }
  if (tabName === "tools") {
    loadGatewaysConfig().catch(() => {});
    loadA2APeers().catch(() => {});
    loadDeclarativeRules().catch(() => {});
    loadWorkspaceFilesForRun(current).catch(() => {});
  } else if (tabName === "run" && current) {
    loadBackgroundProcesses(current).catch(() => {});
  }
}
for (const tabBtn of document.querySelectorAll(".inspector-tab")) {
  tabBtn.onclick = () => switchInspectorTab(tabBtn.dataset.tab || "run");
}

// Tab 1: 后台 PTY 进程与异步子 Agent 监控
async function loadBackgroundProcesses(runId) {
  const box = $("bg-processes-box");
  if (!box) return;
  if (!runId) {
    lastBgProcsSig = "";
    box.textContent = "选择任务后查看活跃的后台 PTY / Shell 进程及异步子 Agent 任务。";
    return;
  }
  try {
    const data = await api("/runs/" + encodeURIComponent(runId) + "/processes");
    const procs = Array.isArray(data.processes) ? data.processes : [];
    const jobs = Array.isArray(data.subagents) ? data.subagents : [];
    const sig = runId + "|" + JSON.stringify({ procs, jobs });
    if (sig === lastBgProcsSig) return;
    lastBgProcsSig = sig;
    box.replaceChildren();
    if (!procs.length && !jobs.length) {
      box.append(el("div", "当前无运行中的后台 PTY 进程或异步子 Agent。", "muted"));
      return;
    }
    for (const pr of procs) {
      const card = el("div", undefined, "proc-item");
      const head = el("div", undefined, "proc-head");
      const alive = pr.alive !== false && pr.status !== "exited";
      head.append(
        el("span", (pr.use_pty ? "PTY " : "SH ") + String(pr.proc_id || ""), "proc-cmd"),
        el("span", alive ? "running" : "exited", "proc-status" + (alive ? "" : " exited")),
      );
      if (alive && pr.proc_id) {
        const killBtn = el("button", "终止", "proc-kill-btn");
        killBtn.type = "button";
        killBtn.onclick = async () => {
          try {
            await api(
              "/runs/" + encodeURIComponent(runId) + "/processes/" + encodeURIComponent(pr.proc_id) + "/kill",
              { method: "POST" },
            );
            toast("已终止后台进程 " + pr.proc_id, "success");
            await loadBackgroundProcesses(runId);
          } catch (e) {
            toast(e.message, "error");
          }
        };
        head.append(killBtn);
      }
      card.append(head, el("div", String(pr.command || "").slice(0, 120), "proc-meta"));
      box.append(card);
    }
    for (const jb of jobs) {
      const card = el("div", undefined, "proc-item");
      const head = el("div", undefined, "proc-head");
      head.append(
        el("span", "Subagent " + String(jb.job_id || "").slice(0, 10), "proc-cmd"),
        el("span", String(jb.status || "running"), "proc-status" + (jb.status === "completed" ? " exited" : "")),
      );
      card.append(head, el("div", "角色：" + (Array.isArray(jb.roles) ? jb.roles.join(", ") : ""), "proc-meta"));
      box.append(card);
    }
  } catch {
    box.textContent = "后台进程状态暂不可用。";
  }
}
if ($("refresh-procs-btn")) {
  $("refresh-procs-btn").onclick = () => loadBackgroundProcesses(current);
}

// Tab 3: 多协议模型网关切换器与脱敏配置编辑器（GET/PUT /api/gateways, POST /api/gateways/activate）
const GATEWAY_PROTOCOL_VENDORS = {
  anthropic_messages: "ANTHROPIC",
  openai_chat: "OPENAI",
  openai_responses: "OPENAI",
  gemini_generate_content: "GEMINI",
  demo: "DEMO",
};

function extractWireProtocol(cfg, fallbackGateway = null) {
  const explicit = String(
    cfg?.protocol ||
      cfg?.wire_protocol ||
      cfg?.gateway_protocol ||
      cfg?.gateway?.protocol ||
      cfg?.active_gateway?.protocol ||
      fallbackGateway?.protocol ||
      "",
  ).trim();
  if (explicit) return explicit;
  const rawProvider = String(cfg?.provider || "").trim().toLowerCase();
  if (GATEWAY_PROTOCOL_VENDORS[rawProvider]) return rawProvider;
  return "";
}

function formatProviderDisplay(cfg, fallbackGateway = null) {
  const rawProvider = String(cfg?.provider || "demo").trim();
  const cleanProvider = rawProvider.replace(/^gateway:/i, "").trim();
  const wireProtocol = extractWireProtocol(cfg, fallbackGateway);
  if (
    cleanProvider.toLowerCase() === "demo" ||
    (wireProtocol === "demo" && (!cleanProvider || cleanProvider.toLowerCase() === "demo"))
  ) {
    return "离线 DEMO · " + (cfg?.dispatch_mode === "queue" ? "队列模式" : "内联模式");
  }
  const vendorPrefix =
    (wireProtocol && cleanProvider.toLowerCase() === wireProtocol.toLowerCase()
      ? GATEWAY_PROTOCOL_VENDORS[wireProtocol]
      : "") ||
    cleanProvider.toUpperCase() ||
    GATEWAY_PROTOCOL_VENDORS[wireProtocol] ||
    "MODEL";
  return wireProtocol ? vendorPrefix + " · " + wireProtocol : vendorPrefix + " · 真实模型";
}

function parseGatewaysResponse(data) {
  const src =
    data && typeof data === "object"
      ? data.config && typeof data.config === "object" && !Array.isArray(data.config)
        ? data.config
        : data.document && typeof data.document === "object" && !Array.isArray(data.document)
          ? data.document
          : data
      : {};
  const rawProfiles = src.profiles;
  const profiles = {};
  if (Array.isArray(rawProfiles)) {
    for (const item of rawProfiles) {
      if (item && typeof item === "object" && item.name) {
        profiles[String(item.name)] = item;
      }
    }
  } else if (rawProfiles && typeof rawProfiles === "object") {
    for (const [name, prof] of Object.entries(rawProfiles)) {
      if (prof && typeof prof === "object") {
        profiles[name] = prof;
      }
    }
  }
  const activeInfo =
    (data && typeof data.active === "object" && data.active) ||
    (data && typeof data.active_gateway === "object" && data.active_gateway) ||
    (data && typeof data.gateway === "object" && data.gateway) ||
    null;
  const activeProfile =
    (typeof src.active_profile === "string" && src.active_profile.trim()) ||
    (typeof data?.active_profile === "string" && data.active_profile.trim()) ||
    (typeof activeInfo?.profile === "string" && activeInfo.profile.trim()) ||
    null;
  const configPath =
    (typeof src.config_path === "string" && src.config_path.trim()) ||
    (typeof data?.config_path === "string" && data.config_path.trim()) ||
    null;
  const editableProfiles = {};
  for (const [name, prof] of Object.entries(profiles)) {
    const copy = { ...prof };
    delete copy.credential_configured;
    editableProfiles[name] = copy;
  }
  const editableDoc = {
    schema_version: Number(src.schema_version || 1),
    active_profile: activeProfile,
    profiles: editableProfiles,
  };
  return { profiles, activeProfile, activeInfo, configPath, editableDoc };
}

async function loadGatewaysConfig({ forceEditor = false, notifyError = false } = {}) {
  const badgeEl = $("gateway-status-badge");
  const selectEl = $("gateway-profile-select");
  const editorEl = $("gateway-json-editor");
  const activateBtn = $("gateway-activate-btn");
  const hintEl = $("gateway-hint-text");
  if (!badgeEl && !selectEl && !editorEl) return false;
  try {
    const data = await api("/gateways");
    const parsed = parseGatewaysResponse(data);
    latestGatewaysData = parsed;
    const profileNames = Object.keys(parsed.profiles);
    const activeName = parsed.activeProfile;
    const activeProf = activeName ? parsed.profiles[activeName] || null : null;
    const activeProto = String(
      parsed.activeInfo?.protocol ||
        activeProf?.protocol ||
        extractWireProtocol(latestConfigSnapshot) ||
        "",
    ).trim();
    const activeModel = String(
      parsed.activeInfo?.model || activeProf?.model || latestConfigSnapshot?.default_model || "",
    ).trim();
    const activeBaseUrl = String(parsed.activeInfo?.base_url || activeProf?.base_url || "").trim();
    const credConfigured =
      typeof activeProf?.credential_configured === "boolean"
        ? activeProf.credential_configured
        : null;

    if (badgeEl) {
      let pillState = "demo";
      if (activeProto && activeProto !== "demo") {
        pillState = credConfigured === false ? "warning" : "ready";
      }
      badgeEl.className = "gateway-protocol-pill " + pillState;
      let badgeLabel = "未激活 Profile（环境变量 / Demo）";
      if (activeName && activeProto) {
        badgeLabel = activeName + " · " + activeProto;
      } else if (activeName) {
        badgeLabel = activeName;
      } else if (activeProto) {
        badgeLabel = "当前协议 · " + activeProto;
      }
      if (credConfigured === false && activeProto !== "demo") {
        badgeLabel += " · 待配密钥";
      }
      badgeEl.textContent = badgeLabel;
      const tipParts = [
        activeName ? "Profile: " + activeName : "",
        activeProto ? "协议: " + activeProto : "",
        activeModel ? "默认模型: " + activeModel : "",
        activeBaseUrl ? "端点: " + activeBaseUrl : "",
        credConfigured === true ? "凭证: 已就绪" : credConfigured === false ? "凭证: 未配置" : "",
      ].filter(Boolean);
      badgeEl.title = tipParts.join(" | ");
    }

    if (selectEl) {
      const prevVal = selectEl.value;
      selectEl.replaceChildren();
      if (!profileNames.length) {
        const emptyOpt = document.createElement("option");
        emptyOpt.value = "";
        emptyOpt.textContent = "暂无已配置 Profile";
        selectEl.append(emptyOpt);
        selectEl.value = "";
      } else {
        for (const name of profileNames) {
          const prof = parsed.profiles[name] || {};
          const opt = document.createElement("option");
          opt.value = name;
          const protoPart = prof.protocol ? " · " + prof.protocol : "";
          const modelPart = prof.model ? " (" + prof.model + ")" : "";
          const credPart =
            prof.credential_configured === true
              ? " ✓"
              : prof.credential_configured === false
                ? " · 待配密钥"
                : "";
          opt.textContent = name + protoPart + modelPart + credPart;
          selectEl.append(opt);
        }
        if (activeName && profileNames.includes(activeName)) {
          selectEl.value = activeName;
        } else if (prevVal && profileNames.includes(prevVal)) {
          selectEl.value = prevVal;
        } else {
          selectEl.value = profileNames[0];
        }
      }
    }

    if (activateBtn) {
      activateBtn.disabled = !profileNames.length;
    }

    if (editorEl && (forceEditor || (!gatewayEditorDirty && document.activeElement !== editorEl))) {
      editorEl.value = JSON.stringify(parsed.editableDoc, null, 2);
      gatewayEditorDirty = false;
    }

    if (hintEl) {
      if (!profileNames.length) {
        hintEl.textContent =
          "尚未创建 gateways.json；可在此编辑或粘贴多协议网关配置（支持 anthropic_messages / openai_chat / openai_responses / gemini_generate_content / demo）后点击「保存网关配置」。";
      } else if (parsed.configPath) {
        hintEl.textContent =
          "配置文件：" +
          parsed.configPath +
          " · 共 " +
          profileNames.length +
          " 个 Profile · 密钥已脱敏为 ***REDACTED***，推荐通过 api_key_env 注入环境变量。";
      } else {
        hintEl.textContent =
          "共 " +
          profileNames.length +
          " 个网关 Profile · 密钥已脱敏为 ***REDACTED***，推荐通过 api_key_env 引用环境变量。";
      }
    }

    if (
      latestConfigSnapshot &&
      $("provider") &&
      !extractWireProtocol(latestConfigSnapshot) &&
      activeProto &&
      latestConfigSnapshot.provider !== "demo"
    ) {
      $("provider").textContent = formatProviderDisplay(latestConfigSnapshot, {
        profile: activeName,
        protocol: activeProto,
      });
    }
    return true;
  } catch (e) {
    if (badgeEl) {
      badgeEl.className = "gateway-protocol-pill warning";
      badgeEl.textContent = "网关配置暂不可用";
      badgeEl.title = String(e.message || "");
    }
    if (selectEl && !latestGatewaysData) {
      selectEl.replaceChildren();
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "无法读取网关 Profile";
      selectEl.append(opt);
    }
    if (activateBtn) activateBtn.disabled = true;
    if (hintEl) hintEl.textContent = "加载网关配置失败：" + e.message;
    if (notifyError) toast("刷新模型网关配置失败：" + e.message, "error");
    return false;
  }
}

if ($("gateway-editor-details")) {
  $("gateway-editor-details").addEventListener("toggle", () => {
    if (
      $("gateway-editor-details").open &&
      $("gateway-json-editor") &&
      !$("gateway-json-editor").value.trim()
    ) {
      loadGatewaysConfig({ forceEditor: true }).catch(() => {});
    }
  });
}

if ($("gateway-json-editor")) {
  $("gateway-json-editor").addEventListener("input", () => {
    gatewayEditorDirty = true;
  });
}

if ($("gateway-activate-btn")) {
  $("gateway-activate-btn").onclick = async () => {
    const profile = $("gateway-profile-select") ? $("gateway-profile-select").value.trim() : "";
    if (!profile) {
      toast("请先选择要切换的模型网关 Profile", "warning");
      return;
    }
    $("gateway-activate-btn").disabled = true;
    try {
      const res = await api("/gateways/activate", {
        method: "POST",
        body: JSON.stringify({ profile }),
      });
      const proto = String(
        res?.protocol ||
          res?.active?.protocol ||
          res?.active_gateway?.protocol ||
          res?.gateway?.protocol ||
          latestGatewaysData?.profiles?.[profile]?.protocol ||
          "",
      ).trim();
      if ($("model-select")) $("model-select").value = "";
      gatewayEditorDirty = false;
      toast("已切换生效模型网关：" + profile + (proto ? " (" + proto + ")" : ""), "success");
      await init();
    } catch (e) {
      toast("切换模型网关失败：" + e.message, "error");
    } finally {
      if ($("gateway-activate-btn")) {
        const hasChoice = Boolean($("gateway-profile-select")?.value);
        $("gateway-activate-btn").disabled = !hasChoice;
      }
    }
  };
}

if ($("gateway-save-btn")) {
  $("gateway-save-btn").onclick = async () => {
    const editor = $("gateway-json-editor");
    if (!editor) return;
    const raw = editor.value.trim();
    if (!raw) {
      toast("网关配置 JSON 不能为空", "warning");
      return;
    }
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch (err) {
      const msg = "JSON 语法错误：" + err.message;
      if ($("gateway-hint-text")) $("gateway-hint-text").textContent = msg;
      toast(msg, "error");
      return;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      toast("网关配置根对象必须是合法 JSON 对象", "error");
      return;
    }
    if (!parsed.profiles || typeof parsed.profiles !== "object" || Array.isArray(parsed.profiles)) {
      toast("网关配置必须包含对象类型的 profiles 字段", "error");
      return;
    }
    $("gateway-save-btn").disabled = true;
    try {
      await api("/gateways", {
        method: "PUT",
        body: JSON.stringify(parsed),
      });
      gatewayEditorDirty = false;
      toast("模型网关配置已保存并生效", "success");
      await loadGatewaysConfig({ forceEditor: true });
      api("/config")
        .then((c) => applyConfigSnapshot(c))
        .catch(() => {});
    } catch (e) {
      if ($("gateway-hint-text")) $("gateway-hint-text").textContent = "保存失败：" + e.message;
      toast("保存网关配置失败：" + e.message, "error");
    } finally {
      if ($("gateway-save-btn")) $("gateway-save-btn").disabled = false;
    }
  };
}

if ($("gateway-refresh-btn")) {
  $("gateway-refresh-btn").onclick = async () => {
    $("gateway-refresh-btn").disabled = true;
    try {
      gatewayEditorDirty = false;
      const ok = await loadGatewaysConfig({ forceEditor: true, notifyError: true });
      if (ok) toast("已刷新模型网关配置", "info");
    } finally {
      if ($("gateway-refresh-btn")) $("gateway-refresh-btn").disabled = false;
    }
  };
}

function openGatewaysPanel() {
  if (document.body.classList.contains("inspector-collapsed")) {
    toggleRightInspector();
  }
  switchInspectorTab("tools");
  $("gateway-card")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  $("gateway-profile-select")?.focus();
}

// Tab 3: A2A 远端智能体协同配置与 AgentCard 发现（GET/PUT /api/a2a/peers, POST /api/a2a/discover）
function parseA2APeersResponse(data) {
  const configSrc =
    data && typeof data === "object"
      ? data.config && typeof data.config === "object" && !Array.isArray(data.config)
        ? data.config
        : data.document && typeof data.document === "object" && !Array.isArray(data.document)
          ? data.document
          : data
      : {};
  const rawPeers =
    configSrc.peers !== undefined
      ? configSrc.peers
      : data && typeof data === "object"
        ? data.peers
        : {};
  const peers = {};
  if (Array.isArray(rawPeers)) {
    for (const item of rawPeers) {
      if (item && typeof item === "object" && item.name) {
        peers[String(item.name)] = item;
      }
    }
  } else if (rawPeers && typeof rawPeers === "object") {
    for (const [name, peerObj] of Object.entries(rawPeers)) {
      if (peerObj && typeof peerObj === "object") {
        peers[name] = peerObj;
      }
    }
  }
  const configPath =
    (typeof configSrc.config_path === "string" && configSrc.config_path.trim()) ||
    (typeof data?.config_path === "string" && data.config_path.trim()) ||
    null;
  const editablePeers = {};
  for (const [name, peerObj] of Object.entries(peers)) {
    const copy = { ...peerObj };
    delete copy.credential_configured;
    delete copy.card_cached;
    delete copy.skills;
    editablePeers[name] = copy;
  }
  const editableDoc = {
    schema_version: Number(configSrc.schema_version || 1),
    peers: editablePeers,
  };
  return { peers, configPath, editableDoc };
}

function formatA2ACardPreview(res, fallbackPeer) {
  const card = res && typeof res.card === "object" && res.card ? res.card : res;
  if (!card || typeof card !== "object") {
    return "未返回有效的 AgentCard。";
  }
  const caps = card.capabilities && typeof card.capabilities === "object" ? card.capabilities : {};
  const skills = Array.isArray(card.skills) ? card.skills : [];
  const skillLabels = skills
    .slice(0, 6)
    .map((s) => (typeof s === "object" ? s.id || s.name : String(s || "")))
    .filter(Boolean);
  const lines = [
    "AgentCard: " +
      String(card.name || fallbackPeer || "A2A Agent") +
      " (v" +
      String(card.version || "-") +
      " · A2A " +
      String(card.protocolVersion || "-") +
      ")",
    "URL: " + String(card.url || res?.card_url || "-"),
    "能力: streaming=" +
      (caps.streaming ? "true" : "false") +
      " · history=" +
      (caps.stateTransitionHistory ? "true" : "false"),
    "技能 (" + skills.length + "): " + (skillLabels.join(", ") || "无"),
  ];
  if (card.description) {
    lines.push("简介: " + String(card.description).slice(0, 180));
  }
  return lines.join("\n");
}

async function loadA2APeers({ forceEditor = false, notifyError = false } = {}) {
  const selectEl = $("a2a-peer-select");
  const statusEl = $("a2a-status-text");
  const editorEl = $("a2a-json-editor");
  const discoverBtn = $("a2a-discover-btn");
  if (!selectEl && !statusEl && !editorEl) return false;
  try {
    const data = await api("/a2a/peers");
    const parsed = parseA2APeersResponse(data);
    latestA2AData = parsed;
    const peerNames = Object.keys(parsed.peers).sort();
    const enabledNames = peerNames.filter((n) => parsed.peers[n]?.enabled !== false);

    if (selectEl) {
      const prevVal = selectEl.value;
      selectEl.replaceChildren();
      if (!peerNames.length) {
        const emptyOpt = document.createElement("option");
        emptyOpt.value = "";
        emptyOpt.textContent = "暂无已配置远端 A2A 对端";
        selectEl.append(emptyOpt);
        selectEl.value = "";
      } else {
        for (const name of peerNames) {
          const peer = parsed.peers[name] || {};
          const opt = document.createElement("option");
          opt.value = name;
          const disabledTag = peer.enabled === false ? " [已禁用]" : "";
          const urlPart = peer.url ? " · " + peer.url : "";
          const credPart = peer.credential_configured === true ? " ✓" : "";
          opt.textContent = name + disabledTag + urlPart + credPart;
          selectEl.append(opt);
        }
        if (prevVal && peerNames.includes(prevVal)) {
          selectEl.value = prevVal;
        } else if (enabledNames.length) {
          selectEl.value = enabledNames[0];
        } else {
          selectEl.value = peerNames[0];
        }
      }
    }

    if (discoverBtn) {
      discoverBtn.disabled = !peerNames.length;
    }

    if (editorEl && (forceEditor || (!a2aEditorDirty && document.activeElement !== editorEl))) {
      editorEl.value = JSON.stringify(parsed.editableDoc, null, 2);
      a2aEditorDirty = false;
    }

    if (statusEl) {
      if (!peerNames.length) {
        statusEl.textContent =
          "尚未配置远端 A2A 对端智能体；可展开下方编辑 a2a.json 或运行 agent-lab a2a init。本节点 AgentCard 与 /a2a 端点默认就绪。";
      } else {
        const summaryLines = [
          "已配置 " +
            peerNames.length +
            " 个远端对端（已启用 " +
            enabledNames.length +
            " 个）",
        ];
        if (parsed.configPath) {
          summaryLines.push("配置文件：" + parsed.configPath);
        }
        summaryLines.push("选择对端并点击「发现 Card」可探测远端能力与技能。");
        statusEl.textContent = summaryLines.join("\n");
      }
    }
    return true;
  } catch (e) {
    if (selectEl && !latestA2AData) {
      selectEl.replaceChildren();
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "无法读取 A2A 对端配置";
      selectEl.append(opt);
    }
    if (discoverBtn) discoverBtn.disabled = true;
    if (statusEl) statusEl.textContent = "A2A 对端配置暂不可用：" + e.message;
    if (notifyError) toast("刷新 A2A 对端配置失败：" + e.message, "error");
    return false;
  }
}

if ($("a2a-editor-details")) {
  $("a2a-editor-details").addEventListener("toggle", () => {
    if (
      $("a2a-editor-details").open &&
      $("a2a-json-editor") &&
      !$("a2a-json-editor").value.trim()
    ) {
      loadA2APeers({ forceEditor: true }).catch(() => {});
    }
  });
}

if ($("a2a-json-editor")) {
  $("a2a-json-editor").addEventListener("input", () => {
    a2aEditorDirty = true;
  });
}

if ($("a2a-discover-btn")) {
  $("a2a-discover-btn").onclick = async () => {
    const peer = $("a2a-peer-select") ? $("a2a-peer-select").value.trim() : "";
    if (!peer) {
      toast("请先选择要发现 AgentCard 的远端 A2A 智能体", "warning");
      return;
    }
    $("a2a-discover-btn").disabled = true;
    if ($("a2a-status-text")) {
      $("a2a-status-text").textContent = "正在拉取 " + peer + " 的 AgentCard…";
    }
    try {
      const res = await api("/a2a/discover", {
        method: "POST",
        body: JSON.stringify({ peer }),
      });
      if (res && res.ok === false) {
        const errMsg = String(res.error || res.code || "未找到远端 AgentCard");
        if ($("a2a-status-text")) $("a2a-status-text").textContent = "发现失败：" + errMsg;
        toast("发现远端 AgentCard 失败：" + errMsg, "error");
        return;
      }
      if ($("a2a-status-text")) {
        $("a2a-status-text").textContent = formatA2ACardPreview(res, peer);
      }
      const cardName = res?.card?.name || res?.name || peer;
      toast("已发现远端 AgentCard：" + cardName, "success");
    } catch (e) {
      if ($("a2a-status-text")) $("a2a-status-text").textContent = "发现失败：" + e.message;
      toast("发现远端 AgentCard 失败：" + e.message, "error");
    } finally {
      if ($("a2a-discover-btn")) {
        $("a2a-discover-btn").disabled = !Boolean($("a2a-peer-select")?.value);
      }
    }
  };
}

if ($("a2a-save-btn")) {
  $("a2a-save-btn").onclick = async () => {
    const editor = $("a2a-json-editor");
    if (!editor) return;
    const raw = editor.value.trim();
    if (!raw) {
      toast("A2A 配置 JSON 不能为空", "warning");
      return;
    }
    let parsed;
    try {
      parsed = JSON.parse(raw);
    } catch (err) {
      const msg = "JSON 语法错误：" + err.message;
      if ($("a2a-status-text")) $("a2a-status-text").textContent = msg;
      toast(msg, "error");
      return;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      toast("A2A 配置根对象必须是合法 JSON 对象", "error");
      return;
    }
    if (!parsed.peers || typeof parsed.peers !== "object" || Array.isArray(parsed.peers)) {
      toast("A2A 配置必须包含对象类型的 peers 字段", "error");
      return;
    }
    $("a2a-save-btn").disabled = true;
    try {
      await api("/a2a/peers", {
        method: "PUT",
        body: JSON.stringify(parsed),
      });
      a2aEditorDirty = false;
      toast("A2A 远端智能体配置已保存", "success");
      await loadA2APeers({ forceEditor: true });
    } catch (e) {
      if ($("a2a-status-text")) $("a2a-status-text").textContent = "保存失败：" + e.message;
      toast("保存 A2A 配置失败：" + e.message, "error");
    } finally {
      if ($("a2a-save-btn")) $("a2a-save-btn").disabled = false;
    }
  };
}

if ($("a2a-refresh-btn")) {
  $("a2a-refresh-btn").onclick = async () => {
    $("a2a-refresh-btn").disabled = true;
    try {
      a2aEditorDirty = false;
      const ok = await loadA2APeers({ forceEditor: true, notifyError: true });
      if (ok) toast("已刷新 A2A 远端智能体配置", "info");
    } finally {
      if ($("a2a-refresh-btn")) $("a2a-refresh-btn").disabled = false;
    }
  };
}

function openA2APanel() {
  if (document.body.classList.contains("inspector-collapsed")) {
    toggleRightInspector();
  }
  switchInspectorTab("tools");
  $("a2a-card")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  $("a2a-peer-select")?.focus();
}

// Tab 3: 声明式权限规则与 Hooks 编辑器（GET/PUT /api/settings/rules）
async function loadDeclarativeRules() {
  if (!$("rules-scope-select")) return;
  const scope = $("rules-scope-select").value || "global";
  const wsParam = memoryScopeQueryParams();
  try {
    const data = await api("/settings/rules" + (wsParam ? "?" + wsParam : ""));
    const bucket =
      scope === "effective"
        ? data.effective?.permissions || {}
        : (data[scope]?.permissions || {});
    if ($("rules-allow-input")) $("rules-allow-input").value = (bucket.allow || []).join("\n");
    if ($("rules-ask-input")) $("rules-ask-input").value = (bucket.ask || []).join("\n");
    if ($("rules-deny-input")) $("rules-deny-input").value = (bucket.deny || []).join("\n");
    const ro = scope === "effective";
    for (const id of ["rules-allow-input", "rules-ask-input", "rules-deny-input"]) {
      if ($(id)) $(id).readOnly = ro;
    }
    if ($("rules-save-btn")) $("rules-save-btn").disabled = ro;
    const hooksObj = (scope === "effective" ? data.effective?.hooks : data[scope]?.hooks) || {};
    const hookKeys = Object.keys(hooksObj).filter((k) => (hooksObj[k] || []).length > 0);
    if ($("rules-hooks-summary")) {
      if (hookKeys.length) {
        $("rules-hooks-summary").classList.remove("hidden");
        $("rules-hooks-summary").textContent =
          "已配置生命周期 Hooks：" + hookKeys.map((k) => k + "(" + hooksObj[k].length + ")").join(" · ");
      } else {
        $("rules-hooks-summary").classList.add("hidden");
      }
    }
  } catch (e) {
    if ($("rules-status-text")) $("rules-status-text").textContent = "加载规则失败：" + e.message;
  }
}
if ($("rules-scope-select")) {
  $("rules-scope-select").onchange = () => loadDeclarativeRules();
}
if ($("rules-save-btn")) {
  $("rules-save-btn").onclick = async () => {
    const scope = $("rules-scope-select")?.value || "global";
    if (scope === "effective") return;
    const parseLines = (id) =>
      ($(id)?.value || "")
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean);
    $("rules-save-btn").disabled = true;
    try {
      await api("/settings/rules", {
        method: "PUT",
        body: JSON.stringify({
          scope,
          ...memoryScopePayload(),
          permissions: {
            allow: parseLines("rules-allow-input"),
            ask: parseLines("rules-ask-input"),
            deny: parseLines("rules-deny-input"),
          },
        }),
      });
      toast("已保存 " + scope + " 声明式权限规则", "success");
      await loadDeclarativeRules();
    } catch (e) {
      toast(e.message, "error");
    } finally {
      if ($("rules-save-btn")) $("rules-save-btn").disabled = false;
    }
  };
}

// Tab 3: 安全审计日志（GET /api/audit-logs）
async function loadAuditLogs() {
  const listEl = $("audit-logs-list");
  if (!listEl) return;
  try {
    const data = await api("/audit-logs?limit=30");
    const rows = Array.isArray(data.logs) ? data.logs : Array.isArray(data) ? data : [];
    listEl.replaceChildren();
    if (!rows.length) {
      listEl.append(el("div", "暂无安全审计记录。", "muted"));
      return;
    }
    for (const row of rows) {
      const card = el("div", undefined, "audit-item");
      const head = el("div", undefined, "audit-head");
      const outcome = String(row.outcome || "allowed");
      head.append(
        el("span", String(row.action || ""), "audit-action"),
        el("span", outcome, "audit-decision " + outcome),
      );
      const ts = row.created_at ? new Date(row.created_at).toLocaleTimeString("zh-CN", { hour12: false }) : "";
      card.append(
        head,
        el("div", (row.actor || "local") + " · " + (row.resource || "") + (ts ? " · " + ts : ""), "audit-meta"),
      );
      listEl.append(card);
    }
  } catch (e) {
    listEl.replaceChildren(el("div", "需要管理员权限或加载失败：" + e.message, "muted"));
  }
}
if ($("refresh-audit-btn")) $("refresh-audit-btn").onclick = () => loadAuditLogs();

// Tab 3: 工作区文件浏览器 + @file 缓存
async function loadWorkspaceFilesForRun(runId, query = "") {
  try {
    let data;
    if (runId) {
      data = await api("/runs/" + encodeURIComponent(runId) + "/files?limit=80" + (query ? "&q=" + encodeURIComponent(query) : ""));
    } else {
      const ws = $("workspace-path")?.value.trim() || "";
      data = await api("/workspaces/files?limit=80" + (ws ? "&workspace_path=" + encodeURIComponent(ws) : "") + (query ? "&q=" + encodeURIComponent(query) : ""));
    }
    cachedWsFiles = Array.isArray(data.files) ? data.files : [];
    renderWorkspaceFileList();
  } catch {
    cachedWsFiles = [];
  }
}
function renderWorkspaceFileList() {
  const box = $("ws-files-list");
  if (!box) return;
  const q = ($("ws-files-filter")?.value || "").trim().toLowerCase();
  const filtered = q
    ? cachedWsFiles.filter((f) => String(typeof f === "string" ? f : f.path || "").toLowerCase().includes(q))
    : cachedWsFiles;
  box.replaceChildren();
  if (!filtered.length) {
    box.append(el("div", "未找到匹配的工作区文件。", "muted"));
    return;
  }
  for (const item of filtered.slice(0, 60)) {
    const fpath = typeof item === "string" ? item : String(item.path || "");
    const btn = el("button", undefined, "ws-file-item");
    btn.type = "button";
    btn.append(el("span", fpath, "ws-file-path"), el("span", "插入 @", "ws-file-meta"));
    btn.onclick = () => {
      insertTextIntoActivePrompt("@" + fpath + " ");
      toast("已插入 @" + fpath, "info");
    };
    box.append(btn);
  }
}
if ($("ws-files-filter")) {
  $("ws-files-filter").oninput = () => renderWorkspaceFileList();
}

// 工作区目录选择器弹窗（GET /api/workspaces/browse）
async function openBrowseModal(startPath = "") {
  if (!$("browse-modal")) return;
  $("browse-modal").classList.remove("hidden");
  await refreshBrowseDir(startPath || $("workspace-path")?.value.trim() || "");
}
function closeBrowseModal() {
  if ($("browse-modal")) $("browse-modal").classList.add("hidden");
}
async function refreshBrowseDir(path) {
  if ($("browse-current-path")) $("browse-current-path").textContent = "加载中…";
  try {
    const data = await api("/workspaces/browse" + (path ? "?path=" + encodeURIComponent(path) : ""));
    browseCurrentPath = data.current_path || "";
    browseParentPath = data.parent_path || null;
    if ($("browse-current-path")) $("browse-current-path").textContent = browseCurrentPath;
    if ($("browse-up-btn")) $("browse-up-btn").disabled = !browseParentPath;
    if ($("browse-recent-list")) {
      $("browse-recent-list").replaceChildren();
      const recents = Array.isArray(data.recent_workspaces) ? data.recent_workspaces : [];
      if (!recents.length) {
        $("browse-recent-list").append(el("span", "暂无最近记录", "muted"));
      } else {
        for (const rPath of recents) {
          const chip = el("button", rPath, "browse-recent-chip");
          chip.type = "button";
          chip.onclick = () => refreshBrowseDir(rPath);
          $("browse-recent-list").append(chip);
        }
      }
    }
    if ($("browse-dir-list")) {
      $("browse-dir-list").replaceChildren();
      const dirs = Array.isArray(data.directories) ? data.directories : [];
      if (!dirs.length) {
        $("browse-dir-list").append(el("div", "此目录下无可选子目录", "muted"));
      } else {
        for (const d of dirs) {
          const row = el("button", undefined, "browse-dir-row");
          row.type = "button";
          row.append(el("span", "📁 " + d.name));
          if (d.is_git) row.append(el("span", "Git", "browse-git-badge"));
          row.onclick = () => refreshBrowseDir(d.path);
          $("browse-dir-list").append(row);
        }
      }
    }
  } catch (e) {
    toast("目录浏览失败：" + e.message, "error");
    closeBrowseModal();
  }
}
if ($("browse-cwd-btn")) $("browse-cwd-btn").onclick = () => openBrowseModal();
if ($("browse-close-btn")) $("browse-close-btn").onclick = closeBrowseModal;
if ($("browse-cancel-btn")) $("browse-cancel-btn").onclick = closeBrowseModal;
if ($("browse-up-btn")) {
  $("browse-up-btn").onclick = () => {
    if (browseParentPath) refreshBrowseDir(browseParentPath);
  };
}
if ($("browse-select-btn")) {
  $("browse-select-btn").onclick = () => {
    if (browseCurrentPath && $("workspace-path")) {
      $("workspace-path").value = browseCurrentPath;
      toast("已选择本地目录：" + browseCurrentPath, "success");
      loadMemoryOverview().catch(() => {});
      loadWorkspaceFilesForRun(null).catch(() => {});
    }
    closeBrowseModal();
  };
}

// 剪贴板贴图 (Ctrl/Cmd+V) + 拖拽文件附加至两个输入框
function bindPasteAndDrop(containerEl, textareaEl, targetList, chipsId) {
  if (textareaEl) {
    textareaEl.addEventListener("paste", (ev) => {
      const items = Array.from(ev.clipboardData?.items || []);
      const files = [];
      for (const item of items) {
        if (item.kind === "file") {
          const f = item.getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length) {
        ev.preventDefault();
        readFilesAsAttachments(files, targetList, chipsId);
        toast("已从剪贴板粘贴 " + files.length + " 个附件", "success");
      }
    });
  }
  if (containerEl) {
    containerEl.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      containerEl.classList.add("drop-target-active");
    });
    containerEl.addEventListener("dragleave", (ev) => {
      if (!containerEl.contains(ev.relatedTarget)) {
        containerEl.classList.remove("drop-target-active");
      }
    });
    containerEl.addEventListener("drop", (ev) => {
      ev.preventDefault();
      containerEl.classList.remove("drop-target-active");
      const files = ev.dataTransfer?.files;
      if (files && files.length) {
        readFilesAsAttachments(files, targetList, chipsId);
        toast("已拖入 " + files.length + " 个附件", "success");
      }
    });
  }
}
bindPasteAndDrop($("new-task-card"), $("prompt"), composerAttachments, "composer-attachments");
bindPasteAndDrop($("followup-card"), $("followup-prompt"), followupAttachments, "followup-attachments");

// 行内 / 技能与 @ 文件自动补全浮层
function hideAutocomplete() {
  activeAcTextarea = null;
  activeAcItems = [];
  if ($("prompt-autocomplete")) $("prompt-autocomplete").classList.add("hidden");
}
function renderAutocompletePopup() {
  const pop = $("prompt-autocomplete");
  if (!pop || !activeAcTextarea || !activeAcItems.length) {
    hideAutocomplete();
    return;
  }
  pop.replaceChildren();
  activeAcItems.forEach((it, idx) => {
    const row = el("button", undefined, "ac-item" + (idx === activeAcIndex ? " active" : ""));
    row.type = "button";
    row.append(el("span", it.insert, "ac-title"), el("span", it.desc || "", "ac-desc"));
    row.onmousedown = (ev) => {
      ev.preventDefault();
      applyAutocompleteItem(it);
    };
    pop.append(row);
  });
  const rect = activeAcTextarea.getBoundingClientRect();
  pop.style.left = Math.max(12, Math.min(rect.left, window.innerWidth - 340)) + "px";
  const below = rect.bottom + 6;
  if (below + 220 < window.innerHeight) {
    pop.style.top = below + "px";
    pop.style.bottom = "auto";
  } else {
    pop.style.top = "auto";
    pop.style.bottom = Math.max(12, window.innerHeight - rect.top + 6) + "px";
  }
  pop.classList.remove("hidden");
}
function applyAutocompleteItem(it) {
  if (!activeAcTextarea || !it) return;
  const val = activeAcTextarea.value;
  activeAcTextarea.value = val.slice(0, activeAcTokenStart) + it.insert + " " + val.slice(activeAcTokenEnd);
  const pos = activeAcTokenStart + it.insert.length + 1;
  activeAcTextarea.focus();
  activeAcTextarea.setSelectionRange(pos, pos);
  hideAutocomplete();
}
function handleAutocompleteInput(ta) {
  const pos = ta.selectionStart ?? ta.value.length;
  const before = ta.value.slice(0, pos);
  const m = before.match(/(?:^|\s)([/@][A-Za-z0-9_./-]{0,80})$/);
  if (!m) {
    hideAutocomplete();
    return;
  }
  const tokenStr = m[1];
  activeAcTextarea = ta;
  activeAcTokenStart = pos - tokenStr.length;
  activeAcTokenEnd = pos;
  activeAcIndex = 0;
  if (tokenStr.startsWith("/")) {
    const q = tokenStr.slice(1).toLowerCase();
    const builtins = [
      { insert: "/plan", desc: "进入或退出只读规划模式 (Plan)" },
      { insert: "/compact", desc: "压缩当前会话早期上下文" },
      { insert: "/rewind", desc: "回退工作区与对话至检查点" },
      { insert: "/fork", desc: "从当前会话分叉创建新分支" },
    ];
    const skills = cachedSkillsCatalog.map((s) => ({
      insert: "/" + s.name,
      desc: String(s.description || "按需技能").slice(0, 60),
    }));
    activeAcItems = [...builtins, ...skills]
      .filter((it) => it.insert.slice(1).toLowerCase().includes(q))
      .slice(0, 8);
  } else {
    const q = tokenStr.slice(1).toLowerCase();
    if (!cachedWsFiles.length) loadWorkspaceFilesForRun(current).catch(() => {});
    activeAcItems = cachedWsFiles
      .map((f) => (typeof f === "string" ? f : String(f.path || "")))
      .filter((p) => p.toLowerCase().includes(q))
      .slice(0, 8)
      .map((p) => ({ insert: "@" + p, desc: "工作区文件" }));
  }
  renderAutocompletePopup();
}
function handleAutocompleteKeydown(ev, ta) {
  const pop = $("prompt-autocomplete");
  if (!pop || pop.classList.contains("hidden") || activeAcTextarea !== ta || !activeAcItems.length) {
    return false;
  }
  if (ev.key === "ArrowDown") {
    ev.preventDefault();
    activeAcIndex = (activeAcIndex + 1) % activeAcItems.length;
    renderAutocompletePopup();
    return true;
  }
  if (ev.key === "ArrowUp") {
    ev.preventDefault();
    activeAcIndex = (activeAcIndex - 1 + activeAcItems.length) % activeAcItems.length;
    renderAutocompletePopup();
    return true;
  }
  if (ev.key === "Enter" || ev.key === "Tab") {
    ev.preventDefault();
    applyAutocompleteItem(activeAcItems[activeAcIndex]);
    return true;
  }
  if (ev.key === "Escape") {
    ev.preventDefault();
    hideAutocomplete();
    return true;
  }
  return false;
}
for (const taId of ["prompt", "followup-prompt"]) {
  if ($(taId)) {
    $(taId).addEventListener("input", () => handleAutocompleteInput($(taId)));
    $(taId).addEventListener("blur", () => setTimeout(hideAutocomplete, 150));
  }
}

// ⌘K / Ctrl+K 命令面板 + 帮助弹窗 + 跳到底部浮钮 + 全局键盘快捷键
let cmdItems = [];
let cmdActiveIdx = 0;
function openCmdPalette() {
  if (!$("cmd-modal")) return;
  $("cmd-modal").classList.remove("hidden");
  if ($("cmd-search-input")) $("cmd-search-input").value = "";
  renderCmdResults("");
  $("cmd-search-input")?.focus();
}
function closeCmdPalette() {
  if ($("cmd-modal")) $("cmd-modal").classList.add("hidden");
}
function buildCmdEntries() {
  const base = [
    { group: "操作", title: "新建并行 Agent 任务", desc: "切回新建任务面板", kbd: "N", run: enterNewSessionMode },
    { group: "操作", title: "切换左侧会话栏", desc: "展开或收起左侧导航", kbd: "◧", run: toggleDesktopSidebar },
    { group: "操作", title: "切换右侧检视面板", desc: "释放主代码 Diff 宽度", kbd: "◫", run: toggleRightInspector },
    { group: "操作", title: "打开模型网关协议配置", desc: "切换多协议模型网关 Profile 与编辑 gateways.json", kbd: "GW", run: openGatewaysPanel },
    { group: "操作", title: "打开 A2A 协同智能体配置", desc: "查看本节点 AgentCard、发现远端智能体与编辑 a2a.json", kbd: "A2A", run: openA2APanel },
    { group: "操作", title: "打开帮助与快捷键指南", desc: "查看系统链路与快捷操作", kbd: "?", run: openHelpModal },
    { group: "操作", title: "配置身份认证会话", desc: "HttpOnly Cookie 登录 / 注销", kbd: "⚿", run: openAuthModal },
    { group: "操作", title: "立即运行 Dreaming 记忆整理", desc: "重放会话纠错与图谱修复", kbd: "↻", run: () => $("run-dream")?.click() },
    { group: "主题", title: "切换主题：松霜 (Editorial)", desc: "默认雅致亮色", kbd: "", run: () => applyTheme("editorial") },
    { group: "主题", title: "切换主题：暗夜 (Dark)", desc: "极客深色高对比", kbd: "", run: () => applyTheme("dark") },
    { group: "主题", title: "切换主题：深海 (Ocean)", desc: "科技深海蓝调", kbd: "", run: () => applyTheme("ocean") },
    { group: "主题", title: "切换主题：暖砂 (Warm)", desc: "柔和琥珀护眼", kbd: "", run: () => applyTheme("warm") },
  ];
  for (const r of cachedRunsForCmd.slice(0, 10)) {
    base.push({
      group: "会话",
      title: String(r.title || r.prompt || r.id).slice(0, 60),
      desc: (labels[r.status] || r.status) + " · " + r.id.slice(0, 8),
      kbd: "↵",
      run: () => selectRun(r.id),
    });
  }
  return base;
}
function renderCmdResults(query) {
  const list = $("cmd-results-list");
  if (!list) return;
  const q = String(query || "").trim().toLowerCase();
  cmdItems = buildCmdEntries().filter(
    (it) => !q || it.title.toLowerCase().includes(q) || it.desc.toLowerCase().includes(q) || it.group.toLowerCase().includes(q),
  );
  cmdActiveIdx = 0;
  list.replaceChildren();
  if (!cmdItems.length) {
    list.append(el("div", "无匹配命令或会话", "muted"));
    return;
  }
  let lastGroup = "";
  cmdItems.forEach((it, idx) => {
    if (it.group !== lastGroup) {
      lastGroup = it.group;
      list.append(el("div", it.group, "cmd-group-label"));
    }
    const btn = el("button", undefined, "cmd-item" + (idx === cmdActiveIdx ? " active" : ""));
    btn.type = "button";
    const left = el("div");
    left.append(el("div", it.title, "cmd-item-title"), el("div", it.desc, "cmd-item-desc"));
    btn.append(left);
    if (it.kbd) btn.append(el("kbd", it.kbd, "cmd-item-kbd"));
    btn.onclick = () => {
      closeCmdPalette();
      it.run();
    };
    list.append(btn);
  });
}
if ($("cmd-palette-btn")) $("cmd-palette-btn").onclick = openCmdPalette;
if ($("cmd-close-btn")) $("cmd-close-btn").onclick = closeCmdPalette;
if ($("cmd-search-input")) {
  $("cmd-search-input").oninput = (ev) => renderCmdResults(ev.target.value);
  $("cmd-search-input").addEventListener("keydown", (ev) => {
    if (!cmdItems.length) return;
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      cmdActiveIdx = (cmdActiveIdx + 1) % cmdItems.length;
      for (const [i, n] of Array.from($("cmd-results-list").querySelectorAll(".cmd-item")).entries()) {
        n.classList.toggle("active", i === cmdActiveIdx);
      }
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      cmdActiveIdx = (cmdActiveIdx - 1 + cmdItems.length) % cmdItems.length;
      for (const [i, n] of Array.from($("cmd-results-list").querySelectorAll(".cmd-item")).entries()) {
        n.classList.toggle("active", i === cmdActiveIdx);
      }
    } else if (ev.key === "Enter") {
      ev.preventDefault();
      const chosen = cmdItems[cmdActiveIdx];
      closeCmdPalette();
      if (chosen) chosen.run();
    }
  });
}

function openHelpModal() {
  if ($("help-modal")) $("help-modal").classList.remove("hidden");
}
function closeHelpModal() {
  if ($("help-modal")) $("help-modal").classList.add("hidden");
}
if ($("help-guide-btn")) $("help-guide-btn").onclick = openHelpModal;
if ($("help-close-btn")) $("help-close-btn").onclick = closeHelpModal;
if ($("help-ok-btn")) $("help-ok-btn").onclick = closeHelpModal;

// 弹窗内 Enter 直接提交 + 点击遮罩关闭 + 全局 Esc / Cmd+K
if ($("auth-token-input")) {
  $("auth-token-input").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      $("auth-login-btn")?.click();
    }
  });
}
if ($("rename-title-input")) {
  $("rename-title-input").addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      $("rename-save-btn")?.click();
    }
  });
}
for (const [modalId, closeFn] of [
  ["auth-modal", closeAuthModal],
  ["rename-modal", closeRenameModal],
  ["browse-modal", closeBrowseModal],
  ["cmd-modal", closeCmdPalette],
  ["confirm-modal", closeConfirmModal],
  ["wide-modal", closeWideModal],
  ["help-modal", closeHelpModal],
]) {
  if ($(modalId)) {
    $(modalId).addEventListener("click", (ev) => {
      if (ev.target === $(modalId)) closeFn();
    });
  }
}
document.addEventListener("keydown", (ev) => {
  if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "k") {
    ev.preventDefault();
    if ($("cmd-modal") && !$("cmd-modal").classList.contains("hidden")) {
      closeCmdPalette();
    } else {
      openCmdPalette();
    }
    return;
  }
  if (ev.key === "Escape") {
    hideAutocomplete();
    closeCmdPalette();
    closeConfirmModal();
    closeWideModal();
    closeBrowseModal();
    closeHelpModal();
    closeAuthModal();
    closeRenameModal();
    setSidebarOpen(false);
  }
});
if ($("jump-bottom-btn")) {
  $("jump-bottom-btn").onclick = () => {
    maybeStickBottom(document.documentElement, { force: true, markJumpBtn: true });
  };
  window.addEventListener("scroll", () => {
    if (isNearBottom(document.documentElement, 80)) {
      $("jump-bottom-btn").classList.add("hidden");
      $("jump-bottom-btn").classList.remove("has-new");
    }
  });
}

function applyConfigSnapshot(c) {
  latestConfigSnapshot = c;
  cachedSkillsCatalog = Array.isArray(c.skills) ? c.skills : [];
  const wireProtocol = extractWireProtocol(c);
  const gatewayProfile = String(
    c.gateway_profile ||
      c.active_gateway_profile ||
      c.gateway?.profile ||
      c.active_gateway?.profile ||
      "",
  ).trim();
  $("provider").textContent = formatProviderDisplay(c);
  const providerTips = [
    gatewayProfile ? "Profile: " + gatewayProfile : "",
    wireProtocol ? "协议: " + wireProtocol : "",
    c.default_model ? "默认模型: " + c.default_model : "",
    c.gateway?.base_url || c.active_gateway?.base_url
      ? "端点: " + (c.gateway?.base_url || c.active_gateway?.base_url)
      : "",
  ].filter(Boolean);
  $("provider").title = providerTips.join(" | ");
  if ($("model-select")) {
    const sel = $("model-select");
    const prev = sel.value;
    sel.replaceChildren();
    const defLabel = c.default_model ? "默认模型 (" + c.default_model + ")" : "默认模型";
    const defOpt = document.createElement("option");
    defOpt.value = "";
    defOpt.textContent = defLabel;
    sel.append(defOpt);
    for (const m of c.available_models || []) {
      const mId = typeof m === "string" ? m : m?.id || m?.name || "";
      if (!mId) continue;
      const opt = document.createElement("option");
      opt.value = mId;
      opt.textContent = typeof m === "object" && m.label ? m.label : mId;
      sel.append(opt);
    }
    if (prev && Array.from(sel.options).some((o) => o.value === prev)) {
      sel.value = prev;
    }
  }
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
    ...cachedSkillsCatalog.map((s) => {
      const chip = el("button", "/" + s.name, "skill-chip");
      chip.type = "button";
      chip.title = (s.description || "") + "（点击插入到输入框）";
      chip.onclick = () => {
        insertTextIntoActivePrompt("/" + s.name + " ");
        toast("已插入技能 /" + s.name, "info");
      };
      return chip;
    }),
  );
  if (c.host_capabilities?.default_workspace && $("workspace-path") && !$("workspace-path").value) {
    $("workspace-path").value = c.host_capabilities.default_workspace;
  }
  if ($("demo-note")) {
    if (c.provider && c.provider !== "demo" && wireProtocol !== "demo") {
      $("demo-note").textContent =
        "真实模型已就绪 (" +
        formatProviderDisplay(c) +
        ") · 支持流式输出、多轮对话流、Checkpoints 回退、⌘K 命令面板与 / @ 补全";
    } else {
      $("demo-note").textContent =
        "DEMO 为确定性内置场景；真实模型接入可在「工具规则 → 模型网关协议配置」切换或由服务端配置。支持 / 技能补全与 @ 工作区文件引用。";
    }
  }
}

async function init() {
  try {
    const c = await api("/config");
    applyConfigSnapshot(c);
    await Promise.all([loadRuns(), loadMemory(), loadGatewaysConfig(), loadA2APeers()]);
    api("/runs?limit=30")
      .then((rs) => {
        if (Array.isArray(rs)) cachedRunsForCmd = rs;
      })
      .catch(() => {});
  } catch (e) {
    toast(e.message, "error");
    $("provider").textContent = "连接失败 · 点击钥匙配置会话认证";
  }
}
function applyTheme(themeName) {
  const valid = ["editorial", "dark", "ocean", "warm"];
  const chosen = valid.includes(themeName) ? themeName : "editorial";
  document.documentElement.dataset.theme = chosen;
  try {
    localStorage.setItem("agent_lab_theme", chosen);
  } catch {}
  for (const btn of document.querySelectorAll(".theme-btn")) {
    btn.classList.toggle("active", btn.dataset.theme === chosen);
  }
}
for (const btn of document.querySelectorAll(".theme-btn")) {
  btn.onclick = () => applyTheme(btn.dataset.theme);
}
try {
  applyTheme(localStorage.getItem("agent_lab_theme") || "editorial");
} catch {
  applyTheme("editorial");
}
init();
