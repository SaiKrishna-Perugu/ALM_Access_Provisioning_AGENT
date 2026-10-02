"""Cloud-neutral adapters: secrets, database tokens, model providers, AD jobs.

The core never imports a cloud SDK; each adapter imports its own, lazily. The
SDKs are faked here so the adapters are tested on any machine.
"""
from __future__ import annotations

import subprocess
import sys
import types
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic")

from alm_core import credentials  # noqa: E402
from alm_core.config import Settings  # noqa: E402
from alm_core.errors import CredentialError  # noqa: E402


def settings(**extra) -> Settings:
    base = {"_env_file": None, "environment": "TEST", "orchestration": "deterministic",
            "llm_enabled": False, "shadow_mode": True}
    base.update(extra)
    return Settings(**base)


# ----------------------------------------------------------------- imports

def test_the_core_imports_no_cloud_sdk():
    """A laptop, an AWS account and an Azure tenant all import the same core."""
    code = (
        "import sys\n"
        "import alm_core.config, alm_core.credentials, alm_core.store\n"
        "import alm_core.tools.gpt_queue, alm_agents.graph, alm_agents.worker\n"
        "import alm_agents.llm, alm_api.main\n"
        "sdks = ('boto3', 'botocore', 'azure.identity', 'azure.keyvault',\n"
        "        'google.cloud.secretmanager', 'google.cloud.pubsub_v1', 'google.auth',\n"
        "        'langchain_aws', 'langchain_openai', 'langchain_google_genai',\n"
        "        'langchain_google_vertexai')\n"
        "loaded = sorted(m for m in sys.modules if m.startswith(sdks))\n"
        "print(','.join(loaded))\n")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True, env={**__import__("os").environ,
                                          "PYTHONPATH": "src"}).stdout.strip()
    assert out == "", f"imported at module load: {out}"


# ----------------------------------------------------------------- secrets

class FakeAwsSecrets:
    def __init__(self):
        self.asked = []

    def get_secret_value(self, SecretId):  # noqa: N803 - the boto3 signature  # pragma: allowlist secret
        self.asked.append(SecretId)  # pragma: allowlist secret
        if SecretId == "alm/prod/alm-service-account-password":  # pragma: allowlist secret
            return {"SecretString": "s3cret\n"}  # pragma: allowlist secret
        raise KeyError("ResourceNotFoundException")


class FakeKeyVault:
    def __init__(self):
        self.asked = []

    def get_secret(self, name):
        self.asked.append(name)
        if name == "alm-webhook-hmac-key":
            return SimpleNamespace(value="hmac-value")
        raise KeyError("SecretNotFound")


def test_aws_secrets_manager_reads_prefixed_names_and_misses_quietly():
    fake = FakeAwsSecrets()
    provider = credentials.AwsSecretsManagerProvider(
        settings(secret_backend="aws", secret_prefix="alm/prod/"), client=fake)  # pragma: allowlist secret
    assert provider.get("alm-service-account-password") == "s3cret"  # pragma: allowlist secret
    assert provider.get("missing") is None
    assert fake.asked == ["alm/prod/alm-service-account-password", "alm/prod/missing"]


def test_azure_key_vault_maps_names_to_what_key_vault_allows():
    fake = FakeKeyVault()
    provider = credentials.AzureKeyVaultProvider(
        settings(secret_backend="azure", azure_key_vault_url="https://v.vault.azure.net/"),  # pragma: allowlist secret
        client=fake)
    assert provider.get("alm_webhook_hmac_key") == "hmac-value"
    assert provider.get("nope") is None


def test_the_chain_uses_the_configured_cloud_store():
    def kinds(s):
        return [p.name for p in credentials.build_resolver(s, interactive=False).providers]

    assert kinds(settings()) == ["environment", "mounted-file"]
    gcp = settings().model_copy(update={"project_id": "p"})
    assert kinds(gcp) == ["environment", "mounted-file", "secret-manager"]
    assert kinds(settings(secret_backend="aws"))[-1] == "aws-secrets-manager"  # pragma: allowlist secret
    assert kinds(settings(secret_backend="azure"))[-1] == "azure-key-vault"  # pragma: allowlist secret
    assert kinds(gcp.model_copy(update={"secret_backend": "none"})) == ["environment",  # pragma: allowlist secret
                                                                         "mounted-file"]


def test_azure_without_a_vault_url_says_so():
    with pytest.raises(CredentialError, match="AZURE_KEY_VAULT_URL"):
        credentials.AzureKeyVaultProvider(settings(secret_backend="azure"))._ensure_client()  # pragma: allowlist secret


# ---------------------------------------------------------- database tokens

@pytest.fixture(autouse=True)
def fresh_tokens():
    credentials._db_tokens.clear()
    yield
    credentials._db_tokens.clear()


def test_an_rds_token_is_minted_for_the_dsns_host_and_user_and_quoted(monkeypatch):
    calls = []

    class Rds:
        def generate_db_auth_token(self, **kw):
            calls.append(kw)
            return "host:5432/?Action=connect&DBUser=alm&X-Amz-Signature=ab=="

    monkeypatch.setitem(sys.modules, "boto3", types.SimpleNamespace(
        client=lambda service, region_name=None: Rds()))
    s = settings(postgres_dsn="postgresql://alm@db.example.com:6432/alm", db_auth="aws_iam",  # pragma: allow-pii
                 aws_region="eu-west-1")
    dsn = credentials.postgres_dsn(s)
    assert calls == [{"DBHostname": "db.example.com", "Port": 6432, "DBUsername": "alm"}]
    assert dsn.startswith("postgresql://alm@db.example.com:6432/alm?password=")  # pragma: allow-pii
    assert "&DBUser" not in dsn and "%26DBUser" in dsn          # quoted, still one param
    credentials.postgres_dsn(s)
    assert len(calls) == 1                                       # reused, not re-minted


def test_an_entra_token_is_minted_for_azure_postgres(monkeypatch):
    scopes = []

    class Credential:
        def get_token(self, scope):
            scopes.append(scope)
            return SimpleNamespace(token="entra-token")

    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = Credential
    monkeypatch.setitem(sys.modules, "azure", types.ModuleType("azure"))
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    s = settings(postgres_dsn="postgresql://alm@db.postgres.database.azure.com/alm",  # pragma: allow-pii
                 db_auth="azure_ad")
    assert credentials.postgres_dsn(s).endswith("?password=entra-token")
    assert scopes == [credentials.AZURE_POSTGRES_SCOPE]


def test_password_auth_and_an_explicit_password_leave_the_dsn_alone():
    plain = "postgresql://alm:pw@db/alm"  # pragma: allowlist secret
    assert credentials.postgres_dsn(settings(postgres_dsn=plain, db_auth="aws_iam")) == plain
    assert credentials.postgres_dsn(settings(postgres_dsn="postgresql://alm@db/alm",
                                             db_auth="password")) == "postgresql://alm@db/alm"
    assert settings(postgres_iam_auth=True).database_auth == "gcp_iam"
    assert settings(postgres_iam_auth=False).database_auth == "password"


# ------------------------------------------------------------------- models

def _fake_chat_module(name: str, cls: str, seen: list):
    module = types.ModuleType(name)

    class Chat:
        def __init__(self, **kw):
            seen.append(kw)

        def bind_tools(self, *_a, **_k):
            return self

    setattr(module, cls, Chat)
    return module


def test_bedrock_builds_a_traced_client_with_the_runtime_role(monkeypatch):
    from alm_agents import llm

    seen = []
    monkeypatch.setitem(sys.modules, "langchain_aws",
                        _fake_chat_module("langchain_aws", "ChatBedrockConverse", seen))
    llm.reset_clients()
    s = settings(orchestration="agentic", llm_enabled=True, llm_provider="bedrock",
                 agent_model="anthropic.claude-sonnet-4-5", aws_region="eu-central-1")
    client = llm.get_agent_llm(s)
    assert type(client).__name__ == "TracedModel"
    assert seen[0]["model"] == "anthropic.claude-sonnet-4-5"
    assert seen[0]["region_name"] == "eu-central-1" and "api_key" not in seen[0]  # pragma: allowlist secret
    llm.reset_clients()


def test_azure_openai_uses_the_key_when_given_else_the_managed_identity(monkeypatch):
    from alm_agents import llm

    seen = []
    monkeypatch.setitem(sys.modules, "langchain_openai",
                        _fake_chat_module("langchain_openai", "AzureChatOpenAI", seen))
    s = settings(orchestration="agentic", llm_enabled=True, llm_provider="azure_openai",
                 agent_model="gpt-4o-alm", azure_openai_endpoint="https://r.openai.azure.com/")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k-123")
    llm.reset_clients()
    llm.get_agent_llm(s)
    assert seen[0]["azure_deployment"] == "gpt-4o-alm" and seen[0]["api_key"] == "k-123"  # pragma: allowlist secret

    monkeypatch.delenv("AZURE_OPENAI_API_KEY")
    identity = types.ModuleType("azure.identity")
    identity.DefaultAzureCredential = lambda: "credential"
    identity.get_bearer_token_provider = lambda cred, scope: (cred, scope)
    monkeypatch.setitem(sys.modules, "azure", types.ModuleType("azure"))
    monkeypatch.setitem(sys.modules, "azure.identity", identity)
    llm.reset_clients()
    llm.get_agent_llm(s)
    assert seen[1]["azure_ad_token_provider"] == (
        "credential", "https://cognitiveservices.azure.com/.default")
    llm.reset_clients()


def test_a_missing_provider_package_degrades_to_no_client(monkeypatch):
    from alm_agents import llm

    monkeypatch.setitem(sys.modules, "langchain_aws", None)   # import fails
    llm.reset_clients()
    s = settings(orchestration="agentic", llm_enabled=True, llm_provider="bedrock",
                 agent_model="anthropic.claude-sonnet-4-5")
    assert llm.get_agent_llm(s) is None
    llm.reset_clients()


@pytest.mark.parametrize(("extra", "message"), [
    ({"llm_provider": "bedrock", "agent_model": "gemini-3.5-flash"}, "cannot serve a Gemini"),
    ({"llm_provider": "azure_openai", "agent_model": "gpt-4o"}, "AZURE_OPENAI_ENDPOINT"),
    ({"llm_provider": "gemini_api", "agent_model": "claude-sonnet"}, "Vertex AI Model Garden"),
])
def test_a_model_its_provider_cannot_serve_is_refused(extra, message):
    with pytest.raises(Exception, match=message):
        settings(orchestration="agentic", llm_enabled=True, **extra)


# ------------------------------------------------------------------ AD jobs

class FakeGpt:
    def __init__(self):
        self.added = []

    def add_member(self, *, userid, group, domain):
        self.added.append((userid, group, domain))
        return True, "GPT accepted the request"

    def close(self):
        pass


def test_an_ad_job_travels_through_the_store_to_the_worker_once(monkeypatch):
    from alm_core.models import ApprovalDecision, RequestedUser, SourceWorkItem
    from alm_core.store.memory import MemoryStore
    from alm_core.tools import gpt_queue
    from alm_core.tools.base import ToolContext
    from alm_worker import main as gpt_worker

    s = settings(shadow_mode=False, ledger_path="unused.db")
    store = MemoryStore()
    worker = gpt_worker.Worker(s)
    worker.store, worker.session = store, FakeGpt()
    ctx = ToolContext(settings=s, client=None, store=store, run_id="r1", thread_id="wi-1001")
    ctx.approval = ApprovalDecision(thread_id="wi-1001", approved=True, approver="boss",
                                    plan_hash="h", approved_userids=["AB12345"])
    user = RequestedUser(userid="AB12345",
                         source_work_items=[SourceWorkItem(work_item_id="1001", summary="")])

    submitted = worker._run_async(gpt_queue.request_group_membership(
        ctx, user, group="GR_D-JazzUser-NA", domain="INETPSA"))
    again = worker._run_async(gpt_queue.request_group_membership(
        ctx, user, group="GR_D-JazzUser-NA", domain="INETPSA"))

    job = worker._run_async(store.claim_job(worker.worker_id, 60, kinds=(gpt_queue.AD_JOB,)))
    worker.handle_store_job(job)
    # A redelivery (another job for the same key) must not reach GPT twice.
    worker._run_async(store.enqueue_job(gpt_queue.AD_JOB, "other", job["payload"]))
    redelivered = worker._run_async(store.claim_job(worker.worker_id, 60,
                                                    kinds=(gpt_queue.AD_JOB,)))
    worker.handle_store_job(redelivered)
    worker._loop.close()

    assert submitted.outcome.value == "ok" and again.replayed
    assert worker.session.added == [("AB12345", "GR_D-JazzUser-NA", "INETPSA")]
    assert [j["status"] for j in store._jobs] == ["done", "done"]
    assert worker._processed == 2


def test_a_poison_ad_job_goes_dead_without_retries():
    from alm_core.store.memory import MemoryStore
    from alm_worker import main as gpt_worker

    worker = gpt_worker.Worker(settings())
    worker.store, worker.session = MemoryStore(), FakeGpt()
    worker._run_async(worker.store.enqueue_job("ad_job", "k", {"operation": "nonsense"}))
    job = worker._run_async(worker.store.claim_job(worker.worker_id, 60, kinds=("ad_job",)))
    worker.handle_store_job(job)
    dead = worker._run_async(worker.store.list_jobs(status="dead"))
    worker._loop.close()
    assert len(dead) == 1 and dead[0]["error"].startswith("poison")
    assert worker.session.added == []
