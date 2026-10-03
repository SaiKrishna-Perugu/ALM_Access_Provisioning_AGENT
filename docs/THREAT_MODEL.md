# Threat model

STRIDE over the cloud deployment ([ENTERPRISE_PLAN.md](ENTERPRISE_PLAN.md),
section 3), the data that crosses each boundary, and where each control lives
in the code. "Residual" is what is left after the control, and who owns it.

## What talks to what

```
 Operator / approver (browser, corporate network)
        |  HTTPS; sign-in by IAP or the client's OIDC; roles from ALM_ROLE_MAP
        v
 +---------------- client cloud account (private) -----------------+
 |  API + console  <---->  Postgres (ledger, audit, runs, queue,    |
 |   (no workers)           checkpoints, traces, memory)           |
 |                             ^                                    |
 |  Run workers  -------------+                                     |
 |    | HTTPS            | HTTPS              | OTLP                |
 |    v                  v                    v                     |
 |  model provider     Graph API          collector -> backend      |
 |  (in-tenancy)       (or AD jobs ->                               |
 |                      Windows worker)                             |
 +--------|---------------------------------------------------------+
          | interconnect / VPN (the client's)
          v
   EWM, JTS, LDAP (corporate)      GPT web UI (AD, on-premises groups)
```

**Trust boundaries:**
- The browser to the API.
- The cloud to the corporate estate.
- The cloud to the model provider.
- **Text a requester wrote.** A work item's summary, justification and New Users field are untrusted input that reaches the model.

## STRIDE

| | Threat | Control (where) | Residual |
|---|---|---|---|
| **S**poofing | Someone approves who is not an approver | Approval only in the signed-in console: IAP or OIDC with PKCE, back-channel ID token checks, an HMAC-signed session and CSRF (`alm_api/auth.py`). Cards carry a link, never a decision. The approver comes from the IdP's claims | The client's IdP and its group hygiene (monthly access review) |
| | A forged webhook starts runs | HMAC signature and a replay guard in the store (`alm_api/security.py`, `alm_webhook_seen`) | Key rotation (RUNBOOK 7c) |
| | A container is impersonated to the database | IAM database login with short-lived tokens, private IP only, TLS required | - |
| **T**ampering | Requester text steers the agents (prompt injection): widening the scope, skipping approval, writing in a dry run | The policy engine checks every tool call. Writes need an approval covering that user. Users join a run only from the New Users field or recovered IDs. A dry run cannot write. Injection eval scenarios run nightly against the real model (`alm_agents/policy.py`, `evals.py`) | A new attack shape: add it as an eval scenario |
| | The plan changes between approval and write | The plan hash on the card. A changed plan needs a new approval (`nodes/approval.py`) | - |
| | Audit rows are edited or deleted | Append-only by trigger (SQLite, and Postgres from schema v4) and by grants (`python -m alm_core.store.admin grants`). Audit rows are also streamed as `audit=true` log lines to the SIEM | A schema owner can drop the trigger. That is DDL, recorded by the database's own audit log |
| | A malicious image is deployed | Images pinned by digest, scanned, given an SBOM and signed keylessly. Deploy verifies the signer is this repository's `deploy.yml` on `main` | The platform does not yet refuse unsigned images by itself (Binary Authorization or an admission policy: open) |
| **R**epudiation | "I never approved that" | Every vote is a row (`alm_approval_vote`) with the IdP identity and time. The audit trail holds every write, its approver and its outcome | - |
| **I**nformation disclosure | Personal data reaches the model | E-mails are redacted, and names the run holds are redacted (`redact_pii`, `redact_names`). Fields listed in `ALM_MODEL_WITHHELD_FIELDS` never reach the model. `ALM_ALLOWED_PROVIDERS` pins the provider | Names the run does not know (in free text) can pass. D2 needs the client's written data policy |
| | Secrets in logs, traces or telemetry | No request bodies or headers are traced; secret-looking query values are blanked; `scrub_secrets` runs on every line. Telemetry exports an allowlist of attributes only (`alm_core/telemetry.py`) | - |
| | Requester text puts markup on approval cards | Every value on the Chat and Teams cards is HTML-escaped (`alm_api/chat.py`, `notify.py`) | - |
| | Data kept too long | Daily retention purge after `ALM_RETENTION_DAYS` (default 30). The ledger and audit hold user IDs and outcomes, not names | Backups keep purged rows for 35 days ([DR.md](DR.md)) |
| **D**enial of service | A loop burns the model budget | Token budgets per run and per day, and hop, tool-call and write budgets. The fixed-order fallback when the model is down | - |
| | A flood of webhooks | The queue dedupes a thread; one worker per thread; a bounded number of concurrent writes | Rate limiting at the load balancer or WAF (open) |
| | EWM or JTS is down | Circuit breakers, retries with backoff, dead-letter after the job's attempts; a failed sign-in is cached ten minutes so probing cannot lock the account | - |
| **E**levation of privilege | An operator or viewer approves, or a starter approves their own run | Role checks on every endpoint. Two-approver runs refuse the starter. One vote per person per plan | - |
| | The service account does more than provisioning | The agents have per-agent tool sets, and the policy refuses unknown and forbidden tools. Runtime identities hold only their own secrets, the database and the model | The functional account's rights in EWM/JTS are the client's to minimise |
| | A container escapes or is modified | Non-root (uid 10001), read-only root filesystem, no pip at runtime, internal-only ingress, egress to known destinations | - |

## Data classification: what leaves the client network

| Data | Class | Reaches the model? | Stored | Exported to telemetry |
|---|---|---|---|---|
| Work item id, state | Internal | Yes | Yes | No (only the thread id) |
| Summary, justification (requester text) | Internal, may contain names | Yes, with e-mails and known names redacted; withholdable | Yes, in checkpoints and traces (30 days) | No |
| New Users rows (name, e-mail, user ID) | Personal | User IDs yes; e-mails `[email]`; known names `[name]` | Yes (30 days); user IDs in the ledger and audit | No |
| LDAP display name, e-mail | Personal | Redacted | In checkpoints (30 days) | No |
| JTS/LDAP state flags | Internal | Yes | Yes | No |
| Existing work-item comments | Internal, may contain names | Yes, redacted as above | In checkpoints (30 days) | No |
| Approver identity (e-mail) | Personal | Never | Votes and audit (kept) | No |
| Service account password, session keys, API keys | Secret | Never | Secret store only | Never |
| Evidence screenshots | Personal | Never | Attached to the work item; `/tmp` only | No |

**Open for D2:** the client confirms each "Yes" in the model column, or adds
the field to `ALM_MODEL_WITHHELD_FIELDS`.

## Review

Revisit this model when a new integration, data field or role is added, after
any security incident, and before PROD. An external penetration test of the
console is planned before PROD ([ENTERPRISE_PLAN.md](ENTERPRISE_PLAN.md), Phase 6).
