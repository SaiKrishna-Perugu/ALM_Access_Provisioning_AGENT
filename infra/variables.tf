variable "project_id" {
  description = "GCP project that owns every resource here."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Cloud SQL and Vertex AI. Keep them together - a cross-region database call on every ledger write is not worth the latency."
  type        = string
  default     = "europe-west1"
}

variable "environment" {
  description = "Which ALM estate this deployment talks to."
  type        = string
  default     = "test"

  validation {
    condition     = contains(["test", "prod"], var.environment)
    error_message = "environment must be test or prod."
  }
}

variable "subnet_cidr" {
  description = "Primary range for the Cloud Run subnet. Must not overlap the corporate estate - confirm with the network team before applying; a clashing range breaks routing for everyone on the interconnect, not just this app."
  type        = string
  default     = "10.60.0.0/23"
}

variable "psa_cidr" {
  description = "Range reserved for Private Service Access, which is where Cloud SQL's private IP is allocated from."
  type        = string
  default     = "10.60.4.0/24"
}

variable "corporate_dns_servers" {
  description = "On-premises DNS servers that resolve *.example.intra. Without these the container cannot find EWM or JTS, whatever the interconnect says."
  type        = list(string)
  default     = []
}

variable "corporate_dns_suffix" {
  description = "Domain forwarded to the corporate resolvers, with a trailing dot. Supplied at deploy time (GitHub environment variable CORPORATE_DNS_SUFFIX), never committed."
  type        = string

  validation {
    condition     = endswith(var.corporate_dns_suffix, ".") && !strcontains(var.corporate_dns_suffix, "example.intra")
    error_message = "corporate_dns_suffix must be the real intranet domain with a trailing dot, not the example.intra placeholder."
  }
}

variable "interconnect_router_name" {
  description = "Existing Cloud Router carrying the Interconnect or HA VPN to the corporate network. Left empty the spoke deploys unattached and the app cannot reach the intranet - deliberate, so an application deploy can never create or destroy a circuit."
  type        = string
  default     = ""
}

variable "container_image" {
  description = "Fully qualified image. Pin a digest in production - a tag can move under a running revision, a digest cannot."
  type        = string
}

variable "approver_group" {
  description = "Google group whose members may open the approval UI through IAP, e.g. group:alm-approvers@example.com."
  type        = string
  default     = ""
}

variable "shadow_mode" {
  description = "Read and plan, write nothing. Both tfvars files ship with this true; turning it off is the decision that lets an agent write, and belongs in a change record."
  type        = bool
  default     = true
}

variable "orchestration" {
  description = "agentic or deterministic."
  type        = string
  default     = "agentic"
}

variable "llm_provider" {
  description = "vertex (service account, no key - recommended) or gemini_api (Gemini Developer API key from Secret Manager)."
  type        = string
  default     = "vertex"

  validation {
    condition     = contains(["vertex", "gemini_api"], var.llm_provider)
    error_message = "llm_provider must be vertex or gemini_api."
  }
}

variable "agent_model" {
  description = "Gemini model the agents reason with. With llm_provider = vertex, a claude-* id from Model Garden switches the client automatically."
  type        = string
  default     = "gemini-3.5-flash"
}

variable "supervisor_model" {
  description = "Optional cheaper model for routing. Empty uses agent_model."
  type        = string
  default     = ""
}

variable "ewm_server" {
  description = "EWM base URL (https://<host>/ccm). Supplied at deploy time (GitHub environment variable EWM_SERVER), never committed."
  type        = string

  validation {
    condition     = startswith(var.ewm_server, "https://") && !strcontains(var.ewm_server, "example.intra")
    error_message = "ewm_server must be the real https:// EWM URL, not empty or the example.intra placeholder."
  }
}

variable "iap_audience" {
  description = "IAP JWT audience, /projects/<project number>/global/backendServices/<backend service id>. When set, the approval API trusts only a verified IAP JWT for the approver's identity. Set it once the load balancer exists."
  type        = string
  default     = ""
}

variable "jts_server" {
  description = "JTS base URL (https://<host>/jts). Supplied at deploy time (GitHub environment variable JTS_SERVER), never committed."
  type        = string

  validation {
    condition     = startswith(var.jts_server, "https://") && !strcontains(var.jts_server, "example.intra")
    error_message = "jts_server must be the real https:// JTS URL, not empty or the example.intra placeholder."
  }
}

variable "service_account_cid" {
  description = "Non-interactive ALM service account (CID) the toolkit authenticates as."
  type        = string
  default     = ""
}

variable "onprem_wif_issuer" {
  description = "OIDC issuer URI for the on-premises Windows worker's existing identity provider. Workload Identity Federation exchanges that for a Google token, so no service account key is downloaded to a host outside the perimeter."
  type        = string
  default     = ""
}

variable "onprem_wif_subject" {
  description = "Subject claim of the Windows worker host, mapped to the worker service account."
  type        = string
  default     = ""
}

variable "github_repository" {
  description = "owner/repo permitted to deploy via Workload Identity Federation. No service account key in CI."
  type        = string
  default     = ""
}

variable "otel_endpoint" {
  description = "OTLP/HTTP endpoint of an OpenTelemetry collector (a sidecar, or a shared collector on the VPC) that forwards to Cloud Trace and Managed Prometheus, e.g. http://localhost:4318. Empty turns telemetry off."
  type        = string
  default     = ""
}

variable "role_map" {
  description = "ALM_ROLE_MAP: JSON of IAP e-mail address (or '*') to role - viewer, operator, approver, auditor, admin."
  type        = string
  default     = "{}"
  validation {
    condition     = can(jsondecode(var.role_map))
    error_message = "role_map must be a JSON object."
  }
}

variable "max_instances" {
  description = "Most API+worker instances. Run state is in Postgres, so this may grow; each instance runs ALM_WORKER_CONCURRENCY runs at once."
  type        = number
  default     = 3
  validation {
    condition     = var.max_instances >= 1 && var.max_instances <= 20
    error_message = "max_instances must be between 1 and 20."
  }
}

variable "worker_concurrency" {
  description = "Runs each instance drives at once (ALM_WORKER_CONCURRENCY)."
  type        = number
  default     = 2
  validation {
    condition     = var.worker_concurrency >= 1 && var.worker_concurrency <= 8
    error_message = "worker_concurrency must be between 1 and 8."
  }
}

variable "db_tier" {
  description = "Cloud SQL machine type."
  type        = string
  default     = "db-custom-2-7680"
}

variable "labels" {
  type = map(string)
  default = {
    application = "alm-access-provisioning"
    managed-by  = "terraform"
  }
}
