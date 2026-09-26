# PRODUCTION.
# diffed against the manual outcome.

project_id  = ""            # required
region      = "europe-west1"
environment = "prod"

subnet_cidr = "10.60.8.0/23"
psa_cidr    = "10.60.12.0/24"

# Ask the network team. Without these the container cannot resolve EWM or JTS,
# whatever the interconnect says.
corporate_dns_servers    = []
interconnect_router_name = ""

# ewm_server, jts_server and corporate_dns_suffix are real intranet names:
# they come from this environment's GitHub variables at deploy time
# (EWM_SERVER, JTS_SERVER, CORPORATE_DNS_SUFFIX) and are never committed.
service_account_cid = ""

container_image = ""        # set by CI to an image digest

# Google group whose members may open the approval UI through IAP.
approver_group = ""

# Federation - no downloaded keys anywhere.
github_repository  = ""
onprem_wif_issuer  = ""
onprem_wif_subject = ""

orchestration    = "agentic"
# vertex: the service account calls Gemini on Vertex AI - no API key exists.
# gemini_api: a Gemini Developer API key, read from Secret Manager.
llm_provider     = "vertex"
agent_model      = "gemini-3.5-flash"
supervisor_model = "gemini-3.1-flash-lite"
shadow_mode      = true

db_tier = "db-custom-4-15360"

# Production stays in shadow mode until the pilot has run its full cycle on TEST
# and the results have been diffed against the manual process. Changing this is
# the decision that lets an agent write to production; it belongs in a change
# record, not in a deploy script.
