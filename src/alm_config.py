"""Central configuration: environment identity, TLS policy, and the PROD write gate.

Two problems this module exists to solve:

1. **Environment ambiguity.** Nothing used to distinguish a TEST run from a PROD
   run except one hand-edited URL in ``.env``. ``alm_env()`` derives the
   environment from the configured servers (or an explicit ``ALM_ENV``), every
   entry point prints ``banner()``, and a PROD write requires a typed
   confirmation via ``confirm_prod_write()``.
2. **TLS verification.** Every module used to hardcode ``verify=False``. TLS
   policy now lives here: point ``ALM_CA_BUNDLE`` at the corporate CA bundle (or
   set ``ALM_TLS_VERIFY=true`` if the CA is already in the system store) and
   every request in the toolkit verifies certificates.

   The default is still unverified, because turning verification on without a
   reachable corporate CA would break a working installation on first run. It is
   a loud default: ``tls_verify()`` warns once per process, and
   ``ALM_TLS_VERIFY=strict`` turns the warning into a hard error so a hardened
   deployment cannot silently regress.

Statuses, schema versions and marker prefixes shared across modules also live
here so the state file, the audit trail and the work-item comments agree.
"""
from __future__ import annotations

import os
import sys

# Bumped whenever the on-disk shape of pipeline_state.json / audit files changes.
STATE_SCHEMA = 1
AUDIT_SCHEMA = 1

# Prefix of the machine-readable marker appended to work-item comments so a
# re-run can recognise its own previous post. Kept short and inert in rich text.
COMMENT_MARKER_PREFIX = "alm-agent"

_TRUE = {"1", "true", "yes", "on"}
_warned = False


def env_or(name: str, default: str) -> str:
    """The environment value, or ``default`` when it is unset OR blank.

    ``os.getenv(name, default)`` returns "" for ``NAME=`` - the shape every key
    in .env.example has - so a copied template would silently set, say, the AD
    group to an empty string instead of falling back to the default.
    """
    return os.getenv(name, "").strip() or default


def _server_hosts() -> list[str]:
    return [
        os.getenv("EWM_SERVER", ""),
        os.getenv("JTS_SERVER", ""),
    ]


def alm_env() -> str:
    """Return "PROD", "TEST" or "UNKNOWN" for the currently configured servers.

    An explicit ``ALM_ENV`` always wins. Otherwise the Chrysler naming
    convention decides: the TEST hosts carry a ``tst`` suffix (``prssetst``),
    production does not (``prsse``).
    """
    explicit = os.getenv("ALM_ENV", "").strip().upper()
    if explicit in {"PROD", "TEST"}:
        return explicit
    hosts = " ".join(_server_hosts()).lower()
    if not hosts.strip():
        return "UNKNOWN"
    if "tst" in hosts or "test" in hosts:
        return "TEST"
    if "prsse" in hosts or "prod" in hosts:
        return "PROD"
    return "UNKNOWN"


def env_mismatch() -> str | None:
    """Describe a split-environment configuration, or None when consistent.

    Pointing EWM at PROD while JTS still points at TEST is the configuration
    that silently comments on production work items about test users.
    """
    ewm, jts = os.getenv("EWM_SERVER", ""), os.getenv("JTS_SERVER", "")
    if not ewm or not jts:
        return None
    ewm_test = "tst" in ewm.lower()
    jts_test = "tst" in jts.lower()
    if ewm_test != jts_test:
        return (f"EWM_SERVER is {'TEST' if ewm_test else 'PROD'} but JTS_SERVER is "
                f"{'TEST' if jts_test else 'PROD'}")
    return None


def tls_verify():
    """The value to pass as ``requests``' ``verify=``: a CA bundle path or False.

    Resolution order: ``ALM_CA_BUNDLE`` (a file or directory) > ``ALM_TLS_VERIFY``
    > unverified with a warning.
    """
    global _warned
    bundle = os.getenv("ALM_CA_BUNDLE", "").strip()
    if bundle:
        if not os.path.exists(bundle):
            raise SystemExit(
                f"[STOP] ALM_CA_BUNDLE points at '{bundle}', which does not exist. "
                "Set it to the corporate CA bundle (.pem) or unset it.")
        return bundle

    mode = os.getenv("ALM_TLS_VERIFY", "").strip().lower()
    if mode in _TRUE:
        return True
    if mode == "strict":
        raise SystemExit(
            "[STOP] ALM_TLS_VERIFY=strict but no ALM_CA_BUNDLE is configured. "
            "Point ALM_CA_BUNDLE at the corporate CA bundle to run verified.")

    # Unverified TLS is tolerated only where we know it is TEST. PROD refuses,
    # and so does UNKNOWN: with no servers configured we cannot tell which
    # estate the credentials are about to be sent to.
    env = alm_env()
    if env != "TEST":
        raise SystemExit(
            f"[STOP] Refusing to send credentials over unverified TLS ({env}). Set "
            "ALM_CA_BUNDLE to the corporate CA bundle (.pem), or ALM_TLS_VERIFY=true "
            "if the corporate CA is in the system trust store.")

    if not _warned:
        _warned = True
        print("[warn] TLS certificate verification is DISABLED (corporate self-signed "
              "certs). Set ALM_CA_BUNDLE=<path to corporate CA .pem> to verify.",
              file=sys.stderr, flush=True)
        try:
            import urllib3
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:  # noqa: S110 - best-effort urllib3 warning suppression
            pass
    return False


def tls_is_verified() -> bool:
    return tls_verify() is not False


def banner(step: str = "", commit: bool = False) -> str:
    """One-line environment banner printed by every entry point."""
    env = alm_env()
    mode = "COMMIT (writes)" if commit else "DRY RUN"
    mark = "!! PRODUCTION !!" if env == "PROD" else env
    line = f"[{mark}] {mode}" + (f" | {step}" if step else "")
    tls = "verified" if tls_is_verified() else "UNVERIFIED"
    return f"{line} | EWM={os.getenv('EWM_SERVER', '-')} | TLS={tls}"


def print_banner(step: str = "", commit: bool = False) -> None:
    print(banner(step, commit), flush=True)
    mismatch = env_mismatch()
    if mismatch:
        print(f"[warn] Split environment: {mismatch}. Check .env before committing.",
              flush=True)


# The phrase an operator must type to authorise a production write. The
# orchestrator exports ALM_PROD_CONFIRM after a successful prompt so the child
# steps of one approved run do not each re-prompt.
CONFIRM_PHRASE = "PROD"
CONFIRM_ENV = "ALM_PROD_CONFIRM"


def confirm_prod_write(action: str, assume_yes: bool = False) -> bool:
    """Gate a production write behind a typed confirmation.

    Returns True when the write may proceed. Non-PROD environments and runs that
    already carry an approved confirmation pass straight through.
    """
    if alm_env() != "PROD":
        return True
    if assume_yes or os.getenv(CONFIRM_ENV, "").strip().upper() == CONFIRM_PHRASE:
        return True
    if not sys.stdin.isatty():
        print(f"[STOP] {action} targets PRODUCTION and no terminal is available to "
              f"confirm. Set {CONFIRM_ENV}={CONFIRM_PHRASE} to authorise it "
              "non-interactively.", flush=True)
        return False
    print(f"\n!! {action} will WRITE TO PRODUCTION ({os.getenv('EWM_SERVER', '?')}).")
    try:
        typed = input(f"   Type {CONFIRM_PHRASE} to continue, anything else to abort: ")
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if typed.strip().upper() != CONFIRM_PHRASE:
        print("[STOP] Not confirmed - nothing was written.", flush=True)
        return False
    os.environ[CONFIRM_ENV] = CONFIRM_PHRASE
    return True
