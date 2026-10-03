"""Doc lint: the documentation must describe the code that exists.

Documentation drift here is not cosmetic - the docs are the agent's context. An
agent that reads "start the debug Edge" will tell the operator to run a browser
the script cannot attach to, and a doc naming a flag that no longer exists sends
the operator down a dead end mid-incident.
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCS = sorted(ROOT.glob("docs/*.md")) + [ROOT / "README.md"]
SOURCES = sorted(ROOT.glob("src/**/*.py")) + sorted(ROOT.glob("scripts/*.ps1")) + \
    sorted(ROOT.glob(".github/hooks/scripts/*.py"))

# Flags belonging to tools we merely document how to invoke (pip, pre-commit,
# chrome). They are not ours to implement.
THIRD_PARTY_FLAGS = {
    "--only-binary", "--python-version", "--implementation", "--abi", "--platform",
    "--no-index", "--find-links", "--timeout", "--retries", "--maxkb", "--baseline",
    "--remote-debugging-port", "--user-data-dir", "--auth-server-allowlist",
    "--auth-negotiate-delegate-allowlist", "--no-first-run", "--no-default-browser-check",
    "--incognito", "--version", "--help", "--no-verify", "--", "--commit-msg",
    # gcloud / terraform / docker / uvicorn, used in the deployment documentation.
    "--region", "--project", "--format", "--quiet", "--wait", "--image",
    "--service-account", "--network", "--subnet", "--vpc-egress", "--args",
    "--update-env-vars", "--set-env-vars", "--min-instances", "--max-instances",
    "--data-file", "--build-arg", "--with-deps", "--backend-config", "--chdir",
    "--service", "--message", "--attribute",
    "--var-file", "--auto-approve", "--backend", "--recursive", "--check",
    "--to-revisions", "--command", "--host", "--port", "--proxy-headers",
    "--no-access-log", "--interval", "--start-period", "--limit", "--freshness",
    # cosign, verifying an image's signature and SBOM attestation (RUNBOOK 7e).
    "--certificate-identity", "--certificate-oidc-issuer", "--type",
}

FLAG_RE = re.compile(r"(?<![\w-])(--[a-z][a-z0-9-]{1,30})")

# The tails left behind when a writer interpreted the escape sequences in
# "\requirements.txt", "\alm-access-retrieval" and "\runInTerminal". The
# lookbehind stops the *repaired* words from matching.
MANGLED_PATTERNS = (
    r"(?<![A-Za-z])equirements\.txt",
    r"(?<![A-Za-z])equests\s",
    r"(?<![A-Za-z])lm[-_]access",
    r"(?<![A-Za-z])unInTerminal",
)

# BEL and form feed: what an interpreted "\a" and "\f" actually leave in a file.
CONTROL_BYTES = (7, 12)


@pytest.fixture(scope="module")
def source_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in SOURCES)


@pytest.fixture(scope="module")
def doc_files() -> list[Path]:
    present = [p for p in DOCS if p.exists()]
    assert present, "no documentation found"
    return present


def test_every_documented_flag_exists_in_the_source(doc_files, source_text):
    unknown: dict[str, set[str]] = {}
    for doc in doc_files:
        for flag in set(FLAG_RE.findall(doc.read_text(encoding="utf-8", errors="replace"))):
            if flag in THIRD_PARTY_FLAGS or flag in source_text:
                continue
            unknown.setdefault(doc.name, set()).add(flag)
    assert not unknown, f"documented flags that no script implements: {unknown}"


def test_the_gpt_step_is_documented_as_chrome_not_edge(doc_files):
    """start-gpt.ps1 launches Chrome; elm_gpt.py attaches to it over CDP."""
    offenders = []
    for doc in doc_files:
        for number, line in enumerate(doc.read_text(encoding="utf-8",
                                                    errors="replace").splitlines(), 1):
            low = line.lower()
            # Word-boundary match: "ledger" contains "edge", and a substring
            # test flagged an unrelated sentence about the Postgres ledger.
            names_edge = re.search(r"edge|inprivate", low) is not None
            about_gpt = any(k in low for k in ("gpt", "9222", "cdp", "start-gpt"))
            # A line may legitimately name both: Chrome drives GPT, headless Edge
            # takes the JTS profile screenshots.
            names_chrome = "chrome" in low or "incognito" in low
            if names_edge and about_gpt and not names_chrome:
                offenders.append(f"{doc.name}:{number}: {line.strip()[:100]}")
    assert not offenders, ("the GPT path uses Chrome/Incognito; these lines still say "
                           "Edge/InPrivate:\n" + "\n".join(offenders))


def test_edge_is_only_claimed_where_it_is_actually_used():
    """jts_profile_attach.py is the one place that really uses msedge."""
    attach_src = (ROOT / "src" / "jts_profile_attach.py").read_text(encoding="utf-8")
    assert 'channel="msedge"' in attach_src


def test_internal_document_links_resolve(doc_files):
    broken = []
    for doc in doc_files:
        text = doc.read_text(encoding="utf-8", errors="replace")
        for raw in re.findall(r"\]\(([^)]+\.md)\)", text):
            if raw.startswith(("http://", "https://")):
                continue
            # Markdown escapes a space as %20 or wraps the path in <>.
            target = unquote(raw.strip("<>"))
            if not (doc.parent / target).resolve().exists():
                broken.append(f"{doc.name} -> {target}")
    assert not broken, f"broken documentation links: {broken}"


def test_referenced_source_files_exist(doc_files):
    missing = []
    for doc in doc_files:
        text = doc.read_text(encoding="utf-8", errors="replace")
        for target in set(re.findall(r"\b(src/[a-z_]+\.py)\b", text)):
            if not (ROOT / target).exists():
                missing.append(f"{doc.name} -> {target}")
    assert not missing, f"documentation references missing source files: {missing}"


def test_readme_has_no_mangled_escape_sequences():
    """The README was written through a layer that interpreted its escapes,
    leaving 'equirements.txt' and a BEL byte before 'lm-access-retrieval'."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    for pattern in MANGLED_PATTERNS:
        found = re.search(pattern, text)
        assert not found, ("mangled text in README.md near "
                           f"{text[max(0, found.start() - 40):found.end() + 20]!r}")


def test_no_documentation_file_contains_control_characters(doc_files):
    """Control bytes are what an interpreted escape actually leaves behind."""
    offenders = []
    for doc in doc_files:
        raw = doc.read_bytes()
        for code in CONTROL_BYTES:
            if bytes([code]) in raw:
                offenders.append(f"{doc.name}: control byte {code}")
    assert not offenders, offenders


def test_env_example_documents_every_variable_the_code_reads():
    """A variable the code honours but the template omits is a trap."""
    example = (ROOT / ".env.example").read_text(encoding="utf-8-sig")
    for name in ("ALM_ENV", "ALM_CA_BUNDLE", "ALM_TLS_VERIFY", "EWM_SERVER",
                 "JTS_SERVER", "CID", "ALM_USERS_OUT", "NO_PROXY"):
        assert re.search(rf"^#?\s*{name}=", example, re.M), f"{name} missing from .env.example"
