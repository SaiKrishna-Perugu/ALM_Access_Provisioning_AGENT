#!/usr/bin/env python3
"""Pre-commit hook: prevent PII, real emails, internal domains, and disallowed binary/generated files.

Checks applied to each staged file:
  1. Internal hostnames of the corporate estate, outside .env (the real file).
  2. Denylisted real personal names, user IDs and work-item numbers.
  3. Real email addresses (only example.com / example.org / example.net / *.example are allowed).
  4. Disallowed file extensions outside docs/ (*.png, *.db, *.jsonl).

The denylist itself is personal data, so it is never committed. It is read from
``.pii-denylist`` in the repository root (gitignored, one token per line) and
from the ``PII_DENYLIST`` environment variable (comma or newline separated),
which CI fills from a repository secret.

Use '# pragma: allow-pii' on any line to explicitly allow a reviewed case.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Domains considered synthetic/safe in tests, documentation, and source
ALLOWED_EMAIL_DOMAINS = {
    "example.com",
    "example.org",
    "example.net",
    "example.edu",
    "test.com",
    "localhost",
    "invalid",
    "corporate.example",
}

INTERNAL_HOSTNAMES = re.compile(
    r"\b(?:[a-zA-Z0-9.-]+\.intra\.chrysler\.com|intra\.chrysler\.com|fiatspa\.com)\b",
    re.IGNORECASE,
)

EMAIL_REGEX = re.compile(r"\b[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")

DISALLOWED_EXTENSIONS = {".png", ".db", ".jsonl"}


def _load_denylist_file(root: Path) -> list[re.Pattern[str]]:
    """Denylisted tokens from .pii-denylist and PII_DENYLIST, as word-bounded patterns."""
    tokens: list[str] = []
    denylist_file = root / ".pii-denylist"
    if denylist_file.is_file():
        try:
            tokens += denylist_file.read_text(encoding="utf-8").splitlines()
        except OSError as err:
            print(f"guard-pii: could not read {denylist_file}: {err}", file=sys.stderr)
    tokens += re.split(r"[,\n]", os.getenv("PII_DENYLIST", ""))
    unique = {t.strip() for t in tokens if t.strip() and not t.strip().startswith("#")}
    return [re.compile(rf"\b{re.escape(t)}\b", re.IGNORECASE) for t in sorted(unique)]


def check_file(path_str: str, patterns: list[re.Pattern[str]]) -> list[str]:
    violations: list[str] = []
    norm_path = path_str.replace("\\", "/")

    # Check G3: Block *.png, *.db, *.jsonl outside docs/
    p = Path(path_str)
    if p.suffix.lower() in DISALLOWED_EXTENSIONS and not norm_path.startswith("docs/"):
        violations.append(
            f"{path_str}: generated/binary file with extension {p.suffix} is not permitted outside docs/"
        )
        return violations

    # Skip the real local configuration (gitignored, so normally never staged) and
    # the detect-secrets baseline. .env.example is committed, so it IS checked.
    base_name = p.name
    if (base_name.startswith(".env") and base_name != ".env.example") \
            or norm_path.startswith(".secrets.") or base_name == ".pii-denylist":
        return violations

    # Check file content
    try:
        content = p.read_text(encoding="utf-8", errors="replace")
    except OSError as err:
        violations.append(f"{path_str}: could not read file ({err})")
        return violations

    for line_num, line in enumerate(content.splitlines(), start=1):
        if "# pragma: allow-pii" in line or "# noqa: pii" in line:
            continue

        # 1. Internal hostnames
        if INTERNAL_HOSTNAMES.search(line):
            violations.append(
                f"{path_str}:{line_num}: internal hostname found: {line.strip()[:100]}"
            )

        # 2. Denylisted IDs
        for pat in patterns:
            if pat.search(line):
                violations.append(
                    f"{path_str}:{line_num}: denylisted ID/token '{pat.pattern}' found: {line.strip()[:100]}"
                )
                break

        # 3. Real emails
        for match in EMAIL_REGEX.finditer(line):
            domain = match.group(1).lower()
            if domain not in ALLOWED_EMAIL_DOMAINS and not domain.endswith(".example"):
                violations.append(
                    f"{path_str}:{line_num}: non-synthetic email domain '{domain}' found: {line.strip()[:100]}"
                )

    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PII and internal data leak guard")
    parser.add_argument("files", nargs="*", help="Files to inspect")
    args = parser.parse_args(argv)

    root = Path.cwd()
    patterns = _load_denylist_file(root)

    all_violations: list[str] = []
    for f in args.files:
        if not os.path.isfile(f):
            continue
        all_violations.extend(check_file(f, patterns))

    if all_violations:
        print("::error:: PII and hygiene guard failed:", file=sys.stderr)
        for v in all_violations:
            print(f"  {v}", file=sys.stderr)
        print(
            "\nFix these issues or add '# pragma: allow-pii' if this is an approved synthetic line.",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
