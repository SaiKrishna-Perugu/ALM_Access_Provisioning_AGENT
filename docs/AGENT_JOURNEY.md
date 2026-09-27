# Building the ALM Access Retrieval + GPT Provisioning Agent

*A complete build journal - from the first idea to a team-ready agent that talks to
IBM EWM/ELM over the OSLC REST API and provisions users into GPT.*

---

## 1. What we set out to build

The team had a manual, repetitive chore:

1. Open the IBM EWM/ELM **"Unified Tracking System (Change Management)"** project in a
   browser, go to the **"ALM Access Request - Pending ICT Action"** queue.
2. Read each access-request work item, copy the requested **user IDs**.
3. Paste those IDs into the **GPT (Global Provisioning Tool)** web app and add them to
   the AD group `GR_D-JazzUser-NA`.

We turned that into an **agent** with two halves:

- **Retrieval** - pull the access requests and parse the user IDs, headless, via the
  **OSLC REST API** (no browser automation, no Jazz SDK).
- **Provisioning** - add those IDs into GPT by driving an authenticated browser session
  over the Chrome DevTools Protocol (CDP).

```mermaid
flowchart LR
    A[EWM/ELM OSLC API] -->|GET work items JSON/XML| B[alm_access_requests.py]
    B -->|parse New Users - USERID| C[user IDs]
    C -->|USER_IDS / out/alm_users.json| D[elm_gpt.py]
    D -->|CDP to debug Chrome| E[GPT Global Provisioning Tool]
    E --> F[GR_D-JazzUser-NA membership]
```

---

## 2. Part 1 - Retrieval via the OSLC REST API

### 2.1 Why OSLC (and not a browser or the SDK)

OSLC (Open Services for Lifecycle Collaboration) is the REST API that Jazz/EWM exposes.
It let us fetch structured work-item data with plain HTTP + JSON/XML - no Selenium, no
Java SDK. That keeps the retrieval side fast, headless, and easy to run in a terminal.

### 2.2 Authenticating with Jazz form auth

Jazz uses **form-based authentication**: you POST credentials to a `j_security_check`
endpoint, receive a session cookie, then call the API. Our first version hardcoded the
login path - which later bit us on TEST (see Roadblock 9). The final, environment-aware
version **discovers** the login URL by following the auth challenge:

```python
def login(user: str, password: str) -> requests.Session:
    s = requests.Session()
    s.verify = False
    # PROD posts to /authenticated/j_security_check, TEST to /auth/j_security_check.
    # Discover it by following the auth challenge; keep the classic path as fallback.
    probe = s.get(f"{SERVER}/authenticated/identity", allow_redirects=True)
    u = urlsplit(probe.url)
    discovered = urlunsplit((u.scheme, u.netloc, u.path.rsplit("/", 1)[0] + "/j_security_check", "", ""))
    classic = f"{SERVER}/authenticated/j_security_check"
    candidates = [discovered] + ([classic] if classic != discovered else [])

    for login_url in candidates:
        resp = s.post(login_url, data={"j_username": user, "j_password": password})
        seen = [resp.url] + [h.headers.get("Location", "") for h in resp.history]
        if any("authfailed" in (x or "") for x in seen):
            continue                      # credentials rejected here; try next endpoint
        if _session_reads_api(s):         # protected resource returns XML, not the web UI
            print("[OK] Authenticated with EWM.")
            return s
    raise RuntimeError(f"Authentication failed on {SERVER} - check the password/account.")
```

The password is **always prompted** at runtime and typed directly into the terminal -
never stored, never read from `.env`.

### 2.3 Discovering the project and workflow state

The query needs a project-area UUID and a workflow-state identifier. The agent can
discover them from XML endpoints, or read cached values from `.env` to skip the lookups:

```
EWM_PROJECT_UUID=_sTK-QebpEea8oNaj29Rsbg
EWM_WORKFLOW_STATE_ID=com.ibm.team.workitem.almAccessRequestWorkflow.state.s4
```

Verified facts for this server:

| Thing | Value |
|-------|-------|
| Project area UUID | `_sTK-QebpEea8oNaj29Rsbg` |
| Work-item type | `com.fca.alm.rtc.workitem.workItemType.almAccessRequest` |
| Workflow | `com.ibm.team.workitem.almAccessRequestWorkflow` |
| "In Progress - ICT" state | `...almAccessRequestWorkflow.state.s4` (the "Pending ICT Action" queue) |
| New Users attribute | `rtc_ext:com.stellantis.alm.rtc.aar.newUsers` |

### 2.4 The OSLC query

We ask for only the properties we need, filter by type + workflow state, and page
through JSON results:

```python
def fetch(s, uuid, where, limit):
    url = f"{SERVER}/oslc/contexts/{uuid}/workitems"
    H = {"Accept": "application/json", "OSLC-Core-Version": "2.0"}
    params = {"oslc.properties": OSLC_PROPERTIES, "oslc.paging": "true",
              "oslc.pageSize": "200", "oslc.where": where}
    # where example:
    #   dcterms:type="...almAccessRequest" and
    #   rtc_cm:state="...almAccessRequestWorkflow.state.s4"
    ...
    return items
```

### 2.5 Parsing the user IDs

The **New Users** field looks like `LASTNAME,FIRSTNAME,email,USERID;` - the **USERID**
token is what we report and feed to GPT:

```
LASTNAME,FIRSTNAME,firstname.lastname@example.com,AB12345;
```

The first successful PROD run returned **9 work items** and **16 unique user IDs**.

---

## 3. Part 2 - Provisioning into GPT

GPT is a JSF web app behind Windows/Kerberos SSO, with no API. So `elm_gpt.py` attaches
to a **debug-enabled Chrome** over CDP - which keeps the user's Kerberos ticket - and
drives the "Modify Membership" flow with Playwright:

```python
with sync_playwright() as p:
    b = p.chromium.connect_over_cdp("http://localhost:9222")
    ids = extract_ids(b) if AUTO else USER_IDS   # from open ELM tab or .env
    provision(page, ids)                         # stage each, click Modify only on --commit
```

`start-gpt.ps1` launches that Chrome. It must be **Incognito** (a GPT requirement) yet
still passes Kerberos:

```powershell
$edge = ... # auto-detected msedge.exe path
Start-Process $chrome "--incognito --remote-debugging-port=9222 --user-data-dir=`"$profileDir`" $Url"
```

The script runs a **dry run** by default (stages users, submits nothing); adding
`--commit` clicks Modify. Our first live run staged all 16 IDs and committed them to
`GR_D-JazzUser-NA`.

---

## 4. The Roadblocks (and how we cleared them)

This is the real story - each problem and its fix, in order.

### Roadblock 1 - `python-dotenv` not installed, and PyPI is blocked
The retrieval script reads `CID` from `.env` via `python-dotenv`, but the module was
missing and `pip install` timed out (`files.pythonhosted.org` unreachable on the
corporate network).
- **Immediate fix:** read `CID` from `.env` ourselves and pass `--user <CID>`.
- **Lasting fix:** a tiny built-in `.env` fallback loader so the script works even
  without `python-dotenv`, plus the offline-install story below.

### Roadblock 2 - `playwright` not installed, still no network
The GPT script needs Playwright, which also could not be installed. Playwright can't be
substituted, so the GPT half was fully blocked.

### Roadblock 3 - Offline wheels
We could not reach PyPI, so we installed from a **local `wheels/` folder** downloaded on
a connected machine:

```powershell
python -m pip install --no-index --find-links .\wheels python-dotenv playwright
```

This unblocked both scripts. We later **bundled the wheels in the repo** so teammates
never hit the same wall.

### Roadblock 4 - GPT 401 / private-window requirement
The first GPT run returned `GPT 401: no Kerberos`. GPT must be opened in a **private**
window. We added `--incognito` to `start-gpt.ps1`, logged in, re-ran, and the dry run
staged all 16 users; `--commit` added them.

### Roadblock 5 - Messy repo
Everything sat at the root. We reorganized into `src/`, `scripts/`, `docs/`, and updated
every reference (the `.github` agent/prompt commands, and `start-gpt.ps1` so the
`edge-debug/` profile stayed at the repo root).

### Roadblock 6 - Making it safe to share with the team
So a teammate wouldn't relive our pain, we added:
- `requirements.txt` (pinned deps).
- `scripts/setup.ps1` - a one-command bootstrap that **creates a `.venv`** and installs
  everything **into it** (online first, automatic fallback to the bundled `wheels/`),
  copies `.env.example` to `.env`, and verifies imports.
- Bundled `wheels/` (verified self-contained offline).
- Chrome **path auto-detection** in `start-gpt.ps1`.
- A getting-started `README.md` with a **troubleshooting/blockers table**.

```powershell
# scripts/setup.ps1 (core idea)
& $venvPy -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0 -and (Test-Path $WheelsDir)) {
    & $venvPy -m pip install --no-index --find-links $WheelsDir -r requirements.txt
}
```

### Roadblock 7 - The hidden `requests` dependency
Once we switched to the venv, retrieval crashed: `ModuleNotFoundError: requests`. It
worked before only because the **global** Python happened to have `requests`. We added
`requests==2.32.3` to `requirements.txt` and into the venv (and the offline bundle),
so the venv is now truly self-sufficient.

### Roadblock 8 - TEST returned malformed / non-XML responses
Pointing `EWM_SERVER` at TEST produced `not well-formed (invalid token)` then
`mismatched tag`. We hardened XML parsing to tolerate bad tokens **and** to detect when a
server returns the **web UI HTML** instead of OSLC XML:

```python
def parse_xml(content):
    text = content.decode("utf-8", "replace") if isinstance(content, (bytes, bytearray)) else content
    head = text.lstrip()[:64].lower()
    if head.startswith("<!doctype html") or head.startswith("<html"):
        raise RuntimeError("Server returned the Jazz web UI, not OSLC XML - session not authorized.")
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        cleaned = _XML_BARE_AMP.sub("&amp;", _XML_INVALID_CHARS.sub("", text))
        return ET.fromstring(cleaned)
```

### Roadblock 9 - TEST authentication silently failing (the big one)
Even authenticated, every **protected** TEST endpoint returned HTML; only the public
`rootservices` returned XML. Probing revealed the truth: the form-login POST was
redirecting to `/ccm/auth/authfailed`. The credentials were fine - **the login endpoint
differs**:

| Environment | Login endpoint |
|-------------|----------------|
| PROD (`prsse`) | `/ccm/authenticated/j_security_check` |
| TEST (`prssetst`) | `/ccm/auth/j_security_check` |

The fix (Section 2.2) **discovers** the endpoint from the auth challenge and verifies the
session can actually read the API. After that, TEST returned **4 work items** and **7
unique user IDs**, and PROD kept working - the same code now runs against both by
changing one line in `.env`.

---

## 5. Final project structure

```
.
|-- src/
|   |-- alm_access_requests.py   # OSLC retrieval + parsing (read-only)
|   |-- ewm_workitems.py         # generic EWM work-item lister
|   `-- elm_gpt.py               # GPT provisioner (Playwright over CDP)
|-- scripts/
|   |-- setup.ps1                # one-time bootstrap: .venv + deps (online/offline)
|   `-- start-gpt.ps1            # Incognito debug Chrome on port 9222
|-- wheels/                      # bundled offline wheels (Win, CPython 3.13)
|-- docs/                        # reference + this journey
|-- .github/                     # Copilot agent + prompts
|-- requirements.txt
|-- .env.example                 # template (.env is git-ignored)
`-- README.md
```

---

## 6. How to run (short version)

```powershell
.\scripts\setup.ps1                                       # create .venv, install deps
# edit .env: EWM_SERVER (PROD/TEST) + CID
.\.venv\Scripts\python.exe src\alm_access_requests.py     # retrieve (prompts password)

.\scripts\start-gpt.ps1                                   # Incognito Chrome; log into GPT
.\.venv\Scripts\python.exe src\elm_gpt.py                 # dry run
.\.venv\Scripts\python.exe src\elm_gpt.py --commit        # add users to the group
```

---

## 7. Interview-style explanation

**Q: In one sentence, what does this agent do?**
It headlessly pulls "ALM Access Request" work items from IBM EWM/ELM via the OSLC REST
API, parses the requested user IDs, and provisions those users into the GPT AD group -
replacing a manual copy-paste-between-two-web-apps chore.

**Q: Why OSLC REST instead of browser automation or the Jazz SDK for retrieval?**
OSLC is the native REST API for Jazz. It returns structured JSON/XML over plain HTTP, so
retrieval is fast, headless, and dependency-light (just `requests`). Browser automation
would be brittle and slow; the Java SDK would be heavy and off-language for a Python tool.

**Q: How does authentication work, and what made it tricky?**
Jazz uses form auth: POST credentials to `j_security_check`, get a session cookie. The
trap was that the login endpoint differs between environments - PROD uses
`/authenticated/j_security_check`, TEST uses `/auth/j_security_check`. Hardcoding it made
TEST silently fail (redirect to `authfailed`, then every protected call returned the web
UI HTML). We fixed it by **discovering** the login URL from the auth challenge and then
**verifying** the session can actually read a protected OSLC resource before trusting it.

**Q: Why can't the GPT side use an API too?**
GPT (the Global Provisioning Tool) is a JSF web app behind Kerberos SSO with no API. So
we attach Playwright to a **debug Chrome** over CDP - which preserves the user's Kerberos
ticket - and drive the UI. It must run in an **InPrivate** window, and no browser binary
download is needed because we connect to an already-running Edge.

**Q: What was the hardest problem?**
The TEST auth failure. It looked like a data/XML bug (invalid token, mismatched tag), but
the root cause was authentication: the session was never authorized because the login
endpoint was wrong, so the server kept returning the login/web-UI HTML. Methodical
probing - comparing a public endpoint (`rootservices`, which worked) against protected
ones (which didn't), then inspecting the redirect to `authfailed` - pinned it down.

**Q: How is it safe to share and reproduce?**
`setup.ps1` builds an isolated `.venv` so it never touches a teammate's other Python
projects. Dependencies install online, or fall back to the **bundled offline wheels**
(because corporate PyPI is blocked). Secrets stay out of git: `.env`, `.venv/`,
`edge-debug/`, and `out/` are git-ignored, and the password is only ever prompted.

**Q: What are the safety guarantees?**
Retrieval is strictly read-only (GET). Provisioning defaults to a **dry run** that stages
without submitting; it only modifies GPT group membership when you explicitly pass
`--commit`.

**Q: If you extended it, what next?**
Wire the retrieval output (`out/alm_users.json`) straight into the GPT and JTS import
steps for a fully unattended pipeline, add a scheduled run, and emit an audit log of who
was added, when, and from which work item.
