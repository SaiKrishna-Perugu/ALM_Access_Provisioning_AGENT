variable "region" {
  description = "AWS region, e.g. eu-west-1."
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
variable "vpc_id" {
  description = "The VPC attached to the corporate network (Direct Connect or VPN)."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnets (two or more, in different zones) for tasks, the database, the ALB and endpoints."
  type        = list(string)
}

variable "private_subnet_cidrs" {
  description = "Those subnets' CIDR ranges."
  type        = list(string)
}

variable "private_route_table_ids" {
  description = "Route tables of the private subnets, for the S3 gateway endpoint."
  type        = list(string)
}

variable "corporate_cidrs" {
  description = "Corporate ranges: who may reach the console, and where EWM and JTS are."
  type        = list(string)
}

variable "certificate_arn" {
  description = "ACM certificate for the console's internal hostname."
  type        = string
}

variable "api_hostname" {
  description = "The console's internal hostname, e.g. alm-test.corp.example."
  type        = string
}

// ------------------------------------------------------------ images
variable "api_image" {
  description = "The Dockerfile's api target, by digest, signed by the deploy workflow."
  type        = string
}

variable "worker_image" {
  description = "The Dockerfile's worker target, by digest, signed by the deploy workflow."
  type        = string
}

variable "api_count" {
  description = "API tasks."
  type        = number
  default     = 2
}

variable "worker_count" {
  description = "Run-worker tasks. At least 1: something must claim jobs and run the scheduler."
  type        = number
  default     = 2
  validation {
    condition     = var.worker_count >= 1
    error_message = "worker_count must be at least 1."
  }
}

variable "worker_concurrency" {
  description = "Runs each worker task drives at once."
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

variable "agent_model" {
  description = "Bedrock model id or inference profile for the agents."
  type        = string
}

variable "supervisor_model" {
  description = "Bedrock model for routing; empty uses the agent model."
  type        = string
  default     = ""
}

variable "bedrock_model_arns" {
  description = "ARNs of the Bedrock models (or inference profiles) the tasks may invoke. Nothing else."
  type        = list(string)
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

variable "oidc_issuer" {
  description = "The client's OIDC issuer, e.g. https://login.microsoftonline.com/<tenant>/v2.0."
  type        = string
}

variable "oidc_client_id" {
  description = "The console's OIDC client id."
  type        = string
}

variable "role_map" {
  description = "ALM_ROLE_MAP: JSON of IdP group (or e-mail) to role."
  type        = string
  default     = "{}"
}

variable "retention_days" {
  description = "ALM_RETENTION_DAYS."
  type        = number
  default     = 30
}

variable "otel_endpoint" {
  description = "OTLP/HTTP endpoint (an ADOT collector); empty turns telemetry off."
  type        = string
  default     = ""
}

// ------------------------------------------------------------ data
variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.m6g.large"
}

variable "db_kms_key_arn" {
  description = "Customer-managed KMS key for the database; empty uses the AWS-managed key."
  type        = string
  default     = ""
}

variable "tags" {
  description = "Extra tags on every resource."
  type        = map(string)
  default     = {}
}
