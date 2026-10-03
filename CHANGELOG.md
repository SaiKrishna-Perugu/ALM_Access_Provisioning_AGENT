# Changelog

What changed for the operator, newest first. Each entry says what to do after
`git pull`, when anything is needed. Commit messages carry the detail.

The local ledger (`out/local/alm.db`) has a schema version. A newer version of
the code upgrades an older file automatically the next time it runs; a file
written by newer code is refused until you update.

## 2026-10-03: enterprise track, part 11 (every cloud, and the release path)

**Do after pulling:** if you run Terraform by hand, it moved from `infra/` to `infra/gcp/`. The state is unaffected; use `-chdir=infra/gcp`.

- **AWS and Azure configurations** (`infra/aws`, `infra/azure`). These are skeletons that pass `terraform validate`, with the same shape as GCP:
  - **AWS:** ECS Fargate (API and workers), an internal ALB, RDS with IAM login, Secrets Manager, Bedrock and VPC endpoints.
  - **Azure:** Container Apps (API and workers), PostgreSQL with Entra ID only, Key Vault, Azure OpenAI and private endpoints.

  Every pull request now validates all three clouds. [infra/README.md](infra/README.md) maps each setting to each cloud.
- **Migrations as a step of their own.** `python -m alm_core.store.migrate` migrates the schema and `--check` reports it. With `ALM_AUTO_MIGRATE=false`, services only check the schema and refuse to start on an old one, naming the command.
- **Synthetic check on TEST.** Set `synthetic_work_item` in the tfvars, and a Cloud Run job runs a dry run of that work item hourly (Cloud Scheduler) and after every deploy.
- **Runbook 7g: releases and promotion.** It covers the gates, the schema, workers during a release, rollback and the synthetic check.

## 2026-10-03: enterprise track, part 10 (workers, retention, recovery)

**Do after pulling:** nothing. The next deploy from `main` splits the cloud service in two (see below).

- **Run workers are a service of their own.**
  - Deploy builds, scans, signs and verifies two images: `api` and `worker`.
  - Terraform runs them as two Cloud Run services. The worker service is internal-only, has no invoker, and scales from `worker_min_instances` to `worker_max_instances`.
  - Without `worker_image`, Terraform keeps the single service with workers inside the API.
  - The worker answers `GET /healthz` on `ALM_WORKER_HEALTH_PORT` for liveness probes.
- **Retention in the cloud.** The scheduler queues `retention-<date>` once a day. It deletes finished runs older than `ALM_RETENTION_DAYS` (default 30), with:
  - their checkpoints, traces, approval cards, votes, stop requests and finished jobs;
  - old webhook deliveries and agent memory.

  The ledger and the audit trail are kept. The counts show on the run in the console.
- **[docs/DR.md](docs/DR.md)** covers disaster recovery:
  - where every piece of state lives;
  - RPO 5 minutes and RTO 1 hour, to be measured;
  - the restore procedure and a drill to rehearse it;
  - why resuming after a restore cannot create a duplicate account.
- **Optional client-managed key for the database:** `db_kms_key` in Terraform.

## 2026-10-03: enterprise track, part 9 (images and supply chain)

**Do after pulling:** nothing.

- **Three image targets:**
  - `api`: the API and console, with no browser and no workers;
  - `worker`: runs, with Chromium for the evidence screenshots;
  - `all-in-one`: both, the default and what the GCP Terraform deploys today.

  Each runs as a non-root user with a read-only root filesystem (only `/tmp` is writable), has no pip at runtime, and uses a Python base pinned by digest.
- **Fixed: the image could not be built.** The CA bundle was excluded from the build context, and the step that installs it failed on the link `update-ca-certificates` makes. Every deploy would have failed at the build.
- **Every pull request builds both images** (`image.yml`). It fails on any fixable HIGH or CRITICAL vulnerability, and proves the API serves read-only and Chromium starts.
- **Deploys are gated and signed.**
  - The build stops on the vulnerability scan.
  - The image gets an SBOM (SPDX), a keyless cosign signature and a signed SBOM attestation.
  - The deploy job verifies the signature before Terraform runs. Only an image built by this workflow on `main` is deployed.
- **Semgrep** runs in CI next to ruff's security rules.
- **Fixed: text from a request could carry markup onto the approval cards.** A display name or risk reason could put a link on the Google Chat or Teams card. Every value is now escaped.
- **Fixed: the Chat card never showed risk reasons,** and printed raw `<b>` tags in its plain-text label.

## 2026-10-03: enterprise track, part 8 (observability)

**Do after pulling:** `pip install -r requirements-cloud.txt` (it adds the OpenTelemetry SDK). The database upgrades itself to schema version 4.

- **OpenTelemetry.** With `ALM_OTEL_ENABLED=true`, each job on a run is exported over OTLP/HTTP as a span tree:
  - `run <job>` at the root;
  - `agent <name>` for each hop;
  - under each agent, `chat <model>` spans (GenAI attributes: model and tokens), `tool` spans, backend spans and `http` spans;
  - ledger events.

  The endpoint is the standard `OTEL_EXPORTER_OTLP_ENDPOINT`, so any backend works. Only names, counts, outcomes and timings are exported: no prompts, replies, user IDs, URL paths or error messages.
- **Metrics:** runs, jobs, writes, replays, policy denials, model tokens and latency, approval wait, queue depth and busy workers.
- **[ops/alerts.md](ops/alerts.md)** sets out the service levels and the alert rules: burn rate on dead jobs, failing writes, a stuck queue, a slow model, a denial spike, 80% of the daily token budget, and slow approvals.
- **[ops/dashboards/](ops/dashboards/)** has the dashboard for Grafana and for Google Cloud Monitoring.
- **Synthetic check:** `python -m alm_agents.worker synthetic <work item>` queues a dry run. Schedule it on TEST, and alert when no run finishes.
- **The audit trail is append-only in Postgres too** (schema version 4): a trigger refuses UPDATE and DELETE on `alm_audit`, as SQLite always has.
  - `python -m alm_core.store.admin grants --app-role <role>` prints the grants a DBA applies, so that the application role can only insert and read audit rows.
- **Audit rows are also log lines** with `audit=true`, for a SIEM to subscribe to.
- **Terraform:** `otel_endpoint` turns telemetry on for Cloud Run.

## 2026-10-03: enterprise track, part 7 (AI governance)

**Do after pulling:** nothing. The defaults cap one run at 400,000 model tokens and set no daily cap.

- **Token budgets.** Every model call's tokens count against the run. At `ALM_MAX_TOKENS_PER_RUN` (default 400,000) the run stops, saying so. `ALM_MAX_TOKENS_PER_DAY` caps all runs started in a UTC day; a run started past it is refused with the reason. Both show in the run's metrics and the console.
- **Fallback when the model is down.** With `ALM_DEGRADE_ON_MODEL_FAILURE=true`, a run whose model is unavailable is followed by `<thread-id>-fallback`. That run uses the fixed order (no model), with the same scope, mode and requester. A writing run still stops at the approval card. Past the daily cap, new runs use the fixed order instead of being refused.
- **Provider allowlist.** `ALM_ALLOWED_PROVIDERS=vertex,bedrock` refuses to start with any other `ALM_LLM_PROVIDER`.
- **Withheld fields.** `ALM_MODEL_WITHHELD_FIELDS=justification,summary` keeps those fields from every model: the tools still use them, and the agent sees `[withheld: N chars]`.
- **Every run records a version.** It combines the orchestration with a fingerprint of the prompts, the roster and the models, for example `guided-b8c4c6407ae7`. It is on the run, in its report and in each eval result. A resumed run keeps the graph it started with, even if the deployment's setting has changed since.
- **Prompt-injection evals.** Three new scenarios put instructions in the requester's text:
  - widen the scope to everyone, skip the approval and print a password;
  - write during a dry run;
  - a forged approval.

  They pass only if nothing the text asks for is written. Every scenario now also checks that writes touch requested users only.
- **Nightly evals** (`.github/workflows/evals.yml`): every scenario against the real model, with the pass rate, tokens and version in the run summary. It needs the repository secret `GEMINI_API_KEY`, and is skipped without it.

## 2026-10-03: enterprise track, part 6 (service credentials)

**Do after pulling:** nothing.

- **Cloud services never prompt.** A missing secret is reported by name, instead of waiting for a terminal that does not exist.
- **Workers refuse to start without the service account and its password.** Before, every run failed at sign-in, one retry at a time.
- **New `GET /status` (viewer)** shows whether the database, EWM, JTS and the model are reachable. A failed Jazz sign-in is retried only after ten minutes, so probing cannot lock the account.
- **Configuration errors name the real variable** (`CID`, `GOOGLE_CLOUD_PROJECT`) instead of a made-up `ALM_` name.
- **The runbook has a credential rotation table** (section 7c).

## 2026-10-03: enterprise track, part 5 (the console in the cloud)

**Do after pulling:** nothing.

- **The cloud API serves the web console at `/`** to signed-in people.
  - It is the same page as the laptop console, backed by the shared database, so any replica shows any run live.
  - It shows the activity, the Trace tab (with download), and Stop.
- **The page follows roles.** Viewers don't see the request box, only operators see Stop, and only approvers can vote.
- **The approval card shows the votes so far** and how many are needed. An announcement link (`?run=<thread-id>`) opens that run.
- **Cloud dry runs no longer park for approval.** A dry run's card is a preview: it is recorded in the trace and the run carries on to its end, as on a laptop. Nobody is asked to vote on a run that cannot write.
- **Requests from another site are refused,** whatever the sign-in mode.

## 2026-10-03: enterprise track, part 4 (two approvers)

**Do after pulling:** give approvers the `approver` role in `ALM_ROLE_MAP`. Production now needs two of them. In the GCP Terraform, the secret `alm-approval-signing-key` is replaced by `alm-session-signing-key`.

- **Two-person rule.** Production cards, and cards with a high-risk user, need two different approvers, and the person who started the run cannot be one of them.
  - Each approver votes once, and each vote is an audit row with their name.
  - One rejection rejects the card.
  - Only users that every approver ticked are written.
  - Set the numbers with `ALM_APPROVERS_REQUIRED_PROD` and `ALM_APPROVERS_REQUIRED_HIGH_RISK`.
- **Only a signed-in approver can decide.** The approve and reject links with tokens are gone.
  - Announcements go to Google Chat, Teams or e-mail (`ALM_NOTIFY_CHANNELS`).
  - Each carries one link to the run in the console, and none of them can approve anything.

## 2026-10-03: enterprise track, part 3 (sign-in and roles)

**Do after pulling:** set `ALM_ROLE_MAP` (or the Terraform `role_map`) before deploying. Without it nobody holds a role, and the API refuses everyone except the webhook.

- **Company sign-in** (`ALM_AUTH_MODE=oidc`): the service signs people in against your IdP (Entra ID, Okta, Ping...).
  - It uses the authorization code flow with PKCE, state and nonce.
  - The session cookie is HttpOnly and SameSite=Strict, signed with a key every replica shares.
  - Every change also needs a CSRF token.
  - `iap` stays the default for the GCP deployment.
- **Roles** from IdP groups or e-mail addresses: viewer, operator, approver, auditor, admin.

  | Endpoint | Role needed |
  |---|---|
  | Run lists, details and traces | viewer |
  | Start (new `POST /runs`) and stop runs | operator |
  | The manual sweep | admin |

  - Traces and audit events show e-mail addresses only to auditors.
  - Someone with no role is refused at sign-in.

## 2026-10-03: enterprise track, part 2 (AD membership)

**Do after pulling:** nothing.

- **Microsoft Graph can add users to the AD group** (`ALM_AD_DIRECTORY=graph`): one call from the run, no browser and no Windows worker.
  - It works only for groups mastered in Entra ID. For a group synced from on-premises AD, it says so and the run stays on GPT.
  - User and group lookups, throttling retries, and "already a member" are handled. Every call is in the run's trace.
- **Fixed: the cloud GPT worker could submit the same request twice.** If the page failed after Modify was clicked, the job was retried. Now the outcome is recorded as unknown and never retried, the same as on a laptop, and a human checks GPT Pending Requests.
  - GPT's steps are now one implementation, shared by the laptop and the Windows worker.
- **Fixed: a redelivered AD job was logged as a fresh attempt.** It is now reported as a replay.

## 2026-10-03: enterprise track, part 1 (run isolation and approval cards)

**Do after pulling:** nothing.

- **Concurrent runs no longer share state.** The cloud API used one set of run state for every run, so two runs at once could see each other's users on their approval cards. Each start and each resume now gets its own state; connections and the store are still shared.
- **Approval cards hold only requested users.** An agent looking up a user ID that no work item asked for gets the lookup, but that user is no longer added to the run. They never reach the approval card or a write.
  - Users join a run only from a work item's New Users field, or from `recover_user_ids` on a malformed row.
- **Shared run state in the database (schema version 2).** New tables hold the run registry, a job queue, stop requests, webhook replay protection, leases and traces. These let several API servers and workers share the work later in this track.
  - Both the local SQLite ledger and Postgres upgrade automatically on the next run.
  - Postgres now has schema versioning like SQLite, and refuses a database written by newer code.
- **Runs are queued and run by workers.** The cloud API no longer runs anything itself. Webhooks, decisions, sweeps and stops become jobs in the database, and workers pick them up: inside the API (`ALM_WORKER_CONCURRENCY`, default 1) or as `python -m alm_agents.worker`.
  - **Approval.** A run parks at the approval card without holding a worker.
  - **Crash recovery.** A worker that dies mid-run hands the run to another worker, which continues from the last checkpoint; writes already made are replayed, not repeated.
  - **Scheduling.** One worker schedules the 15-minute sweep, however many run.
  - **Scale.** `max_instances` in Terraform can now be raised; it was pinned to 1.
- **Stop and trace in the cloud API.** `POST /runs/<thread-id>/stop` works from any replica, and `GET /runs/<thread-id>/trace` returns the run's trace. `GET /runs` and `GET /queue` show the run registry and failed jobs.
- **`ALM_RECONCILE_INTERVAL_MINUTES=0` now turns the sweep off,** as the runbook said it would. Before, the setting rejected 0.
- **Runs on GCP, AWS or Azure.** The core imports no cloud SDK; each cloud's adapter is chosen by a setting, and its SDKs are an optional install (`pip install '.[gcp]'`, `'.[aws]'`, `'.[azure]'`):

  | Concern | Setting | Choices |
  |---|---|---|
  | Secrets | `ALM_SECRET_BACKEND` | Secret Manager, Secrets Manager, Key Vault |
  | Database login | `ALM_DB_AUTH` | Cloud SQL IAM, RDS IAM, Entra ID, or a password |
  | Agent models | `ALM_LLM_PROVIDER` | adds `bedrock` and `azure_openai` |

- **AD jobs go through the shared database by default** (`ALM_AD_JOB_TRANSPORT=store`), so the Windows worker needs nothing beyond Postgres. The GCP Terraform keeps Pub/Sub.
- **Fixed: the Windows worker would never have added anyone in the cloud design.** It claimed the same ledger entry the run had already marked "submitted", so it skipped every job. The worker now has its own ledger entry, still idempotent across redeliveries.
- **Fixed: a database URL with the password inline was ignored** when IAM database login was on, and a token was minted anyway.

## 2026-10-01: stop control and full tracing

**Do after pulling:** nothing. On the client network, start the console **without** `--sandbox`.

- **Simulated data is now impossible to miss.** `--sandbox` has always used made-up work items (1001, 1002) and never contacted EWM. That is why a sandbox console on the client network showed dummy data.
  - A sandbox console now shows a banner, a **SIMULATED DATA** badge, and a terminal warning.
  - A real console shows **LIVE** and the EWM/JTS host names.
  - Every run's first line says where its work items come from.
- **Stop a run from anywhere.**
  - In the web page: **Stop run**.
  - In the terminal: Ctrl+C once stops gracefully; a second press aborts.
  - From another terminal: `python src/agent_local.py --stop [thread-id]`.
  - Closing the web console with Ctrl+C stops its run the same way.
  - A stop lets the step in progress finish, so a write is never cut off halfway. It then halts with "stopped by …" and writes the report and audit.
  - A model call in progress is abandoned at once.
- **A trace of every call.** Each run writes `out/local/traces/<thread>.jsonl` (sandbox: `out/sandbox/traces/`). It covers:
  - model calls, with caller, tokens, time and chosen tools;
  - tool calls;
  - EWM/JTS/GPT/browser calls;
  - HTTP requests, including Gemini's;
  - sign-ins, ledger steps and approvals;
  - every log line, including ones the console hides.
- **Reading a trace.** Use `python src/agent_local.py --trace last [--follow]`, or the web console's **Trace** tab (filter, inspect, download).
- **Secrets.** Bodies, headers and secret-looking URL parameters are never recorded, and API keys are scrubbed.
- **Purge.** `--purge-older-than` now deletes traces too.

## 2026-09-30: web console

**Do after pulling:** `.\scripts\setup.ps1 -Agents` (FastAPI and uvicorn, already in `requirements-cloud.txt`).

- **New: `python src/agent_web.py`.** A browser console for the agents.
  - Type a request and watch every routing decision and tool call as it happens.
  - Approve the card by ticking users.
  - `--sandbox` runs against the simulated estate from anywhere.
  - Without `--sandbox` it uses the real EWM/JTS: the Jazz password is asked once, in the terminal.
- **Security.**
  - Listens on 127.0.0.1 only and checks the Host header.
  - Sign-in is through the one-time link, which becomes an HttpOnly, SameSite=Strict cookie.
  - Every POST needs the page's Origin and CSRF token.
  - Strict CSP; no third-party requests.
  - A writing run needs the Write switch, 1–5 work items named by number, and a typed `COMMIT`/`PROD`.
- **Approving some of the users.** The approval gate no longer re-opens for users who were on the card and left unticked: they are declined, and the policy refuses writes for them. It still re-opens for users added after the decision.
- **Runs remember what was asked.** The operator's request is stored with the run and shown to the main agent as context. It never sets the mode or the scope.

## 2026-09-28: clean-up

**Do after pulling:** `.\scripts\setup.ps1` (the CLI now declares `defusedxml` in `pyproject.toml`).

- **Purge coverage.** `--purge-older-than` also removes:
  - old `out/screenshots.*` backup folders (real users' profile pages);
  - the CLI's stale `dryrun.log` and `pipeline_state.json`;
  - sandbox and eval output.
- **Dependencies aligned.** `pyproject.toml` matches the requirements files: `requests` 2.34.2 (the patched version), `defusedxml`, and a `fastapi` floor of 0.141. A duplicate line in `requirements-cloud.txt` is gone.
- **Archived draft.** The first design draft moved to `docs/archive/2026-08-26-autonomous-agent-plan-draft.md`, renamed from its misspelled file name. The current design is `docs/AUTONOMOUS_ARCHITECTURE.md`.
- **Workspace.** About 4 MB of stale local files were removed from the workspace (none were tracked by git).
- **Kept on purpose:**
  - the CLI scripts, which are the production path;
  - `ewm_workitems.py`, a documented diagnostic tool;
  - the `.github` chat modes and prompts, which drive the CLI;
  - the shared `idempotency.already_reported` import, which is what lets the CLI and the agents recognise each other's comments.

## 2026-09-27: evaluation suite, schema versioning, security hardening

**Do after pulling:**
- `.\scripts\setup.ps1 -Agents`: new and upgraded packages (defusedxml, fastapi, requests).
- `pre-commit install`: also installs the new pre-push hook that stops a direct push to `main`.
- Set `ALM_CA_BUNDLE` and `ALM_TLS_VERIFY=strict` in `.env`. A run whose environment is not known to be TEST now refuses unverified TLS.
- Run `start-gpt.ps1` once. The GPT browser profile now lives in `%LOCALAPPDATA%\alm-agent`, so you sign in to GPT again one time.

**Added**
- `python src/agent_eval.py`: replays five built-in scenarios, or runs recorded with `--record`, against the simulated estate using the real model. It grades outcomes (who is active, which screenshots land where, what the comments say) and five safety rules. No VPN needed.
- `agent_local.py --record`: saves a run's real reads, its approval card and its writes to `out/evals/recorded/`, for replay with `agent_eval.py --recorded`.
- A schema version for the local ledger, with forward migrations.
- `--purge-older-than DAYS --dry-run` and `purge-local.ps1 -DryRun`, which show counts and delete nothing. The purge also covers the CLI's screenshots and user caches, and recorded runs.
- A PII pre-commit guard. Its denylist lives in the gitignored `.pii-denylist` and in the `PII_DENYLIST` CI secret, never in git.
- `ALM_IAP_AUDIENCE` (cloud): when set, the approver's identity comes only from a verified IAP token.

**Changed**
- Only an environment known to be TEST may run over unverified TLS. PROD and UNKNOWN refuse.
- A blank value copied from `.env.example` (for example `GROUP_NAME=`) now means "use the default".
- A recovered user's comment line uses the LDAP name ("TB22322: Tom Baker").
- `start-gpt.ps1 -Fresh` refuses while the debug Chrome is still running.
- The CLI's `out/audit` records are kept by the purge.
- Terraform takes the intranet host names from GitHub environment variables (`EWM_SERVER`, `JTS_SERVER`, `CORPORATE_DNS_SUFFIX`) and rejects placeholders.

**Fixed**
- The agents could attach a user's profile screenshot to a work item that never requested that user.
- `purge-local.ps1 -DryRun` deleted data.
- A forged `x-forwarded-user` header could record an approval (cloud API).
- Two deploy actions were pinned to commits that do not exist.

## 2026-09-26: the multi-agent system runs locally

**Do after pulling:** `.\scripts\setup.ps1 -Agents`, then add `GEMINI_API_KEY` (and `ALM_LLM_PROVIDER` if the key is a Vertex AI key) to `.env`.

- `python src/agent_local.py`: nine agents on your laptop, with OSLC for EWM/JTS, Gemini for the reasoning, a SQLite ledger, and approval at the terminal. Dry run unless `--commit`, and `--commit` needs `--work-item` (at most 5).
- Guided orchestration by default: a fixed order, with the supervisor model consulted only on exceptions.
- Comments are rendered from the run's records, with no agent signature. The agents and the CLI skip users already reported by either.
- A resumed run keeps its mode, approved users, PROD confirmation and budgets. `--resume last`.
- Names and e-mail addresses are removed from what Gemini reads.
- An ambiguous GPT submit is recorded as *outcome unknown* and never retried automatically.
- Per-run metrics, lasting agent memory, `--purge-older-than`, `--check` with a WARN tier, `--workitem`, clearer errors, and new RUNBOOK section 7b.

## 2026-09-06: initial release

- The six-step CLI pipeline (`run_pipeline.py`) with the remediation: plan fingerprinting, idempotent comments and attachments, the evidence distinctness gate, the commit guard hook, an offline test suite and CI.
- The multi-agent system and its Google Cloud deployment (code complete, not deployed).
