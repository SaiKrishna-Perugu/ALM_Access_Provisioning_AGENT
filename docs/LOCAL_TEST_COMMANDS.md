# Local and client-network testing commands

Verified against the local source on 2026-10-10. These commands are for PowerShell.
Run sections separately: the commands marked COMMIT change the selected TEST systems.

The agent web console and agent_local.py require a reachable model endpoint.
They are local applications, but not fully offline AI applications.
The original CLI works on the client intranet without model access.
Automated pytest tests work without service connectivity after dependencies are installed.
Do not set ALM_ORCHESTRATION=deterministic for agent_local.py: this launcher rejects it.
The Gemini Developer API provider value is gemini_api, not gemini.

## 1. Every new PowerShell terminal

```powershell
Set-Location "C:\Users\psaik\Downloads\ALM_Access_Provisioning_AGENT"
$almPython = ".\.venv\Scripts\python.exe"
$env:COMMIT = "false"
New-Item -ItemType Directory -Path ".\out" -Force | Out-Null
```

COMMIT=false prevents standalone CLI scripts from inheriting an automatic write default.
Write commands below explicitly pass --commit.
Passwords are prompted in the terminal; do not put them in command arguments.

## 2. Prepare while internet is available

Use the same Windows computer and Python version that will run the application on the client network.

```powershell
python --version
.\scripts\setup.ps1 -Agents -Dev
& $almPython --version
& $almPython -m pip check

# Cache packages for reinstalling while offline.
& $almPython -m pip download --only-binary=:all: -r requirements-cloud.txt -r requirements-dev.txt -d ".\wheels"

# Optional: prepare Chromium if Microsoft Edge is unavailable.
& $almPython -m playwright install chromium
```

Check each command succeeds before disconnecting. If pip download fails, the wheelhouse is incomplete.
The application normally uses installed Microsoft Edge for JTS screenshots and Chrome for GPT.
A different computer/Python architecture needs a matching wheelhouse; do not copy .venv between computers.

## 3. Install dependencies with no internet

Skip this if the prepared .venv works and has all dependencies.

```powershell
# Creates a venv only when it does not exist.
if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    python -m venv .venv
}

& $almPython -m pip install --no-index --find-links ".\wheels" -r requirements-cloud.txt -r requirements-dev.txt
& $almPython -m pip check
& $almPython -c "import requests, dotenv, playwright, pytest, langgraph, aiosqlite, fastapi; print('Dependencies OK')"
```

Do not run the online setup script to force an offline install: it tries PyPI first.
Offline installation requires a complete wheelhouse already downloaded.

## 4. Automated tests: no client services or model access

```powershell
& $almPython -m pytest -q --junitxml=".\out\test-results.xml"
& $almPython -m ruff check src tests .github/hooks/scripts
& $almPython -m pytest -q --cov=src --cov-report=term --cov-report=html:out/coverage
```

The coverage command is optional. Expected: no failing tests and no lint violations.
Results: out/test-results.xml and out/coverage/index.html.
Some tests for unavailable optional integrations may be skipped.

## 5. Configure the TEST estate

Keep the existing .env; create it only when absent.

```powershell
if (-not (Test-Path ".\.env")) {
    Copy-Item ".\.env.example" ".\.env"
}
notepad ".\.env"
```

Edit the following values in .env, replacing placeholders with your client's TEST settings:

```dotenv
ALM_ENV=TEST
EWM_SERVER=https://YOUR-TEST-EWM-HOST/ccm
JTS_SERVER=https://YOUR-TEST-JTS-HOST/jts
CID=YOUR_JAZZ_USER
ALM_CA_BUNDLE=C:/path/to/corporate-ca.pem
COMMIT=false
```

Use the actual base URLs configured for your client; /ccm and /jts are examples.
Use ALM_TLS_VERIFY=true instead of ALM_CA_BUNDLE only if Python already trusts the corporate CA.
If environment variables are already set in Windows, they may override .env.

## 6. Client intranet only: original CLI, no model required

### Retrieve a small TEST sample

```powershell
& $almPython src\alm_access_requests.py --limit 1 --users-out "out/cli-test-users.json"
Get-Content ".\out\cli-test-users.json"
```

This selects the first item from the configured query, not a specific item ID.
Review EVERY user and work-item mapping in the resulting file before committing.
An item can request several users. Change the state/project arguments if the default queue has no items:

```powershell
& $almPython src\alm_access_requests.py --help
# Alternative retrieval across active states:
& $almPython src\alm_access_requests.py --all-open --limit 1 --users-out "out/cli-test-users.json"
```

Do not rerun retrieval after reviewing the file unless you intend to review the new result.

### Dry run without AD provisioning

```powershell
& $almPython src\run_pipeline.py --users-in "out/cli-test-users.json" --skip-retrieve --skip-gpt
```

### COMMIT: TEST writes without AD provisioning

Run only after reviewing the dry run and confirming the TEST banner.

```powershell
& $almPython src\run_pipeline.py --users-in "out/cli-test-users.json" --skip-retrieve --skip-gpt --commit
```

### Include AD provisioning through GPT

```powershell
.\scripts\start-gpt.ps1
```

Sign in to GPT in the debug Chrome window, then run a new dry run:

```powershell
& $almPython src\run_pipeline.py --users-in "out/cli-test-users.json" --skip-retrieve
```

COMMIT after review:

```powershell
& $almPython src\run_pipeline.py --users-in "out/cli-test-users.json" --skip-retrieve --commit
```

The full pipeline can wait up to 30 minutes for permission propagation.
Its --workitem option filters comment/attachment steps, not the full import/AD scope.
Use the reviewed --users-in file to constrain the whole pipeline.

Resume the same interrupted CLI pipeline with the same options:

```powershell
# For the no-AD run:
& $almPython src\run_pipeline.py --users-in "out/cli-test-users.json" --skip-retrieve --skip-gpt --resume --commit

# For the run including AD:
& $almPython src\run_pipeline.py --users-in "out/cli-test-users.json" --skip-retrieve --resume --commit
```

Stop a CLI pipeline with Ctrl+C. Inspect live state before resuming a write that was interrupted.

### Standalone diagnostics

```powershell
# Import plan only:
& $almPython src\jts_import_users.py --users-in "out/cli-test-users.json"

# Replace USER_ID with an actual user from the reviewed file:
& $almPython src\jts_permission.py USER_ID
& $almPython src\jts_permission.py USER_ID --wait 30 --interval 5

# Capture evidence and show the attachment plan; no attachment upload:
& $almPython src\jts_profile_attach.py --users-in "out/cli-test-users.json"

# Browser-visible evidence capture for troubleshooting:
& $almPython src\jts_profile_attach.py --users-in "out/cli-test-users.json" --headed
```

The evidence dry run still captures screenshots and writes them locally.

## 7. Agent application: requires client network plus approved model connectivity

The client network must allow the configured model endpoint, directly or through its proxy.
Without model connectivity, use section 6. The web launcher has no fully offline model-free mode.

For Gemini Developer API, add to .env:

```dotenv
ALM_LLM_PROVIDER=gemini_api
GEMINI_API_KEY=YOUR_KEY
ALM_ORCHESTRATION=guided
```

For another approved provider, retain its corresponding provider settings instead.
If a client proxy is required, configure HTTPS_PROXY and NO_PROXY in .env.
If it inspects HTTPS, configure REQUESTS_CA_BUNDLE and SSL_CERT_FILE with its CA bundle.
Keep EWM/JTS intranet hosts in NO_PROXY where required by the client's network.

### Model and application checks

```powershell
& $almPython src\agent_sandbox.py --check

# Needed only if the run uses the GPT AD path:
.\scripts\start-gpt.ps1

& $almPython src\agent_local.py --check
```

### Simulated web application

```powershell
& $almPython src\agent_web.py --sandbox
```

Open the terminal's one-time sign-in link. Confirm SIMULATED DATA.
Request: Process work item 1001.
With Write off, review the plan. With Write on, type COMMIT and test approval/rejection.
No EWM/JTS/GPT writes occur in sandbox, but the model endpoint is real.
Sandbox state is recreated per run; use live TEST to validate persistence and idempotency.

### Live TEST web application

```powershell
& $almPython src\agent_web.py
```

Confirm LIVE TEST and the correct host names.
Enter one actual TEST work-item ID. Start with Write off.
Then enable Write, type COMMIT, review the users and approve selected users.
Use the Trace tab to inspect outcomes. Stop the server with Ctrl+C.

If port 8765 is in use:

```powershell
& $almPython src\agent_web.py --port 8766
```

### Agent terminal runs

Replace 123456 with an actual TEST work-item ID.

```powershell
# Dry run and recording:
& $almPython src\agent_local.py --work-item 123456 --record

# COMMIT: execute after approval:
& $almPython src\agent_local.py --work-item 123456 --commit --record

# Dry-run resume:
& $almPython src\agent_local.py --resume last

# Writing-run resume:
& $almPython src\agent_local.py --resume last --commit

# Inspect trace:
& $almPython src\agent_local.py --trace last

# Follow trace in a second terminal:
& $almPython src\agent_local.py --trace last --follow

# Stop agent/web runs from a second terminal:
& $almPython src\agent_local.py --stop

# Or stop a specific thread:
& $almPython src\agent_local.py --stop YOUR_THREAD_ID
```

A resumed run must keep its original dry/commit mode.
Use one execution path (CLI or agents) per work item while their ledgers remain separate.

### Evaluation suite

These commands do not require the client intranet, but DO require model connectivity.

```powershell
& $almPython src\agent_eval.py
& $almPython src\agent_eval.py --recorded out/evals/recorded
```

## 8. Results and acceptance checks

```powershell
Get-ChildItem ".\out\local\run-*.json" | Sort-Object LastWriteTime -Descending | Select-Object -First 5
Get-ChildItem ".\out\audit" | Sort-Object LastWriteTime -Descending | Select-Object -First 10
Get-ChildItem ".\out\screenshots" -Filter "*.png"
```

Agent reports/ledger: out/local/.
Agent traces: out/local/traces/.
CLI audit: out/audit/.
CLI screenshots: out/screenshots/.
Replay recordings: out/evals/recorded/.
These files may contain personal data; review/sanitize before sharing.

Verify:
- Dry runs do not change EWM/JTS/AD.
- New users become active; archived users reactivate; existing users are not duplicated.
- Missing/invalid IDs are reported accurately.
- Rejected and unselected users are not provisioned by agent writes.
- Comments describe actual outcomes.
- Screenshots show the correct user and differ between users.
- Repeating the same agent run replays/skips completed writes.
- Stop/resume does not create duplicate writes.
- Work-item scope never expands beyond the reviewed request.

## 9. Data-cleanup preview and deletion

Preview:

```powershell
.\scripts\purge-local.ps1 -Days 30 -DryRun
```

DELETE old local transient data after reviewing the preview:

```powershell
.\scripts\purge-local.ps1 -Days 30
```

The ledger and audit are preserved. Local cleanup does not remove EWM attachments,
comments, JTS accounts, AD memberships, or cloud backups.

## 10. Troubleshooting

- Missing packages: use section 3 with a complete wheelhouse.
- Model 401/403: verify key/provider; gemini_api is the Developer API value.
- Model timeout: check approved proxy/model endpoint access; CLI section 6 needs no model.
- TLS failure: correct ALM_CA_BUNDLE; use the actual corporate CA.
- GPT 401: sign in again in debug Chrome.
- No queued items: check workflow state/project; --all-open may help.
- Port occupied: --port 8766.
- Edge unavailable: prepare Chromium before going offline, then set ALM_BROWSER_CHANNEL=chromium for agents.
- Sign-in page in browser: reopen the latest terminal link.
- Some user failures: inspect per-user results; do not treat process completion alone as success.
