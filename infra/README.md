# Infrastructure

One Terraform configuration per cloud. The application is the same image on
all of them; a cloud is chosen with settings, not with code:

| Concern | Setting | GCP | AWS | Azure |
|---|---|---|---|---|
| Secrets | `ALM_SECRET_BACKEND` | Secret Manager (`gcp`) | Secrets Manager (`aws`) | Key Vault (`azure`) |
| Database login | `ALM_DB_AUTH` | Cloud SQL IAM (`gcp_iam`) | RDS IAM (`aws_iam`) | Entra ID (`azure_ad`) |
| Models | `ALM_LLM_PROVIDER` | Vertex AI (`vertex`) | Bedrock (`bedrock`) | Azure OpenAI (`azure_openai`) |
| Sign-in | `ALM_AUTH_MODE` | IAP (`iap`) | OIDC (`oidc`) | OIDC with Entra ID (`oidc`) |
| AD jobs | `ALM_AD_JOB_TRANSPORT` | Pub/Sub (`pubsub`) | the database (`store`) | the database (`store`) |
| Telemetry | `OTEL_EXPORTER_OTLP_ENDPOINT` | a collector to Cloud Trace | ADOT | a collector to Azure Monitor |

| Folder | State |
|---|---|
| [`gcp/`](gcp/) | **Complete**, and what `deploy.yml` applies. It includes Cloud Run (API plus a worker service), Cloud SQL, Pub/Sub, Secret Manager, IAP, private DNS and the synthetic check |
| [`aws/`](aws/) | **Skeleton**, validated in CI. ECS Fargate API and workers, internal ALB, RDS with IAM authentication, Secrets Manager, Bedrock, VPC endpoints |
| [`azure/`](azure/) | **Skeleton**, validated in CI. Container Apps API and workers, PostgreSQL Flexible Server with Entra ID only, Key Vault, Azure OpenAI, private endpoints |

A skeleton has the right resources, identities, network posture and settings,
and passes `terraform validate`. It has not been applied. Before first use:
- add the backend values and an `envs/<env>.tfvars`;
- review the sizes;
- add the deploy job for that cloud: build and sign as today, then verify and apply.

The skeletons are kept valid on every pull request (the `terraform` job in
`ci.yml`), so they do not rot while they wait.

What every cloud shares:
- **No public endpoint.** The console is reached on the corporate network.
- **No stored credential.** Workloads use their platform identity: an attached service account, a task role or a managed identity. The database login is a short-lived token. Secrets are created empty, and an operator sets their values.
- **The client's network is consumed, never created.** The interconnect, Direct Connect, ExpressRoute or VPN has its own owner and lifecycle.
- **Two services, one image build.** The API runs the `api` target with no workers. The run workers run the `worker` target and answer `/healthz`.
- **Read-only root filesystem.** Only `/tmp` is writable.
- **Postgres with point-in-time recovery**, and 35 days of backups. See [`docs/DR.md`](../docs/DR.md).

After the first apply on any cloud, a database administrator does two things once:
1. Create the application's database role: on RDS, `CREATE USER alm_app; GRANT rds_iam TO alm_app;`; on Azure, `pgaadauth_create_principal`.
2. Apply the grants: `python -m alm_core.store.admin grants --app-role <role>`.

If the services must not change the schema, set `ALM_AUTO_MIGRATE=false` and
run `python -m alm_core.store.migrate` as the owner on each release.
