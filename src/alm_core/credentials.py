"""Credential resolution: environment, mounted file, the cloud's secret store, prompt.

The cloud's secret store is whichever the deployment uses (``ALM_SECRET_BACKEND``):
Google Secret Manager, AWS Secrets Manager or Azure Key Vault. Each SDK is
imported only by its own adapter, so the core runs without any of them.

``getpass.getpass()`` at the top of every script is what made this system
unschedulable - it cannot run without a human at a terminal. The provider chain
replaces it without losing the operator experience: on a laptop the interactive
prompt is still the last resort, in Cloud Run it is never reached because there
is no TTY.

Secrets are cached in memory for the process lifetime and never written to disk,
never logged (``alm_core.logging`` redacts by key name), and never placed in a
child process environment by this module. Nothing here returns a secret in a
repr or an exception message.

The Cloud SQL password is a special case handled at the bottom: with IAM
database authentication there is no password at all, only a short-lived access
token minted from the runtime service account.
"""
from __future__ import annotations

import getpass
import os
import re
import sys
import threading
import time
from typing import Protocol

from .errors import CredentialError
from .logging import get_logger

log = get_logger("alm.credentials")

# How long a Secret Manager value is trusted before it is fetched again. Short
# enough that a rotation takes effect without a restart.
CACHE_TTL_SECONDS = 15 * 60

# IAM access tokens for Cloud SQL last an hour; refresh well before that.
DB_TOKEN_TTL_SECONDS = 45 * 60
CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


class SecretProvider(Protocol):
    """Anything that can answer "what is the value of this secret?"."""

    name: str

    def get(self, key: str) -> str | None:
        ...


class EnvironmentProvider:
    """Reads a secret from the process environment.

    This is how Cloud Run delivers a Secret Manager reference mounted as an
    environment variable, and how the CLI orchestrator already hands the
    password to its child steps.
    """

    name = "environment"

    def __init__(self, mapping: dict[str, str] | None = None):
        # Logical secret name -> environment variable.
        self.mapping = mapping or {}

    def _var(self, key: str) -> str:
        return self.mapping.get(key, key.upper().replace("-", "_"))

    def get(self, key: str) -> str | None:
        return os.getenv(self._var(key)) or None


class FileProvider:
    """Reads a secret from a mounted file.

    Cloud Run can mount a Secret Manager version as a file, which keeps the
    value out of the environment block entirely - worth preferring for the
    service account password, since a process listing exposes an environment.
    """

    name = "mounted-file"

    def __init__(self, directory: str = "/secrets"):
        self.directory = directory

    def get(self, key: str) -> str | None:
        path = os.path.join(self.directory, key)
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read().strip() or None
        except OSError:
            return None


class SecretManagerProvider:
    """Reads a secret from Google Secret Manager using Application Default Credentials.

    Imported lazily so the module works on a machine without the Google SDKs.
    """

    name = "secret-manager"

    def __init__(self, settings):
        self.settings = settings
        self._client = None

    def _ensure_client(self):
        if self._client is not None:
            return self._client
        try:
            from google.cloud import secretmanager
        except ImportError as err:  # pragma: no cover
            raise CredentialError(
                "Secret Manager requested but google-cloud-secret-manager is not "
                "installed (pip install -r requirements-cloud.txt)") from err
        self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    def get(self, key: str) -> str | None:
        try:
            name = self.settings.secret_path(key)
            response = self._ensure_client().access_secret_version(request={"name": name})
        except CredentialError:
            raise
        except Exception as err:  # SDK raises a wide range of transport errors
            log.warning("secret_manager_lookup_failed", secret=key, error=str(err))
            return None
        return response.payload.data.decode("utf-8").strip() or None


class AwsSecretsManagerProvider:
    """Reads a secret from AWS Secrets Manager with the runtime's IAM role.

    The secret id is ``ALM_SECRET_PREFIX`` + the logical name, so one account
    can hold several environments (``alm/test/...``, ``alm/prod/...``).
    """

    name = "aws-secrets-manager"

    def __init__(self, settings, client=None):
        self.settings = settings
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            try:
                import boto3
            except ImportError as err:  # pragma: no cover
                raise CredentialError(
                    "AWS Secrets Manager requested but boto3 is not installed "
                    "(pip install '.[aws]')") from err
            self._client = boto3.client("secretsmanager",
                                        region_name=self.settings.aws_region_name or None)
        return self._client

    def get(self, key: str) -> str | None:
        try:
            response = self._ensure_client().get_secret_value(
                SecretId=f"{self.settings.secret_prefix}{key}")
        except CredentialError:
            raise
        except Exception as err:  # noqa: BLE001 - not found, access denied, network
            log.warning("aws_secret_lookup_failed", secret=key, error=type(err).__name__)
            return None
        return (response.get("SecretString") or "").strip() or None


class AzureKeyVaultProvider:
    """Reads a secret from Azure Key Vault with the runtime's managed identity.

    Key Vault names allow letters, digits and dashes only, so underscores in a
    logical name become dashes.
    """

    name = "azure-key-vault"

    def __init__(self, settings, client=None):
        self.settings = settings
        self._client = client

    def _ensure_client(self):
        if self._client is None:
            if not self.settings.azure_key_vault_url:
                raise CredentialError("ALM_SECRET_BACKEND=azure needs ALM_AZURE_KEY_VAULT_URL")
            try:
                from azure.identity import DefaultAzureCredential
                from azure.keyvault.secrets import SecretClient
            except ImportError as err:  # pragma: no cover
                raise CredentialError(
                    "Azure Key Vault requested but azure-identity / azure-keyvault-secrets "
                    "are not installed (pip install '.[azure]')") from err
            self._client = SecretClient(self.settings.azure_key_vault_url,
                                        DefaultAzureCredential())
        return self._client

    def get(self, key: str) -> str | None:
        name = f"{self.settings.secret_prefix}{key}".replace("_", "-").replace("/", "-")
        try:
            secret = self._ensure_client().get_secret(name)
        except CredentialError:
            raise
        except Exception as err:  # noqa: BLE001 - not found, access denied, network
            log.warning("key_vault_lookup_failed", secret=key, error=type(err).__name__)
            return None
        return (secret.value or "").strip() or None


def cloud_secret_provider(settings) -> SecretProvider | None:
    """The deployment's secret store, or None on a laptop."""
    store = getattr(settings, "secret_store", "gcp" if settings.project_id else "none")
    if store == "gcp":
        return SecretManagerProvider(settings)
    if store == "aws":
        return AwsSecretsManagerProvider(settings)
    if store == "azure":
        return AzureKeyVaultProvider(settings)
    return None


class InteractiveProvider:
    """Last resort: prompt a human. Refuses when there is no terminal.

    Preserves the CLI behaviour - the operator types the password and it is
    never stored - while guaranteeing an unattended run can never block on it.
    """

    name = "interactive"

    def __init__(self, prompt: str = "Password", labels: dict[str, str] | None = None):
        self.prompt = prompt
        # What the person is asked, per secret: a question naming their account
        # means something to an operator; the secret's id does not.
        self.labels = labels or {}

    def get(self, key: str) -> str | None:
        if not _has_console():
            return None
        question = self.labels.get(key) or f"{self.prompt} ({key})"
        return getpass.getpass(f"{question}: ") or None


def _has_console() -> bool:
    """True only when a human can actually answer a prompt.

    ``isatty()`` alone is not enough on Windows: the NUL device reports itself as
    a tty, so a scheduled task or service with stdin on NUL - the Windows worker
    - would pass the check and then block forever inside getpass.
    """
    try:
        if not (sys.stdin and sys.stdin.isatty()):
            return False
    except (AttributeError, ValueError):
        return False
    if os.name != "nt":
        return True
    try:
        import ctypes
        import msvcrt

        mode = ctypes.c_uint32()
        handle = msvcrt.get_osfhandle(sys.stdin.fileno())
        # GetConsoleMode fails for anything that is not a real console - NUL included.
        return bool(ctypes.windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except (OSError, AttributeError, ValueError):
        return False


class CredentialResolver:
    """Tries each provider in order and caches what it finds.

    The order is the policy: a value already delivered to the process beats a
    Secret Manager round trip, and a human is only asked when nothing else has
    an answer.
    """

    def __init__(self, providers: list[SecretProvider], ttl: float = CACHE_TTL_SECONDS):
        if not providers:
            raise CredentialError("a CredentialResolver needs at least one provider")
        self.providers = providers
        self.ttl = ttl
        self._cache: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, *, refresh: bool = False) -> str:
        """Return the secret, or raise. The value is never included in the error."""
        now = time.monotonic()
        with self._lock:
            if not refresh:
                cached = self._cache.get(key)
                if cached and now - cached[1] < self.ttl:
                    return cached[0]

            tried = []
            for provider in self.providers:
                tried.append(provider.name)
                value = provider.get(key)
                if value:
                    self._cache[key] = (value, now)
                    log.info("credential_resolved", secret=key, provider=provider.name)
                    return value

        raise CredentialError(
            f"no value for secret {key!r}", context={"providers_tried": tried})

    def invalidate(self, key: str = "") -> None:
        """Drop one cached secret, or all of them, after a rotation or a 401."""
        with self._lock:
            if key:
                self._cache.pop(key, None)
            else:
                self._cache.clear()


def build_resolver(settings=None, *, prompt: str = "Password", interactive: bool = True,
                   labels: dict[str, str] | None = None) -> CredentialResolver:
    """The standard chain: environment -> mounted file -> cloud store -> interactive.

    The cloud store is only added when one is configured, so a laptop run does
    not pay for a lookup that cannot succeed. ``interactive=False`` is for
    optional secrets: an absent optional key must not stop a run to ask.
    """
    if settings is None:
        from .config import get_settings

        settings = get_settings()

    providers: list[SecretProvider] = [
        EnvironmentProvider({
            settings.password_secret_name: "EWM_PASSWORD",  # pragma: allowlist secret
            settings.approval_signing_secret_name: "ALM_APPROVAL_SIGNING_KEY",  # pragma: allowlist secret
            settings.webhook_secret_name: "ALM_WEBHOOK_HMAC_KEY",  # pragma: allowlist secret
            settings.gemini_api_key_secret_name: "GEMINI_API_KEY",  # pragma: allowlist secret
            settings.typesafe_api_key_secret_name: "TYPESAFE_API_KEY",  # pragma: allowlist secret
        }),
        FileProvider(os.getenv("ALM_SECRET_DIR", "/secrets")),
    ]
    cloud = cloud_secret_provider(settings)
    if cloud is not None:
        providers.append(cloud)
    if interactive:
        providers.append(InteractiveProvider(prompt, labels))
    return CredentialResolver(providers)


# ---------------------------------------------------------- Gemini API key

# GEMINI_API_KEY is what Google AI Studio tells you to set; GOOGLE_API_KEY is
# what the google-genai SDK reads on its own. Either works.
GEMINI_KEY_ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
GEMINI_KEY_HELP = (
    "Create a key at https://aistudio.google.com/apikey and put it in .env as "
    "GEMINI_API_KEY=... (the file is gitignored). In Cloud Run it comes from the "
    "Secret Manager secret named by ALM_GEMINI_API_KEY_SECRET_NAME.")


# Keys that live in .env for a local run. Listed so load_env can spot a
# placeholder in the Windows environment shadowing a real value in .env.
API_KEY_ENV_VARS = (*GEMINI_KEY_ENV_VARS, "TYPESAFE_API_KEY")

_PLACEHOLDER_RE = re.compile(
    r"^[\s{<\[$%(].*[}>\]%)]\s*$"                      # {YOUR_API_KEY}, <key>, ${VAR}, %VAR%
    r"|your[_ -]?(api[_ -]?)?key|api[_ -]?key[_ -]?here|changeme|^x{6,}$|^\.\.\.$",
    re.IGNORECASE)


def looks_like_placeholder(value: str) -> bool:
    """True for template text left where a key should be, e.g. ``{YOUR_API_KEY}``."""
    return bool(value) and _PLACEHOLDER_RE.search(value.strip()) is not None


def gemini_api_key(settings, resolver: CredentialResolver | None = None) -> str:
    """The Gemini Developer API key: environment, mounted file, Secret Manager, prompt.

    Resolved on demand and handed straight to the model client; it is never
    stored on Settings, so it cannot turn up in a settings dump or a repr.
    """
    for var in GEMINI_KEY_ENV_VARS:
        value = os.getenv(var, "").strip()
        if value and not looks_like_placeholder(value):
            return value
    resolver = resolver or build_resolver(settings, prompt="Gemini API key")
    try:
        return resolver.get(settings.gemini_api_key_secret_name)
    except CredentialError as err:
        raise CredentialError(f"no Gemini API key found. {GEMINI_KEY_HELP}") from err


def typesafe_api_key(settings, resolver: CredentialResolver | None = None) -> str | None:
    """The TypeSafe API key, or None. Optional, so it never prompts.

    The SDK would read TYPESAFE_API_KEY itself; resolving it here as well lets the
    key come from a mounted file or Secret Manager like every other secret.
    """
    resolver = resolver or build_resolver(settings, interactive=False)
    try:
        return resolver.get(settings.typesafe_api_key_secret_name)
    except CredentialError:
        return None


# ------------------------------------------------- database IAM authentication

_db_tokens: dict[str, tuple[str, float]] = {}
_db_lock = threading.Lock()

# How long each cloud's database token is reused: a little under its lifetime.
DB_TOKEN_REUSE_SECONDS = {"gcp_iam": DB_TOKEN_TTL_SECONDS, "aws_iam": 10 * 60,
                          "azure_ad": DB_TOKEN_TTL_SECONDS}
AZURE_POSTGRES_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"


def database_access_token(settings=None, *, mode: str = "gcp_iam") -> str:
    """A short-lived token to use as the database password.

    With IAM database authentication there is no stored database password to
    rotate, leak or commit - the runtime identity mints a token that expires on
    its own. Cached a little under its lifetime so a busy pool does not mint one
    per connection.
    """
    with _db_lock:
        now = time.monotonic()
        cached = _db_tokens.get(mode)
        if cached and now - cached[1] < DB_TOKEN_REUSE_SECONDS.get(mode, 600):
            return cached[0]
        token = {"gcp_iam": _gcp_db_token, "aws_iam": _aws_db_token,
                 "azure_ad": _azure_db_token}[mode](settings)
        if not token:
            raise CredentialError(f"could not mint a database token ({mode})")
        _db_tokens[mode] = (token, now)
        log.info("database_token_minted", mode=mode)
        return token


def _gcp_db_token(_settings) -> str:
    try:
        import google.auth
        import google.auth.transport.requests
    except ImportError as err:  # pragma: no cover
        raise CredentialError(
            "google-auth is required for Cloud SQL IAM authentication "
            "(pip install '.[gcp]')") from err
    credentials, _project = google.auth.default(scopes=[CLOUD_PLATFORM_SCOPE])
    credentials.refresh(google.auth.transport.requests.Request())
    return credentials.token


def _aws_db_token(settings) -> str:
    """An RDS IAM authentication token for the DSN's host, port and user."""
    from urllib.parse import urlsplit

    try:
        import boto3
    except ImportError as err:  # pragma: no cover
        raise CredentialError("boto3 is required for RDS IAM authentication "
                              "(pip install '.[aws]')") from err
    parts = urlsplit(settings.postgres_dsn)
    if not (parts.hostname and parts.username):
        raise CredentialError("RDS IAM authentication needs a DSN with a host and a user")
    client = boto3.client("rds", region_name=settings.aws_region_name or None)
    return client.generate_db_auth_token(DBHostname=parts.hostname, Port=parts.port or 5432,
                                         DBUsername=parts.username)


def _azure_db_token(_settings) -> str:
    try:
        from azure.identity import DefaultAzureCredential
    except ImportError as err:  # pragma: no cover
        raise CredentialError("azure-identity is required for Entra ID database "
                              "authentication (pip install '.[azure]')") from err
    return DefaultAzureCredential().get_token(AZURE_POSTGRES_SCOPE).token


def postgres_dsn(settings) -> str:
    """The DSN to connect with, with a fresh database token when IAM auth is on.

    The token is a password, so it never appears in a log line: the caller hands
    this straight to psycopg and nothing else.
    """
    from urllib.parse import quote, urlsplit

    dsn = settings.postgres_dsn
    mode = getattr(settings, "database_auth", "gcp_iam" if settings.postgres_iam_auth
                   else "password")
    if mode == "password" or not dsn:
        return dsn
    if "password=" in dsn or urlsplit(dsn).password:
        return dsn  # an explicit password wins; do not fight the operator
    token = database_access_token(settings, mode=mode)
    separator = "&" if "?" in dsn else "?"
    # RDS tokens are full of '&' and '='; quote so the DSN still parses.
    return f"{dsn}{separator}password={quote(token, safe='')}"
