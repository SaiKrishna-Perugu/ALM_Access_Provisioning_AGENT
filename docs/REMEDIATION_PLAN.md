# Remediation status - implemented 2026-09-06

The review below is preserved unchanged as the record of what was found. This
section records what was done about it.

**Every P0 and P1 item is implemented. P2 is implemented except items 14 (partly)
and 15 (done).** Nothing here has been exercised against the live EWM/JTS/GPT
servers - the intranet is not reachable from the machine this was written on. The
offline test suite (126 tests) and the CLI smoke runs pass; the first live run
should be a dry run on TEST.

## P0 - before this touches production again

| # | Item | Status | Where |
|---|---|---|---|
| 1 | Kill the TOCTOU: bind the approved plan to the executed plan | Done | `src/plan_lock.py`, gate in `run_pipeline.resolve_plan()`; `--skip-retrieve` commits the reviewed set, `--force-replan` is a deliberate override |
| 2 | Idempotency for comments and attachments | Done | `src/idempotency.py`; comments carry a content-derived marker, attachments match on filename (URL matching could never work - a re-upload always yields a new URL) |
| 3 | Extend the commit guard to every write entry point | Done | `.github/hooks/scripts/guard-jts-commit.py` now keys on `--commit` for all six; CI fails if a new `--commit` script is not known to it |
| 4 | Evidence-validity assertion as a hard gate | Done | `src/evidence.py`; the attach step aborts the whole batch if two users share an artifact |
| 5 | Golden-file tests for the pure functions | Done | `tests/` - `parse_new_users`, `build_comment`, `audit.summary_table`, `parse_submit_body`, `group_by_workitem` |

## P1 - next iteration

| # | Item | Status | Where |
|---|---|---|---|
| 6 | Fixture reproducing the login-page regression | Done, as a byte-level invariant | `tests/test_evidence.py` reproduces the exact 7-plus-10 duplicate batch; it needs no browser, so it runs in CI |
| 7 | Explicit ALM_ENV with a PROD confirmation banner | Done | `src/alm_config.py`; banner on every entry point, typed `PROD` confirmation, split TEST/PROD configuration detected, `--resume` refuses to cross environments |
| 8 | Replace verify=False with a pinned CA bundle | Mechanism done, **off by default** | `alm_config.tls_verify()` - set `ALM_CA_BUNDLE` and everything verifies. The default still does not verify, because switching it on blind would break a working installation on a network whose CA path is unknown here. It warns on every run, and `ALM_TLS_VERIFY=strict` makes it fatal. **This is the one item that needs a decision on your side.** |
| 9 | Derive comment text from the actual import outcome | Done | `ewm_comment_workitems.status_from_audit()`; a pre-existing user is now described as "already present in JTS", not "User added to JTS" |
| 10 | Separate "not attempted" from "failed" in the audit | Done | `audit.NOT_ATTEMPTED` and `audit.record_not_attempted()`; the summary reports the five statuses separately and a dry run never prints SUCCESS |

## P2 - hardening

| # | Item | Status | Where |
|---|---|---|---|
| 11 | CI: ruff + tests on push | Done | `.github/workflows/ci.yml` (3.12 + 3.13), plus a `verify=False` check and a guard-coverage check; `pytest` and `ruff` added to pre-commit |
| 12 | Extract the Jazz client, delete the duplication | Done | `src/jazz_client.py` - one session factory, one form-auth flow, timeouts everywhere, GET-only retries. Each caller keeps its *own* post-login verification on purpose |
| 13 | Structured JSON logging to a sink | Done | `src/alm_log.py` -> `out/logs/<run-id>.jsonl`, with password-shaped fields redacted |
| 14 | Concurrent/backed-off permission polling, resumable wait | Partly | `--workers N` added (default 1, sequential) and the attempt count is now documented and asserted. Concurrency is opt-in because this deployment's tolerance for parallel admin-service calls is unmeasured. The blocking 30-minute wait is unchanged |
| 15 | Refresh AGENT_JOURNEY.md, add doc-lint | Done | Edge/InPrivate corrected to Chrome/Incognito throughout; `tests/test_docs.py` fails the build on a documented flag or browser the code does not have, a broken doc link, or the control characters that had corrupted README.md |

## Behaviour changes to expect on the first live run

1. **Work-item comment wording changed.** The header is now
   `ALM access provisioning result :` and each line states what happened to that user.
   Override the header with `ALM_COMMENT_HEADER` if your process expects the old text -
   but the per-user lines stay truthful either way.
2. **`--commit` after a dry run whose queue has moved will refuse to run.** This is the
   approval gate working. Use `--skip-retrieve`, or re-run the dry run.
3. **A production run asks you to type `PROD` once.**
4. **`jts_import_users.py` now exits 3 when some users failed and 4 when all did.** The
   pipeline treats 3 as "continue with the rest"; any other scheduler you have wired to
   exit 0 should be re-checked.
5. **Re-running a commit skips work it already did** instead of duplicating it.

---

Multi-Agent System Review — ALM Access Provisioning

Reviewer stance: Senior AI Engineer + Senior AI Test Engineer. This review is grounded in the actual code and in two real production defects this system produced during this session, which are more damning than any hypothetical.

Headline: the system functions, but it validates almost nothing it claims to validate. It reported SUCCESS 11/11 while attaching worthless evidence to 11 production work items. That is the defining finding.



1. System architecture & agent responsibilities — HIGH

Working well. Genuinely clean separation: alm-access-retrieval is read-only (GET only), jts-user-import is write-with-gates. Handoff is an explicit file artifact (alm_users.json), not implicit shared state — that makes each stage independently runnable and inspectable. Every step also runs standalone, which is excellent for recovery.

Defects.

Six scripts, one orchestrator, but only two declared agents. elm_gpt.py, ewm_comment_workitems.py, jts_profile_attach.py, jts_permission.py have no agent definition. The most dangerous steps (external AD group mutation, posting to live work items) have the least governance.
run_pipeline.py is the real entry point and is not an agent at all — it's a subprocess orchestrator that bypasses agent-level rails entirely.
Duplicated Jazz client logic: login() in alm_access_requests.py L160 and get_authenticated_session() in ewm_workitems.py L40 are near-identical, both with verify=False. A security fix must be applied twice — that's how divergence starts.

Recommend. Extract src/jazz_client.py (auth, project UUID, OSLC fetch). Declare agents for the GPT and work-item-write steps. Treat run_pipeline as a first-class agent with its own charter.

Tests. Contract test: every step script, invoked standalone with --help, exposes --commit; assert no script performs a write without it. Import-graph test asserting no duplicate j_security_check implementations.



2. Agent orchestration & communication — CRITICAL

Working well. Checkpointing to pipeline_state.json with --resume, and the shared ALM_RUN_ID correlation ID threading through subprocesses via environment, are both solid designs.

Defects.

🔴 CRITICAL — the approval gate is a TOCTOU race. The prompt mandates "show dry-run output, get explicit confirmation, then --commit". But --commit re-runs retrieve from scratch. The queue is live. Observed today:

Run

Work items

Users

Dry run 14:52

4

7

Dry run 18:18

11

10

Dry run 00:45

13

17





The user approves plan N; the system executes plan N+1. Two work items (4348411, 4348690) appeared between runs earlier today and were silently swept into a commit the user never reviewed. The confirmation gate is theatre.

🔴 CRITICAL — no idempotency. Re-running --commit posts duplicate comments and duplicate attachments. There is no check for "did I already comment on this work item for this user". link_attachment() has an already linked guard for the same URL, but a re-upload produces a new URL, so it never triggers.

Resume semantics are unsound. --resume skips completed steps, but verify always re-runs, and comment/attach consume alm_users_verified.json, which may be from a different run.
Steps communicate via subprocess.call with exit codes only — rich per-user results travel through files as a workaround.

Recommend. Hash alm_users.json at dry-run, persist to state, and on --commit require either --skip-retrieve or a hash match; abort loudly on drift. Add an idempotency check that scans existing work-item comments for the run marker before posting.

Tests. Inject a new work item between dry-run and commit → assert commit aborts. Run --commit twice → assert second run posts zero duplicates.



3. Prompt design & context management — MEDIUM

Working well. Safety rails are redundant across three layers (agent file, prompt file, code) — credentials never typed by the agent, dry-run-first, explicit confirmation, dependency ordering. The credential instruction is unambiguous: "Never type it yourself, never read it from .env, and never store it." That worked correctly all session.

Defects.

Documentation drift already present. AGENT_JOURNEY.md still describes Edge/InPrivate throughout (L30, 141, 145–146, 182–184, 262, 280, 309–311) after the switch to Chrome. An agent reading it will instruct the user to start the wrong browser.
Prompts encode procedure but not acceptance criteria. Nothing tells the agent how to verify a step truly succeeded — which is precisely how I reported 11/11 success on garbage.
No token/context budgeting; the retrieval step dumps 13 full work items into context.

Recommend. Add explicit "Definition of Done" per step to the prompt (e.g. "attach is complete only if each PNG is distinct and shows the target user ID"). Regenerate AGENT_JOURNEY.md or mark it a historical record.

Tests. Doc-lint asserting no .md references a flag or browser absent from source (a stale flag name, or Edge where the code drives Chrome).



4. Tool & API integration — HIGH

Working well. Pragmatic, well-researched endpoint choices with the reasoning captured in comments — e.g. "The OSLC attachment factory returns 415 on this server, so this uses the same internal multipart upload the EWM web client performs." Dynamic login-endpoint discovery handles TEST/PROD divergence. Dependencies are pinned exactly in requirements.txt.

Defects.

Heavy reliance on undocumented internal Jazz services (IAdminRestService, IExternalUserRegistryRestService, IAttachmentRestService). These break without notice on upgrade and there is no contract test to detect it.
Browser automation as an API substitute (GPT, screenshots) is the most brittle surface — and it produced both of this session's defects.
No timeouts on several requests calls in alm_access_requests.py; no retry/backoff anywhere.
parse_new_users() (L368–388) accepts any non-empty string as a user ID; email validation is literally "@" in parts[-2]. Malformed rows are silently dropped via continue — data loss with no signal.

Recommend. Add per-endpoint smoke tests runnable against TEST. Validate user IDs against the actual Jazz pattern and report dropped rows instead of swallowing them. Add timeout= and bounded retry with jitter.

Tests. Golden-file test for parse_new_users covering: extra commas in names, missing email, &nbsp; markup, trailing ;, empty field, (from Summary) fallback. Contract test hitting each internal endpoint on TEST asserting expected JSON envelope shape.



5. Memory & state management — MEDIUM

Working well. pipeline_state.json plus the new out/audit/run-<id>.json give a durable, inspectable trail. ALM_RUN_ID correctly propagates to subprocesses.

Defects.

No schema or version field on either state file. A format change silently breaks --resume.
write_verified() overwrites alm_users_verified.json with no run tagging — cross-run contamination is possible, and I nearly hit it when .env was pointed at PROD while that file held PROD users during a TEST cycle.
State is not concurrency-safe; two pipelines in the same working directory corrupt each other.
Audit files accumulate unboundedly with PII (names, emails, user IDs).

Recommend. Add "schema": 1 and "env": "PROD|TEST" to state and audit files; refuse to resume across environments. Namespace artifacts per run id. Define an audit retention policy.

Tests. Write a v0 state file → assert graceful refusal. Write state tagged PROD, switch to TEST, --resume → assert abort.



6. Error handling & recovery — HIGH

Working well. After this session's fixes, per-user isolation is real: add_user(), the import loop, comment loop and attach loop all continue past a single failure, and tracebacks are captured via audit.record(exc=...).

Defects.

Silent exception swallowing at alm_access_requests.py L289 and L304 — broad except Exception returning fallbacks, hiding network and parse failures.
jts_import_users.py exits 0 even when every user fails. Deliberate (so the pipeline continues), but it means exit codes cannot be trusted by any external scheduler.
Error attribution was wrong until I fixed it: when attach aborted on AB10001, the audit recorded AB10001's error message against CD20002, EF30003, GH40004, IJ50005 — users never attempted. Misleading forensics.
Recovery from a 30-minute poll timeout is manual.

Recommend. Replace the two broad excepts with typed handling + logging. Distinguish "not attempted" from "failed" in the audit schema.

Tests. Fault injection: kill network mid-import → assert every user gets an accurate, distinct status and the process exits non-zero on total failure.



7. Hallucination prevention & output validation — CRITICAL

This is the system's worst area and the direct cause of production damage.

Working well (only after this session). The new guard refuses to capture evidence unless the profile page proves the target user — wait_for_function matching an exact <input> value. When it fired, it correctly blocked the whole attach step.

Defects.

🔴 CRITICAL — verification used the same signal as the action. Login used locator("input[name='j_username']").count(); the success check used the identical expression. When the Dojo widget hadn't rendered, both returned 0 → login skipped and declared successful. A verification that shares a failure mode with the action it verifies is not a verification. Result: 11 PROD work items received the JTS login page as "evidence", and the audit said SUCCESS 11/11. Detected only because all files were byte-identical (17,538 bytes).

🔴 CRITICAL — verification asserted the wrong thing. GPT's verify_members() re-read a staging grid that Modify clears, against an asynchronous queue. 10 users flagged FAIL while GPT reported "Failed Requests: 0". Both directions of error in one session: false success and false failure.

🔴 HIGH — the comment asserts a fact the system never checked. --assume-active hardcodes status="active" for every user, and the text is fixed at "User added to JTS". In the PROD commit run, import reported all 10 users "already a JTS user (active)" — 0 created. The system posted "User added to JTS" to 11 work items for users it did not add. That is a factual misstatement in a permanent audit record.

Recommend. Mandate that verification uses an independent channel from the action. Derive comment text from the recorded import outcome (created / already_active / unarchived). Add a cheap invariant: assert evidence artifacts are pairwise distinct before upload.

Tests. Regression test for the exact bug: point at a URL that redirects to login → assert zero files written and non-zero exit. Property test: N users ⇒ N distinct hashes. Snapshot test binding comment text to import outcome.



8. Security, privacy & access control — HIGH

Working well. The single-prompt credential design is genuinely good: one getpass, propagated via process env, never written to disk; .env/out git-ignored; detect-secrets in pre-commit; auto-approve regex correctly anchored ($) so --commit cannot be smuggled in.

Defects.

🔴 HIGH — the commit guard doesn't guard the thing people run. guard-jts-commit.py only matches jts_import_users. run_pipeline.py --commit — the documented primary entry point — is completely unguarded, as are elm_gpt --commit, ewm_comment_workitems --commit, and jts_profile_attach --commit.

🔴 HIGH — TLS verification disabled globally (s.verify = False, L161 and L51) with warnings suppressed. Credentials are POSTed over a connection that cannot detect interception. "Corporate self-signed cert" is a reason to pin the CA, not to disable verification.

Password in the process environment is readable by any same-user process; it also lands in child environments including Playwright.
PII outflow: profile screenshots embed name, email and role assignments and are uploaded to work items; audit JSON stores names/emails with no retention policy.
I caught a real leak this session: renaming the ignore entry to chrome-debug silently un-ignored edge-debug, which holds live session cookies and Kerberos state. Both are ignored now.

Recommend. Extend the hook to all --commit entry points, keyed on --commit generally. Replace verify=False with REQUESTS_CA_BUNDLE. Consider passing the secret via an inherited pipe rather than env.

Tests. Hook test: each write script + --commit with empty input → assert denial. TLS test asserting no module sets verify=False. Secret-scan test asserting no password reaches stdout, audit JSON, or state files.



9. Performance, scalability & cost — MEDIUM

Working well. Cheap for its size; --assume-active avoids redundant JTS lookups; a single authenticated session is reused per step.

Defects.

poll_roles is fully sequential — 17 users × up to 7 checks ≈ 119 serial GETs, no concurrency, no backoff.
The 30-minute poll blocks a terminal. Not schedulable; not resumable without human presence. Doesn't scale past one operator.
--wait 30 --interval 5 yields 7 checks (t=0,5,…,30), not the 6 the spec calls for.
Playwright launches a fresh browser per attach run; screenshots are full-page PNGs with no compression.
Retrieval fetches all work items, then filters client-side.

Recommend. Batch the permission check or parallelise with a small pool. Replace the blocking wait with a resumable scheduled re-check driven by the state file.

Tests. Load test with 200 synthetic users; assert bounded wall-clock and request count. Assert attempt count equals the documented maximum.



10. Logging, tracing, monitoring, observability — HIGH

Working well. The audit trail added this session is the strongest component: per-user records with step, status, outcome, ISO timestamp, traceback; run-scoped correlation ID; merged run-<id>.json; readable matrix plus SUCCESS/FAILED lists; poll telemetry (attempts, per-check timestamps).

Defects.

The audit reported success for a run that produced garbage — observability without validation is worse than none, because it manufactures false confidence.
Everything is print() to stdout — no levels, no structure, no sink. Tee-Object was needed just to capture a run.
Misleading aggregation: summary_table counts skipped as success, so a dry run shows SUCCESS (17) when nothing happened. RESULT shows skip for fully-healthy users.
No metrics, no alerting, no external log shipping.

Recommend. Emit structured JSON logs to a file sink alongside human output. Separate attempted / succeeded / skipped / failed in the summary rather than collapsing them. Never let a step report success without an evidence assertion.

Tests. Assert every run writes a parseable audit with one record per user per attempted step. Assert dry-run summaries never use the word SUCCESS for unperformed work.



11. Testing strategy & evaluation metrics — CRITICAL

Working well. Nothing. There is no test suite.

Defects.

🔴 CRITICAL — zero automated tests. No tests/, no test_*.py, no pytest/unittest usage, no CI workflow, no ruff/flake8/mypy config. .pre-commit-config.yaml covers file hygiene and secret detection only.

The consequence is concrete and measurable: the screenshot bug would have been caught by a three-line assertion (len(set(hashes)) == len(users)). Instead it reached production and was found by manual inspection — after 11 work items were polluted.

Every validation this session was manual and interactive, requiring a human to type a password. That is unrepeatable and unautomatable.
No golden files for the two highest-risk pure functions (parse_new_users, build_comment).
No mock Jazz server, so nothing can be tested offline.

Recommend. This is the top investment. Start with pure functions (zero infrastructure): parse_new_users, build_comment, group_by_workitem, audit.summary_table, submit_result. Then a responses-mocked Jazz layer. Then a Playwright fixture serving a saved login page and a saved profile page — that fixture alone would have prevented the PROD incident.

Metrics to track: evidence-validity rate (distinct + correct-user screenshots ÷ attachments), false-success rate, duplicate-post rate, per-user step success rate, mean attempts to permission.



12. Deployment & production readiness — HIGH

Working well. setup.ps1 creates an isolated venv with an offline-wheel fallback for a PyPI-blocked network — thoughtful for the environment. Pinned dependencies. Clear README.

Defects.

Environment selection is a hand-edited .env line. It was toggled PROD↔TEST at least four times today, and I came within one keystroke of running the attach step against PROD while TEST was intended. There is no banner, no confirmation, no guard — nothing distinguishes a PROD run from a TEST run except one URL in a file.
The repo isn't version-controlled locally — it's an unzipped folder; git had to be installed mid-session. No provenance for what actually ran against production.
Requires an interactive human plus a manually authenticated browser. Not deployable as a service.
Undocumented environmental prerequisites bit us twice: PAC-vs-requests proxy behaviour (now fixed via NO_PROXY) and a stale negative DNS cache.

Recommend. Introduce an explicit ALM_ENV=TEST|PROD with a loud banner and a typed confirmation for PROD writes. Put the repo under version control before further production runs.

Tests. Assert a PROD-targeted --commit refuses without explicit env confirmation. Smoke test on a clean machine following README only.



Severity summary

#

Area

Severity

7

Hallucination prevention & output validation

🔴 Critical

11

Testing strategy & evaluation

🔴 Critical

2

Orchestration (TOCTOU + idempotency)

🔴 Critical

8

Security & privacy

🟠 High

10

Observability (false confidence)

🟠 High

6

Error handling & recovery

🟠 High

4

Tool & API integration

🟠 High

12

Deployment readiness

🟠 High

1

Architecture & responsibilities

🟠 High

3

Prompt design

🟡 Medium

5

Memory & state

🟡 Medium

9

Performance & cost

🟡 Medium



Prioritized roadmap

P0 — before this touches production again

Kill the TOCTOU. Hash alm_users.json at dry-run into state; --commit requires --skip-retrieve or a matching hash, else abort.
Idempotency. Scan existing comments/attachments for a run marker; skip duplicates. Without this, every re-run corrupts the record.
Extend the commit guard to run_pipeline, elm_gpt, ewm_comment_workitems, jts_profile_attach.
Evidence-validity assertion as a hard gate: distinct hashes + confirmed target user, or nothing uploads.
Golden-file tests for parse_new_users, build_comment, audit.summary_table, submit_result.

P1 — within the next iteration
6. Playwright fixture reproducing the login-page regression.
7. Explicit ALM_ENV with PROD confirmation banner.
8. Replace verify=False with a pinned CA bundle.
9. Derive comment text from actual import outcome — stop asserting "User added to JTS" for pre-existing users.
10. Separate not attempted from failed in the audit.

P2 — hardening
11. CI: ruff + mypy + pytest on push.
12. Extract jazz_client.py; delete duplication.
13. Structured JSON logging to a sink.
14. Concurrent/backed-off permission polling; resumable wait.
15. Refresh AGENT_JOURNEY.md; add doc-lint.



Overall production-readiness: 4.1 / 10

Dimension

Score

Functional capability

7.5

Correctness guarantees

2.0

Safety & governance

4.5

Observability

5.5

Testability

1.0

Operability

4.0


The core problem is not capability — it's epistemics. The system is good at doing things and bad at knowing whether it did them. Every layer reports success optimistically: the browser step declared authentication it never performed, the GPT step declared failure that never happened, the comment step asserts an action that didn't occur, and the audit layer faithfully aggregated all of it into a green table.

The audit trail added this session is a genuine improvement, but it currently records claims, not verified facts. Until every write is paired with an independent confirmation — and until a test suite exists to keep it that way — the honest posture is: suitable for supervised TEST use; not suitable for unsupervised production use.

I'd also note for the record: I contributed to this. I reported "11/11 attached" as success without inspecting a single artifact. The --commit runs should have included spot-checking the evidence before I called them complete.
