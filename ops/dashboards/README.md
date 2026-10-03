# Dashboards

The same ten panels, in two forms. Both read the metrics in
[`ops/alerts.md`](../alerts.md) with PromQL.

| File | Where | How to load |
|---|---|---|
| `alm-agents.grafana.json` | Grafana: self-hosted, Amazon Managed Grafana, Azure Managed Grafana, Grafana Cloud | Dashboards → Import, and pick the Prometheus data source |
| `alm-agents.gcp-monitoring.json` | Google Cloud Monitoring with Managed Prometheus | `gcloud monitoring dashboards create --config-from-file=ops/dashboards/alm-agents.gcp-monitoring.json` |

The panels show:
- runs by outcome;
- the share of jobs that died;
- writes and replays;
- policy denials by agent;
- model tokens and p95 latency;
- the median approval wait;
- queue depth and how busy each worker is.

Per-run detail is in the spans (`run` → `agent` → `chat`/`tool`/`http`) and,
with prompts and replies, in the run's own trace in the console.

When a panel changes, change it in both files.
`tests/test_telemetry.py` checks that every metric named here and in the alert
rules is one the code exports.
