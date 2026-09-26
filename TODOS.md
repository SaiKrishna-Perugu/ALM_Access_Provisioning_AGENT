# TODOS

Deferred from the /autoplan review of the local multi-agent run (2026-09-26).
The work being done now (tasks T0–T27) is tracked in that review, not here;
this file holds only what was deliberately put off.

## Agents

### Eval suite that replays recorded TEST runs

**What:** Record real TEST work items: the raw OSLC reads plus what the agents
decided. Replay them against the scripted estate whenever a prompt, the roster
or the model changes.

**Why:** Prompt and model changes are currently verified only by hand-run dry
runs. A regression (for example, the closer's wording, or a routing detour)
reaches a live work item before anyone notices.

**Context:** `tests/test_agentic_sandbox.py` already has a scripted-LLM harness
and `SandboxEstate`. The missing pieces are capturing real observations from
`out/local/run-*.json` and comparing outcomes (writes, comments, approval cards)
rather than exact wording. Start from the three TEST runs of 2026-09-25.

**Effort:** M
**Priority:** P2
**Depends on:** T6 (per-run metrics) helps with comparing runs

### Package the tool and policy layer as an MCP server

**What:** Expose the OSLC tools, the policy engine, the ledger and the evidence
capture as an MCP server that any orchestrator can call.

**Why:** If IBM Engineering AI Hub or another orchestrator arrives, the
durable assets are these guards and tools, not the nine-agent roster.

**Context:** `toolkit.build_registry` already defines typed tools with a
`Backend` protocol, and `PolicyEngine` checks every call. An MCP server would
wrap `ToolRegistry`, with policy checks kept on the server side.

**Effort:** L
**Priority:** P3
**Depends on:** T9 (run integrity), so the guards are complete before exposure

### Second approver and scheduled runs

**What:** Let someone other than the operator approve, through an EWM field or
state, a Teams card or an e-mail link, so a scheduled run can do everything up
to the approval gate unattended.

**Why:** Today the same person runs and approves at a terminal. That is
acceptable on TEST but weak for PROD, and it cannot run unattended.

**Context:** The cloud design already has an approval API and Chat cards
(`alm_api`). Locally, the LangGraph interrupt and the SQLite checkpoint already
support pausing for hours. What's missing is only the channel for the decision
and a Task Scheduler entry.

**Effort:** L
**Priority:** P2 (before any PROD use)
**Depends on:** T9

## Infrastructure

### Changelog and SQLite schema versioning

**What:** Add a CHANGELOG and a `schema_version` table with forward
migrations for `out/local/alm.db`.

**Why:** Schema changes to the ledger are currently silent. An older database
could misbehave after `git pull`.

**Context:** `alm_core/store/sqlite.py` creates its tables with
`CREATE TABLE IF NOT EXISTS`. Add a version row and a migration list checked in
`migrate()`.

**Effort:** S
**Priority:** P3
**Depends on:** None

### Merge or retire one of the two implementations

**What:** Converge the CLI (`src/*.py`, `out/audit/`) and the agents (SQLite
ledger) on one ledger, or retire the CLI once the agents are trusted.

**Why:** Two engines and two ledgers mean duplicate-write risk and double
maintenance. T5 only guards against the worst case, duplicate comments.

**Context:** Decide after about 20 TEST items processed by the agents, using the
per-run metrics (T6) against the CLI's timings.

**Effort:** L
**Priority:** P3
**Depends on:** T5, T6

## Completed
