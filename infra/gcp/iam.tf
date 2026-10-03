// Service accounts and the narrowest roles that work.
//
// Three principals, deliberately separate:
//
//   run     the orchestrator + API on Cloud Run
//   worker  the on-premises Windows host, federated (no downloaded key)
//   deploy  GitHub Actions, federated (no downloaded key)
//
// Nothing here grants a project-level editor or owner role, and no
// `google_service_account_key` resource exists in this configuration at all -
// a long-lived key is the credential most likely to end up in a repository.

resource "google_service_account" "run" {
  account_id   = "${local.prefix}-run"
  display_name = "ALM provisioning orchestrator (Cloud Run)"
}

resource "google_service_account" "worker" {
  account_id   = "${local.prefix}-worker"
  display_name = "ALM AD worker (on-premises Windows)"
}

resource "google_service_account" "deploy" {
  account_id   = "${local.prefix}-deploy"
  display_name = "ALM CI/CD deployer"
}

// ------------------------------------------------------------------ runtime
locals {
  run_roles = [
    "roles/cloudsql.client",       // connect to the instance
    "roles/cloudsql.instanceUser", // authenticate as an IAM database user
    "roles/pubsub.publisher",      // publish AD jobs
    "roles/aiplatform.user",       // call Vertex AI models
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
    "roles/monitoring.metricWriter",
  ]
}

resource "google_project_iam_member" "run" {
  for_each = toset(local.run_roles)
  project  = var.project_id
  role     = each.key
  member   = "serviceAccount:${google_service_account.run.email}"
}

// Secret access is granted per secret, not project-wide: the orchestrator can
// read its own secrets (three, or four with the Gemini API key) and nothing else.
// The Gemini key needs no env var or mount: the application reads it from
// Secret Manager by name (alm_core.credentials.gemini_api_key).
resource "google_secret_manager_secret_iam_member" "run" {
  for_each  = google_secret_manager_secret.secrets
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.run.email}"
}

resource "google_artifact_registry_repository_iam_member" "run_pull" {
  location   = google_artifact_registry_repository.images.location
  repository = google_artifact_registry_repository.images.name
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.run.email}"
}

// ------------------------------------------------------------------- worker
locals {
  worker_roles = [
    "roles/cloudsql.client",
    "roles/cloudsql.instanceUser",
    "roles/logging.logWriter",
  ]
}

resource "google_project_iam_member" "worker" {
  for_each = toset(local.worker_roles)
  project  = var.project_id
  role     = each.key
  member   = "serviceAccount:${google_service_account.worker.email}"
}

// Subscriber on one subscription, not project-wide - the worker consumes AD
// jobs and has no business reading anything else.
resource "google_pubsub_subscription_iam_member" "worker" {
  subscription = google_pubsub_subscription.worker.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.worker.email}"
}

// ------------------------------------------- Workload Identity Federation
// One pool, two providers: the on-premises worker and GitHub Actions. Both
// exchange an identity they already have for a short-lived Google token.
resource "google_iam_workload_identity_pool" "main" {
  workload_identity_pool_id = "${local.prefix}-pool"
  display_name              = "ALM federated identities"
  description               = "On-premises worker and CI. No downloaded keys."
  depends_on                = [google_project_service.required]
}

resource "google_iam_workload_identity_pool_provider" "onprem" {
  count = var.onprem_wif_issuer == "" ? 0 : 1

  workload_identity_pool_id          = google_iam_workload_identity_pool.main.workload_identity_pool_id
  workload_identity_pool_provider_id = "onprem-worker"
  display_name                       = "On-premises Windows worker"

  attribute_mapping = {
    "google.subject" = "assertion.sub"
  }

  // Only the named host may impersonate the worker account. Without an
  // attribute condition the provider would trust every subject the issuer signs.
  attribute_condition = "assertion.sub == '${var.onprem_wif_subject}'"

  oidc {
    issuer_uri = var.onprem_wif_issuer
  }
}

resource "google_service_account_iam_member" "onprem_impersonation" {
  count = var.onprem_wif_issuer == "" ? 0 : 1

  service_account_id = google_service_account.worker.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/${google_iam_workload_identity_pool.main.name}/subject/${var.onprem_wif_subject}"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count = var.github_repository == "" ? 0 : 1

  workload_identity_pool_id          = google_iam_workload_identity_pool.main.workload_identity_pool_id
  workload_identity_pool_provider_id = "github-actions"
  display_name                       = "GitHub Actions"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }

  // Scoped to one repository. Omitting this would let any GitHub repository in
  // the world mint a token for this project - the well-known WIF misconfiguration.
  attribute_condition = "assertion.repository == '${var.github_repository}'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account_iam_member" "github_impersonation" {
  count = var.github_repository == "" ? 0 : 1

  service_account_id = google_service_account.deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.main.name}/attribute.repository/${var.github_repository}"
}

// ---------------------------------------------------------------- deployer
locals {
  deploy_roles = [
    "roles/run.admin",
    "roles/artifactregistry.writer",
    "roles/iam.serviceAccountUser", // to deploy a revision *as* the run account
  ]
}

resource "google_project_iam_member" "deploy" {
  for_each = toset(local.deploy_roles)
  project  = var.project_id
  role     = each.key
  member   = "serviceAccount:${google_service_account.deploy.email}"
}

// ---------------------------------------------------------------- approvers
// IAP decides who may reach the service at all. What they may do is then
// decided per request by their role (ALM_ROLE_MAP) - approving needs the
// approver role, and production needs two approvers - and the audit row
// records who they were.
resource "google_iap_web_backend_service_iam_member" "approvers" {
  count = var.approver_group == "" ? 0 : 1

  web_backend_service = google_compute_backend_service.api.name
  role                = "roles/iap.httpsResourceAccessor"
  member              = var.approver_group
}
