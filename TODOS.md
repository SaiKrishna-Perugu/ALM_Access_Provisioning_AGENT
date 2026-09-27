# TODOS

Deferred from the /autoplan review of the local multi-agent run (2026-09-26).
The work being done now (tasks T0–T27) is tracked in that review, not here;
this file holds only what was deliberately put off.

## Agents

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

### Eval suite that replays recorded TEST runs (2026-09-27)

`python src/agent_eval.py` grades five built-in scenarios, and runs saved with
`agent_local.py --record`, against the simulated estate with the real model:
outcomes plus five safety invariants. **Still open:** record real TEST runs
(needs the client network), then replay them after every prompt or model change.

### Changelog and SQLite schema versioning (2026-09-27)

`CHANGELOG.md`, and an `alm_schema_version` table with forward migrations in
`alm_core/store/sqlite.py` (`MIGRATIONS`). A ledger from newer code is refused.
