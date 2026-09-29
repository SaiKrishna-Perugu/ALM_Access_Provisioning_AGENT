// ALM Access Console. Everything the agents say is shown with textContent,
// never as HTML: work-item text is written by requesters, not by us.
"use strict";

const WRITE_TOOLS = new Set(["provision_jts_user", "reactivate_jts_user",
  "request_ad_group_membership", "post_workitem_comment", "attach_workitem_evidence"]);
const ROUTE = ["triage", "extractor", "validator", "risk_officer", "approval",
  "provisioner", "verifier", "evidence_officer", "closer", "remediator"];
const WORK_ITEM_RE = /(?<![\w-])(\d{4,10})(?![\w-])/g;

const state = { session: null, mode: "dry", current: null, source: null, visits: {} };
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
  if (!response.ok) throw new Error(data.error || `request failed (${response.status})`);
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
  document.querySelectorAll(".segmented button").forEach((b) =>
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
    done: "done", failed: "failed" };
  return el("span", `status ${status}`, labels[status] || status);
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
}

async function selectRun(id) {
  if (state.source) state.source.close();
  state.current = id;
  resetRunView();
  const run = await api(`/api/runs/${encodeURIComponent(id)}`);
  renderFacts(run);
  document.querySelectorAll("#runs button").forEach((b) => b.removeAttribute("aria-current"));
  refreshRuns();
  const source = new EventSource(`/api/runs/${encodeURIComponent(id)}/events`);
  state.source = source;
  source.onmessage = (message) => handle(JSON.parse(message.data));
  source.addEventListener("end", async () => {
    source.close();
    const done = await api(`/api/runs/${encodeURIComponent(id)}`);
    renderFacts(done);
    renderOutcome(done);
    refreshRuns();
  });
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
      });
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
      addEntry(d.halted ? "fail" : "end", (b) => b.append(el("div", "tl-title",
        d.halted ? `Stopped: ${d.halt_reason}` : "Run finished")));
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
  const actions = el("div", "decide");
  const approve = el("button", "approve", "Approve selected");
  const reject = el("button", "reject", "Reject");
  approve.type = reject.type = "button";
  const send = async (approved) => {
    const userids = [...list.querySelectorAll("input:checked")].map((i) => i.value);
    approve.disabled = reject.disabled = true;
    try {
      await api(`/api/runs/${encodeURIComponent(state.current)}/decision`,
        { method: "POST", body: { approved, userids } });
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
}

function renderFactsStatus(status) {
  $("tl-status").replaceChildren(statusPill(status));
  const first = $("facts").querySelector("dd");
  if (first) first.replaceChildren(statusPill(status));
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
  box.append(el("div", `banner ${report.halted ? "fail" : "ok"}`,
    report.halted ? `Stopped: ${report.halt_reason}` : done));
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
   ["seconds", m.wall_seconds !== undefined ? Math.round(m.wall_seconds) : undefined]]
    .forEach(([label, value]) => {
      const cell = el("div", "metric");
      cell.append(el("b", null, value ?? "–"), el("span", null, label));
      metrics.append(cell);
    });
  box.append(metrics);
}

// ------------------------------------------------------------------ boot
async function boot() {
  initTheme();
  state.session = await api("/api/session");
  const s = state.session;
  const env = $("env");
  env.classList.add(s.sandbox ? "env-sandbox" : s.environment === "PROD" ? "env-prod" : "env-test");
  $("env-text").textContent = s.sandbox ? `sandbox · ${s.environment}` : s.environment;
  $("model").textContent = `${s.model} · ${s.orchestration}`;
  $("operator").textContent = s.operator;
  $("confirm-word").textContent = s.confirm_word;
  $("confirm-env").textContent = s.sandbox ? "the simulated estate" : s.environment;
  examples();
  setMode("dry");
  renderRoute(null);
  document.querySelectorAll(".segmented button").forEach((b) =>
    b.addEventListener("click", () => setMode(b.dataset.mode)));
  $("prompt").addEventListener("input", renderScope);
  $("request").addEventListener("submit", submit);
  const runs = await refreshRuns();
  const active = runs.find((r) => ["starting", "running", "awaiting_approval"].includes(r.status));
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
