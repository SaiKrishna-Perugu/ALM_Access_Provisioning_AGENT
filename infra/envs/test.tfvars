# TEST. Shadow mode is on here too: turn it off only after a shadow run has been
# diffed against the manual outcome.

project_id  = ""            # required
region      = "europe-west1"
environment = "test"

subnet_cidr = "10.60.0.0/23"
psa_cidr    = "10.60.4.0/24"

# Ask the network team. Without these the container cannot resolve EWM or JTS,
# whatever the interconnect says.
corporate_dns_servers    = []
interconnect_router_name = ""

ewm_server          = "https://prssetst.intra.chrysler.com/ccm"
jts_server          = "https://prssetst.intra.chrysler.com/jts"
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
