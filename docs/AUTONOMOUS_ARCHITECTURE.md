# Autonomous ALM Provisioning — Architecture

> Implements [docs/Autonoums Agent Plan.md](Autonoums%20Agent%20Plan.md).
> Status: **code complete, never executed.** Written without access to the
> intranet, the Google Cloud project, Cloud DNS or an Vertex AI deployment.
> Nothing here has run against EWM, JTS, GPT, Postgres, Pub/Sub or a model.
> Treat every claim below as a design statement, not a test result.
>
> This is a genuine multi-agent system: an LLM supervisor routes nine agents,
> each of which runs its own tool-calling loop and decides its own actions.
> Section 2 is the part to read before a security review - it describes exactly
> what the models can and cannot cause.

The existing CLI (`src/*.py`) is untouched and still works exactly as before.
This is an additive second implementation of the same operations, built for
unattended operation.

---

## 1. The shape of it

```mermaid
graph TD
    W[EWM webhook] --> API[FastAPI - alm_api]
    R[Reconciliation timer - 15 min] --> API
    API --> S{{Supervisor<br/>LLM router}}

    S <--> T[triage]
    S <--> E[extractor]
    S <--> V[validator]
    S <--> RO[risk_officer]
    S <--> P[provisioner]
    S <--> VF[verifier]
    S <--> EV[evidence_officer]
    S <--> C[closer]
    S <--> RM[remediator]

    RO -.requests.-> GATE{{Approval gate<br/>interrupt + checkpoint}}
    GATE -->|Chat card| H[Human approver]
    H -->|signed token| API
    GATE --> S

    T & E & V & RO & P & VF & EV & C & RM --> POL[Policy engine<br/>every tool call]
    POL --> TOOLS[alm_core.tools<br/>typed + idempotent]
    TOOLS --> DB[(Postgres:<br/>ledger, audit,<br/>memory, checkpoints)]
```

There are no fixed edges between agents. The supervisor reads what has been
established and chooses who acts next, so the sequence differs between runs: a
batch where every user is already active never reaches the provisioner, a run
that hits a transient failure visits the remediator, and an agent can hand
directly to a peer when it knows who should continue.

### Layers

| Package | Responsibility | Reasons? |
|---|---|---|
| `alm_api` | Webhook receiver, approval endpoints, run queries | No |
| `alm_agents` | Supervisor, nine agents, policy, memory, tool wrappers | **Yes** |
| `alm_core` | Contracts, tools, transport, persistence | **Never** |
| `alm_worker` | Domain-joined Windows worker for AD group changes | No |

`alm_core` importing the agent layer is a CI failure. The tool layer executes;
it does not decide.

---

## 2. The agents, and what constrains them

Nine agents, each with the narrowest toolset that lets it work. An agent asked
to do something outside its toolset has exactly one legal move: hand off.

| Agent | Decides | Can write? |
|---|---|---|
| `triage` | What is in scope, what looks unusual | No |
| `extractor` | Which user IDs are genuinely present, including in malformed fields | No |
| `validator` | The true registry state and risk of each user | No |
| `risk_officer` | Whether the batch should go to a human at all, and what to tell them | No |
| `provisioner` | Order and manner of the JTS and AD changes | **Yes** |
| `verifier` | Who actually holds the access | No |
| `evidence_officer` | What proof to capture and attach | **Yes** |
| `closer` | What each work item is told | **Yes** |
| `remediator` | Retry, escalate, or leave alone | No |

### Where safety lives now

The previous design kept the model out of the write path entirely. That is no
longer true, and pretending otherwise would be the dangerous version. Agents
call write tools directly. The guarantees moved to the tool boundary, where they
are mechanical rather than persuasive:

**`alm_agents/policy.py` checks every tool call before it runs.** A denial is
returned to the agent as an observation, not raised as an exception - an agent
told *"you may not write yet; call request_human_approval first"* adapts, which
is the behaviour we want. What it cannot do is proceed anyway.

The invariants, in evaluation order:

1. Writes are impossible in shadow mode.
2. Writes require an `ApprovalDecision` from a human covering that specific user.
3. Production writes require the run to carry a production confirmation.
4. A run has a write budget and a shared tool-call budget.
5. User IDs must match the estate's pattern - a hallucinated identifier never
   reaches LDAP.
6. No agent may write to the audit trail.

Underneath the policy, `guarded_write` still applies shadow mode, approval,
idempotency claim and audit to every write. An agent that somehow reached a
write tool without an approval would still be refused there. Two independent
layers, because one of them is new.

### What the model can and cannot cause

| | |
|---|---|
| **Can** | Choose which agent acts next, in what order, and how many times |
| **Can** | Decide which tools to call, with which arguments, and when to stop |
| **Can** | Hand work to another agent, request human approval, write memory |
| **Can** | Perform an approved write, on an approved user, once |
| **Cannot** | Write anything without a human approval naming that user |
| **Cannot** | Write in shadow mode, or beyond the run's write budget |
| **Cannot** | Grant itself or another agent a tool the roster did not give it |
| **Cannot** | Act on a user ID that does not match the estate's pattern |
| **Cannot** | Alter or suppress an audit row, or route around a denial |

### Bounds on autonomy

- **Hops** (`ALM_MAX_HOPS`, default 24) - routing decisions per run. A supervisor
  oscillating between two agents cannot do so indefinitely.
- **Iterations** - per agent run, from the roster definition.
- **Tool calls** (`ALM_MAX_TOOL_CALLS_PER_RUN`, default 400) - shared across all
  agents.
- **Writes** (`ALM_MAX_WRITES_PER_RUN`, default 50) - a hard ceiling regardless
  of what any agent concludes.
- **Wall clock** - per agent run.
- **Explicit termination** - "done" is a `finish` call an agent makes, not
  something inferred from silence.

### When the model is unavailable

The supervisor falls back to the nominal sequence, so a routing-model outage
degrades the system to the deterministic workflow rather than stopping it. The
agents themselves need a model; if none can be built, `ALM_ORCHESTRATION=deterministic`
runs the fixed graph with identical tools and guarantees.

---

## 3. What makes a write safe

`alm_core/tools/base.py::guarded_write` is the only way to write, and it enforces
four things in order:

| Guard | What it stops |
|---|---|
| Shadow mode | The pilot reads and plans; writes become recorded no-ops |
| Approval | A write with no human decision covering that user raises |
| Idempotency claim | A replayed webhook returns the original result instead of writing twice |
| Audit | Every attempt produces a row, including replays and failures |

The idempotency key is `sha256(work_item_id + userid + operation)` — deliberately
free of run ids and timestamps, so a redelivered webhook, a retried node and a
manual re-run all derive the *same* key and collapse into one write.

The ledger has three states, and the distinction matters: **claimed** (proceed),
**completed** (return the original result), **in flight** (another worker owns
it — unless the claim is older than the 15-minute lease, in which case that
worker died and we take over). A crash between claim and complete must not wedge
the pipeline forever, but nor may two workers race.

---

## 4. The approval gate

A LangGraph `interrupt()` backed by an `AsyncPostgresSaver`. State is
checkpointed on every superstep, so the container can restart while a run waits
four hours for a human, and the run resumes exactly where it stopped.

The gate is **binding**, not advisory:

- The batch is fingerprinted (`plan_hash`). A decision only unblocks the batch it
  was shown; if the queue moved while the approver was thinking, the hash no
  longer matches and the run goes back for re-approval. This is the cloud form
  of the defect that once swept two unreviewed work items into a production
  commit.
- Approvals expire (`ALM_APPROVAL_TTL_MINUTES`, default 4 hours).
- The card's buttons carry a signed token bound to one thread and one plan hash.
  It is not a bearer capability to approve anything else.
- The approver's identity comes from IAP (Identity-Aware Proxy headers). When it cannot
  be determined it is recorded as `unknown` rather than invented — an audit row
  naming the wrong person is worse than one admitting it does not know.
- `ALM_AUTO_APPROVE_LOW_RISK` can skip the wait, but **only** for a batch that
  creates and reactivates nothing. Provisioning always goes to a person.

---

## 5. Triggers

EWM/RTC has no first-class outbound webhook — this is open question 1 in the
plan, and it is still open. Both paths are implemented so the answer can change
without a rewrite:

- **Webhook** — `POST /webhooks/ewm`, HMAC over `timestamp.body`, a 5-minute
  timestamp window, and a delivery-id replay guard. Whatever bridges EWM to this
  endpoint (a follow-up action plugin, or an intermediary) must sign requests.
- **Reconciliation** — an in-process timer sweeps the whole active queue every
  15 minutes. This is the safety net, and it is why the Container App is pinned
  to `minReplicas: 1`: a replica scaled to zero stops sweeping.

The two overlap constantly by design. The ledger is what makes that harmless.

---

## 6. The Windows worker

GPT authenticates with Windows Kerberos SSO, which does not exist in a Linux
container. So the orchestrator does not perform the AD change — it enqueues a
job and records that it enqueued one.

`alm_worker` runs on a domain-joined Windows host as a gMSA or service account.
It launches its own browser with the Negotiate allowlist set, so there is no
`start-gpt.ps1` and no human logging in. It pulls from a Pub/Sub subscription and
authenticates with **Workload Identity Federation** — the host exchanges an
identity it already has for a short-lived Google token, so there is no
downloaded service account key on a machine outside the cloud perimeter. It
shares the same Cloud SQL ledger and audit table, so a job appears in one audit
trail regardless of which host performed it.

**A queued job is not a completed job.** GPT queues the AD change itself, and
clicking Modify clears the staging grid — the previous implementation re-read
that cleared grid and reported ten false failures while GPT reported zero. The
strongest synchronous claim available is "submitted"; the `JazzUsers` permission
poll is what proves access landed.

---

## 7. Evidence

Only VERIFIED users get evidence — a screenshot of a profile whose permission has
not propagated proves nothing.

Two independent signals, and neither can satisfy the other:

- **Per artifact** — the page must contain the target user ID in an `<input>`
  value.
- **Per batch** — N users must yield N distinct files.

If two artifacts are identical the capture mechanism is broken, so *nothing* is
uploaded: the remaining files cannot be trusted either, however plausible they
look. This is the check that would have stopped seventeen copies of the JTS
login page reaching production work items under a green report.

---

## 7a. Memory

Agents carry what previous runs learned, in two kinds kept deliberately apart:

- **Episodic** - a scoped fact about a subject. *"AB12345 was not found in LDAP
  on 2026-09-01."*
- **Semantic** - a generalisation an agent drew. *"Work items from this requester
  list users in the Justification field."*

Semantic memories are **hints, never authority**. The prompts require an agent to
confirm one with a live tool call before acting on it, and `supersede()` retires
a memory that a check has contradicted. A memory store that only accumulates
becomes a source of confident errors, which is worse than having no memory.

Nothing in memory can authorise a write.

---

## 8. Security posture

| Concern | Position |
|---|---|
| Identity | No downloaded service account keys anywhere. Cloud Run uses an attached service account, CI and the on-premises worker use Workload Identity Federation. CI fails if `google_service_account_key` appears in the Terraform |
| TLS | Verification **on** by default here (unlike the CLI). The corporate CA is baked into the image; `ALM_TLS_INSECURE` is refused when the environment is PROD |
| Secrets | Secret Manager via service account. Resolution order: environment → Secret Manager → interactive (unreachable in a container) |
| Credential in memory | Cached for 15 minutes, never written to disk, never in a log — `alm_core/logging.py` redacts by key name in a processor, not at the call site |
| PII to the LLM | `redact_pii()` strips e-mail addresses and names before any prompt; deterministic parsing runs first so most work items never reach a model |
| Network | No public ingress. Private endpoints for Secret Manager, Postgres, Pub/Sub and Artifact Registry. Corporate estate over Interconnect/HA VPN |
| Database | No password for the app — Workload Identity. Admin credentials are for migrations only |
| Audit | Append-only by construction: no UPDATE or DELETE statement exists for `alm_audit`, and the app role should be granted INSERT and SELECT only |
| Rate limiting | Bounded concurrency plus a circuit breaker per server. EWM and JTS are shared systems; a retry storm from an agent is somebody else's outage |

---

## 8a. What runs where

| Service | Configuration | Why this one |
|---|---|---|
| **Cloud Run** | Direct VPC egress, `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`, min 1 instance, `cpu_idle = false` | Serverless containers with VPC attachment; no cluster to operate. Two settings are load-bearing: min 1 because the reconciliation sweep is an in-process timer, and CPU-always-allocated because Cloud Run otherwise throttles CPU between requests and would freeze both that timer and any run waiting on a 30-minute permission poll |
| **Cloud SQL for PostgreSQL 16** | Private IP only, IAM database auth, PITR, 35 backups | Ledger, audit, approvals, agent memory and the LangGraph checkpointer — one store, transactional. IAM auth means there is no database password to store or rotate |
| **Pub/Sub** | 600s ack deadline, dead-letter topic, 5 delivery attempts | Bridge to the Windows worker. The long deadline is because the consumer drives a browser; the worker extends the lease while a job runs |
| **Secret Manager** | Three secrets, per-secret IAM, mounted as a file | The password is mounted rather than injected as an environment variable — a process listing exposes an environment |
| **Vertex AI** | Gemini by default; Claude via Model Garden | The agent and supervisor models. Application Default Credentials, so no API key exists |
| **Artifact Registry** | Immutable tags, keep 20 versions | Immutable tags stop a tag moving under a running revision |
| **Cloud DNS** | Forwarding zone for the corporate domain + private `googleapis.com` zone | Both directions are needed; getting one right and not the other is the classic silent failure |
| **Cloud Interconnect / HA VPN** | Consumed, never created | The circuit is a network-team asset with its own lifecycle. Terraform names the Cloud Router; it cannot destroy the attachment |
| **IAP** | In front of the internal load balancer | Authenticates the approver before the request reaches the service, which is what makes the approver field in the audit row trustworthy |
| **Cloud Logging / Trace / Monitoring** | Structured JSON, correlation ids | Operational telemetry. The audit trail lives in Cloud SQL |

### The DNS trap, stated plainly

The container must resolve two different things privately, and the configuration
is different for each:

1. `*.intra.chrysler.com` → a **Cloud DNS forwarding zone** pointing at the
   corporate resolvers, with `forwarding_path = PRIVATE` so the query goes over
   the interconnect rather than the internet.
2. `*.googleapis.com` → a **private zone** CNAMEd to `restricted.googleapis.com`,
   plus Private Google Access on the subnet and a route to `199.36.153.4/30`.

Configure one and not the other and you get a service that works from the
console and times out from the application.

---

## 9. Configuration

All settings are `ALM_`-prefixed and validated once at startup
(`alm_core/config.py`). The ones that decide behaviour:

| Variable | Default | Meaning |
|---|---|---|
| `ALM_ENVIRONMENT` | `TEST` | `TEST` or `PROD`. Never inferred in the cloud |
| `ALM_SHADOW_MODE` | `true` | Read and plan, write nothing |
| `ALM_CA_BUNDLE` | `/etc/ssl/certs/corporate-ca.pem` | Corporate CA; TLS verifies when present |
| `GOOGLE_CLOUD_PROJECT` | — | Project id. Cloud Run sets it; required for agentic mode |
| `ALM_REGION` | `europe-west1` | Cloud Run, Cloud SQL and Vertex AI |
| `ALM_POSTGRES_DSN` | — | Cloud SQL private IP. Required to write |
| `ALM_POSTGRES_IAM_AUTH` | `true` | IAM token as the password; no stored secret |
| `ALM_PUBSUB_TOPIC` | `alm-ad-provisioning` | AD jobs for the Windows worker |
| `ALM_AGENT_MODEL` | `gemini-2.0-flash` | A `claude-*` id switches to Model Garden |
| `ALM_LLM_ENABLED` | `true` | False disables extraction fallback and drafting |
| `ALM_APPROVAL_TTL_MINUTES` | `240` | How long an approval stays valid |
| `ALM_AUTO_APPROVE_LOW_RISK` | `false` | Phase 9 step 3; never covers provisioning |
| `ALM_RECONCILE_INTERVAL_MINUTES` | `15` | Queue sweep interval |
| `ALM_ORCHESTRATION` | `agentic` | `agentic` or `deterministic` |
| `ALM_MAX_HOPS` | `24` | Routing decisions per run |
| `ALM_MAX_WRITES_PER_RUN` | `50` | Hard write ceiling |
| `ALM_MAX_TOOL_CALLS_PER_RUN` | `400` | Shared tool-call budget |
| `ALM_AGENT_TEMPERATURE` | `0.0` | Agent sampling; routing is always 0 |
| `ALM_SUPERVISOR_MODEL` | (agent model) | Optional cheaper model for routing |

Two combinations are rejected at startup rather than at first write:
`ALM_TLS_INSECURE` with `ALM_ENVIRONMENT=PROD`, and `shadow_mode=false` with no
`ALM_POSTGRES_DSN`.

---

## 10. Plan coverage

| Phase | Status |
|---|---|
| 0 — Foundation refactor | `alm_core`: config, auth, oslc, logging, errors, credentials |
| 1 — Tool layer and contracts | `alm_core/models.py`, `alm_core/tools/*`, idempotency ledger |
| 2 — LangGraph orchestration | `alm_agents/agentic.py` (supervisor loop) and `alm_agents/graph.py` (deterministic fallback), Postgres checkpointer, `interrupt()` |
| 3 — Approval surface | `alm_api`: webhook, approval endpoints, Chat card, HTML fallback |
| 4 — Containerisation and IaC | `Dockerfile`, `infra/*.terraform`, `.github/workflows/deploy.yml` |
| 5 — Google Cloud networking | Terraform: Direct VPC egress, Private Service Connect and Private Google Access, DNS zones, Interconnect attachment. **The circuit itself is the network team's** |
| 6 — Windows Kerberos worker | `alm_worker`: Pub/Sub consumer, gMSA, dead-lettering, heartbeat |
| 7 — Triggers | Webhook with HMAC + replay protection; 15-minute reconciliation |
| 8 — Hardening | Structured logs, redaction, circuit breakers, append-only audit, [RUNBOOK](RUNBOOK.md) |
| 9 — Pilot rollout | `ALM_SHADOW_MODE` (default on) and `ALM_AUTO_APPROVE_LOW_RISK` |

### Beyond the plan

The plan described nine agents in a fixed graph. What is built is a genuine
multi-agent system: an LLM supervisor routes them, each runs its own
tool-calling loop, they hand off to each other directly, and they share a memory
of previous runs. `alm_agents/policy.py`, the roster's per-agent toolsets and the
hop/write/tool-call budgets are the machinery that makes that safe, and none of
them were in the plan.

### Not done, and deliberately so

- **Nothing has been executed.** No unit tests exist for the new packages and
  none were run. The offline suite still covers only the CLI. For the agentic
  layer specifically this means the prompts have never been exercised against a
  real model - prompt behaviour is the part most likely to need iteration, and
  it is the part with zero evidence behind it.
- **No evaluation harness.** A multi-agent system needs one: fixed scenarios
  (a malformed field, a user missing from LDAP, a mid-run failure) replayed
  against recorded tool responses, asserting the agents reach the right
  decisions. Without it, a prompt change is unverifiable. This is the first
  thing to build once a model endpoint exists.
- **Cost is unmeasured.** Every hop is a model call, and a 17-user batch may
  make tens of them. Budget the token spend on TEST before enabling this on a
  queue that runs continuously.
- **OpenTelemetry export** is configured through the Cloud Trace connection
  string but no spans are emitted yet; the structured logs carry the correlation
  ids in the meantime.
- **Postgres Workload Identity auth** is wired in the Terraform DSN but the token
  provider is not implemented in `PostgresStore` — the first deployment will
  need either a password DSN or `google-auth` token plumbing added there.
- **The audit table's grants** are described but not applied; `alm_audit` is
  append-only by construction, not yet by permission.
- **Open question 2 (Microsoft Graph instead of the GPT UI)** is untouched.
  Option C in the plan — modifying AD groups through Graph or LDAP — would
  delete `alm_worker` entirely and is the better long-term answer.

---

## 11. Running it

```bash
pip install -r requirements-cloud.txt
export PYTHONPATH=src

# Connectivity only - read-only, no model, no writes.
export ALM_ENVIRONMENT=TEST ALM_ORCHESTRATION=deterministic ALM_LLM_ENABLED=false
export EWM_SERVER=https://prssetst.intra.chrysler.com/ccm
export JTS_SERVER=https://prssetst.intra.chrysler.com/jts
export CID=<service account> EWM_PASSWORD=<prompted or injected>
python -m alm_core.smoke

# Agentic, shadow mode: the agents run and plan, and write nothing.
export ALM_ORCHESTRATION=agentic ALM_SHADOW_MODE=true
export GOOGLE_CLOUD_PROJECT=<project-id>
export ALM_REGION=europe-west1
export ALM_AGENT_MODEL=gemini-2.0-flash
uvicorn alm_api.main:app --port 8080

python -m alm_worker.main             # on the Windows host
```

Bring it up in this order, and do not skip a step:

1. `python -m alm_core.smoke` from inside the deployed container. If DNS or TLS
   fails there, the Cloud DNS private zones or the Interconnect attachment are missing and
   nothing else will work.
2. **Agentic in shadow mode.** Every write becomes a recorded no-op, so this
   exercises the supervisor's routing, the agents' tool use and the policy
   denials with no way to affect the estate. Read the audit trail: `step` values
   of `supervisor` and `agent:<name>` show exactly what each one decided and why.
3. **Diff a shadow run against the manual outcome** for the same work items. If
   the agents reach different conclusions than an operator would, fix that before
   giving them the ability to act on it.
4. Only then turn shadow mode off, on TEST, with a low `ALM_MAX_WRITES_PER_RUN`.

The audit trail is the observability story for this system. Every routing
decision, every tool call, every policy denial and every write is a row keyed by
`run_id`, so an agentic run can be reconstructed after the fact - which is the
only way to debug one.
