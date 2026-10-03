# Operational Runbook — Autonomous ALM Provisioning

> Phase 8, item 33. Written before the system has ever run; the procedures are
> derived from the design and from the failure modes the CLI actually produced,
> not from operating this deployment. Correct them the first time reality
> disagrees.

**The one thing to know:** every write is idempotent and every write is
recorded. Re-running is safe. Guessing is not — check the ledger before you act.

---

## 1. First response

| Symptom | First look |
|---|---|
| Nothing is being provisioned | `GET /runs` — are runs starting at all? |
| Runs start and stop immediately | Audit `step=run_summary`, `halt_reason` |
| Runs park and never resume | Nobody approved. `GET /approvals/{thread}` |
| Users provisioned but no comment | Verification timed out — that is by design |
| AD never happens | The Windows worker. Check its heartbeat log |
| Everything is a no-op | `ALM_SHADOW_MODE` is still `true` |

```sql
-- What happened in a run
SELECT at, step, userid, outcome, message
FROM alm_audit WHERE run_id = :run_id ORDER BY at;

-- Anything failing across the estate today
SELECT step, outcome, count(*), max(at)
FROM alm_audit WHERE at > now() - interval '24 hours'
GROUP BY 1, 2 ORDER BY 3 DESC;
```

---

## 2. Stop it

**One run.** Stop it after its current step; a write in progress always
finishes, nothing new starts, and the run ends `stopped` with its report:

```bash
curl -X POST https://<api>/runs/<thread-id>/stop -H "<IAP or OIDC auth>"   # any replica
python src/agent_local.py --stop <thread-id>                              # a laptop run
```

A queued run never starts; a run parked at the approval card ends without
writing. `GET /runs/<thread-id>` shows `stopping`, then `stopped`.

**Everything.** In order of severity. All three are reversible.

1. **Stop writing, keep observing** — set `ALM_SHADOW_MODE=true` and restart the
   revision. Runs continue and record what they *would* do. This is the right
   first move in almost every incident.
2. **Stop triggering** — set `ALM_RECONCILE_INTERVAL_MINUTES=0` and rotate the
   webhook HMAC secret in Secret Manager. Parked runs stay parked.
3. **Stop everything** — scale the service to zero instances. In-flight runs
   are checkpointed; their jobs stay claimed until the lease runs out
   (`ALM_JOB_LEASE_SECONDS`, 2 minutes), then the first worker back continues
   them. In-flight *writes* leave a claim in the ledger that expires after 15
   minutes.

```bash
gcloud run services update alm-prod-api --region <region> --update-env-vars ALM_SHADOW_MODE=true
gcloud run services update alm-prod-api --region <region> \
    --min-instances 0 --max-instances 0
```

Nothing here rolls back a write that already happened. See §6.

---

## 3. Rollback

Revisions are immutable; roll back by pointing traffic at the previous one.

```bash
gcloud run revisions list --service alm-prod-api --region <region>
gcloud run services update-traffic alm-prod-api --region <region> \
    --to-revisions <previous-revision>=100
```

**Database schema:** versioned (`alm_schema_version`). Migrations only add, and
run on start-up under an advisory lock. An image refuses a database whose schema
is *newer* than it knows, so a rollback across a migration needs the previous
image *and* a restore taken before the migration ran (PITR), or a roll forward
instead. A migration that ever removes a column must be split across two
releases, so the old revision keeps working while traffic is still on it.

**LangGraph checkpoints** are keyed by thread id and version. A rollback across a
state-shape change can leave a parked run unresumable; if that happens, reject
the approval to close it out and let the reconciliation sweep pick the work item
up fresh. The ledger stops the retry from duplicating anything already done.

---

## 4. Dead-letter replay

**Run jobs** (the Postgres queue). A job that fails `ALM_JOB_MAX_ATTEMPTS` times
(default 5, with backoff), or fails on a configuration error, goes `dead`. Its
run is `failed`, with the error in `GET /runs/<thread-id>`. List them with
`GET /queue`. Fix the cause, then queue the run again: re-send the trigger, or
start it from the console. The ledger reports any write the failed attempts
already made as a replay.

**AD jobs** (Pub/Sub, for the Windows worker) move to `alm-ad-provisioning-dead-letter` after 5 delivery attempts, or
immediately when the worker nacks a message it cannot parse.

```bash
# What is in there and why
# Backlog on the live subscription, and what is stuck in the dead letter.
gcloud pubsub subscriptions describe alm-ad-provisioning-worker
gcloud pubsub subscriptions pull alm-ad-provisioning-dead-letter-sub \
    --limit 10 --format=json
```

Inspect before replaying. The two causes need opposite responses:

- **Unparseable body** — replaying will fail identically. Fix the producer and
  discard the message; the worker logs `poison_message` with the reason.
- **Delivery attempts exhausted** — GPT was unreachable, Kerberos failed, or the
  browser crashed. Fix the cause, then replay. The worker logs
  `giving_up_after_retries` with the attempt count.

To replay, publish the original body back to `alm-ad-provisioning`. The idempotency key
travels in the message, so a job that actually succeeded before dead-lettering
becomes a recorded no-op rather than a second group membership request.

```sql
-- Did this job already complete? Check before replaying.
SELECT status, completed_at, result FROM alm_idempotency WHERE key = :key;
```

---

## 5. Stuck claims

A worker that dies mid-write leaves `status = 'in_flight'`. The 15-minute lease
lets the next attempt take it over automatically — **wait first**. Only clear a
claim by hand when you have confirmed in the target system that the write did
not land:

```sql
-- Claims held for over an hour: something is genuinely wrong
SELECT key, userid, operation, run_id, claimed_at
FROM alm_idempotency
WHERE status = 'in_flight' AND claimed_at < now() - interval '1 hour';

-- Only after confirming in JTS/AD that the write did not happen
UPDATE alm_idempotency SET status = 'failed', completed_at = now()
WHERE key = :key AND status = 'in_flight';
```

Never delete a ledger row. Marking it failed permits a retry; deleting it
destroys the record that the attempt happened at all.

---

## 6. Manual override

When the agent cannot proceed and the users need access today, the CLI is still
the escape hatch — it is unchanged, independently operable, and has its own
approval gate:

```powershell
python src\run_pipeline.py                          # dry run, review the plan
python src\run_pipeline.py --commit --skip-retrieve # commit exactly that plan
python src\jts_permission.py <UID> --wait 30        # check propagation later
```

The CLI writes to `out/audit/`, not to the Postgres ledger. **The two do not
share idempotency state**, so a manual run followed by an agent run *can*
duplicate a comment. If you use the override:

1. Put the orchestrator in shadow mode first (§2.1).
2. Record which work items you touched.
3. Before re-enabling writes, insert completed ledger rows for what you did, or
   accept that the agent will attempt those users again.

Undoing a write:

- **JTS contributor created in error** — archive it (`jfs:archived=true`); do not
  delete. `src/jts_unarchive_user.py` shows the conditional-PUT pattern.
- **Work-item comment** — cannot be deleted via OSLC. Post a correction; the
  original stays in the record, which is the correct outcome for an audit trail.
- **Attachment** — remove it through the EWM web UI.
- **AD group membership** — through GPT, by hand. There is no automated removal
  path, deliberately.

---

## 7. Approvals

```sql
-- Waiting on a human
SELECT thread_id, run_id, created_at, expires_at,
       request -> 'items' -> 0 ->> 'userid' AS first_user
FROM alm_approval WHERE decision IS NULL AND expires_at > now();

-- Expired without a decision
SELECT thread_id, run_id, expires_at FROM alm_approval
WHERE decision IS NULL AND expires_at < now();
```

```sql
-- Who has voted on a card so far (two are needed in production)
SELECT approver, approved, userids, at FROM alm_approval_vote
WHERE thread_id = '<thread-id>' ORDER BY at;
```

An expired approval is not a failure — the run halts and the work item is swept
again by the next reconciliation. If announcements are not arriving, check
`ALM_NOTIFY_CHANNELS`, the channel's URL or SMTP settings, and the
`approval_announced` log event; the run is listed in the console as awaiting
approval regardless, because a message that fails to send must not lose the run.

**Do not approve on someone's behalf.** Each vote's audit row records the
signed-in approver, and it is the only evidence of who authorised a production
change.

---

## 7a. Agentic runs

The agents decide their own sequence, so "what happened" is a question the audit
trail answers rather than something you can read off the code.

```sql
-- Replay a run's decisions in order: who acted, why, and what they did.
SELECT at, step, outcome, message, detail -> 'task' AS task
FROM alm_audit
WHERE run_id = :run_id AND (step = 'supervisor' OR step LIKE 'agent:%')
ORDER BY at;

-- Every policy denial in the last day, and which agent hit it.
SELECT step, message, detail ->> 'tool' AS tool, count(*)
FROM alm_audit
WHERE at > now() - interval '24 hours' AND detail ->> 'denied' = 'true'
GROUP BY 1, 2, 3 ORDER BY 4 DESC;
```

### Reading it

- `step = 'supervisor'` rows carry the routing decision and its reason. A
  `fallback: true` in the detail means the model was unavailable or unusable and
  the run took the nominal sequence instead - which is a model problem, not a
  provisioning problem.
- `step = 'agent:<name>'` rows carry that agent's summary, tool count and
  denials.
- A run that ends with `halt_reason` mentioning hops used its routing budget.
  Look at the supervisor rows: two agents alternating is the usual cause, and it
  usually means one of them cannot make progress and is not saying so.

### The kill switch

Agentic mode is a configuration, not a rebuild. To drop to the fixed sequence
with identical tools and guarantees:

```bash
gcloud run services update alm-prod-api --region <region> \
    --update-env-vars ALM_ORCHESTRATION=deterministic
```

Use this when agent behaviour is the problem and the provisioning still needs to
happen. Combine with `ALM_SHADOW_MODE=true` if you are not yet sure which it is.

### Symptoms and causes

| Symptom | Likely cause |
|---|---|
| Runs end immediately, no agent acted | The supervisor returned DONE. Read its `why` - usually the queue was genuinely empty |
| The same agent runs repeatedly | It is not calling `finish`, or it is failing and not saying so. Check its denials |
| Many denials naming approval | An agent is trying to write before the risk_officer requested approval. Expected occasionally; constant means a prompt problem |
| Writes stop mid-batch | The write budget. Raise `ALM_MAX_WRITES_PER_RUN` deliberately, or split the batch |
| An agent invented a user ID | The policy rejected it before LDAP - look for a denial mentioning the ID pattern. Nothing was provisioned |
| A recovered user is wrong, or a requested one is missing | Look for the `userid_recovery` log line: it names the judge (`typesafe` or `gemini`) and how many candidates were accepted. A missing user whose ID is not in the row text cannot be recovered by design - it goes to a human. Adjust `ALM_EXTRACTION_MIN_PROBABILITY` only with evidence from several rows |
| `typesafe_extraction_failed` in the logs | TypeSafe was unreachable or refused the key; with `auto`, Gemini judged instead. From Cloud Run this is expected - there is no internet egress |
| Costs climbing | Every hop is a model call. Lower `ALM_MAX_HOPS`, or set `ALM_SUPERVISOR_MODEL` to a cheaper model. The run's tokens are in its metrics; `ALM_MAX_TOKENS_PER_DAY` caps the day |
| A run halts with "token budget" | It spent `ALM_MAX_TOKENS_PER_RUN`. A run that needs far more than its peers is usually looping; read its supervisor rows before raising the cap |
| A run fails with "daily model token budget" | Runs started today spent `ALM_MAX_TOKENS_PER_DAY`. It resets at 00:00 UTC. Raise the cap, or set `ALM_DEGRADE_ON_MODEL_FAILURE=true` so new runs use the fixed order instead |
| A `<thread-id>-fallback` run appears | The model was unavailable and `ALM_DEGRADE_ON_MODEL_FAILURE=true`: the same work items, re-run in the fixed order. Writes the first run made show as replays. Fix the model; nothing else is needed |
| Behaviour changed and nobody changed the code | Compare the runs' `version` (on the run and in its report). A different fingerprint means the prompts, roster or models changed; the same one points at the model provider or the data |
| Agents stop with `model unavailable: ...ResourceExhausted` or HTTP 429 | The Gemini quota. Lower `ALM_LLM_REQUESTS_PER_MINUTE`, or move to `ALM_LLM_PROVIDER=vertex`, whose quota is the project's |
| Agents stop with `model unavailable` naming a 400 or 403 | The API key was revoked or the model id retired. Run `python src/agent_sandbox.py --check` with the same settings |

### Before changing a prompt, the roster or the model

Run the eval suite first: `python src/agent_eval.py`. It runs every built-in
scenario, including the prompt-injection ones, against the simulated estate.
The nightly **Evals** workflow does the same against the real model; its run
summary has the pass rate, tokens and version. A change that fails an injection
scenario does not ship.

**Do not "fix" an agent by loosening the policy.** A denial is the system
working. If an agent legitimately needs a capability it lacks, that is a roster
change, reviewed like any other code.

---

## 7b. Local runs (`python src/agent_local.py`)

| Symptom | Cause and fix |
|---|---|
| `cannot reach ... Check DNS, the VPN ... NO_PROXY` | The laptop cannot reach EWM/JTS. Connect the VPN; put the intranet domain in `NO_PROXY` |
| `the server rejected the user ID or password` | Wrong CID or password. Nothing was written |
| `the login was accepted but the session did not verify` | The account signs in but cannot open that server - check access in a browser |
| `ALM_CA_BUNDLE points at ... which does not exist` | Point it at the corporate CA `.pem`, as for the CLI |
| `ALM_TLS_INSECURE cannot be set when ... PROD` / `Refusing to send credentials over unverified TLS (PROD)` (or `UNKNOWN`) | Only TEST may run unverified. Set `ALM_CA_BUNDLE` (or `ALM_TLS_VERIFY=true`); for UNKNOWN, also set `EWM_SERVER`/`JTS_SERVER` |
| AD step failed: `no debug Chrome at ...` | Run `scripts\start-gpt.ps1`, sign in to GPT, then `--resume` the run |
| `the agent model is unavailable` | Key or model problem: `python src/agent_local.py --check` |
| Closed the terminal at the approval prompt | Nothing was written. `--resume last --commit` (or `--resume <thread-id> --commit`) brings the prompt back |
| A second run reports `(replay)` | Correct: the ledger recorded the first write. Nothing was repeated |
| AD step: `GPT may or may not have accepted` | The page failed after Modify, or GPT's reply was unreadable. It is **not** retried. Check GPT Pending Requests: if the request is there, do nothing; if it is not, clear the ledger entry and re-run: `UPDATE alm_idempotency SET status='failed' WHERE userid='<ID>' AND operation='ad_group_add' AND status='completed';` in `out/local/alm.db` |
| AD step (Graph): `mastered in on-premises AD, so Microsoft Graph cannot change it` | The group is synced from on-premises AD. Set `ALM_AD_DIRECTORY=gpt`; nothing was written |
| AD step (Graph): `the app registration may not change this group's members` | Grant the app `GroupMember.ReadWrite.All` (or make it an owner of the group), then re-run; the ledger replays everything else |
| AD step (Graph): `not found (or not unique) in Entra ID` | The user ID is not a synced `onPremisesSamAccountName`. Check the account, or use `ALM_GRAPH_USER_LOOKUP=upn` with `ALM_GRAPH_UPN_SUFFIX` |
| `the local ledger is locked by another process` | Two local runs at once. Let the other finish; the write was not attempted |
| `setup: Python package '...' is not installed` | The agent packages are missing from that Python. Run `.\scripts\setup.ps1 -Agents`, then use `.\.venv\Scripts\python.exe` (or activate the venv) |
| `the agent model is unavailable` with `429` or `RESOURCE_EXHAUSTED` | Gemini quota. Lower `ALM_LLM_REQUESTS_PER_MINUTE` (or pass `--rpm`), wait for the daily quota to reset, or use a paid key. Then `--resume last` (add `--commit` if the run had it) |
| `--check`: `the certificate Google presented is not trusted` | A proxy is inspecting HTTPS. Set `REQUESTS_CA_BUNDLE` and `SSL_CERT_FILE` in `.env` to the company root CA bundle (`.pem`) |
| `--check`: `Set HTTPS_PROXY` / `the proxy ... refused the connection` | Gemini is on the internet: name the corporate proxy in `HTTPS_PROXY` (intranet hosts stay in `NO_PROXY`) |
| EWM/JTS: `answered but its TLS certificate is not trusted` | Set `ALM_CA_BUNDLE` to the company CA `.pem`, as for the CLI |
| `--check`: `Microsoft Edge did not start` | Edge is missing or blocked. Run `python -m playwright install chromium` and set `ALM_BROWSER_CHANNEL=chromium` in `.env` |
| AD step: `GPT returned 401: Kerberos did not flow` | The debug Chrome's GPT session expired or was never signed in. Sign in again in that window (or close it and re-run `scripts\start-gpt.ps1`), then `--resume last --commit`. The request was not submitted, so it is retried |
| `resume: no saved run with thread id ...` / `no previous run recorded for --resume last` | The thread id is wrong, or its data was purged. The id is printed when a run starts and is in `out/local/run-*.json`; otherwise start a new run - the ledger still prevents repeated writes |
| `resume refused: run ... was started as a commit` (or `dry run`) | A run resumes in the mode it started in. Use the command the message gives |
| `--commit needs --work-item <id>` | A run that writes must name its work items (at most 5). Dry runs may scan the queue |

**Web console (`python src/agent_web.py`)**

| Symptom | Cause and fix |
|---|---|
| "Sign in from the terminal" page | Opened without the link, or the console was restarted. Use the link the terminal printed last |
| `unexpected Host header` | The page was opened by a name other than `127.0.0.1` or `localhost`. Use the printed link |
| `request did not come from this console` / `CSRF token` | A stale tab from an earlier console. Reload it with the new link |
| `A run is already in progress` | One run at a time. Decide or wait for the current one (the list on the left) |
| `A run that writes must name its work items` / `Type COMMIT` | By design: name 1-5 work items by number and type the word shown |
| `setup: sign-in failed` at start-up | As for `agent_local.py`: CID, password, VPN, `ALM_CA_BUNDLE`. Nothing was written |
| A writing run waits at the card and nobody decides | After 4 hours the batch is rejected and the run stops. Nothing was written after the gate |
| Port in use | Another console is running. Stop it, or pass `--port 8766` |
| The page shows work items 1001/1002, Alice Smith, Bao Nguyen… | The console was started with `--sandbox` (the **SIMULATED DATA** banner says so). Ctrl+C it and start `python src/agent_web.py` without `--sandbox` |
| A run must stop now | **Stop run** on the page, or `python src/agent_local.py --stop` from any terminal. It ends after the current step; a write in progress finishes first |
| Status stays **stopping** | The current step is a write, or a slow EWM/JTS/GPT call, finishing. The trace (Trace tab, or `--trace last --follow`) shows which. Nothing new starts |
| What exactly did the run call? | The Trace tab, or `python src/agent_local.py --trace <thread-id>`. The file is `out/local/traces/<thread-id>.jsonl` |

Everything a local run did is in `out/local/run-<run_id>.json` and in the
`alm_audit` table of `out/local/alm.db` (the SQL in section 1 works unchanged in
any SQLite client).

## 7c. Credentials and their rotation

Cloud services never prompt for anything. Each secret comes from the
environment, a mounted file, or the cloud secret store
(`ALM_SECRET_BACKEND`), by name.

| Secret (default name) | Used for | How to rotate |
|---|---|---|
| `alm-service-account-password` | The Jazz functional account (`CID`) on EWM and JTS | Change it in Jazz, add a new version of the secret, then nothing else: a 401 makes the service re-read the secret and sign in again. The cache is 15 minutes at most |
| `alm-webhook-hmac-key` | The EWM bridge's webhook signature | Add the new version, then update the bridge. Deliveries signed with the old key are refused once it is gone |
| `alm-session-signing-key` | Console session cookies (OIDC) | Add a new version and restart the instances. Everyone signs in again |
| `alm-oidc-client-secret` | The console's OIDC app registration | Add the new secret in the IdP, then the new version here, then remove the old one in the IdP |
| `alm-graph-client-secret` | Microsoft Graph (`ALM_AD_DIRECTORY=graph`) | Same as the OIDC secret |
| `alm-gemini-api-key` | Keyed Gemini providers only | Create a new key, add the version, delete the old key |

A worker refuses to start without the service account and its password, and
says which is missing. `GET /status` (viewer) shows whether the database, EWM,
JTS and the model are reachable. A failed Jazz sign-in there is retried only
after ten minutes, so a wrong password cannot lock the account by probing.

## 7d. Telemetry and the audit trail

**Turning telemetry on.** Set `ALM_OTEL_ENABLED=true` and
`OTEL_EXPORTER_OTLP_ENDPOINT` to an OpenTelemetry collector (Terraform:
`otel_endpoint`). The collector forwards to the client's backend, for example
Cloud Trace with Managed Prometheus, X-Ray with CloudWatch, or Azure Monitor.

- Dashboards are in `ops/dashboards/` and the alert rules in `ops/alerts.md`.
- A span tree has the shape and the timings, but no content. For the prompts,
  replies and tool results, open the same run's Trace tab in the console. Its
  `thread_id` is the root span's `alm.thread_id`.
- When the collector is unreachable, spans are dropped after the exporter's
  retries. Runs are not affected.

**The audit trail is append-only.** A trigger refuses UPDATE and DELETE on
`alm_audit` for every role (`alm_audit is append-only`). If the services
connect as a role that does not own the tables, apply the grants once as an
administrator:

```bash
python -m alm_core.store.admin grants --app-role <the services' database user> > grants.sql
# review grants.sql, then run it as the owner or an administrator
```

It only prints SQL. It never connects, and needs no credentials.

**SIEM.** Every audit row is also a log line with `"event": "audit"` and
`"audit": true`. Route those lines to the SIEM with a log sink filtered on
`jsonPayload.audit=true` (or the equivalent filter in CloudWatch or Azure Monitor).

---

## 7e. Images and their security

| Image | Target | Holds |
|---|---|---|
| API and console | `api` | No browser; `ALM_WORKER_CONCURRENCY=0` |
| Run worker | `worker` | Chromium for evidence screenshots |
| Both (today's Cloud Run service) | `all-in-one` | API, embedded workers, Chromium |

**Is this image the one we built?**

```bash
cosign verify <image@digest>   --certificate-identity https://github.com/<owner>/<repo>/.github/workflows/deploy.yml@refs/heads/main   --certificate-oidc-issuer https://token.actions.githubusercontent.com
cosign verify-attestation <image@digest> --type spdxjson ...same flags...   # its SBOM
```

The SBOM is also attached to each deploy run as the artifact `sbom`.

**A deploy stopped at the scan.** Trivy found a fixable HIGH or CRITICAL
vulnerability. Read which package it is in the log:

- A Python package: raise its pin in `requirements-cloud.txt`.
- A Debian package: the build already runs `apt-get upgrade`, so refresh the base image digest in the `Dockerfile`.

**Monthly:**
- refresh the base image digest (`docker pull python:3.13-slim`, then copy the new digest into the `FROM` line);
- refresh the scanner pins in `image.yml` and `deploy.yml`;
- let the PR's image job prove the images still build and pass.

---

## 7f. Recovery

The restore procedure, the drill and why a restored run is safe to resume are
in [DR.md](DR.md). In short: switch to shadow mode, clone the database to the
last good point, repoint the services, re-run as a dry run, then turn writes
back on.

---

## 7g. Releases and promotion

| Step | What happens | Gate |
|---|---|---|
| Pull request | Tests (with Postgres), lint, security (ruff `S`, Semgrep, pip-audit), secrets, image build, scan and read-only checks, Terraform validation on all three clouds | Every check green; a review |
| Merge to `main` | Deploy to **TEST**: build `api` and `worker`, scan, SBOM, sign, verify the signatures, `terraform apply` (`infra/gcp`), connectivity smoke, synthetic dry run | Automatic |
| Promote to **PROD** | Actions → Deploy → Run workflow → `prod`, from `main` | The `prod` environment's required reviewers |

- **Schema.** By default each service migrates on start. Migrations only add, so the old revision keeps working while the new one starts. Where the services must not change the schema (`ALM_AUTO_MIGRATE=false`), run `python -m alm_core.store.migrate` as the owner before the new revision takes traffic. `--check` reports whether the schema is current.
- **Workers during a release.** A worker that gets SIGTERM stops claiming jobs and gives the running one up to a quarter of the lease to finish. Anything still running is taken over from its checkpoint by a new worker. The ledger turns a completed write into a replay.
- **Rollback.** Re-run Deploy on the previous good commit (section 3). The schema stays: an older revision runs on a newer schema version only if no migration was added in between. Otherwise it refuses to start, and the fix is to roll forward.
- **Synthetic check.** With `synthetic_work_item` set in the environment's tfvars (TEST only), the deploy and an hourly schedule queue a dry run of that work item ([ops/alerts.md](../ops/alerts.md)).
- **Other clouds.** `infra/aws` and `infra/azure` are validated skeletons ([infra/README.md](../infra/README.md)). Deploying to one means adding its apply job next to the GCP one; build, scan, SBOM and signing stay as they are.

---

## 8. Connectivity

The failure that looks like everything else.

```bash
gcloud run jobs execute alm-prod-smoke --region <region> --wait  # "python -m alm_core.smoke"
```

- **DNS fails** — the Cloud DNS private zones or the corporate DNS servers on the
  VPC are missing. Deploying successfully does not imply either.
- **TLS fails but DNS resolves** — the corporate CA in the image does not chain
  to the intranet certificate. Rebuild with the right bundle; do not set
  `ALM_TLS_INSECURE`, which is refused in PROD anyway.
- **HTTP times out but TLS connects** — a firewall rule on the circuit.

---

## 9. On-call escalation

| Situation | Action |
|---|---|
| Wrong user provisioned | Shadow mode (§2.1), then §6 to reverse. Preserve the audit row |
| Duplicate comments appearing | Ledger is not being consulted — check `ALM_POSTGRES_DSN` is set and the app can reach it |
| Evidence gate firing repeatedly | The browser session is failing to authenticate. **Do not disable the gate** — it is doing its job |
| Circuit breaker open on EWM/JTS | The upstream is unhealthy. Confirm with the ALM platform team before raising the threshold |
| An approver is refused | `this needs the approver role`: add them to `ALM_ROLE_MAP`. `neither may be the person who started the run`: a second, different approver decides. `already decided`: someone else completed it |
| LLM producing odd extractions | Set `ALM_LLM_ENABLED=false`. Deterministic parsing continues; unparseable rows go to a human |

Anything involving a production write that should not have happened: capture
`run_id`, the `alm_audit` rows and the `alm_idempotency` rows **before** changing
configuration. Restarting the revision does not lose them, but knowing the run id
is what makes the rest of the investigation possible.

---

## 10. Data Retention and Cleanup (30-Day Policy)

**In the cloud** the workers do it. Once a day, the worker holding the
scheduler lease queues `retention-<date>`. That job deletes finished runs
(done, stopped, failed) that last changed more than `ALM_RETENTION_DAYS` ago
(default 30), together with:
- their checkpoints, traces, approval cards, votes, stop requests and finished jobs;
- old webhook deliveries;
- agent memory older than the cutoff.

The ledger and the audit trail are never touched. A run still parked at the
approval gate is never purged. The counts are on the `retention-<date>` run in
the console. If the job failed, its error is there too, and tomorrow's job
tries again. Backups keep purged rows for up to 35 days ([DR.md](DR.md)).

**On a laptop**, local runs and CLI executions generate operational files in `out/` and `out/local/` that may contain transient personal data (usernames, screenshots, query responses).

### Retention Policy
- **Maximum Retention:** 30 days for the agents' checkpoints, approval cards, memory, reports and evidence, and for the CLI's screenshots (`out/screenshots`), user caches (`out/alm_users*.json`) and `comment_capture.json`.
- **Permanent Records:** the agents' ledger and audit trail (`alm_idempotency`, `alm_audit` in `out/local/alm.db`) and the CLI's audit records (`out/audit/`) are kept. They record what was written and who approved it, and the agents' ledger stops a re-run from repeating a write. They hold user IDs and outcomes, never passwords or session cookies.

### Cleanup Procedures
One implementation (`agent_local.py --purge-older-than`) does all of it; the script is a wrapper. Run it periodically or schedule it with Windows Task Scheduler:
```powershell
# Delete data older than 30 days
.\scripts\purge-local.ps1 -Days 30

# Show what would be removed; delete nothing
.\scripts\purge-local.ps1 -Days 30 -DryRun

# The same, directly
.\.venv\Scripts\python.exe src\agent_local.py --purge-older-than 30 --dry-run
```
