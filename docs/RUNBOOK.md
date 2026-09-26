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

In order of severity. All three are reversible.

1. **Stop writing, keep observing** — set `ALM_SHADOW_MODE=true` and restart the
   revision. Runs continue and record what they *would* do. This is the right
   first move in almost every incident.
2. **Stop triggering** — set `ALM_RECONCILE_INTERVAL_MINUTES=0` and rotate the
   webhook HMAC secret in Secret Manager. Parked runs stay parked.
3. **Stop everything** — scale the Container App to zero replicas. In-flight
   runs are checkpointed and resume when it comes back; in-flight *writes* leave
   a claim in the ledger that expires after 15 minutes.

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

**Database schema:** `SCHEMA_SQL` is additive (`CREATE TABLE IF NOT EXISTS`), so
an older image runs against a newer schema. A migration that ever removes a
column must be split across two releases — the old revision must keep working
while traffic is still on it.

**LangGraph checkpoints** are keyed by thread id and version. A rollback across a
state-shape change can leave a parked run unresumable; if that happens, reject
the approval to close it out and let the reconciliation sweep pick the work item
up fresh. The ledger stops the retry from duplicating anything already done.

---

## 4. Dead-letter replay

AD jobs move to `alm-ad-provisioning-dead-letter` after 5 delivery attempts, or
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

An expired approval is not a failure — the run halts and the work item is swept
again by the next reconciliation. If cards are not arriving, check
`ALM_CHAT_WEBHOOK_URL` and the `approval_notification_failed` log event; the
run is still reachable at `/approvals/{thread_id}` regardless, because a card
that fails to send must not lose the run.

**Do not approve on someone's behalf.** The audit row records the caller's IAP
identity, and it is the only evidence of who authorised a production change.

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
| Costs climbing | Every hop is a model call. Lower `ALM_MAX_HOPS`, or set `ALM_SUPERVISOR_MODEL` to a cheaper model |
| Agents stop with `model unavailable: ...ResourceExhausted` or HTTP 429 | The Gemini quota. Lower `ALM_LLM_REQUESTS_PER_MINUTE`, or move to `ALM_LLM_PROVIDER=vertex`, whose quota is the project's |
| Agents stop with `model unavailable` naming a 400 or 403 | The API key was revoked or the model id retired. Run `python src/agent_sandbox.py --check` with the same settings |

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
| `ALM_TLS_INSECURE cannot be set when ... PROD` | Production requires verified TLS: set `ALM_CA_BUNDLE` |
| AD step failed: `no debug Chrome at ...` | Run `scripts\start-gpt.ps1`, sign in to GPT, then `--resume` the run |
| `the agent model is unavailable` | Key or model problem: `python src/agent_local.py --check` |
| Closed the terminal at the approval prompt | Nothing was written. `--resume <thread-id>` brings the prompt back |
| A second run reports `(replay)` | Correct: the ledger recorded the first write. Nothing was repeated |

Everything a local run did is in `out/local/run-<run_id>.json` and in the
`alm_audit` table of `out/local/alm.db` (the SQL in section 1 works unchanged in
any SQLite client).

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
| Approval card links rejected | The signing key was rotated, or the plan changed after the card was sent. Trigger a fresh run |
| LLM producing odd extractions | Set `ALM_LLM_ENABLED=false`. Deterministic parsing continues; unparseable rows go to a human |

Anything involving a production write that should not have happened: capture
`run_id`, the `alm_audit` rows and the `alm_idempotency` rows **before** changing
configuration. Restarting the revision does not lose them, but knowing the run id
is what makes the rest of the investigation possible.
