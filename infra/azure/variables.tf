variable "subscription_id" {
  description = "The Azure subscription."
  type        = string
}

variable "location" {
  description = "Azure region, e.g. westeurope."
  type        = string
}

variable "environment" {
  description = "test or prod."
  type        = string
  validation {
    condition     = contains(["test", "prod"], var.environment)
    error_message = "environment must be test or prod."
  }
}

// ------------------------------------------------------------ the network
// The client's, consumed: none of it is created here.
variable "apps_subnet_id" {
  description = "Subnet for the Container Apps environment (/23 or larger), in the virtual network peered with the corporate network."
  type        = string
}

variable "database_subnet_id" {
  description = "Subnet delegated to Microsoft.DBforPostgreSQL/flexibleServers."
  type        = string
}

variable "endpoints_subnet_id" {
  description = "Subnet for private endpoints (Key Vault)."
  type        = string
}

variable "postgres_private_dns_zone_id" {
  description = "privatelink.postgres.database.azure.com zone linked to the virtual network."
  type        = string
}

variable "api_hostname" {
  description = "The console's internal hostname, e.g. alm-test.corp.example."
  type        = string
}

// ------------------------------------------------------------ images
variable "registry_server" {
  description = "Container registry the images are pulled from (by the apps' managed identity), e.g. almregistry.azurecr.io."
  type        = string
}

variable "api_image" {
  description = "The Dockerfile's api target, by digest, signed by the deploy workflow."
  type        = string
}

variable "worker_image" {
  description = "The Dockerfile's worker target, by digest, signed by the deploy workflow."
  type        = string
}

variable "api_min_replicas" {
  description = "API replicas always up."
  type        = number
  default     = 1
}

variable "api_max_replicas" {
  description = "Upper bound on API replicas."
  type        = number
  default     = 3
}

variable "worker_min_replicas" {
  description = "Run workers always up. At least 1: something must claim jobs and run the scheduler."
  type        = number
  default     = 1
  validation {
    condition     = var.worker_min_replicas >= 1
    error_message = "worker_min_replicas must be at least 1."
  }
}

variable "worker_max_replicas" {
  description = "Upper bound on run workers."
  type        = number
  default     = 3
}

variable "worker_concurrency" {
  description = "Runs each worker replica drives at once."
  type        = number
  default     = 2
}

// ------------------------------------------------------------ application
variable "shadow_mode" {
  description = "true: plan only, never write. Turning it off is a change record, not a deploy."
  type        = bool
  default     = true
}

variable "orchestration" {
  description = "guided, agentic or deterministic."
  type        = string
  default     = "guided"
}

variable "azure_openai_resource_id" {
  description = "The Azure OpenAI resource the apps may call (the client's, in its tenancy)."
  type        = string
}

variable "azure_openai_endpoint" {
  description = "https://<resource>.openai.azure.com/"
  type        = string
}

variable "agent_deployment" {
  description = "Azure OpenAI deployment name for the agents."
  type        = string
}

variable "supervisor_deployment" {
  description = "Deployment for routing; empty uses the agent deployment."
  type        = string
  default     = ""
}

variable "ewm_server" {
  description = "https://<ewm host>/ccm - from the deploy environment, never committed."
  type        = string
}

variable "jts_server" {
  description = "https://<jts host>/jts - from the deploy environment, never committed."
  type        = string
}

variable "service_account_cid" {
  description = "The functional account's CID; its password is the alm-service-account-password secret."
  type        = string
}

variable "oidc_client_id" {
  description = "The console's Entra ID app registration (client id)."
  type        = string
}

variable "role_map" {
  description = "ALM_ROLE_MAP: JSON of Entra group id (or e-mail) to role."
  type        = string
  default     = "{}"
}

variable "retention_days" {
  description = "ALM_RETENTION_DAYS."
  type        = number
  default     = 30
}

variable "otel_endpoint" {
  description = "OTLP/HTTP endpoint (a collector forwarding to Azure Monitor); empty turns telemetry off."
  type        = string
  default     = ""
}

// ------------------------------------------------------------ data
variable "db_sku" {
  description = "Flexible Server SKU."
  type        = string
  default     = "GP_Standard_D2ds_v5"
}

variable "db_admin_group_object_id" {
  description = "Entra group that administers the database (runs migrations and the grants)."
  type        = string
}

variable "db_admin_group_name" {
  description = "That group's display name."
  type        = string
}

variable "tags" {
  description = "Extra tags on every resource."
  type        = map(string)
  default     = {}
}
