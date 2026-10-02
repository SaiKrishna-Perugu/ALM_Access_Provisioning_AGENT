# Enterprise plan: from a laptop tool to a client-cloud AI system

Status: in progress. Written 2026-10-02 as a proposal; the table below tracks
what is built. Each milestone lands as one pull request whose description
gives the detail; what changed for operators is in [CHANGELOG.md](../CHANGELOG.md).

| Milestone | State |
|---|---|
| M1 per-run state (no shared runtime between runs) | Done |
| M2 store schema v2 (registry, queue, stop, leases, traces) on every store | Done |
| M3 run workers, queue-based API, scheduler lease | Done |
| M4 stop and traces in the cloud path | Done |
| M5 cloud-neutral adapters (secrets, DB tokens, models, AD transport) | Next |
| M6-M8 sign-in, roles, two approvers, service credentials | Planned |
| M9 directory port (Graph + GPT) | Planned |
| M10 AI governance, eval gate, cost caps | Planned |
| M11 OpenTelemetry | Planned |
| M12-M14 images, supply chain, DR, per-cloud IaC, pipeline | Planned |
| M15 PROD shadow and staged enablement | Needs the client network |

## 1. Where we are

What exists today, and is solid:

| Area | State |
|---|---|
| Agents | LangGraph supervisor + 9 tool-calling agents, guided orchestration by default, deterministic fallback |
| Safety | Policy engine on every tool call; `guarded_write` (shadow, approval, idempotency, audit); approval covers named users only; scope limits; PROD confirmation; budgets on hops, writes and tool calls |
| Durability | Checkpointed runs (SQLite locally, Postgres in the cloud design); resume after crash; idempotency ledger with replay |
| Control | Stop from the UI, Ctrl+C, or `--stop`; a write in progress always finishes |
| Visibility | Per-run JSONL trace of every model, tool, service, HTTP, ledger and log event; audit table |
| Quality | ~300 offline tests, eval suite (5 scenarios, record/replay of real runs), CI with security and secret scanning |
| Cloud design | GCP Terraform: Cloud Run, Cloud SQL (IAM auth), Pub/Sub, Secret Manager, Vertex AI, IAP, private DNS, no public ingress, WIF for CI and the worker |

What stops it from being an enterprise system:

| # | Gap | Why it matters |
|---|---|---|
| G1 | The cloud design is GCP-only, and the target cloud is undecided | A client may mandate AWS or Azure |
| G2 | One instance: in-process sweep timer, in-memory run locks, a local stop file | No horizontal scale, no high availability, a restart stalls the queue |
| G3 | The cloud API does not use stop control or traces; traces are local files | Operators cannot stop or see a cloud run |
| G4 | No enterprise sign-in or roles; the web console is localhost-only | Cannot serve a team; approver identity is weak |
| G5 | One person runs and approves | Unacceptable for PROD (segregation of duties) |
| G6 | The AD step drives the GPT web UI in a browser on a Windows worker | Fragile, slow, and needs a desktop session |
| G7 | The model is called with work-item text; no client sign-off on data use | Commonly a blocker in client security review |
| G8 | No OpenTelemetry export, SLOs, dashboards or alerts | No production operations |
| G9 | Two engines (CLI and agents) with two ledgers | Duplicate-write risk and double maintenance |
| G10 | No supply-chain controls (SBOM, signed images), DR plan or formal threat model | Standard enterprise gates |

## 2. Decisions needed before building

These shape everything else. Each has a recommendation.

| ID | Decision | Options | Recommendation |
|---|---|---|---|
| D1 | Target cloud | GCP (built) / AWS / Azure / client Kubernetes | Make the code cloud-neutral now (Phase 1). Ship on the cloud the client mandates; GCP is the fastest because it exists |
| D2 | Model provider and data policy | Vertex Gemini / AWS Bedrock / Azure OpenAI / client-hosted model / deterministic only | A provider inside the client's own cloud tenancy and region (Vertex, Bedrock or Azure OpenAI), with zero data retention. Get written sign-off on what work-item text may reach it. Keep deterministic mode as a no-model option |
| D3 | AD group membership | GPT web UI (today) / Microsoft Graph / LDAP / ITSM request | Microsoft Graph (or the client's IAM API). Deletes the Windows worker. If the client insists on GPT, keep the worker but isolate it |
| D4 | Identity | Client IdP over OIDC/SAML (Entra ID, Okta, Ping) | Client IdP with groups mapped to roles (section 5) |
| D5 | Approval model | One approver / two (four-eyes) / risk-based | Two approvers for PROD and for high-risk users; one for TEST |
| D6 | Tenancy | Single-tenant per client / shared multi-tenant | Single-tenant per client: one deployment in the client's account, simplest data boundary |
| D7 | CLI future | Keep both / retire the CLI / one shared ledger | Retire the CLI after ~20 PROD-like items run cleanly through the agents |
| D8 | Change management | None / ITSM (ServiceNow) ticket per writing run | Open or link an ITSM change for each writing run in PROD, if the client requires it |

## 3. Target architecture

```
                 Client IdP (OIDC)            ITSM (optional)        Chat/Teams/e-mail
                        |                           ^                      ^
 operators/approvers ---+--> WAF / internal LB --> Console + API (stateless, N replicas)
                                                     | enqueue run / stop / decide
                                                     v
 Scheduler (cloud cron) ---> /reconcile        Run queue (SQS / Pub/Sub / Service Bus)
 EWM webhook  -------------> /webhooks/ewm          |
                                                     v
                                      Run workers (N replicas, one run per slot)
                                      LangGraph + agents + policy + guarded_write
                       +--------------+----------+---------------+--------------+
                       v              v          v               v              v
               Postgres (ledger,  Model API   Secrets      EWM / JTS over   Directory API
               audit, approvals,  in client   manager      private link     (Graph) - or GPT
               checkpoints,       tenancy                  (OSLC)           worker (Windows)
               memory, stop flags)
                       |
             OpenTelemetry --> traces / metrics / logs --> client SIEM + dashboards + alerts
```

Principles:

- **Stateless front, stateful store.** The API and console hold no run state.
  Everything durable is in Postgres, so any replica can serve any request and a
  restart loses nothing.
- **Runs are jobs, not requests.** A run is enqueued and picked up by a worker.
  That separates scale for users (API) from scale for work (workers), and
  survives deploys.
- **One writer per work item.** A Postgres advisory lock (or lease row) per
  thread id replaces the in-memory lock, so two workers never drive the same run.
- **Same guards everywhere.** Policy, approval scope, idempotency and audit stay
  in the shared core; no deployment can bypass them.

### Cloud mapping (cloud-neutral by design)

| Capability | Port in code | GCP (exists) | AWS | Azure |
|---|---|---|---|---|
| Containers | Image | Cloud Run | ECS Fargate (or EKS) | Container Apps (or AKS) |
| Database | `Store` (exists) | Cloud SQL Postgres, IAM auth | RDS/Aurora Postgres, IAM auth | Azure Database for PostgreSQL, Entra auth |
| Run queue | `RunQueue` (new) | Pub/Sub | SQS + DLQ | Service Bus |
| Scheduler | HTTP call | Cloud Scheduler | EventBridge Scheduler | Container Apps jobs / Logic Apps |
| Secrets | `CredentialResolver` (exists) | Secret Manager | Secrets Manager | Key Vault |
| Model | `llm.py` provider (exists) | Vertex AI | Bedrock | Azure OpenAI / AI Foundry |
| Identity for code | Workload identity | WIF | IAM roles (IRSA / task roles) | Managed identity |
| User sign-in | OIDC middleware (new) | IAP or OIDC | ALB + Cognito/IdP OIDC | Entra ID (Easy Auth or OIDC) |
| Network to EWM/JTS | none | Interconnect / HA VPN | Direct Connect / Site-to-Site VPN | ExpressRoute / VPN |
| Telemetry | OpenTelemetry (new) | Cloud Trace/Monitoring | X-Ray/CloudWatch (ADOT) | Azure Monitor |
| IaC | Terraform module per cloud | `infra/` (exists) | `infra/aws/` (new) | `infra/azure/` (new) |

Container images, the OTel exporter and Terraform are the only cloud-specific
parts. Application code talks to ports.

## 4. Phased plan

Estimates are for one senior engineer with AI assistance. A second engineer
roughly halves calendar time from Phase 2 on.

### Phase 0: Decisions and pilot evidence (1-2 weeks)

- Close D1-D8 with the client (owners: you + client security/IAM/network).
- Run 20+ TEST work items through the agents locally with `--record`. Collect
  per-run metrics and traces: model calls, tokens, time, denials, replays,
  outcomes versus the CLI.
- Write the threat model (STRIDE on section 3), the data-flow diagram and the
  data classification for every field that leaves the client network.
- **Exit:** signed decisions; a measured baseline (tokens and minutes per work
  item, success rate); the recorded runs become CI eval scenarios.

### Phase 1: Cloud-neutral core and horizontal scale (3-4 weeks) - fixes G1, G2, G3

- `RunQueue` port with Pub/Sub, SQS and in-memory adapters; a worker entry
  point (`alm_worker.runs`) that pulls run jobs and drives the graph.
- Replace the in-process sweep with an external scheduler calling
  `/admin/reconcile` (idempotent: it only enqueues).
- Distributed run lock: Postgres advisory lock or a lease row per thread id,
  with heartbeat and takeover after expiry.
- Move stop control to the store: an `alm_run_control` row per thread
  (requested_by, at). `RunControl` reads it; the stop file stays for local mode.
- Wire `RunControl` and tracing into the cloud runtime (`graph.build_runtime`,
  `alm_api`).
- Configuration only through environment and secret references; drop every
  remaining GCP import from the core path.
- Remove `max_instance_count = 1`; API and workers scale independently.
- **Exit:** two API replicas and three workers run 20 recorded scenarios
  concurrently against the sandbox estate with zero duplicate writes; kill a
  worker mid-run and the run resumes on another; Stop works from any replica.

### Phase 2: Identity, roles, approvals (2-3 weeks) - fixes G4, G5

- OIDC sign-in against the client IdP for the console and API; IdP groups map
  to roles:

  | Role | Can |
  |---|---|
  | Viewer | See runs, traces (PII-masked), reports |
  | Operator | Start dry runs; start writing runs on TEST; stop runs |
  | Approver | Approve or reject cards (never their own run in PROD) |
  | Auditor | Read audit and full traces; export |
  | Admin | Settings, kill switch, retention |

- Approval policy as data: PROD and high-risk users need two distinct
  approvers; neither may be the operator who started the run. The gate checks
  this before any write.
- Notifications: Teams/Chat card and e-mail with a deep link to the card;
  approval only inside the authenticated console (no approve-by-link tokens).
- The console becomes a hosted multi-user app: per-user sessions, CSRF as
  today, the run list from the store rather than memory.
- Service credentials: a functional Jazz account per environment in the
  secrets manager, with rotation; no personal CID or password prompt in cloud.
- **Exit:** a PROD-mode run on TEST data needs two approvers, refuses the
  starter as approver, and the audit row names both from IdP claims.

### Phase 3: Directory integration and the worker (2-4 weeks, depends on D3) - fixes G6

- Preferred: Microsoft Graph `POST /groups/{id}/members/$ref` through a
  `DirectoryBackend` port, with an app registration holding least privilege
  (`GroupMember.ReadWrite.All` scoped by an administrative unit if possible).
  Idempotent: a 400 "already exists" counts as success.
- If GPT must stay: keep the Windows worker, run it as a service in an isolated
  VM with its own identity and a lease-extending heartbeat (exists), and add
  screenshot-on-failure into the trace.
- **Exit:** AD membership in under 10 seconds per user with no browser, or the
  isolated worker passing a 50-user soak test.

### Phase 4: AI governance and quality gates (2 weeks, runs alongside) - fixes G7

- Provider in the client tenancy (D2), model versions pinned, zero data
  retention confirmed in writing, region pinned.
- Keep the redaction (exists); add a field-level allowlist of what may enter a
  prompt, and record in each trace which fields were sent.
- Evals as a release gate: the five built-in scenarios plus every recorded
  pilot run must pass in CI on each prompt, roster, model or policy change.
  Track pass rate, tokens and hops per scenario over time.
- Prompt and roster versioning: the version is stored with each run and shown
  in its report.
- Cost guardrails: a token budget per run and per day, alerting at 80%; a
  degrade path to deterministic orchestration when the budget or the provider
  is exhausted (`ALM_ORCHESTRATION=deterministic`, exists).
- Red-team the prompt-injection paths: requester-written text trying to widen
  scope, change mode or approve. The policy engine should refuse all of them;
  add each attempt as an eval.
- **Exit:** evals gate CI; the injection suite passes; the cost per work item is
  known and alerting.

### Phase 5: Observability and operations (2 weeks) - fixes G8

- OpenTelemetry SDK: one trace per run, a span per hop, agent, model call
  (GenAI semantic conventions: model, tokens, latency), tool call, EWM/JTS/
  directory call and ledger write. The JSONL trace becomes one exporter of the
  same events.
- Metrics: runs by outcome, writes by operation and outcome, denials, replays,
  approval wait time, tokens, model latency and errors, queue depth, worker
  utilisation.
- SLOs, for example: 99% of approved writes complete within 15 minutes; 99.5%
  API availability; zero duplicate writes (ledger-checked). Burn-rate alerts.
- Dashboards: operations (queue, runs, errors), AI (tokens, hops, denials,
  eval trend), audit (writes by approver).
- Audit export to the client SIEM (append-only stream). Make the audit table
  append-only by permission: INSERT and SELECT grants only (still open).
- Runbook updates (section 7 below); on-call ownership agreed with the client.
- **Exit:** a synthetic run every 15 minutes in TEST, alerts fire on injected
  faults (EWM down, model 429, worker killed).

### Phase 6: Security hardening and compliance (2 weeks, overlaps 4-5) - fixes G10

- Supply chain: pinned dependencies (exists), SBOM (Syft) per image, image
  signing (cosign) and verification at deploy, vulnerability scan gate, base
  image refresh monthly.
- Container: non-root, read-only filesystem, no shell in production image,
  minimal base (distroless or slim).
- Network: no public ingress (exists in GCP design); egress allowlist to EWM,
  JTS, the model endpoint and the directory API only; private endpoints for
  every cloud service; WAF on the internal load balancer.
- Data: encryption at rest with client-managed keys where required; retention
  per data class (traces 30 days, audit per the client's policy, purge exists
  locally and needs a cloud job); backups with PITR.
- Testing: SAST (ruff `S`, exists, plus Semgrep), dependency audit (exists),
  DAST against the console, an external penetration test before PROD.
- Compliance evidence mapped to the client's framework (SOC 2 / ISO 27001
  controls): access reviews, change records, audit retention, DR tests.
- **Exit:** clean pen test (or accepted findings), signed images only, security
  sign-off from the client.

### Phase 7: Delivery pipeline and environments (1-2 weeks)

- Environments: dev (sandbox estate), TEST (client TEST ELM), PROD. One
  Terraform module per cloud, one tfvars per environment (pattern exists).
- Pipeline: PR → tests, evals, lint, security → build, SBOM, sign → deploy to
  dev → smoke and eval run → manual promotion to TEST → soak → change-approved
  promotion to PROD.
- Releases: blue/green or canary for the API; workers drain (finish the current
  step, as Stop does) before a version swap.
- Feature flags and kill switches: shadow mode, deterministic orchestration,
  writes disabled per environment, per-operation disable (for example
  directory writes off).
- Database migrations run as a separate, approved pipeline step (the schema
  version table exists).
- **Exit:** a full promotion dev → TEST → PROD rehearsed, including rollback.

### Phase 8: Pilot in PROD and convergence (3-4 weeks) - fixes G9

- Shadow in PROD for 1-2 weeks: dry runs on the real queue, compared with what
  the CLI or humans did.
- Limited writes: low-risk users only (already active / reactivation), two
  approvers, five work items per run (limit exists).
- Widen to new imports and AD once the error budget holds.
- Retire the CLI (D7), or point it at the shared ledger.
- **Exit:** 4 weeks in PROD within SLO, zero duplicate or unapproved writes,
  handover to the client's operations team.

### Timeline

| Weeks | Work |
|---|---|
| 1-2 | Phase 0 |
| 3-6 | Phase 1 |
| 7-9 | Phase 2; Phase 4 alongside |
| 9-12 | Phase 3; Phase 5 |
| 11-13 | Phase 6; Phase 7 |
| 14-17 | Phase 8 |

About 4 months for one engineer, about 3 with two. The critical path is the
client decisions (D2, D3) and their network and security reviews, not the code.

## 5. Non-functional targets

| Area | Target |
|---|---|
| Availability | API 99.5% in business hours; no single point of failure except the client's ELM |
| Throughput | Sized from Phase 0 data; initial target 200 work items/day with 3 workers |
| Latency | Dry run of one work item under 3 minutes (P90); approved writes complete within 15 minutes |
| Correctness | Zero duplicate writes; zero writes outside an approval (both enforced in code today) |
| Recovery | RPO 5 minutes (PITR), RTO 1 hour; a run resumes from its checkpoint after any restart |
| Cost | Tokens per work item measured in Phase 0; daily cap with alerting |
| Security | No public ingress; no long-lived keys; no secrets in logs or traces (enforced) |

## 6. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Client will not allow work-item text to reach a model | Medium | High | Deterministic orchestration (exists) does the job with no model; the model then only helps with malformed rows, or is off entirely |
| ELM rate limits or outages | Medium | Medium | Circuit breakers and bounded concurrency (exist); queue backpressure; SLO excludes ELM downtime |
| GPT UI changes break the worker | High while GPT is used | Medium | Graph API (D3); until then, selector tests in CI and screenshots in traces |
| Prompt injection from requester text | Medium | High | Policy engine, scope and approval are enforced in code (exist); injection evals (Phase 4) |
| Model behaviour drifts with a provider update | Medium | Medium | Pinned versions; eval gate; deterministic fallback |
| Network/DNS from the cloud to ELM | High at first | High | The DNS design exists for GCP; reproduce per cloud and test from inside the VPC first |
| Scope creep to other provisioning flows | Medium | Medium | Package tools and policy as an MCP server later (TODOS.md), not in this plan |

## 7. Operating model after go-live

- **Runbook:** extend `docs/RUNBOOK.md` with cloud procedures: stop a run,
  drain workers, kill switch, replay a dead letter, restore from PITR, rotate
  the Jazz and directory credentials, roll back a release.
- **On-call:** client operations own availability; this team owns agent
  behaviour and evals for the first 3 months.
- **Change process:** prompts, roster, policy and model versions change only
  through PRs that pass evals; each PROD release links an ITSM change if D8
  requires it.
- **Reviews:** monthly access review of the approver and admin groups;
  quarterly DR test; eval suite grows with every incident.

## 8. Immediate next steps (this week)

1. Send the client the D1-D8 decision list.
2. Run the TEST pilot from the web console with `--record` (see the README);
   collect traces and reports.
3. Start Phase 1 with the parts that do not depend on the cloud choice: the
   `RunQueue` port, the store-backed run lock and stop flag, and wiring
   control and tracing into `alm_api`.
4. Update `docs/AUTONOMOUS_ARCHITECTURE.md`: its "Not done" section is out of
   date (the tests, eval suite and Postgres IAM auth now exist).
