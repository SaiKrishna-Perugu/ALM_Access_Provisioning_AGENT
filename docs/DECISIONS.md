# Decision record

The decisions that shape the enterprise deployment ([ENTERPRISE_PLAN.md](ENTERPRISE_PLAN.md),
section 2). Each row says what was decided, by whom and when, and what the code
does about it today.

Status:
- **Decided**: settled; the code follows it.
- **Built either way**: the code supports every option, and a setting picks one.
- **Proposed**: a recommendation the client has not yet confirmed.
- **Open**: needs the client.

| ID | Decision | Status | Decided | What the code does |
|---|---|---|---|---|
| D1 | Target cloud | Built either way | 2026-10-02: undecided; keep the core neutral | One image and one core for every cloud. `infra/gcp` is complete; `infra/aws` and `infra/azure` are validated skeletons. Secrets, database login, models and AD transport are chosen by settings ([infra/README.md](../infra/README.md)) |
| D2 | Model provider and data policy | Proposed | - | Vertex AI, Bedrock, Azure OpenAI and the Gemini API are all supported. `ALM_ALLOWED_PROVIDERS` pins the client's choice, and `ALM_MODEL_WITHHELD_FIELDS` keeps fields from every model. Open: the provider, the region, written zero-retention terms, and which work-item fields may reach a model ([THREAT_MODEL.md](THREAT_MODEL.md), data classification) |
| D3 | AD group membership | Built either way | 2026-10-02: build both | `ALM_AD_DIRECTORY=graph` (Microsoft Graph, Entra-mastered groups), or `gpt` (the GPT web UI through the isolated Windows worker, for groups mastered on-premises). Open: which groups the client's AD masters where |
| D4 | Identity | Decided | 2026-10-02: generic OIDC | `ALM_AUTH_MODE=oidc` (any provider: issuer, client id and groups claim), or `iap` on GCP. Roles come from `ALM_ROLE_MAP`. Open: the client's issuer, group names and app registration |
| D5 | Approval model | Proposed, built | - | Two distinct approvers for PROD and for high-risk users, one for TEST; the starter can never approve a two-approver run; only users every approver ticked are written (`ALM_APPROVERS_REQUIRED*`). The client confirms or changes the numbers |
| D6 | Tenancy | Proposed | - | Single-tenant: one deployment in the client's account. Nothing in the code is multi-tenant |
| D7 | CLI future | Open | - | Both run today and share nothing but EWM/JTS. Recommendation: retire the CLI once the PROD shadow report shows about 20 work items planned exactly as the CLI did (see below) |
| D8 | Change management | Open | - | Nothing links to an ITSM today. If the client requires a change per writing run, the hook is the approval node: open or link the change when the card is raised |

## D7 in practice: retiring the CLI

The CLI scripts (`src/*.py`) and the agents both write to EWM and JTS, but
only the agents use the idempotency ledger. A work item the CLI handled first
is safe for the agents: they read the live state, find the accounts active and
the CLI's comment on the work item, and post no second one. The reverse is
not safe. The CLI does not look for the agents' comment, so running it after
the agents can post a second comment. Until the CLI is retired, let one or the
other handle a given work item. The steps:

1. **Shadow.** In PROD, run the agents as dry runs (`ALM_SHADOW_MODE=true`) beside the CLI for one to two weeks. Each week, run `python -m alm_agents.shadow_report --csv shadow.csv` and fill its two columns from what the CLI did.
2. **Stage the writes.**
   - Begin with `ALM_ALLOWED_OPERATIONS=jts_unarchive,workitem_comment,workitem_attach`: reactivations and their evidence and comments. Nothing new is created and AD is not touched.
   - Then add `jts_create`.
   - Then add `ad_group_add`.

   Widen only after a clean week at each stage.
3. **Retire.** Stop scheduling the CLI. Keep `src/alm_config.py` and the scripts in the repository for a quarter, then move them to `legacy/` with a changelog entry.

A kill switch stays available throughout. `ALM_WRITES_DISABLED_OPERATIONS=ad_group_add`
(for example) turns one operation off immediately. Runs plan it, show it and skip it.

## How to change a decision

Edit this table in a pull request that also changes the code or settings it
names, and record the date and who decided.
