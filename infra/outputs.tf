output "api_url" {
  description = "Cloud Run URL. Internal ingress only - reachable from the VPC and over the interconnect, not the internet."
  value       = google_cloud_run_v2_service.api.uri
}

output "run_service_account" {
  description = "Grant this principal any additional access the toolkit needs."
  value       = google_service_account.run.email
}

output "worker_service_account" {
  description = "The on-premises Windows worker federates into this account."
  value       = google_service_account.worker.email
}

output "deploy_service_account" {
  description = "GitHub Actions impersonates this via Workload Identity Federation."
  value       = google_service_account.deploy.email
}

output "workload_identity_provider" {
  description = "Pass as `workload_identity_provider` in the google-github-actions/auth step."
  value = length(google_iam_workload_identity_pool_provider.github) > 0 ? (
    google_iam_workload_identity_pool_provider.github[0].name
  ) : ""
}

output "postgres_private_ip" {
  description = "Cloud SQL private IP. No public endpoint exists."
  value       = google_sql_database_instance.main.private_ip_address
}

output "pubsub_topic" {
  value = google_pubsub_topic.ad_jobs.name
}

output "artifact_registry" {
  value = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.images.repository_id}"
}

output "next_steps" {
  value = join(" ", [
    "1. Add secret versions: gcloud secrets versions add alm-service-account-password --data-file=-",
    "2. Confirm *.intra.chrysler.com resolves from the Cloud Run subnet.",
    "3. Run the connectivity smoke job before turning shadow mode off.",
  ])
}
