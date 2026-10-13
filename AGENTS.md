# AGENTS.md — ALM Access Provisioning Agent

## Setup and run (Windows + PowerShell)

- All Python runs inside `.venv`. Activate once per terminal or use the venv binary directly:
  ```powershell
  .\scripts\setup.ps1                 # CLI only (requirements.txt)
  .\scripts\setup.ps1 -Agents         # + multi-agent stack (requirements-cloud.txt)
  .\scripts\setup.ps1 -Dev            # + pytest/ruff
  .\.venv\Scripts\Activate.ps1
  .\.venv\Scripts\python.exe src\alm_access_requests.py
  ```
- `setup.ps1` repairs a broken `.venv` automatically and falls back to `wheels/` when PyPI is blocked. System Python lacks the deps — never use it.
- Scripts run as `python src/<script>.py` with bare imports (`src/` is the source root via `pythonpath=["src"]` in `pyproject.toml`). Only the worker uses `python -m` (`python -m alm_agents.worker`, `PYTHONPATH=/app/src` in Docker).
- Pipeline order: `python src\run_pipeline.py` (dry run, review plan) → `python src\run_pipeline.py --commit`. `--commit --skip-retrieve` commits the reviewed file unchanged; `--force-replan` overrides the approval gate.
- GPT/AD path needs `.\scripts\start-gpt.ps1` first (debug Chrome on `:9222`, Kerberos SSO). JTS screenshots use headless Edge (`channel="msedge"`), not Chrome.
- Local agent runs: `python src\agent_local.py --check`, then `--work-item <id>` (dry run) / `--commit`. Never run CLI (`out/*.json`) and agents (`out/local/alm.db`) against the same work items — they have separate idempotency state and will duplicate comments.

## Verify

- Offline suite, no EWM/JTS/GPT network: `.venv\Scripts\python.exe -m pytest` (whole suite, <1s). Single test: `pytest tests/test_plan_lock.py -q` or `-k <name>`.
- Lint: `ruff check src tests` locally; CI runs `ruff check src tests .github/hooks/scripts`. Line length 100; isort first-party list in `pyproject.toml` covers both CLI modules and `alm_*` packages.
- `conftest.py` clears `ALM_*/EWM_*/JTS_*/GPT_*` env per test and sets `ALM_RUN_ID=TESTRUN`, `ALM_TLS_VERIFY=true`. Tests that behave differently with a real `.env` loaded are broken — keep it that way.
- Agent/cloud tests need `requirements-cloud.txt` + Postgres (`PG_TEST_DSN`); CI runs them with coverage floor 50% (`pytest -q --cov=src --cov-fail-under=50`).
- `tests/test_docs.py` is a doc lint that fails CI on drift: every `--flag` in `docs/*.md`/`README.md` must exist in `src|scripts|hooks` source; GPT docs must say Chrome/Incognito (Edge only for `jts_profile_attach.py`); `.env.example` must document every var the code reads; internal `.md` links and `src/*.py` references must resolve. Update code + docs + `.env.example` together.

## Architecture

- `src/*.py` (flat): production CLI. Shared plumbing: `alm_config.py` (env identity, TLS, PROD gate), `jazz_client.py` (sole session/form-auth factory, GET-only retries), `plan_lock.py` (dry-run fingerprint vs `--commit` queue), `evidence.py` (N users ⇒ N distinct files), `idempotency.py` (comment markers/attachment names), `audit.py` + `alm_log.py` (per-user audit, redacted JSONL run log).
- `src/alm_{agents,core,api,worker}/`: additive autonomous stack (Gemini via `alm_agents/llm.py`, Postgres ledger, queue workers). Code-complete, never run against live estate; shares no state with the CLI.
- `infra/{gcp,aws,azure}/`: all three must stay `terraform fmt/validate`-clean. Use workload identity — downloaded keys fail CI.

## Safety invariants (do not weaken)

- Dry run by default; `--commit` writes. PROD writes require typing `PROD` (`alm_config.confirm_prod_write`); non-interactive runs need deliberate `ALM_PROD_CONFIRM=PROD`.
- TLS policy lives only in `alm_config.tls_verify()`. Never add `verify=False` elsewhere — CI greps `src/` and fails. `ALM_CA_BUNDLE` (.pem) turns verification on; `ALM_TLS_VERIFY=strict` makes unverified a hard error.
- Every `--commit` entry point must be known to `.github/hooks/scripts/guard-jts-commit.py` — CI fails otherwise. When adding a write script, extend the guard's `USER_FILE_SCRIPTS`/`run_pipeline` logic and add tests in `tests/test_guard.py`.
- `alm_config.env_or()` treats `NAME=` (blank) as unset — `.env.example` keys are blank by default, so use `env_or`, not `os.getenv`, for defaults.
- `NO_PROXY` must include intranet hosts — `requests` ignores the PAC file and intranet calls fail with 502 through the proxy.
- Secrets: `.env`, `out/`, `certs/`, `chrome-debug/`, `wheels/` are gitignored. Passwords are prompted, never stored; API keys come from env/secrets only. Pre-commit runs `detect-secrets` (`.secrets.baseline`) + `guard-pii` (`.pii-denylist` + `PII_DENYLIST` CI secret); `no-push-to-main` runs on pre-push.
- Windows quirk: async PG tests must use `conftest.run_async()` (selector loop — proactor breaks `psycopg`).
