# Service levels and alerts

What "working" means for the ALM agents in the cloud, and when someone is
paged. The numbers come from the metrics in `src/alm_core/telemetry.py`,
exported over OTLP when `ALM_OTEL_ENABLED=true`. The queries are PromQL, which
Google Managed Prometheus, Amazon Managed Prometheus, Azure Monitor's managed
Prometheus and Grafana all accept. Each name below is how an OpenTelemetry
collector's Prometheus exporter names the metric.

| Metric | Type | Labels |
|---|---|---|
| `alm_runs_total` | counter | `outcome`: done, halted, stopped, awaiting_approval |
| `alm_jobs_total` | counter | `kind`: start, resume, reconcile; `result`: done, queued (will retry), dead |
| `alm_writes_total` | counter | `operation`, `outcome` |
| `alm_replays_total` | counter | `operation` |
| `alm_policy_denials_total` | counter | `agent`, `tool` |
| `alm_model_tokens_total` | counter | `model`, `direction`: input, output |
| `alm_model_latency_seconds` | histogram | `model` |
| `alm_approval_wait_seconds` | histogram | `environment` |
| `alm_queue_depth` | gauge | `status`: queued, running, done, dead |
| `alm_worker_busy` | gauge | `worker` |

Spans carry the same story per run: `run <job>` → `agent <name>` → `chat`,
`tool`, backend and `http` spans. They hold no prompts, replies, user IDs or
error messages; the run's own trace (console, Trace tab) has those.

## Service level objectives

| SLO | Objective | Window | Measured as |
|---|---|---|---|
| Jobs complete | 99% of jobs do not end dead | 28 days | `alm_jobs_total{result="dead"}` / `alm_jobs_total{result!="queued"}` |
| Writes succeed | 98% of attempted writes end `ok` | 28 days | `alm_writes_total{outcome!="ok"}` / `alm_writes_total` |
| The model answers | 95% of model calls in under 30 s | 7 days | `alm_model_latency_seconds` histogram |

A replay is not an error: it is the ledger refusing to repeat a write.

## Alerts

### Page: jobs are dying (fast burn of the jobs SLO)

Both windows must fire: 14.4x the budget over an hour, still burning now.

```promql
(
  sum(rate(alm_jobs_total{result="dead"}[1h]))
    / sum(rate(alm_jobs_total{result!="queued"}[1h])) > (14.4 * 0.01)
)
and
(
  sum(rate(alm_jobs_total{result="dead"}[5m]))
    / sum(rate(alm_jobs_total{result!="queued"}[5m])) > (14.4 * 0.01)
)
```

Runbook: section 4 (dead-letter replay). Read the dead job's error in
`GET /queue`, then the run's trace.

### Ticket: jobs are dying slowly

```promql
(
  sum(rate(alm_jobs_total{result="dead"}[6h]))
    / sum(rate(alm_jobs_total{result!="queued"}[6h])) > (6 * 0.01)
)
and
(
  sum(rate(alm_jobs_total{result="dead"}[30m]))
    / sum(rate(alm_jobs_total{result!="queued"}[30m])) > (6 * 0.01)
)
```

### Page: writes are failing

```promql
sum(increase(alm_writes_total{outcome!="ok"}[1h]))
  / sum(increase(alm_writes_total[1h])) > 0.10
and sum(increase(alm_writes_total[1h])) >= 5
```

Usually EWM or JTS is down, or the service account lost a permission. Check
`GET /status`. Do not re-run anything until the cause is known: the ledger makes
a re-run safe, but not useful.

### Ticket: the queue is not draining

```promql
max(alm_queue_depth{status="queued"}) > 20
and max(alm_queue_depth{status="running"}) == 0
```

Held for 15 minutes. Workers are down, or none can reach the database.
Runbook section 5 (stuck claims).

### Ticket: the model is slow

```promql
histogram_quantile(0.95,
  sum by (le, model) (rate(alm_model_latency_seconds_bucket[15m]))) > 30
```

Held for 30 minutes. The provider is degraded or the requests-per-minute limit
is too low. With `ALM_DEGRADE_ON_MODEL_FAILURE=true`, a model that fails outright
re-runs the work in the fixed order.

### Ticket: policy denials spike

```promql
sum(increase(alm_policy_denials_total[1h])) > 30
```

Some denials are normal: an agent tries to write before approval and is told
no. A spike after a prompt, roster or model change means the agents are
fighting the policy. Compare runs' `version` (runbook 7a) and run the evals.

### Ticket: 80% of the daily token budget

Replace `DAILY_CAP` with `ALM_MAX_TOKENS_PER_DAY`. At 100%, new runs are
refused or degrade, so this is the warning before that.

```promql
sum(increase(alm_model_tokens_total[24h])) > 0.8 * DAILY_CAP
```

### Ticket: approvals waiting

```promql
histogram_quantile(0.5, sum by (le) (rate(alm_approval_wait_seconds_bucket[1d]))) > 4 * 3600
```

The median approval took more than four hours. That is not an outage, but
the default approval lifetime is four hours (`ALM_APPROVAL_TTL_MINUTES`), so
cards are expiring. Check that the announcements reach the approvers.

## Synthetic check

On TEST, schedule a dry run of one known work item every hour, for example with
Cloud Scheduler, EventBridge Scheduler or a Logic App:

```bash
curl -fsS -X POST "$API/runs" -H "Authorization: Bearer $ID_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "Dry run work item <a closed TEST work item>", "mode": "dry"}'
```

This works behind IAP (`ALM_AUTH_MODE=iap`). The caller is a service account
with the operator role in `ALM_ROLE_MAP`, sending an ID token for the IAP
audience. In OIDC mode the API accepts browser sessions only. There, schedule
the worker image with this command instead, which queues the same dry run:

```bash
python -m alm_agents.worker synthetic <a closed TEST work item>
```

Alert when no `alm_runs_total{outcome="done"}` arrives for two hours. A dry
run writes nothing, so this is safe against the real estate, and it exercises
sign-in to EWM and JTS, the model and the queue end to end.
