// ALM Access Provisioning - Google Cloud infrastructure.
//
// Everything is private. Cloud Run takes internal ingress only, Cloud SQL has no
// public IP, and there is no downloaded service account key anywhere: the
// runtime uses an attached service account, CI uses Workload Identity
// Federation, and the on-premises Windows worker federates its existing
// identity.
//
// Connectivity to the corporate estate (EWM, JTS, LDAP) rides an existing Cloud
// Interconnect or HA VPN. This configuration *consumes* that circuit by naming
// the Cloud Router; it never creates or destroys one. The circuit is a network
// team asset with its own lifecycle and must not be reachable by an application
// deploy.
//
//   terraform init
//   terraform apply -var-file=envs/test.tfvars

terraform {
  required_version = ">= 1.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.12"
    }
    google-beta = {
      source  = "hashicorp/google-beta"
      version = "~> 6.12"
    }
  }

  // Remote state in GCS: the ledger and the approval records are real, so a lost
  // state file means a rebuild that orphans them.
  backend "gcs" {
    // bucket and prefix supplied by `terraform init -backend-config=...`
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

provider "google-beta" {
  project = var.project_id
  region  = var.region
}

locals {
  prefix = "alm-${var.environment}"
  labels = merge(var.labels, { environment = var.environment })

  // Secret ids the application resolves by name. Keep in step with
  // alm_core/config.py - the names are the contract between the two.
  secret_ids = merge({
    password = "alm-service-account-password" # pragma: allowlist secret
    session  = "alm-session-signing-key"
    webhook  = "alm-webhook-hmac-key"
  }, { for k, v in { gemini = "alm-gemini-api-key" } : k => v if local.gemini_api })

  // The Gemini Developer API is keyed; Vertex AI is not. Everything the key
  // needs - its secret, its API, its network path - exists only in this mode.
  gemini_api = var.llm_provider == "gemini_api"
}

// APIs are enabled explicitly rather than assumed. A first apply into a fresh
// project otherwise fails halfway through with an opaque permission error.
resource "google_project_service" "required" {
  for_each = toset(concat([
    "run.googleapis.com",
    "sqladmin.googleapis.com",
    "pubsub.googleapis.com",
    "secretmanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "aiplatform.googleapis.com",
    "compute.googleapis.com",
    "servicenetworking.googleapis.com",
    "dns.googleapis.com",
    "iap.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "cloudtrace.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "certificatemanager.googleapis.com",
    ], compact([
      local.gemini_api ? "generativelanguage.googleapis.com" : "",
      var.synthetic_work_item != "" ? "cloudscheduler.googleapis.com" : "",
  ])))

  project = var.project_id
  service = each.key

  // Never disable an API on destroy - other workloads in the project may
  // depend on it.
  disable_on_destroy = false
}
