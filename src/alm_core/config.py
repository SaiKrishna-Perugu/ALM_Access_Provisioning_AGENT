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
    approval_signing_secret_name: str = Field(default="alm-approval-signing-key")
    webhook_secret_name: str = Field(default="alm-webhook-hmac-key")
    secret_version: str = Field(
        default="latest",
        description="Pin to a numeric version in PROD if rotation must be deliberate.")

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
        description="Use a short-lived IAM access token as the database password.")
    postgres_pool_min: int = Field(default=1, ge=0)
    postgres_pool_max: int = Field(default=10, ge=1)

    # ------------------------------------------------------------ pub/sub
    pubsub_topic: str = Field(
        default="alm-ad-provisioning",
        description="Topic the orchestrator publishes AD jobs to.")
    pubsub_subscription: str = Field(
        default="alm-ad-provisioning-worker",
        description="Subscription the Windows worker pulls from.")

    # ---------------------------------------------------------- vertex ai
    vertex_location: str = Field(
        default="", description="Vertex AI region. Defaults to `region` when unset.")
    agent_model: str = Field(
        default="gemini-2.0-flash",
        description=("Model the agents reason and call tools with. A Claude id from "
                     "Vertex Model Garden switches the client - see llm.py."))
    supervisor_model: str = Field(
        default="",
        description="Optional cheaper model for routing. Defaults to agent_model.")
    llm_enabled: bool = Field(
        default=True,
        description="Turn the extraction fallback and comment drafting off entirely.")
    llm_max_output_tokens: int = Field(default=800, ge=1)

    # ---------------------------------------------------- orchestration
    orchestration: Literal["agentic", "deterministic"] = Field(
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

    # ----------------------------------------------------------- approval
    approval_ttl_minutes: int = Field(default=240, ge=1)
    approval_base_url: str = Field(default="", description="Public URL of the approval API.")
    chat_webhook_url: str = Field(
        default="",
        description="Google Chat space webhook that receives the approval card.")
    auto_approve_low_risk: bool = Field(
        default=False,
        description="Phase 9 step 3 only. Comments and evidence, never provisioning.")

    # -------------------------------------------------------------- polls
    reconcile_interval_minutes: int = Field(default=15, ge=1)
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
        if (self.orchestration == "agentic" and self.llm_enabled
                and not self.project_id):
            raise ValueError(
                "ALM_ORCHESTRATION=agentic needs GOOGLE_CLOUD_PROJECT: the agents are "
                "Vertex AI models and the client is project-scoped. Set it, or run with "
                "ALM_ORCHESTRATION=deterministic.")
        if self.orchestration == "agentic" and not self.llm_enabled:
            raise ValueError(
                "ALM_ORCHESTRATION=agentic contradicts ALM_LLM_ENABLED=false. Choose "
                "one: agentic routing with a model, or the deterministic graph.")
        if not self.shadow_mode and not self.postgres_dsn:
            raise ValueError(
                "ALM_POSTGRES_DSN is required outside shadow mode: the idempotency "
                "ledger and approval record are what make a write safe to replay.")
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
            raise ConfigError(
                "missing required configuration: "
                + ", ".join(f"ALM_{n.upper()}" for n in missing))


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
