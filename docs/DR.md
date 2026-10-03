# Disaster recovery

What can be lost, how fast it comes back, and how to prove it before you need
it. Written against the GCP Terraform in `infra/`. The same reasoning holds on
AWS (RDS with PITR) and Azure (Flexible Server with PITR), because the design
keeps all durable state in one Postgres database.

## Where the state is

| State | Where | Lost with | Recovery |
|---|---|---|---|
| Idempotency ledger, audit trail | Postgres (`alm_idempotency`, `alm_audit`) | The database | PITR restore |
| Runs, job queue, approvals, votes, traces, agent memory | Postgres | The database | PITR restore |
| Run checkpoints (where a run is up to) | Postgres (LangGraph tables) | The database | PITR restore, consistent with the ledger |
| Secrets (service account password, keys) | Secret Manager | Deleting the secret | Secret versions; re-enter from the owner |
| Images | Artifact Registry, signed | Deleting the repository | Rebuild from the commit; the signature names it |
| Infrastructure | Terraform state in the state bucket | Deleting the bucket | Object versioning on the bucket; re-apply |
| AD jobs in flight | Pub/Sub (one day of retention), or the job table | The topic | The run's ledger entry stays "submitted"; reconcile re-checks |
| Evidence screenshots, local traces | Container `/tmp` | Any restart | By design: the screenshot is attached to the work item, and the trace is in the database |

Nothing durable lives in a container. Any instance can be replaced at any
time, and a dead worker's runs are taken over from their checkpoints.

## Objectives

| | Target | Why it holds |
|---|---|---|
| **RPO** (data lost) | 5 minutes | Point-in-time recovery, with 7 days of transaction logs (`data.tf`) |
| **RTO** (time to working) | 1 hour | Restore a clone, repoint `ALM_POSTGRES_DSN`, redeploy. To be measured in the first drill and corrected here |
| Zone loss (PROD) | No data loss, failover in about a minute | `availability_type = REGIONAL` |
| Region loss | RPO is the last backup (daily, 35 kept); RTO is hours | Restore a backup into another region and apply the Terraform there |

## Why a restore is safe to resume from

The ledger, the audit trail and the run checkpoints are in the same database,
so a point-in-time restore brings them back **consistent with each other**.
What the restore cannot roll back is the outside world: a write EWM, JTS or AD
accepted after the restore point.

A run resumed or re-run after a restore meets those writes like this:

| Write | What happens on a re-run | Duplicate? |
|---|---|---|
| JTS account created or reactivated | The validator reads the registry first and finds the account active | No |
| Work-item comment | `existing_work_item_comments` finds the earlier comment ("already reported") | No |
| Evidence attachment | Attached again to the same work item | Possibly a second copy of the same screenshot |
| AD group membership | The directory treats an existing member as success (Graph), or the GPT request is a no-op | No new access, at worst a second request |

So the worst case after a restore is a repeated screenshot or a repeated GPT
request, never a second account or wider access. After a restore, set
`ALM_SHADOW_MODE=true`, run the sweep as a dry run, read the plan, then turn
writes back on.

## Restore (the procedure the drill follows)

1. **Stop writes.** Set `ALM_SHADOW_MODE=true` on the services (RUNBOOK §2). Workers finish their current step and stop writing.
2. **Pick the point.** This is the last moment you trust: before the bad migration, the deletion, or the incident.
3. **Clone to that point** (a new instance; the original is kept for forensics):
   ```bash
   gcloud sql instances clone alm-<env>-pg alm-<env>-pg-restore \
     --point-in-time "2026-10-03T10:15:00Z"
   ```
   From a backup instead (region loss): `gcloud sql backups list --instance alm-<env>-pg`,
   then `gcloud sql backups restore <id> --restore-instance alm-<env>-pg-restore`.
4. **Point the services at it.** For a lasting change, use the Terraform (import the clone, or rename it). For speed, use `gcloud run services update ... --update-env-vars ALM_POSTGRES_DSN=...`, keeping `?sslmode=require` and IAM login. The schema migrates itself, and a database newer than the code is refused.
5. **Check.**
   - `GET /status` is green.
   - `GET /runs` shows the runs you expect.
   - `SELECT max(at) FROM alm_audit` is about the restore point.
6. **Re-run as a dry run.** Queue the sweep (`POST /admin/reconcile`) with shadow mode still on. Read the plan.
7. **Turn writes back on** (`ALM_SHADOW_MODE=false`) once the plan contains nothing surprising. Record the incident, the restore point and the re-run's thread id.

## The drill

Before PROD goes live, and then every six months, on TEST:

1. Note the time, start a dry run and a writing run, and approve the writing run.
2. Restore a clone to a point between the two (steps 3–5 above), against a copy of the TEST services.
3. Measure the time from "start" to `/status` green: that is the RTO. Write it in the table above.
4. Confirm that the writing run's writes replay or are found, and none are repeated (the ledger and the audit rows show it).
5. Delete the clone.

## Retention is not a backup

The daily retention purge (`ALM_RETENTION_DAYS`, default 30) deletes finished
runs' checkpoints, traces, approval cards and agent memory. It never deletes
the ledger or the audit trail. Backups and PITR still hold purged rows until
they age out (35 days of backups), so a purge can be undone within that window
by restoring a clone. That also means personal data in purged rows lives on in
backups for up to 35 days. State that in the client's records of processing.

## Encryption

Cloud SQL encrypts at rest by default. If the client requires its own key,
set `db_kms_key` to a Cloud KMS key in the same region. Before applying, grant
the Cloud SQL service agent `roles/cloudkms.cryptoKeyEncrypterDecrypter` on
that key. A key can only be set when the instance is created; changing it later
means restoring into a new instance.
