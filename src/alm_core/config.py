"""Typed configuration for the autonomous Google Cloud deployment.

Everything the services need comes from the environment (Cloud Run injects
Secret Manager references as environment variables or mounted files), is
validated once at startup, and is then passed around as an object rather than
re-read with ``os.getenv`` at each call site - which is how the CLI ended up with
the same server URL parsed three different ways.

Two settings are deliberately strict:

* ``ca_bundle`` - TLS verification is on by default here. The CLI defaults to
  unverified because switching it on blind would break a working operator
  install; a container we build ourselves has the corporate CA baked in, so
  there is no such excuse. ``ALM_TLS_INSECURE=true`` exists only for a local
  developer loop and refuses to apply when ``environment`` is PROD.
* ``environment`` - TEST or PROD, explicit. Never inferred in the cloud.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Literal

try:
    from pydantic import Field, field_validator, model_validator
    from pydantic_settings import BaseSettings, SettingsConfigDict
except ImportError as err:  # pragma: no cover - cloud extra not installed
    raise ImportError(
        "alm_core.config needs the cloud dependencies: "
        "pip install -r requirements-cloud.txt"
    ) from err

from .errors import ConfigError

Environment = Literal["TEST", "PROD"]


class Settings(BaseSettings):
    """Validated configuration for every autonomous component."""

    model_config = SettingsConfigDict(
        env_prefix="ALM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ----------------------------------------------------------- identity
    environment: Environment = Field(
        default="TEST",
        description="Which ALM estate this process talks to. Never inferred in the cloud.")
    service_name: str = Field(default="alm-provisioning")
    shadow_mode: bool = Field(
        default=True,
        description="Phase 9 pilot: read and plan, never write. The safe default.")

    # ------------------------------------------------------ google cloud
    project_id: str = Field(
        default="", validation_alias="GOOGLE_CLOUD_PROJECT",
        description="GCP project id. Cloud Run sets this automatically.")
    region: str = Field(
        default="europe-west1",
        description="Region for Cloud Run, Cloud SQL and Vertex AI.")

    # ------------------------------------------------------------ servers
    ewm_server: str = Field(default="", validation_alias="EWM_SERVER")
    jts_server: str = Field(default="", validation_alias="JTS_SERVER")
    ewm_project_uuid: str = Field(default="", validation_alias="EWM_PROJECT_UUID")
    ewm_state_ids: str = Field(default="", validation_alias="EWM_ACTIVE_STATE_IDS")

    # -------------------------------------------------------- credentials
    service_account: str = Field(default="", validation_alias="CID",
                                 description="Non-interactive service account (CID).")
    # Secret Manager secret ids - the project and version are assembled by
    # secret_path(), so rotating the pinned version is one setting.
    password_secret_name: str = Field(default="alm-service-account-password")
    webhook_secret_name: str = Field(default="alm-webhook-hmac-key")
    secret_version: str = Field(
        default="latest",
        description="Pin to a numeric version in PROD if rotation must be deliberate.")
    secret_backend: Literal["auto", "gcp", "aws", "azure", "none"] = Field(
        default="auto",
        description=("Where secrets live, after the environment and mounted files. "
                     "gcp: Secret Manager. aws: Secrets Manager. azure: Key Vault. "
                     "auto: gcp when GOOGLE_CLOUD_PROJECT is set, otherwise none."))
    secret_prefix: str = Field(
        default="",
        description="Prefix for secret names in AWS Secrets Manager, e.g. 'alm/prod/'.")
    aws_region: str = Field(
        default="", description="AWS region (Secrets Manager, RDS tokens, Bedrock). "
                                "Defaults to AWS_REGION.")
    azure_key_vault_url: str = Field(
        default="", description="Key Vault URL, e.g. https://alm-prod.vault.azure.net/")

    # ---------------------------------------------------------------- TLS
    ca_bundle: str = Field(
        default="/etc/ssl/certs/corporate-ca.pem",
        description="Corporate CA bundle baked into the image.")
    tls_insecure: bool = Field(
        default=False,
        description="Local development only. Refused when environment is PROD.")

    # ----------------------------------------------------------- database
    postgres_dsn: str = Field(
        default="",
        description=("Cloud SQL DSN. Reached on the instance private IP over Direct "
                     "VPC egress. With IAM database auth the password is omitted - an "
                     "access token is fetched at connect time."))
    postgres_iam_auth: bool = Field(
        default=True,
        description="Use a short-lived IAM access token as the database password "
                    "(Cloud SQL). Kept for older settings; db_auth says which cloud.")
    db_auth: Literal["auto", "password", "gcp_iam", "aws_iam", "azure_ad"] = Field(
        default="auto",
        description=("How the service authenticates to Postgres. password: the DSN's own. "
                     "gcp_iam / aws_iam / azure_ad: a short-lived token from the "
                     "runtime identity, minted per connection. auto: gcp_iam when "
                     "postgres_iam_auth is on, otherwise password."))
    postgres_pool_min: int = Field(default=1, ge=0)
    postgres_pool_max: int = Field(default=10, ge=1)
    ledger_path: str = Field(
        default="",
        description=("Local SQLite file for the ledger, audit trail, approvals and graph "
                     "checkpoints - the laptop alternative to ALM_POSTGRES_DSN."))

    # ------------------------------------------------------------ local run
    redact_for_model: bool = Field(
        default=True,
        description=("Strip e-mail addresses from tool results before a model sees them. "
                     "The agents never need them: the e-mail-vs-LDAP check runs in code."))

    # ------------------------------------------------------------ pub/sub
    ad_directory: Literal["gpt", "graph"] = Field(
        default="gpt",
        description=("How a user joins the AD group. gpt: the GPT web UI, driven by the "
                     "Windows worker (or the debug Chrome locally). graph: Microsoft "
                     "Graph, directly from the run - only for groups mastered in Entra "
                     "ID; a group synced from on-premises AD must stay on gpt."))
    graph_tenant_id: str = Field(default="", description="Entra ID tenant id.")
    graph_client_id: str = Field(default="", description="App registration (client) id.")
    graph_client_secret_name: str = Field(
        default="alm-graph-client-secret",
        description="Secret holding the app registration's client secret.")
    graph_group_id: str = Field(
        default="", description="Object id of the group; looked up by name when empty.")
    graph_user_lookup: Literal["sam", "upn"] = Field(
        default="sam",
        description=("How a user ID is found in Entra ID. sam: onPremisesSamAccountName "
                     "(synced accounts). upn: <userid>@<graph_upn_suffix>."))
    graph_upn_suffix: str = Field(default="", description="e.g. example.com, for upn lookup.")
    ad_job_transport: Literal["store", "pubsub"] = Field(
        default="store",
        description=("How AD jobs reach the Windows worker. store: the shared Postgres "
                     "job queue (any cloud). pubsub: Google Pub/Sub."))
    pubsub_topic: str = Field(
        default="alm-ad-provisioning",
        description="Topic the orchestrator publishes AD jobs to.")
    pubsub_subscription: str = Field(
        default="alm-ad-provisioning-worker",
        description="Subscription the Windows worker pulls from.")

    # ---------------------------------------------------------------- llm
    llm_provider: Literal["gemini_api", "vertex_express", "vertex", "bedrock",
                          "azure_openai"] = Field(
        default="gemini_api",
        description=("gemini_api: the Gemini Developer API with a key from Google AI "
                     "Studio (GEMINI_API_KEY). vertex_express: Vertex AI with an API key "
                     "created in the Google Cloud console (also GEMINI_API_KEY) - the two "
                     "kinds of key look alike but each works on one endpoint only. "
                     "vertex: Vertex AI as a service account - no key, project-scoped. "
                     "bedrock: AWS Bedrock with the runtime IAM role (Claude, Llama, "
                     "Mistral...). azure_openai: Azure OpenAI with a managed identity or "
                     "AZURE_OPENAI_API_KEY; ALM_AGENT_MODEL is the deployment name. "
                     "See llm.py."))
    azure_openai_endpoint: str = Field(
        default="", description="https://<resource>.openai.azure.com/")
    azure_openai_api_version: str = Field(default="2024-10-21")
    gemini_api_key_secret_name: str = Field(
        default="alm-gemini-api-key",
        description="Secret Manager id holding the Gemini API key (gemini_api provider).")
    llm_requests_per_minute: float = Field(
        default=10.0, gt=0,
        description=("Client-side ceiling on model calls, shared by every agent in the "
                     "process. The Gemini free tier rejects bursts; a multi-agent run "
                     "makes several calls per second without this."))
    llm_thinking_level: Literal["default", "minimal", "low", "medium", "high"] = Field(
        default="low",
        description=("How much a Gemini model reasons before answering. Tool routing "
                     "needs little; 'low' keeps latency and free-tier quota down. "
                     "Gemini 3+ takes it as thinking_level. On Gemini 2.5 Flash, "
                     "'minimal' switches thinking off; other values leave the model's "
                     "default. 'default' never sends the parameter."))
    vertex_location: str = Field(
        default="", description="Vertex AI region. Defaults to `region` when unset.")
    agent_model: str = Field(
        default="gemini-3.5-flash",
        description=("Model the agents reason and call tools with. With the vertex "
                     "provider a Claude id from Model Garden switches the client - see "
                     "llm.py."))
    supervisor_model: str = Field(
        default="",
        description="Optional cheaper model for routing. Defaults to agent_model.")
    llm_enabled: bool = Field(
        default=True,
        description="Turn the extraction fallback and comment drafting off entirely.")
    llm_max_output_tokens: int = Field(default=800, ge=1)

    # ------------------------------------------------------ AI governance
    allowed_providers: str = Field(
        default="",
        description=("Comma-separated model providers this deployment may use; empty "
                     "allows any. A client data policy belongs here, e.g. 'vertex' or "
                     "'bedrock,azure_openai'."))
    model_withheld_fields: str = Field(
        default="",
        description=("Comma-separated fields never shown to a model, e.g. "
                     "'justification,summary'. Tools still use them; the model sees "
                     "'[withheld]'."))
    max_tokens_per_run: int = Field(
        default=400_000, ge=0, description="Model tokens one run may spend; 0 = no cap.")
    max_tokens_per_day: int = Field(
        default=0, ge=0, description="Model tokens all runs may spend per UTC day; 0 = no cap.")
    otel_enabled: bool = Field(
        default=False,
        description=("Export each run as OpenTelemetry spans, and the service's metrics, "
                     "over OTLP/HTTP to OTEL_EXPORTER_OTLP_ENDPOINT."))
    otel_metric_interval_seconds: int = Field(
        default=60, ge=5, description="How often metrics are exported.")
    degrade_on_model_failure: bool = Field(
        default=False,
        description=("When the model is unavailable, re-run the same work items in the "
                     "fixed order (deterministic orchestration), which needs no model; "
                     "past the daily token cap, start new runs that way instead of "
                     "refusing them."))

    # --------------------------------------------- user-ID recovery (TypeSafe)
    extraction_provider: Literal["auto", "typesafe", "gemini"] = Field(
        default="auto",
        description=("Who judges user IDs in rows the parser rejected. Either way, code "
                     "finds the candidate tokens and only those can be returned. "
                     "typesafe: a yes/no probability per candidate from TypeSafe's Jev "
                     "model. gemini: the agent model proposes, and anything not in the "
                     "text is dropped. auto: TypeSafe when TYPESAFE_API_KEY resolves, "
                     "Gemini otherwise or if TypeSafe fails."))
    typesafe_model: str = Field(default="jev-latest")
    typesafe_api_key_secret_name: str = Field(
        default="alm-typesafe-api-key",
        description="Secret Manager id holding the TypeSafe API key.")
    extraction_min_probability: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description=("A candidate below this probability is not proposed. A placeholder: "
                     "tune it on real malformed rows before relying on it. Every "
                     "recovered ID is HIGH risk and goes to a human regardless."))

    # ---------------------------------------------------- orchestration
    orchestration: Literal["guided", "agentic", "deterministic"] = Field(
        default="agentic",
        description=("agentic: an LLM supervisor routes autonomous tool-calling agents. "
                     "deterministic: the fixed graph, no routing model. The agentic "
                     "mode degrades to the fixed sequence if the supervisor model is "
                     "unavailable, but the agents themselves require an LLM."))
    max_hops: int = Field(
        default=24, ge=1, le=200,
        description="Routing decisions per run. Bounds a supervisor that oscillates.")
    max_writes_per_run: int = Field(
        default=50, ge=0,
        description="Hard ceiling on writes, whatever any agent decides.")
    max_tool_calls_per_run: int = Field(
        default=400, ge=1,
        description="Shared tool-call budget across every agent in a run.")
    agent_temperature: float = Field(default=0.0, ge=0.0, le=2.0)

    # ------------------------------------------------------ sign-in, roles
    auth_mode: Literal["iap", "oidc"] = Field(
        default="iap",
        description=("How people sign in to the API and console. iap: an identity-aware "
                     "proxy in front (Google IAP) vouches for them. oidc: the service "
                     "signs them in against the company IdP (Entra ID, Okta, Ping...)."))
    oidc_issuer: str = Field(default="", description="e.g. https://login.microsoftonline.com/<tenant>/v2.0")
    oidc_client_id: str = Field(default="")
    oidc_client_secret_name: str = Field(default="alm-oidc-client-secret")
    oidc_groups_claim: str = Field(default="groups")
    oidc_scopes: str = Field(default="openid profile email")
    session_secret_name: str = Field(
        default="alm-session-signing-key",
        description="Secret that signs session cookies; the same on every replica.")
    session_hours: float = Field(default=10.0, gt=0, le=24)
    role_map: str = Field(
        default="{}",
        description=("JSON: IdP group (id or name) or e-mail address -> role. Roles: "
                     "viewer, operator, approver, auditor, admin. '*' gives every "
                     "signed-in person a role."))

    # ----------------------------------------------------------- approval
    approval_ttl_minutes: int = Field(default=240, ge=1)
    approvers_required: int = Field(default=1, ge=1, le=5)
    approvers_required_prod: int = Field(
        default=2, ge=1, le=5, description="Approvers a production card needs.")
    approvers_required_high_risk: int = Field(
        default=2, ge=1, le=5, description="Approvers a card with a high-risk user needs.")
    notify_channels: str = Field(
        default="chat",
        description="Where approval cards go: any of chat, teams, email (comma-separated).")
    teams_webhook_url: str = Field(default="", description="Teams incoming webhook.")
    smtp_host: str = Field(default="")
    smtp_port: int = Field(default=587)
    smtp_from: str = Field(default="")
    approver_emails: str = Field(default="", description="Comma-separated recipients.")
    smtp_password_secret_name: str = Field(
        default="alm-smtp-password", description="Optional; SMTP without auth when unset.")
    approval_base_url: str = Field(default="", description="Public URL of the approval API.")
    chat_webhook_url: str = Field(
        default="",
        description="Google Chat space webhook that receives the approval card.")
    auto_approve_low_risk: bool = Field(
        default=False,
        description="Phase 9 step 3 only. Comments and evidence, never provisioning.")

    # ------------------------------------------------------------ workers
    worker_concurrency: int = Field(
        default=1, ge=0, le=32,
        description="Runs one process drives at once. The API process runs this many "
                    "embedded workers (0: the API only enqueues; run `python -m "
                    "alm_agents.worker` separately).")
    job_lease_seconds: int = Field(
        default=120, ge=15,
        description="How long a claimed job is a worker's alone without a heartbeat. "
                    "A dead worker's job is taken over after this.")
    job_max_attempts: int = Field(default=5, ge=1, le=20)
    worker_poll_seconds: float = Field(default=2.0, gt=0, le=60)
    trace_dir: str = Field(
        default="", description="Where workers write run traces (JSONL). Default: the "
                                "system temp directory; the store keeps a copy.")

    # -------------------------------------------------------------- polls
    reconcile_interval_minutes: int = Field(
        default=15, ge=0, description="Minutes between queue sweeps; 0 turns the sweep off.")
    auto_migrate: bool = Field(
        default=True,
        description=("Migrate the schema when a service starts. false: the services only "
                     "check it, and a separate step runs python -m alm_core.store.migrate "
                     "as the schema's owner (see that module)."))
    retention_days: int = Field(
        default=30, ge=0,
        description=("Delete finished runs' data (checkpoints, traces, approval cards, "
                     "votes, agent memory) this many days after they last changed, once a "
                     "day. The ledger and the audit trail are kept. 0 keeps everything."))
    worker_health_port: int = Field(
        default=0, ge=0, le=65535,
        description=("A run worker answers GET /healthz on this port, for a platform's "
                     "liveness probe. 0: no listener."))
    permission_wait_minutes: int = Field(default=30, ge=0)
    permission_interval_minutes: int = Field(default=5, ge=1)
    jazz_role: str = Field(default="JazzUsers")

    # ------------------------------------------------------- rate limits
    max_concurrent_writes: int = Field(default=4, ge=1)
    http_timeout_connect: float = Field(default=15.0, gt=0)
    http_timeout_read: float = Field(default=120.0, gt=0)
    http_max_retries: int = Field(default=3, ge=0)
    circuit_breaker_threshold: int = Field(default=5, ge=1)
    circuit_breaker_cooldown_seconds: float = Field(default=60.0, gt=0)

    # ---------------------------------------------------------- telemetry
    trace_enabled: bool = Field(default=True, description="Export spans to Cloud Trace.")
    log_level: str = Field(default="INFO")

    @field_validator("ewm_server", "jts_server", "approval_base_url", mode="after")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @model_validator(mode="after")
    def _check_coherent(self) -> Settings:
        if self.tls_insecure and self.environment == "PROD":
            raise ValueError(
                "ALM_TLS_INSECURE cannot be set when ALM_ENVIRONMENT=PROD. "
                "Mount the corporate CA bundle and point ALM_CA_BUNDLE at it.")
        # The Gemini API key is not checked here: it is a secret, resolved by
        # credentials.gemini_api_key() from the environment, a mounted file or
        # Secret Manager, and never held on this object.
        if (self.orchestration in ("agentic", "guided") and self.llm_enabled
                and self.llm_provider == "vertex" and not self.project_id):
            raise ValueError(
                "ALM_LLM_PROVIDER=vertex needs GOOGLE_CLOUD_PROJECT: the Vertex AI "
                "client is project-scoped. Set it, use ALM_LLM_PROVIDER=gemini_api "
                "with a GEMINI_API_KEY, or run with ALM_ORCHESTRATION=deterministic.")
        allowed = {p.strip() for p in self.allowed_providers.split(",") if p.strip()}
        if allowed and self.llm_enabled and self.llm_provider not in allowed:
            raise ValueError(
                f"ALM_LLM_PROVIDER={self.llm_provider} is not in ALM_ALLOWED_PROVIDERS "
                f"({', '.join(sorted(allowed))}): this deployment's data policy forbids it")
        models = [m.lower() for m in (self.agent_model, self.supervisor_model) if m]
        if self.llm_provider in ("gemini_api", "vertex_express") and any(
                m.startswith("claude") for m in models):
            raise ValueError(
                "Claude models are served through Vertex AI Model Garden or AWS Bedrock, "
                "not the Gemini API. Set ALM_LLM_PROVIDER=vertex or bedrock, or choose "
                "a gemini-* model.")
        if self.llm_provider in ("bedrock", "azure_openai") and any(
                m.startswith("gemini") for m in models):
            raise ValueError(
                f"ALM_LLM_PROVIDER={self.llm_provider} cannot serve a Gemini model. Set "
                "ALM_AGENT_MODEL to a model this provider serves (for azure_openai, "
                "the deployment name).")
        if (self.llm_provider == "azure_openai" and self.llm_enabled
                and self.orchestration in ("agentic", "guided")
                and not self.azure_openai_endpoint):
            raise ValueError("ALM_LLM_PROVIDER=azure_openai needs ALM_AZURE_OPENAI_ENDPOINT.")
        if self.orchestration in ("agentic", "guided") and not self.llm_enabled:
            raise ValueError(
                f"ALM_ORCHESTRATION={self.orchestration} contradicts ALM_LLM_ENABLED=false. Choose "
                "one: agentic routing with a model, or the deterministic graph.")
        if not self.shadow_mode and not (self.postgres_dsn or self.ledger_path):
            raise ValueError(
                "A durable ledger is required outside shadow mode - ALM_POSTGRES_DSN, or "
                "ALM_LEDGER_PATH for a local SQLite file. The idempotency ledger and "
                "approval record are what make a write safe to replay.")
        for name, host in (("EWM_SERVER", self.ewm_server), ("JTS_SERVER", self.jts_server)):
            if host and not host.startswith("https://"):
                raise ValueError(f"{name} must be an https:// URL, got {host!r}")
        return self

    # ------------------------------------------------------------ helpers

    @property
    def verify(self) -> str | bool:
        """The value to hand ``requests``/``httpx`` as ``verify=``."""
        if self.tls_insecure:
            return False
        if self.ca_bundle and os.path.exists(self.ca_bundle):
            return self.ca_bundle
        if self.ca_bundle:
            raise ConfigError(
                f"ALM_CA_BUNDLE points at {self.ca_bundle!r}, which does not exist in this "
                "container. The corporate CA must be baked into the image (see Dockerfile).")
        return True

    @property
    def timeout(self) -> tuple[float, float]:
        return (self.http_timeout_connect, self.http_timeout_read)

    @property
    def is_prod(self) -> bool:
        return self.environment == "PROD"

    @property
    def state_ids(self) -> list[str]:
        return [s.strip() for s in self.ewm_state_ids.split(",") if s.strip()]

    @property
    def vertex_region(self) -> str:
        return self.vertex_location or self.region

    @property
    def routing_model(self) -> str:
        return self.supervisor_model or self.agent_model

    @property
    def secret_store(self) -> str:
        """The cloud secret store in use: gcp, aws, azure or none."""
        if self.secret_backend != "auto":  # noqa: S105  # pragma: allowlist secret
            return self.secret_backend
        return "gcp" if self.project_id else "none"

    @property
    def database_auth(self) -> str:
        """password, gcp_iam, aws_iam or azure_ad."""
        if self.db_auth != "auto":
            return self.db_auth
        return "gcp_iam" if self.postgres_iam_auth else "password"

    @property
    def aws_region_name(self) -> str:
        return (self.aws_region or os.getenv("AWS_REGION", "")
                or os.getenv("AWS_DEFAULT_REGION", ""))

    def secret_path(self, secret_id: str) -> str:
        """Full Secret Manager resource name for a secret id."""
        self.require("project_id")
        return f"projects/{self.project_id}/secrets/{secret_id}/versions/{self.secret_version}"

    def topic_path(self) -> str:
        self.require("project_id")
        return f"projects/{self.project_id}/topics/{self.pubsub_topic}"

    def subscription_path(self) -> str:
        self.require("project_id")
        return f"projects/{self.project_id}/subscriptions/{self.pubsub_subscription}"

    def require(self, *names: str) -> None:
        """Fail fast, naming every missing setting at once rather than one per restart."""
        missing = [n for n in names if not getattr(self, n, None)]
        if missing:
            raise ConfigError("missing required configuration: "
                              + ", ".join(self.env_name(n) for n in missing))

    @classmethod
    def env_name(cls, field_name: str) -> str:
        """The environment variable a setting is read from (CID, not ALM_SERVICE_ACCOUNT)."""
        alias = (cls.model_fields[field_name].validation_alias
                 if field_name in cls.model_fields else None)
        return alias if isinstance(alias, str) else f"ALM_{field_name.upper()}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings, validated once."""
    try:
        return Settings()
    except Exception as err:  # pydantic ValidationError, or our own ValueError
        raise ConfigError(f"invalid configuration: {err}") from err


def reset_settings_cache() -> None:
    """Only for tests and for a deliberate hot reload."""
    get_settings.cache_clear()
