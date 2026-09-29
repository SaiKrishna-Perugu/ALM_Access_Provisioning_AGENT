# Changelog

What changed for the operator, newest first. Each entry says what to do after
`git pull`, when anything is needed. Commit messages carry the detail.

The local ledger (`out/local/alm.db`) has a schema version. A newer version of
the code upgrades an older file automatically the next time it runs; a file
written by newer code is refused until you update.

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
