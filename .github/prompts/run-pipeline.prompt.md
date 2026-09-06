---
description: 'Run the full ALM provisioning pipeline: retrieve access-request user IDs, optionally provision the GPT AD group, import users into JTS, wait for the JazzUsers permission to propagate (poll up to 30 min), then post a success comment and attach a JTS profile screenshot to each work item for VERIFIED users only.'
---
Run the end-to-end ALM provisioning pipeline:

1. Dry run first, in the terminal at the repository root:
   `python src/run_pipeline.py`
   (CID and servers come from `.env`; the password is prompted ONCE and typed directly
   into the terminal - the orchestrator hands it to the child steps via the process
   environment, never storing it. Every step runs in dry-run form and the permission
   check is a single pass instead of the 30-minute poll.)
2. Show the user the dry-run output: the retrieved work items/users, the JTS import plan,
   the current JazzUsers verification state, and the comment/attach plan. Ask for explicit
   confirmation before committing.
3. After the user confirms, commit:
   `python src/run_pipeline.py --commit`
   The commit run re-fingerprints the queue and compares it to the plan the dry run
   recorded. If the queue changed in between it prints `[STOP] APPROVAL GATE`, names
   the users and work items that appeared or vanished, and writes nothing. That is
   correct behaviour, not a bug - do NOT pass `--force-replan` to get past it. Either
   re-run the dry run and get the new plan approved, or commit exactly the reviewed
   set with `--skip-retrieve`.
   If the servers are PRODUCTION the run asks the operator to type `PROD` once.
   The user types it; never type it for them and never set `ALM_PROD_CONFIRM`
   yourself.
   This imports the users into JTS, then polls the JazzUsers repository permission every
   5 minutes for up to 30 minutes (`--wait` / `--interval` to change). Users that verify
   get a success comment AND a JTS profile screenshot attached to their work item(s).
   Users that never verify are SKIPPED and listed in the final report - nothing is posted
   for them.
4. The GPT AD-group step always runs; it requires the debug Chrome from
   `scripts/start-gpt.ps1` and being logged in to GPT. Pass `--skip-gpt` to omit it.
5. If the run is interrupted (or you re-run after the wait), use `--resume` to continue
   from the last completed step recorded in `out/pipeline_state.json`.
6. Useful flags: `--skip-retrieve` (reuse the existing `out/alm_users.json`),
   `--workitem <ID>` (limit comment/attach to one work item, repeatable),
   `--users-in <path>` (alternate input file).
7. If authentication fails, remind the user they must be on the Chrysler intranet / VPN.
   A `502 Bad Gateway` / `ProxyError` instead means the corporate proxy is intercepting
   the intranet host - check that `NO_PROXY` is set in `.env`.
8. Every run writes a per-user audit trail to `out/audit/run-<id>.json`, a structured
   log to `out/logs/<run-id>.jsonl`, and ends with a per-user table. Report that table
   verbatim, including any user that timed out waiting for the JazzUsers permission
   (attempts performed and last check time). Nothing is posted to the work items of
   timed-out users.
   Read the statuses precisely: `SUCCEEDED` means done and confirmed, `SKIPPED` means
   deliberately not done (already present, dry run), `NOT ATTEMPTED` means the step
   never reached that user, `FAILED` means it tried and could not. A dry run reports
   `PLANNED`, never success - do not describe a dry run as though users were provisioned.
9. Re-running a commit is safe: the comment and attach steps recognise their own
   previous writes and skip them. If a step reports `[SKIP] ... already commented` or
   `already attached`, report it as "already present", not as a new write.
10. If the attach step reports `[STOP] Evidence validation failed`, stop. It means two
   users' screenshots are identical, which is the signature of a broken capture (the
   JTS login page was once attached to 11 production work items this way). Report it and
   let the user investigate; do not retry with `--skip-shots`.
11. To check unverified users later without the pipeline:
   `python src/jts_permission.py <UID> [<UID>...]` (add `--wait 30` to poll).
