# ALM Access Retrieval — Complete File & Folder Reference

A two-step internal toolkit that:
1. **Retrieves** ALM Access Request work items (and the requested user IDs) from IBM EWM/ELM via the OSLC REST API — read-only.
2. **Imports** those user IDs into the Jazz Team Server (JTS) user registry, or provisions them into the GPT (Global Provisioning Tool) AD group GR_D-JazzUser-NA.

> Runs entirely inside a local .venv so it never interferes with your other Python projects.

---

## How the agent pipeline works

```
ALM Access Request (EWM work item)
        |
        v
[alm-access-retrieval agent]        <-- .github/agents/alm-access-retrieval.agent.md
  runs src/alm_access_requests.py   <-- reads .env for EWM_SERVER + CID
        |
        v
  out/alm_users.json                <-- list of unique user IDs parsed from work items
        |
        v
[jts-user-import agent]             <-- .github/agents/jts-user-import.agent.md
  runs src/jts_import_users.py      <-- reads .env for JTS_SERVER + CID
        |
        v
  Users created in JTS registry
```

---

## Folder & File Reference

### Root-level files

---

#### .env
**Purpose:** Stores your personal configuration: the EWM server URL and your CID (Jazz username).
**Why needed:** Every script reads EWM_SERVER, CID/EWM_USER, and JTS_SERVER from this file so you never have to pass them on the command line.
**Agent role:** The retrieval agent reads EWM_SERVER and CID from here before connecting to EWM. The import agent reads JTS_SERVER and CID. The password is **never** stored here — it is always prompted at runtime.
> Git-ignored. Never commit this file.

---

#### .env.example
**Purpose:** Template showing every supported environment variable with comments.
**Why needed:** Lets a new team member know exactly what to configure before running the agent — EWM server URL (TEST vs PROD), user ID, optional workflow state overrides, GPT URL, and JTS server.
**Agent role:** Reference only; the agent reads .env, not .env.example.

---

#### .gitignore
**Purpose:** Prevents secrets and generated artifacts from being committed.
**Why needed:** Ensures .env, .venv/, chrome-debug/, out/, and Python cache folders are never accidentally pushed to the repository.
**Agent role:** Passive safety net — keeps credentials out of version control.

---

#### README.md *(this file)*
**Purpose:** Central documentation entry point for the entire project.
**Why needed:** Explains every file and folder, the agent pipeline, prerequisites, quick-start steps, and troubleshooting to any team member opening the repository.
**Agent role:** Reference for the agent context window — explains tool layout and constraints.

---

#### requirements.txt
**Purpose:** Pinned Python dependency list.
**Why needed:** Ensures reproducible installs. Declares exactly three direct dependencies:
`requests` (HTTP calls to EWM/JTS), `python-dotenv` (reads .env), and `playwright` (drives Chrome over CDP for GPT provisioning, and headless Edge for the JTS profile screenshots).
**Agent role:** scripts/setup.ps1 installs from this file into .venv; the agent scripts import these libraries.

---

### src/ — Python source scripts

The core of the agent. All scripts run inside .venv.

---

#### src/alm_config.py
**Purpose:** Environment identity (TEST vs PROD), TLS policy, and the production write gate.
**Why needed:** Nothing used to distinguish a TEST run from a PROD run except one hand-edited
URL in `.env`. Every entry point now prints an environment banner, and a write to PROD requires
the operator to type `PROD` (once per pipeline run - child steps inherit the approval).
**Agent role:** Read by every script. Also the single place TLS verification is decided:
set `ALM_CA_BUNDLE` to the corporate CA bundle and the whole toolkit verifies certificates.

---

#### src/jazz_client.py
**Purpose:** One session factory and one Jazz form-auth implementation, with the TLS policy,
per-request timeouts and bounded GET retries applied in one place.
**Why needed:** Six modules each built their own session and repeated the `j_security_check`
handshake, so a fix had to be applied six times. Retries are GET-only by design - a retried
POST could duplicate a write.
**Agent role:** Used by every script that talks to EWM or JTS. Each caller still supplies its
own post-login verification (EWM: OSLC XML; JTS: `/whoami`) so no two steps share a failure mode.

---

#### src/plan_lock.py
**Purpose:** Fingerprints the plan a dry run showed, and refuses a `--commit` whose retrieved
queue no longer matches it.
**Why needed:** The queue is live. A dry run reviewed at 14:52 held 4 work items; by 00:45 the
same query returned 13. The commit run re-queried from scratch, so users the operator never saw
were swept into a production write. The fingerprint covers what is actually approved (which user
goes on which work item), not cosmetic fields.
**Agent role:** Enforces the approval gate in `run_pipeline.py`. Bypass with `--skip-retrieve`
(commit exactly the reviewed file) or `--force-replan` (deliberate override).

---

#### src/evidence.py
**Purpose:** Validates evidence artifacts before upload: every file present, non-trivial, and
**pairwise distinct**.
**Why needed:** 17 "profile screenshots" that were all the JTS login page were once attached to
production work items and reported as success. N users must produce N different files; if they
do not, the capture mechanism is broken and the whole batch is refused.
**Agent role:** Hard gate in `jts_profile_attach.py`, independent of how the capture step
decided it had succeeded.

---

#### src/idempotency.py
**Purpose:** Lets a re-run recognise its own previous writes - a content-derived marker on each
work-item comment, and a filename match for each attachment.
**Why needed:** Re-running a commit used to post every comment again and upload every screenshot
again, so the normal recovery action after a partial failure corrupted the record.
**Agent role:** Consulted by the comment and attach steps before every write.

---

#### src/audit.py
**Purpose:** The per-user, per-step audit trail merged into `out/audit/run-<id>.json`.
**Why needed:** Statuses now distinguish `not_attempted` (the step never reached this user) from
`failed` (it tried and could not), and the summary reports succeeded / skipped / not attempted /
failed separately instead of counting skips as successes. A dry-run summary never prints the
word SUCCESS.
**Agent role:** Written by every step; the final report is generated from it.

---

#### src/alm_log.py
**Purpose:** Structured JSON run log at `out/logs/<run-id>.jsonl`, alongside the human output.
**Why needed:** Capturing a run previously required piping the console through `Tee-Object`, and
the result could not be parsed. Password-shaped fields are redacted before anything is written.
**Agent role:** Used by every step; one file per run id, shared across the child processes.

---

#### src/alm_access_requests.py
**Purpose:** Fetches ALM Access Request work items from IBM EWM via the OSLC REST API and prints their fields. Parses the New Users attribute (LASTNAME,FIRSTNAME,email,USERID; tokens) to extract unique user IDs. Writes the user list to out/alm_users.json.
**Why needed:** This is the primary retrieval engine. It authenticates with Jazz form auth, discovers the project area and workflow states dynamically, and queries work items filtered by type and state.
**Agent role:** The alm-access-retrieval agent runs this script. Its output (`out/alm_users.json`) feeds the `jts-user-import` agent. Supports `--all-open`, `--state`, `--csv` and `--limit`.

---

#### src/jts_import_users.py
**Purpose:** Imports user IDs from out/alm_users.json into the Jazz Team Server (JTS) user registry using the same REST calls the JTS admin UI uses (LDAP lookup then create contributor).
**Why needed:** Automates the manual JTS "Import Users" admin task. Default mode is a **dry run** (no password, no writes). Only creates users when --commit is passed.
**Agent role:** The jts-user-import agent runs this script. The commit guard hook (guard-jts-commit.py) blocks --commit unless out/alm_users.json is populated.

---

#### src/jts_unarchive_user.py
**Purpose:** Checks whether a JTS user is archived and reactivates them. Reads the contributor resource and, when archived, re-PUTs it with the archived flag cleared.
**Why needed:** Users skipped by the import are often already present in JTS but archived, not missing. Without this they look like failures and need a manual admin fix.
**Agent role:** Used after the JTS import to resolve skipped IDs; also imported by ewm_comment_workitems.py to report each user's real status (active / archived / not in JTS).

---

#### src/ewm_comment_workitems.py
**Purpose:** Posts a JTS-import status comment back onto each ALM Access Request work item, e.g. "User added to JTS : AB12345: FIRSTNAME LASTNAME: User added to JTS - (active)". Creates the comment via the OSLC `rtc_cm:comments/oslc:comment` factory.
**Why needed:** Closes the loop so the requester sees the outcome on the work item itself. Default is a **dry run** that prints the comment; only --commit posts it. Per-user status is read live from JTS (--assume-active skips that lookup).
**Agent role:** Final step of the pipeline, after the JTS import. Author and timestamp come from the authenticated session.

---

#### src/jts_profile_attach.py
**Purpose:** Screenshots each imported user's JTS profile page (headless Edge via Playwright channel=msedge — uses the installed browser, no download) to out/screenshots/<USERID>.png, then attaches each screenshot to the requesting work item(s).
**Why needed:** Provides visual evidence of the completed import directly on the work item. The OSLC attachment factory returns HTTP 415 on this server, so uploads go through the web UI's IAttachmentRestService (multipart) followed by an OSLC partial PUT that links the attachment.
**Agent role:** Optional evidence step after the JTS import. Dry run takes screenshots and prints the attach plan; --commit uploads and links them. --skip-shots reuses existing screenshots; --headed shows the browser for login debugging.

---

#### src/jts_permission.py
**Purpose:** Checks whether users hold a JTS repository permission (default JazzUsers), via the same internal service the JTS admin UI uses (IAdminRestService/contributorByUserId, which returns the roles list and archived flag). --wait N polls every --interval minutes until every user verifies or the cap elapses.
**Why needed:** Permission propagation after import can take up to ~30 minutes; posting success evidence before the permission is live would be premature.
**Agent role:** Step 5 of the pipeline. A user is "verified" only when the role is present AND the account is not archived. Exit 0 = all verified, 3 = some unverified.

---

#### src/run_pipeline.py
**Purpose:** End-to-end orchestrator: retrieve → GPT AD group → JTS import → poll JazzUsers permission (5-min interval, 30-min cap) → success comment + profile-screenshot attachment for VERIFIED users only. Unverified users are skipped and reported.
**Why needed:** Runs the whole provisioning flow as one command with a single password prompt (handed to child steps via the process environment, never stored) and a resumable checkpoint file (out/pipeline_state.json, --resume).
**Agent role:** The /run-pipeline prompt drives this script. Dry run (default) runs every step in dry-run form with a single permission check; --commit performs the writes and the real 30-minute poll. Verified users are written to out/alm_users_verified.json, which feeds the comment and attach steps. The GPT step always runs (needs the debug Chrome from scripts/start-gpt.ps1) unless --skip-gpt is passed. Every step appends per-user outcomes to out/audit/run-&lt;id&gt;.json and the run ends with a success/failure table.

---

#### src/ewm_workitems.py
**Purpose:** Generic EWM work-item lister — retrieves any work-item type from any project area via OSLC, with optional CSV export and custom --where filters.
**Why needed:** Utility / diagnostic tool. Lets you explore other EWM project areas or work-item types without modifying the specialised ALM Access Request script. Usable as a standalone CLI.
**Agent role:** Sibling tool; not invoked by the main agents but referenced in the agent instructions as a generic fallback for project exploration (--list flag enumerates project areas).

---

#### src/elm_gpt.py
**Purpose:** GPT (Global Provisioning Tool) provisioner. Attaches to an Incognito debug Chrome over CDP (port 9222), extracts user IDs, and adds them to the GR_D-JazzUser-NA AD group. Clicks "Modify" only when --commit is passed.
**Why needed:** Automates the GPT web UI workflow using Playwright/CDP so no manual form-filling is needed. Uses Windows Kerberos SSO transparently through the existing debug Chrome session.
**Agent role:** Secondary provisioner (GPT path). Used when users need to be added to the AD group rather than registered directly in JTS. Requires scripts/start-gpt.ps1 to be run first.

---

### scripts/ — PowerShell setup and launcher scripts

---

#### scripts/setup.ps1
**Purpose:** One-time bootstrap script. Creates .venv and installs all Python dependencies. Tries PyPI first, then falls back to the local wheels/ folder automatically if PyPI is unreachable (corporate network).
**Why needed:** The corporate intranet blocks files.pythonhosted.org, so a plain pip install would fail. This script handles both online and offline install transparently.
**Agent role:** Must be run once before any agent script can execute. Produces the .venv that all agent commands rely on.

---

#### scripts/start-gpt.ps1
**Purpose:** Launches Google Chrome in Incognito mode with remote debugging enabled on port 9222, pointed at the GPT URL. Auto-detects the Chrome installation path and allowlists the GPT host for Negotiate/Kerberos auth.
**Why needed:** src/elm_gpt.py uses Playwright's CDP attach mode — it cannot launch its own browser because GPT requires Windows Kerberos SSO, which only flows through an existing authenticated browser session.
**Agent role:** Prerequisite for the GPT provisioning path. Must be run before elm_gpt.py. Not needed for the JTS import path.

---

### .github/ — GitHub Copilot agent and prompt definitions

These files teach VS Code's Copilot Chat how to behave as specialised agents for this project.

---

#### .github/agents/alm-access-retrieval.agent.md
**Purpose:** Defines the alm-access-retrieval Copilot agent mode — its description, allowed tools (`runInTerminal`, `getTerminalOutput`, `editFiles`), model, and full behavioural instructions (OSLC auth flow, field extraction, output format, security constraints).
**Why needed:** Without this file, Copilot has no knowledge of EWM, OSLC, ALM Access Requests, or how to safely handle credentials. This file encodes all of that domain knowledge as agent-mode instructions.
**Agent role:** IS the retrieval agent. Copilot reads this file when the alm-access-retrieval mode is active and follows its instructions to run src/alm_access_requests.py and report results.

---

#### .github/agents/jts-user-import.agent.md
**Purpose:** Defines the jts-user-import Copilot agent mode — reads out/alm_users.json, runs src/jts_import_users.py (dry run first, then --commit after explicit user confirmation), and reports import results.
**Why needed:** JTS import is a write operation and must only happen after human review of the dry-run output. This agent encodes the two-step workflow and confirmation gate so it cannot be skipped.
**Agent role:** IS the second-stage agent. Receives the user list from out/alm_users.json (produced by the retrieval agent) and drives the JTS import.

---

#### .github/prompts/retrieve-access-requests.prompt.md
**Purpose:** A reusable Copilot prompt (mode: alm-access-retrieval) that instructs the agent to run the retrieval script, handle auth failures, and report all work-item fields and parsed user IDs.
**Why needed:** Provides a one-click, repeatable trigger for the most common task — fetch and display the current "Pending ICT Action" queue — without the user needing to type instructions each time.
**Agent role:** Entry point for the retrieval workflow. Users invoke this prompt to start the agent.

---

#### .github/prompts/import-users-to-jts.prompt.md
**Purpose:** A reusable Copilot prompt (mode: jts-user-import) for the import workflow — validates out/alm_users.json exists, runs a dry run, shows the user list, asks for confirmation, then runs with --commit.
**Why needed:** Makes the two-step import workflow (dry run -> confirm -> commit) repeatable and safe. Prevents accidental imports without review.
**Agent role:** Entry point for the JTS import workflow.

---

#### .github/hooks/jts-commit-guard.json
**Purpose:** Copilot hook configuration that registers a PreToolUse hook running guard-jts-commit.py before every agent tool call.
**Why needed:** Safety net — prevents the import agent from running --commit if out/alm_users.json is missing or empty, which would result in a failed or no-op import.
**Agent role:** Acts as an automated pre-flight check before any JTS write operation.

---

#### .github/hooks/scripts/guard-jts-commit.py
**Purpose:** The actual PreToolUse guard script. Reads the Copilot hook payload from stdin; if the command is jts_import_users.py --commit, it verifies out/alm_users.json exists and contains at least one user. Denies the tool call otherwise.
**Why needed:** Prevents an accidental --commit run against an empty user file (e.g., if the retrieval step was skipped or failed silently). Enforces retrieval-before-import ordering.
**Agent role:** Enforces the pipeline ordering rule automatically, without relying on the user remembering to check.

---

### .vscode/ — VS Code workspace settings

---

#### .vscode/settings.json
**Purpose:** Configures VS Code Copilot Chat to auto-approve specific terminal commands matching the retrieval script invocation pattern, so the agent does not pause to ask permission every time it runs alm_access_requests.py.
**Why needed:** Without this, Copilot prompts "Allow terminal command?" on every retrieval run, interrupting the workflow. The auto-approve rules are tightly scoped to only the retrieval script command pattern.
**Agent role:** Quality-of-life setting that makes the agent feel seamless for the retrieval task.

---

### `out/` — Agent output (git-ignored)

---

#### out/alm_users.json
**Purpose:** JSON file written by alm_access_requests.py containing the unique user IDs (with name, email, and source work-item references) parsed from the New Users field of retrieved work items.
**Why needed:** Acts as the data handoff point between the two agents. The retrieval agent writes it; the import agent reads it. Decoupling retrieval from import means they can run at different times and the results can be reviewed before committing.
**Agent role:** The single shared artifact that connects alm-access-retrieval -> jts-user-import. The commit guard verifies it is non-empty before allowing --commit.

Example structure:
```json
{
  "source": "alm_access_requests.py",
  "count": 2,
  "users": [
    { "userId": "MWPABC01", "name": "Smith, John", "email": "john.smith@example.com", "workItems": ["WI-1234"] }
  ]
}
```

---

### wheels/ — Offline Python packages (local only, not in Git)

**Purpose:** Pre-downloaded binary wheel files for all project dependencies (CPython 3.13, win_amd64).
**Why needed:** Some corporate networks block PyPI (files.pythonhosted.org), so pip install times out. scripts/setup.ps1 falls back to these wheels automatically when present.
**Not tracked by Git:** the wheels are platform- and Python-version-specific (cp313 / win_amd64) and would add ~37 MB to every clone. If you have PyPI access you do not need them at all — setup.ps1 installs from requirements.txt online.

**If PyPI is blocked on your machine**, create the folder yourself on any machine with internet access:

```powershell
pip download --only-binary=:all: `
  --python-version 3.13 --implementation cp --abi cp313 --platform win_amd64 `
  -r requirements.txt -d wheels
```

Copy the resulting `wheels` folder into the repository root (or pass `-WheelsDir <path>` to setup.ps1), then run `.\scripts\setup.ps1`.

| Wheel | Dependency | Used by |
|---|---|---|
| requests-2.32.3 | HTTP client | All scripts |
| python_dotenv-1.2.2 | .env loader | All scripts |
| playwright-1.61.0 | CDP browser automation | elm_gpt.py |
| certifi-* | TLS certificate bundle | requests (transitive) |
| charset_normalizer-* | Character encoding detection | requests (transitive) |
| idna-* | Internationalised domain names | requests (transitive) |
| urllib3-* | HTTP connection pooling | requests (transitive) |
| greenlet-* | Coroutine support | playwright (transitive) |
| pyee-* | Event emitter | playwright (transitive) |
| typing_extensions-* | Type hint backports | playwright (transitive) |

---

### `chrome-debug/` — Chrome browser debug profile (git-ignored, auto-created)

**Purpose:** Google Chrome user-data directory created automatically by `scripts/start-gpt.ps1` when launching Chrome with remote debugging for GPT provisioning.
**Why needed:** Keeps the debug Chrome profile isolated from your normal Chrome profile so cookies, history, and auth tokens do not bleed between them.
**Agent role:** Runtime artifact for the GPT provisioning path (`elm_gpt.py`). Not touched by the retrieval or JTS import agents. Git-ignored and auto-created by Chrome on first launch — **do not commit or share this folder**.

---

### .venv/ — Python virtual environment (git-ignored)

**Purpose:** Isolated Python environment containing all installed dependencies.
**Why needed:** Prevents dependency conflicts with other Python projects on the machine. All agent scripts must be run with .venv\Scripts\python.exe (or with .venv activated).
**Agent role:** The execution environment for every src/*.py script. Created once by scripts/setup.ps1. Git-ignored — recreate with setup.ps1 if deleted.

---

## Two implementations, one set of operations

This repository now holds two ways to run the same provisioning work.

| | CLI (`src/*.py`) | Multi-agent system (`src/alm_*/`) |
|---|---|---|
| What runs it | An operator types a command and a password | An LLM supervisor routes nine agents, each running its own tool-calling loop |
| Trigger | A person | A webhook, or a 15-minute reconciliation sweep |
| Approval | Dry run, then `--commit` in the same terminal | An agent decides the batch is ready; a Chat card goes out; the run is checkpointed while it waits |
| Safety | Guards in each script | A policy engine checks every tool call, plus the same guards underneath |
| State | `out/*.json` on the operator's disk | Postgres: ledger, audit, agent memory, graph checkpoints |
| AD group step | Debug Chrome on the operator's machine | A job on Pub/Sub for a domain-joined Windows worker |
| Status | **In production use** | **Code complete; agents tested offline, never run against the live estate** |

### Where the AI is, and how to try it

The models are Gemini, called from one place: `src/alm_agents/llm.py`. They
drive the supervisor (who acts next) and the nine agents (which tools to call,
with what, and when to stop). With a Gemini API key from
[Google AI Studio](https://aistudio.google.com/apikey) you can watch them work
on a laptop, against a simulated estate:

```powershell
.\scripts\setup.ps1 -Agents          # the .venv plus the agent packages (repairs a broken .venv)
.\.venv\Scripts\Activate.ps1          # every "python" below is now the project's own
# add GEMINI_API_KEY=... to .env - the file is gitignored; never commit the key
python src/agent_sandbox.py --check
python src/agent_sandbox.py
```

Details, including why production should use Vertex AI instead of a key, are in
[section 2a of the architecture](docs/AUTONOMOUS_ARCHITECTURE.md).

When a New Users row is malformed, user IDs are *selected* from the row, never
generated: code lists the tokens that look like user IDs, and a model judges
which are requested. With a `TYPESAFE_API_KEY` in `.env` the judge is TypeSafe's
Jev model, giving a probability per candidate that the approver sees; without
one, Gemini judges the same candidates.

### Run the agents locally, against the real EWM and JTS

No cloud needed. OSLC over your network, Gemini for the reasoning, GPT through
the same debug Chrome the CLI uses, and every record in one SQLite file under
`out/local/`. Your existing `.env` works as is; add `GEMINI_API_KEY`.

```powershell
.\scripts\start-gpt.ps1                                 # then sign in to GPT in that window
python src/agent_local.py --check                       # Gemini, EWM/JTS login, OSLC, GPT, ledger
python src/agent_local.py --work-item 123456            # dry run: the agents plan, nothing is written
python src/agent_local.py --work-item 123456 --commit   # writes, after your y/N at the approval prompt
python src/agent_local.py --resume last --commit       # continue the last run (same mode it started in)
python src/agent_local.py --purge-older-than 30        # delete run data older than 30 days (ledger kept)
python src/agent_local.py --work-item 123456 --record  # also save the run for replay off the VPN
```

### The web console

The same agents, driven from a page in your browser: type what you want, watch
the main agent route the specialists live, and approve the card with checkboxes.

```powershell
python src/agent_web.py --sandbox     # simulated estate, real Gemini - works anywhere
python src/agent_web.py               # real EWM/JTS: asks the Jazz password once, in the terminal
```

It prints a one-time link (and opens it). What keeps it safe:

- **This computer only.** The console listens on `127.0.0.1` and refuses any
  other Host header.
- **The link is the key.** It signs the browser in with an HttpOnly,
  SameSite=Strict cookie. A restart makes a new link.
- **Your words can't widen a run.** Work items are the numbers in your request.
  Writing needs the **Write** switch and 1–5 named work items, and you must type
  `COMMIT` (`PROD` on production).
- **You pick who is written.** A writing run pauses at the card. Only the users
  you tick are written; everyone left unticked is declined. High-risk users
  start unticked.
- **The password stays in the terminal.** The page never sees it.
- **The page is locked down.** No third-party requests, a strict
  Content-Security-Policy, and all agent output is shown as text.

One run at a time. The page lists this session's runs. A real run keeps the same
records as `agent_local.py` (`out/local/`), so the ledger stops either tool from
repeating the other's writes.

To check the agents after a prompt, roster or model change, with no VPN:

```powershell
python src/agent_eval.py                                # five built-in scenarios, real model, simulated estate
python src/agent_eval.py --recorded out/evals/recorded  # replay runs saved with --record
```

Each scenario is graded on outcomes (who ends up active, which screenshots land on
which work item, what the comments claim) and on safety rules that must always hold.
What changed between versions, and what to do after `git pull`, is in
[CHANGELOG.md](CHANGELOG.md).

It keeps the CLI's safety model - dry run unless `--commit`, TEST/PROD detected
from the server names, typed `PROD` confirmation, the same TLS settings - and
adds the agents' guards on top: every tool call checked by the policy engine,
a human approval before any write, the run limited to the work items named, and
an idempotency ledger so a re-run reports earlier writes instead of repeating
them. E-mail addresses and the requesters' names are stripped before Gemini sees
anything; user IDs are kept. What a run keeps on disk - checkpoints, approval
cards, agent memory, reports, evidence screenshots - does hold names; remove it
with `--purge-older-than DAYS` (the ledger and audit trail stay: they hold user
IDs only, and a re-run needs them).

The CLI is unchanged and remains the supported path. The autonomous stack is
additive - it shares no state with the CLI and cannot interfere with it - and is
described in [docs/AUTONOMOUS_ARCHITECTURE.md](docs/AUTONOMOUS_ARCHITECTURE.md),
with operational procedures in [docs/RUNBOOK.md](docs/RUNBOOK.md).

> The two do **not** share idempotency state. Running both against the same work
> items can duplicate a comment. See the manual override section of the runbook
> before mixing them.

---

## Safety model

Five things stand between a command and a production change:

| Control | What it does | Where |
|---|---|---|
| Dry run by default | Every step writes nothing until `--commit` | all scripts |
| Binding approval gate | A `--commit` whose queue changed since the reviewed dry run is refused, naming who appeared or vanished | `plan_lock.py`, `run_pipeline.py` |
| Production confirmation | A PROD write requires typing `PROD`; a non-interactive run is refused unless `ALM_PROD_CONFIRM=PROD` is set deliberately | `alm_config.py` |
| Commit guard hook | Blocks `--commit` on any of the six write entry points when its input is missing, empty, or unreviewed | `.github/hooks/scripts/guard-jts-commit.py` |
| Evidence gate | Refuses to upload an artifact set where two users share a file | `evidence.py` |

And two that constrain what is *claimed*:

- **Work-item comments describe what happened.** Each line is derived from the import step's
  recorded outcome - added, reactivated, or already present - instead of asserting
  "User added to JTS" for everyone.
- **Writes are idempotent.** Re-running a partially failed commit skips the comments and
  attachments it already made, so recovery is safe.

### TLS

Certificate verification is configured in one place. Point `ALM_CA_BUNDLE` at the corporate CA
bundle (`.pem`) and every request in the toolkit verifies the server certificate:

```powershell
$env:ALM_CA_BUNDLE = "C:\certs\corporate-ca.pem"   # or set it in .env
```

Without it the toolkit still runs, but prints `TLS=UNVERIFIED` on every banner and a warning on
every run - credentials are being POSTed over a connection that cannot detect interception.
Set `ALM_TLS_VERIFY=strict` to make an unverified run a hard error instead of a warning.

---

## Tests

The suite is offline: it never contacts EWM, JTS, GPT or LDAP, so it runs anywhere, including CI.

```powershell
.\scripts\setup.ps1 -Dev          # or: pip install -r requirements-dev.txt
pytest                            # the whole suite, well under a second
ruff check src tests
```

It covers the parser that decides who gets provisioned, the comment text, the approval gate, the
evidence invariant, the idempotency markers, the audit reporting, the commit guard's rules, and
a doc lint that fails when the documentation describes flags or a browser the code does not have.
`.github/workflows/ci.yml` runs lint + tests on push, and additionally fails the build if any
module hardcodes `verify=False` or if a new `--commit` entry point is not known to the guard.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Windows + PowerShell | Scripts use PowerShell; .venv uses Scripts\python.exe |
| Python 3.13 (win_amd64) | Must be on PATH. Bundled wheels are built for CPython 3.13 win_amd64 |
| Microsoft Edge | Required for the JTS profile screenshots (jts_profile_attach.py) |
| Google Chrome | Required only for GPT provisioning (elm_gpt.py) |
| Chrysler intranet / VPN | Required for EWM OSLC and JTS REST API access |
| `NO_PROXY` in `.env` | Required when a corporate proxy is configured: `requests` does not evaluate the PAC file, so intranet hosts would be tunnelled through the proxy and fail with `502 Bad Gateway` |

---

## Quick start

Every `python` below means the project's own `.venv`: run
`.\.venv\Scripts\Activate.ps1` once per terminal (or type
`.\.venv\Scripts\python.exe` instead). The system Python does not have the
packages, and installing into it mixes this project with everything else.

```powershell
# 1. One-time setup (add -Agents for the multi-agent system: agent_local.py)
.\scripts\setup.ps1
.\.venv\Scripts\Activate.ps1

# 2. Configure credentials — copy the template and edit
copy .env.example .env
# Set EWM_SERVER and CID (your Jazz username) in .env

# 3. Retrieve ALM Access Requests (password is prompted — type it directly in the terminal)
python src\alm_access_requests.py

# 4. Import users into JTS — dry run first (safe, no password, no writes)
python src\jts_import_users.py

# 5. Import users into JTS — live commit (prompts for JTS password)
python src\jts_import_users.py --commit
```

Or run the whole pipeline. The dry run records the plan it showed you; the commit run refuses to
proceed if the live queue has changed since:

```powershell
python src\run_pipeline.py                 # dry run — review the plan it prints
python src\run_pipeline.py --commit        # executes exactly that plan, or aborts
python src\run_pipeline.py --commit --skip-retrieve   # commit the reviewed file unchanged
```

---

## Security notes

- The EWM and JTS passwords are **always prompted** and typed directly into the terminal — never stored, never read from .env.
- .env (which holds your CID) and .venv/ are git-ignored. Do not commit them.
- The retrieval step is strictly read-only (GET requests only).
- The import step only creates JTS users; it never modifies or deletes existing users.
- Provisioning to GPT (elm_gpt.py) only modifies group membership when --commit is explicitly passed.
- Certificate verification is on whenever `ALM_CA_BUNDLE` is set; when it is not, every run says
  so on its banner rather than failing quietly open.
- A production write requires an explicit typed confirmation, and a non-interactive process
  cannot make one by accident.
- The structured run log redacts password-shaped fields; the password itself is never written to
  `.env`, the state file, the audit trail or the log.
