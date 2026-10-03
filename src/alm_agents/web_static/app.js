// ALM Access Console. Everything the agents say is shown with textContent,
// never as HTML: work-item text is written by requesters, not by us.
"use strict";

const WRITE_TOOLS = new Set(["provision_jts_user", "reactivate_jts_user",
  "request_ad_group_membership", "post_workitem_comment", "attach_workitem_evidence"]);
const ROUTE = ["triage", "extractor", "validator", "risk_officer", "approval",
  "provisioner", "verifier", "evidence_officer", "closer", "remediator"];
const WORK_ITEM_RE = /(?<![\w-])(\d{4,10})(?![\w-])/g;
const ACTIVE = ["starting", "running", "awaiting_approval", "stopping"];
// Trace filters, in the order a run meets them.
const SERVICES = ["run", "supervisor", "model", "agent", "tool", "ewm", "jts", "http", "auth",
  "gpt", "browser", "ledger", "approval", "log"];

const state = {
  session: null, mode: "dry", current: null, source: null, visits: {}, view: "activity",
  status: "", trace: { records: [], next: -1, filter: "all", timer: null, run: null },
};
const $ = (id) => document.getElementById(id);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (options.method && options.method !== "GET") headers["X-CSRF-Token"] = state.session.csrf;
  const response = await fetch(path, {
    method: options.method || "GET", headers, credentials: "same-origin",
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });
  let data = {};
  try { data = await response.json(); } catch (_) { /* empty body */ }
  if (!response.ok) {
    // The laptop console says {error}, the cloud API {detail}.
    throw new Error(data.error || data.detail || `request failed (${response.status})`);
  }
  return data;
}

// ----------------------------------------------------------------- theme
function applyTheme(theme) {
  if (theme) document.documentElement.setAttribute("data-theme", theme);
  else document.documentElement.removeAttribute("data-theme");
}
function initTheme() {
  let saved = null;
  try { saved = localStorage.getItem("alm-theme"); } catch (_) { /* storage blocked */ }
  applyTheme(saved);
  $("theme").addEventListener("click", () => {
    const dark = document.documentElement.getAttribute("data-theme") === "dark" ||
      (!document.documentElement.getAttribute("data-theme") &&
        matchMedia("(prefers-color-scheme: dark)").matches);
    const next = dark ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem("alm-theme", next); } catch (_) { /* storage blocked */ }
  });
}

// ------------------------------------------------------------- composer
function workItems(text) {
  return [...new Set([...text.matchAll(WORK_ITEM_RE)].map((m) => m[1]))];
}

function renderScope() {
  const ids = workItems($("prompt").value);
  const scope = $("scope");
  scope.replaceChildren();
  if (!ids.length) {
    scope.append(el("span", "muted", state.mode === "commit"
      ? "name 1-5 work items to write" : "the whole active queue"));
  } else {
    ids.forEach((id) => scope.append(el("span", "wi", id)));
  }
}

function setMode(mode) {
  state.mode = mode;
  document.querySelectorAll("button[data-mode]").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
  $("confirm-row").hidden = mode !== "commit";
  $("start").classList.toggle("write", mode === "commit");
  $("start").textContent = mode === "commit" ? "Start writing run" : "Start dry run";
  renderScope();
}

function examples() {
  const sandbox = state.session.sandbox;
  const list = sandbox
    ? ["Dry run work item 1001", "Process work items 1001 and 1002",
       "Check who on 1002 already has access"]
    : ["Dry run work item 123456", "Process the open requests in the queue",
       "Provision the users on work item 123456"];
  const box = $("examples");
  list.forEach((text) => {
    const chip = el("button", "chip", text);
    chip.type = "button";
    chip.addEventListener("click", () => { $("prompt").value = text; renderScope(); $("prompt").focus(); });
    box.append(chip);
  });
}

async function submit(event) {
  event.preventDefault();
  const error = $("form-error");
  error.hidden = true;
  $("start").disabled = true;
  try {
    const run = await api("/api/runs", { method: "POST", body: {
      prompt: $("prompt").value, mode: state.mode, confirm: $("confirm").value,
    } });
    $("confirm").value = "";
    await refreshRuns();
    selectRun(run.id);
  } catch (err) {
    error.textContent = err.message;
    error.hidden = false;
  } finally {
    $("start").disabled = false;
  }
}

// ----------------------------------------------------------------- runs
function statusPill(status) {
  const labels = { starting: "starting", running: "running", awaiting_approval: "needs approval",
    stopping: "stopping", stopped: "stopped", done: "done", failed: "failed" };
  return el("span", `status ${status}`, labels[status] || status);
}

// The Stop button shows while the selected run can still be stopped.
function can(role) {
  // The laptop console has one person, who may do everything.
  const roles = state.session && state.session.roles;
  return !roles || roles.includes(role) || roles.includes("admin");
}

function renderStop(status) {
  state.status = status;
  const stop = $("stop");
  stop.hidden = !ACTIVE.includes(status) || !can("operator");
  stop.disabled = status === "stopping";
  stop.textContent = status === "stopping" ? "Stopping…" : "Stop run";
}

async function stopRun() {
  if (!state.current) return;
  const stop = $("stop");
  stop.disabled = true;
  stop.textContent = "Stopping…";
  try {
    const answer = await api(`/api/runs/${encodeURIComponent(state.current)}/stop`,
      { method: "POST", body: {} });
    renderFactsStatus(answer.status);
  } catch (err) {
    renderStop(state.status);
    addEntry("fail", (b) => b.append(el("div", "tl-title", `Could not stop: ${err.message}`)));
  }
}

async function refreshRuns() {
  const { runs } = await api("/api/runs");
  const list = $("runs");
  list.replaceChildren();
  $("no-runs").hidden = runs.length > 0;
  runs.forEach((run) => {
    const item = el("li");
    const button = el("button");
    button.type = "button";
    if (state.current === run.id) button.setAttribute("aria-current", "true");
    const top = el("div", "r-top");
    top.append(el("span", `mode ${run.mode}`, run.mode === "commit" ? "write" : "dry run"),
      statusPill(run.status));
    button.append(top, el("div", "r-prompt", run.prompt),
      el("div", "r-time", new Date(run.created * 1000).toLocaleTimeString()));
    button.addEventListener("click", () => selectRun(run.id));
    item.append(button);
    list.append(item);
  });
  return runs;
}

function resetRunView() {
  $("timeline").replaceChildren();
  $("tl-empty").hidden = false;
  $("approval").hidden = true;
  $("outcome").hidden = true;
  state.visits = {};
  renderRoute(null);
  resetTrace();
}

async function selectRun(id) {
  if (state.source) state.source.close();
  state.current = id;
  resetRunView();
  const run = await api(`/api/runs/${encodeURIComponent(id)}`);
  renderFacts(run);
  document.querySelectorAll("#runs button").forEach((b) => b.removeAttribute("aria-current"));
  refreshRuns();
  if (state.view === "trace") loadTrace();
  const source = new EventSource(`/api/runs/${encodeURIComponent(id)}/events`);
  state.source = source;
  source.onmessage = (message) => handle(JSON.parse(message.data));
  source.addEventListener("end", async () => {
    source.close();
    const done = await api(`/api/runs/${encodeURIComponent(id)}`);
    renderFacts(done);
    renderOutcome(done);
    refreshRuns();
    if (state.view === "trace") loadTrace();
  });
}

// ---------------------------------------------------------------- views
function setView(view) {
  state.view = view;
  document.querySelectorAll("button[data-view]").forEach((b) =>
    b.setAttribute("aria-pressed", String(b.dataset.view === view)));
  $("activity-view").hidden = view !== "activity";
  $("trace-view").hidden = view !== "trace";
  if (view === "trace") loadTrace();
  else stopTracePolling();
}

// ---------------------------------------------------------------- trace
function resetTrace() {
  stopTracePolling();
  state.trace.records = [];
  state.trace.next = -1;
  state.trace.run = state.current;
  $("trace-list").replaceChildren();
  $("trace-path").textContent = "";
  $("trace-empty").hidden = false;
  const link = $("trace-download");
  link.hidden = !state.current;
  if (state.current) {
    link.href = `/api/runs/${encodeURIComponent(state.current)}/trace.jsonl`;
    link.setAttribute("download", "");
  }
  renderTraceFilters();
}

function stopTracePolling() {
  if (state.trace.timer) clearTimeout(state.trace.timer);
  state.trace.timer = null;
}

async function loadTrace() {
  stopTracePolling();
  const id = state.current;
  if (!id) return;
  try {
    const data = await api(`/api/runs/${encodeURIComponent(id)}/trace?after=${state.trace.next}`);
    if (id !== state.current) return;
    $("trace-path").textContent = `Saved to ${data.path}`;
    if (data.records.length) {
      state.trace.records.push(...data.records);
      state.trace.next = data.next;
      appendTrace(data.records);
      renderTraceFilters();
    }
    if (!data.done || data.records.length) {
      state.trace.timer = setTimeout(loadTrace, data.records.length ? 400 : 1500);
    }
  } catch (err) {
    $("trace-path").textContent = `Trace unavailable: ${err.message}`;
  }
}

function renderTraceFilters() {
  const counts = {};
  state.trace.records.forEach((r) => { counts[r.service] = (counts[r.service] || 0) + 1; });
  const box = $("trace-filters");
  box.replaceChildren();
  const chip = (name, label, n) => {
    const b = el("button", null, label);
    b.type = "button";
    b.setAttribute("aria-pressed", String(state.trace.filter === name));
    if (n !== undefined) b.append(el("span", "n", n));
    b.addEventListener("click", () => {
      state.trace.filter = name;
      renderTraceFilters();
      $("trace-list").replaceChildren();
      appendTrace(state.trace.records);
    });
    box.append(b);
  };
  chip("all", "all", state.trace.records.length);
  SERVICES.filter((s) => counts[s]).forEach((s) => chip(s, s, counts[s]));
  Object.keys(counts).filter((s) => !SERVICES.includes(s)).forEach((s) => chip(s, s, counts[s]));
}

function appendTrace(records) {
  const list = $("trace-list");
  const following = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
  const shown = records.filter((r) => state.trace.filter === "all" || r.service === state.trace.filter);
  if (state.trace.records.length) $("trace-empty").hidden = true;
  shown.forEach((r) => list.append(traceRow(r)));
  // A long run keeps the page responsive: the newest rows stay, the file has all.
  while (list.childElementCount > 4000) list.firstElementChild.remove();
  if (following) list.scrollTop = list.scrollHeight;
}

function traceRow(r) {
  const item = el("li", r.ok === false || r.denied ? "bad" : "");
  const details = el("details");
  const summary = el("summary");
  const ms = typeof r.ms === "number" ? (r.ms >= 1000 ? `${(r.ms / 1000).toFixed(1)}s` : `${r.ms}ms`) : "";
  summary.append(el("span", "t", `+${Number(r.t || 0).toFixed(1)}s`),
    el("span", `svc svc-${r.service}`, r.service), el("span", "what", traceSummary(r)),
    el("span", "ms", ms));
  details.append(summary);
  // The full record appears only when opened: some carry long observations.
  details.addEventListener("toggle", () => {
    if (details.open && details.childElementCount === 1) {
      details.append(el("pre", null, JSON.stringify(r, null, 2)));
    }
  });
  item.append(details);
  return item;
}

function traceSummary(r) {
  const s = r.service;
  if (s === "model") {
    const calls = (r.tool_calls || []).map((c) => c.name).join(", ");
    const tokens = r.input_tokens != null ? `  ${r.input_tokens}→${r.output_tokens} tokens` : "";
    return r.ok === false ? `${r.caller}: ${r.error}` : `${r.caller} → ${calls || "text"}${tokens}`;
  }
  if (s === "tool") {
    const args = Object.entries(r.args || {}).map(([k, v]) => `${k}=${typeof v === "string" ? v : JSON.stringify(v)}`).join(" ");
    return `${r.agent}.${r.tool}(${args})${r.denied ? "  DENIED" : ""}${r.write ? "  write" : ""}`;
  }
  if (s === "supervisor") return `hop ${r.hop} → ${r.next}${r.why ? ": " + r.why : ""}`;
  if (s === "http") {
    return `${r.system || ""} ${r.method || ""} ${r.url || ""} → ${r.status ?? r.error ?? ""}`;
  }
  if (s === "ledger") {
    return `${r.kind} ${r.operation || ""} ${r.userid || ""} wi ${r.work_item_id || "-"} ${r.outcome || r.reason || ""}`;
  }
  if (s === "log") return `[${r.level}] ${r.kind}`;
  if (s === "agent") return `${r.agent}: ${String(r.text || "").replace(/\s+/g, " ")}`;
  if (s === "approval") {
    return r.kind === "card" ? `card for ${(r.users || []).join(", ")}`
      : `${r.approved ? "approved" : "not approved"} by ${r.approver}: ${(r.userids || []).join(", ")}`;
  }
  if (["ewm", "jts", "gpt", "browser"].includes(s)) {
    const target = r.userid || r.work_item_id || r.target || (r.userids || []).join(", ") || "";
    const result = r.error || r.outcome || r.state || r.result || "";
    return `${r.kind} ${target}${result !== "" ? " → " + result : ""}${r.source === "simulated" ? "  (simulated)" : ""}`;
  }
  if (s === "run") {
    if (r.kind === "started" || r.kind === "session") {
      return `${r.kind}: ${r.data_source || r.source || ""} ${r.environment || ""} ${r.mode || ""}`;
    }
    return `${r.kind}${r.halt_reason ? ": " + r.halt_reason : ""}${r.error ? ": " + r.error : ""}`;
  }
  const rest = Object.fromEntries(Object.entries(r).filter(([k]) =>
    !["seq", "at", "t", "thread_id", "service", "kind", "ms", "ok"].includes(k)));
  return `${r.kind || ""} ${JSON.stringify(rest)}`;
}

// ---------------------------------------------------------------- route
function renderRoute(current) {
  const route = $("route");
  route.replaceChildren();
  ROUTE.forEach((name) => {
    const count = state.visits[name] || 0;
    const station = el("li", "station" + (name === "approval" ? " gate" : "") +
      (count ? " visited" : "") + (name === current ? " current" : ""));
    station.append(el("span", "stop"), el("span", "name", name.replace("_", " ")),
      el("span", "count", count > 1 ? `×${count}` : ""));
    route.append(station);
  });
}

function visit(name) {
  state.visits[name] = (state.visits[name] || 0) + 1;
  renderRoute(name);
}

// ------------------------------------------------------------- timeline
function addEntry(cls, build) {
  $("tl-empty").hidden = true;
  const timeline = $("timeline");
  // Follow the run only while the reader is at the bottom; never move the page.
  const following = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 80;
  const item = el("li", `tl ${cls}`);
  const rail = el("div", "rail");
  rail.append(el("span", "node"));
  const body = el("div");
  build(body);
  item.append(rail, body);
  timeline.append(item);
  if (following) timeline.scrollTop = timeline.scrollHeight;
}

// One readable line from an observation, which is often indented JSON.
function digest(text) {
  return text.replace(/\s+/g, " ").trim().slice(0, 160);
}

function handle(event) {
  const d = event.data || {};
  switch (event.kind) {
    case "run_started":
      addEntry("", (b) => {
        const title = el("div", "tl-title");
        title.append(el("b", null, d.sandbox ? "Sandbox run started" : "Run started"),
          document.createTextNode(` on ${d.environment} · ${Array.isArray(d.work_items)
            ? "work items " + d.work_items.join(", ") : d.work_items}`));
        b.append(title);
        b.append(el("div", "why", d.data_source === "live"
          ? `Live: reading work items from EWM ${d.ewm_host}; users from JTS ${d.jts_host}.`
          : "Simulated estate: made-up work items and users. Nothing reaches EWM, JTS or GPT."));
      });
      break;
    case "stop_requested":
      addEntry("stop", (b) => {
        b.append(el("div", "tl-title", `Stop requested by ${d.by}`));
        b.append(el("div", "why", "The run ends after its current step. A write in progress finishes first."));
      });
      renderFactsStatus("stopping");
      break;
    case "stopped":
      addEntry("stop", (b) => b.append(el("div", "tl-title", `Run stopped ${d.at}`)));
      break;
    case "supervisor": {
      const next = d.next || "";
      if (next && next !== "DONE") visit(next);
      addEntry("hop", (b) => {
        const title = el("div", "tl-title", `Hop ${d.hop} · Main agent → ${next === "DONE" ? "finish" : next.replace("_", " ")}`);
        if (d.guided) title.append(el("span", "tag fixed", "fixed order"));
        if (d.fallback) title.append(el("span", "tag fixed", "fallback"));
        b.append(title);
        if (d.why) b.append(el("div", "why", d.why));
        if (d.task && next !== "DONE") b.append(el("div", "why", `Task: ${d.task}`));
      });
      break;
    }
    case "tool_call": {
      const write = WRITE_TOOLS.has(d.tool);
      addEntry(d.denied ? "denied" : write ? "write" : "", (b) => {
        const title = el("div", "tl-title");
        title.append(el("span", "agent", d.agent), document.createTextNode("  "),
          el("span", "tool", d.tool));
        if (d.denied) title.append(el("span", "tag denied", "denied"));
        else if (write) title.append(el("span", "tag write", "write"));
        b.append(title);
        const args = Object.entries(d.args || {}).map(([k, v]) => `${k}=${v}`).join("  ");
        if (args) b.append(el("div", "args", args));
        if (d.observation) {
          const details = el("details", "obs");
          const text = String(d.observation);
          details.append(el("summary", null, digest(text)), el("pre", null, text));
          b.append(details);
        }
      });
      break;
    }
    case "agent_text":
      addEntry("", (b) => {
        b.append(el("div", "agent", d.agent), el("div", "said", d.text));
      });
      break;
    case "model_error":
      addEntry("denied", (b) => b.append(el("div", "tl-title", `${d.agent}: model error ${d.error}`)));
      break;
    case "log":
      addEntry("", (b) => b.append(el("div", "log", d.text)));
      break;
    case "approval_preview":
      visit("approval");
      addEntry("gate", (b) => b.append(el("div", "tl-title", "Approval card (dry run preview: nothing is written)")));
      renderApproval(d, true);
      break;
    case "approval_required":
      visit("approval");
      addEntry("gate", (b) => b.append(el("div", "tl-title", "Paused for your approval")));
      renderApproval(d, false);
      renderFactsStatus("awaiting_approval");
      break;
    case "approval_decided":
      addEntry("gate", (b) => b.append(el("div", "tl-title",
        d.approved ? `Approved by ${d.approver}: ${(d.userids || []).join(", ")}` : `Rejected by ${d.approver}`)));
      $("approval").hidden = true;
      renderFactsStatus("running");
      break;
    case "run_finished":
      renderRoute(null);
      addEntry(d.stopped ? "stop" : d.halted ? "fail" : "end", (b) => b.append(el("div", "tl-title",
        d.stopped ? `Run ended: ${d.halt_reason}` : d.halted ? `Halted: ${d.halt_reason}` : "Run finished")));
      break;
    case "run_failed":
      renderRoute(null);
      addEntry("fail", (b) => b.append(el("div", "tl-title", `Run failed: ${d.error}`)));
      break;
    default:
      break;
  }
}

// ------------------------------------------------------------ approval
function renderApproval(card, preview) {
  const box = $("approval");
  box.replaceChildren();
  box.classList.toggle("preview", preview);
  box.hidden = false;
  box.append(el("h3", null, preview ? "Approval card: preview" : "Approval required"));
  box.append(el("p", "reason", preview
    ? "A dry run shows the card a writing run would ask you to approve. Nothing is written."
    : (card.reason || "The main agent is paused. Approve only the users you have checked.")));
  if (!preview) {
    box.append(el("p", "reason",
      "Only ticked users are written; the rest are declined. High-risk users start unticked."));
  }
  const list = el("ul", "card-users");
  (card.items || []).forEach((item) => {
    const risk = String(item.risk || "").toLowerCase();
    const row = el("li", risk === "high" ? "high" : "");
    const box2 = document.createElement("input");
    box2.type = "checkbox";
    box2.value = item.userid;
    // A high-risk user needs a deliberate tick, never a default one.
    box2.checked = !preview && risk !== "high";
    box2.disabled = preview;
    box2.setAttribute("aria-label", `Approve ${item.userid}`);
    const who = el("span", "who", `${item.userid}${item.display_name ? " · " + item.display_name : ""}`);
    row.append(box2, who, el("span", "what",
      `${item.action || item.state} · work item ${(item.work_item_ids || []).join(", ") || "-"}`));
    const reasons = (item.risk_reasons || []).join("; ");
    row.append(el("span", "risk", `risk ${item.risk}${reasons ? " · " + reasons : ""}`));
    list.append(row);
  });
  box.append(list);
  if (preview) return;
  if (card.needed) {
    // The cloud console: the two-person rule, and who has voted.
    const votes = card.votes || [];
    const approvals = votes.filter((v) => v.approved).map((v) => v.approver);
    box.append(el("p", "reason", `${approvals.length} of ${card.needed} approval(s)` +
      (approvals.length ? `: ${approvals.join(", ")}` : "") +
      (card.needed > 1 ? ". Two different people, neither the one who started the run." : "")));
  }
  if (card.can_vote === false) {
    box.append(el("p", "muted", "Deciding needs the approver role."));
    return;
  }
  const actions = el("div", "decide");
  const approve = el("button", "approve", "Approve selected");
  const reject = el("button", "reject", "Reject");
  approve.type = reject.type = "button";
  const send = async (approved) => {
    const userids = [...list.querySelectorAll("input:checked")].map((i) => i.value);
    approve.disabled = reject.disabled = true;
    try {
      const answer = await api(`/api/runs/${encodeURIComponent(state.current)}/decision`,
        { method: "POST", body: { approved, userids } });
      if (answer.tally && !answer.tally.complete) {
        actions.remove();
        box.append(el("p", "reason",
          `Your decision is recorded. Waiting for ${answer.tally.needed - answer.tally.approvals.length} more approver(s).`));
      }
    } catch (err) {
      approve.disabled = reject.disabled = false;
      box.append(el("p", "form-error", err.message));
    }
  };
  approve.addEventListener("click", () => send(true));
  reject.addEventListener("click", () => send(false));
  actions.append(approve, reject);
  box.append(actions);
}

// ---------------------------------------------------------- facts, outcome
function renderFacts(run) {
  const facts = $("facts");
  facts.replaceChildren();
  const rows = [
    ["Status", statusPill(run.status)],
    ["Mode", el("span", `mode ${run.mode}`, run.mode === "commit" ? "write" : "dry run")],
    ["Scope", run.work_items.length ? run.work_items.join(", ") : "active queue"],
    ["Thread", el("span", "mono", run.thread_id)],
    ["Request", run.prompt],
  ];
  if (run.stopped_by) rows.push(["Stopped by", run.stopped_by]);
  if (run.error) rows.push(["Error", run.error]);
  rows.forEach(([label, value]) => {
    facts.append(el("dt", null, label));
    const dd = el("dd");
    if (value instanceof Node) dd.append(value); else dd.textContent = value;
    facts.append(dd);
  });
  $("tl-status").replaceChildren(statusPill(run.status));
  $("tl-mode").replaceChildren(el("span", `mode ${run.mode}`, run.mode === "commit" ? "write" : "dry run"));
  $("tl-thread").textContent = run.thread_id;
  renderStop(run.status);
}

function renderFactsStatus(status) {
  $("tl-status").replaceChildren(statusPill(status));
  const first = $("facts").querySelector("dd");
  if (first) first.replaceChildren(statusPill(status));
  renderStop(status);
  refreshRuns();
}

function renderOutcome(run) {
  const report = run.report;
  const box = $("outcome-body");
  box.replaceChildren();
  if (!report) { $("outcome").hidden = true; return; }
  $("outcome").hidden = false;
  const done = run.mode === "commit"
    ? "Completed. Every write below went through the policy check and the ledger."
    : "Dry run complete. Nothing was written; the activity shows what a writing run would do.";
  const stopped = run.status === "stopped";
  const reason = String(report.halt_reason || "");
  box.append(el("div", `banner ${stopped ? "stop" : report.halted ? "fail" : "ok"}`, stopped
    ? `${reason.charAt(0).toUpperCase()}${reason.slice(1)}. Nothing more was started; anything below was written before the stop and is in the ledger.`
    : report.halted ? `Halted: ${reason}` : done));
  const results = report.results || [];
  if (results.length) {
    const wrap = el("div", "scroll-x");
    const table = el("table", "results");
    const head = el("tr");
    ["User", "Operation", "Outcome", "WI"].forEach((h) => head.append(el("th", null, h)));
    table.append(head);
    results.forEach((r) => {
      const row = el("tr");
      row.append(el("td", "mono", r.userid), el("td", "mono", r.operation),
        el("td", `out-${r.outcome}`, r.replayed ? `${r.outcome} (replay)` : r.outcome),
        el("td", "mono", r.work_item_id || ""));
      table.append(row);
    });
    wrap.append(table);
    box.append(wrap);
  } else {
    box.append(el("p", "muted", "No writes."));
  }
  const m = report.metrics || {};
  const metrics = el("div", "metrics");
  [["model calls", m.model_calls], ["tool calls", m.tool_calls],
   ["tokens", m.tokens ? m.tokens.toLocaleString() : undefined],
   ["seconds", m.wall_seconds !== undefined ? Math.round(m.wall_seconds) : undefined]]
    .forEach(([label, value]) => {
      const cell = el("div", "metric");
      cell.append(el("b", null, value ?? "–"), el("span", null, label));
      metrics.append(cell);
    });
  box.append(metrics);
  if (report.version) {
    box.append(el("p", "muted version", `Prompts and models: ${report.version}`));
  }
}

// ------------------------------------------------------------------ boot
async function boot() {
  initTheme();
  state.session = await api("/api/session");
  const s = state.session;
  const env = $("env");
  const live = s.data_source === "live";
  // Which data the agents see must never be in doubt: live systems or made-up ones.
  env.classList.add(!live ? "env-sandbox" : s.environment === "PROD" ? "env-prod" : "env-live");
  $("env-text").textContent = live ? `live · ${s.environment}` : "simulated data";
  $("source-banner").hidden = live;
  if (live && (s.ewm_host || s.jts_host)) {
    $("hosts").hidden = false;
    $("hosts").textContent = [s.ewm_host, s.jts_host].filter(Boolean).join(" · ");
    $("hosts").title = `EWM ${s.ewm_host}, JTS ${s.jts_host}`;
  }
  $("model").textContent = `${s.model} · ${s.orchestration}`;
  $("operator").textContent = s.roles ? `${s.operator} · ${s.roles.join(", ")}` : s.operator;
  if (s.hosted && s.auth_mode === "oidc") $("signout").hidden = false;
  if (s.hosted) $("runs-label").textContent = "Recent runs";
  if (!can("operator")) {
    // A viewer or approver sees runs; starting them is the operator's.
    $("request").hidden = true;
    $("composer-help").hidden = true;
    $("ask").textContent = "Runs";
    $("composer-note").hidden = false;
  }
  if (s.hosted && !s.writes) $("write-mode").disabled = true;
  $("confirm-word").textContent = s.confirm_word;
  $("confirm-env").textContent = s.sandbox ? "the simulated estate" : s.environment;
  examples();
  setMode("dry");
  renderRoute(null);
  document.querySelectorAll("button[data-mode]").forEach((b) =>
    b.addEventListener("click", () => setMode(b.dataset.mode)));
  document.querySelectorAll("button[data-view]").forEach((b) =>
    b.addEventListener("click", () => setView(b.dataset.view)));
  $("stop").addEventListener("click", stopRun);
  $("prompt").addEventListener("input", renderScope);
  $("request").addEventListener("submit", submit);
  resetTrace();
  const runs = await refreshRuns();
  // A link from an approval announcement: <console>/?run=<thread-id>.
  const linked = new URLSearchParams(window.location.search).get("run");
  const active = runs.find((r) => r.id === linked) || runs.find((r) => ACTIVE.includes(r.status));
  if (active) selectRun(active.id);
  setInterval(() => { refreshRuns().catch(() => {}); }, 8000);
}

document.addEventListener("DOMContentLoaded", () => {
  boot().catch((err) => {
    const error = $("form-error");
    error.textContent = `Could not load the console: ${err.message}`;
    error.hidden = false;
  });
});
