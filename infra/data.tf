// Cloud SQL, Pub/Sub, Secret Manager, Artifact Registry.

// ------------------------------------------------------------------ Cloud SQL
resource "google_sql_database_instance" "main" {
  name             = "${local.prefix}-pg"
  database_version = "POSTGRES_16"
  region           = var.region

  // Production databases holding an audit trail should not vanish on a
  // `terraform destroy` typo.
  deletion_protection = var.environment == "prod"

  // The client's own key, where policy requires it (docs/DR.md). Set only at
  // creation; the Cloud SQL service agent needs encrypt/decrypt on the key.
  encryption_key_name = var.db_kms_key != "" ? var.db_kms_key : null

  settings {
    tier              = var.db_tier
    availability_type = var.environment == "prod" ? "REGIONAL" : "ZONAL"
    disk_type         = "PD_SSD"
    disk_size         = 50
    disk_autoresize   = true

    ip_configuration {
      // No public IP at all. Reached on the private IP over Direct VPC egress.
      ipv4_enabled                                  = false
      private_network                               = google_compute_network.vpc.id
      enable_private_path_for_google_cloud_services = true
      ssl_mode                                      = "ENCRYPTED_ONLY"
    }

    database_flags {
      // IAM database authentication: no stored password to rotate or leak.
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    backup_configuration {
      enabled                        = true
      start_time                     = "02:00"
      point_in_time_recovery_enabled = true
      // The audit table is a compliance record; a week is the floor, not a target.
      transaction_log_retention_days = 7

      backup_retention_settings {
        retained_backups = 35
      }
    }

    insights_config {
      query_insights_enabled = true
      record_client_address  = false // client addresses are not useful here and are PII-adjacent
    }

    maintenance_window {
      day  = 7 // Sunday
      hour = 3
    }
  }

  depends_on = [google_service_networking_connection.psa]
}

resource "google_sql_database" "alm" {
  name     = "alm"
  instance = google_sql_database_instance.main.name
}

// The Cloud Run service account authenticates as itself. The username Postgres
// sees is the service account address with the .gserviceaccount.com suffix
// removed - a detail that costs an hour if you do not know it.
resource "google_sql_user" "app" {
  name     = trimsuffix(google_service_account.run.email, ".gserviceaccount.com")
  instance = google_sql_database_instance.main.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

resource "google_sql_user" "worker" {
  name     = trimsuffix(google_service_account.worker.email, ".gserviceaccount.com")
  instance = google_sql_database_instance.main.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}

// -------------------------------------------------------------------- Pub/Sub
resource "google_pubsub_topic" "ad_jobs" {
  name                       = "alm-ad-provisioning"
  labels                     = local.labels
  message_retention_duration = "86400s" // 1 day

  depends_on = [google_project_service.required]
}

resource "google_pubsub_topic" "ad_jobs_dead_letter" {
  name                       = "alm-ad-provisioning-dead-letter"
  labels                     = local.labels
  message_retention_duration = "604800s" // 7 days - somebody has to look at these
}

resource "google_pubsub_subscription" "worker" {
  name  = "alm-ad-provisioning-worker"
  topic = google_pubsub_topic.ad_jobs.id

  // Driving a browser takes minutes; the worker extends the lease while a job
  // runs, and this is the ceiling it may extend to.
  ack_deadline_seconds       = 600
  message_retention_duration = "86400s"
  retain_acked_messages      = false
  enable_message_ordering    = false

  expiration_policy {
    ttl = "" // never expire - an idle queue is normal here
  }

  retry_policy {
    minimum_backoff = "30s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.ad_jobs_dead_letter.id
    max_delivery_attempts = 5
  }
}

// Pub/Sub's own service agent needs these to move a message to the dead-letter
// topic; without them messages are retried forever and the dead-letter policy
// silently does nothing.
data "google_project" "current" {}

locals {
  pubsub_agent = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_topic_iam_member" "dead_letter_publisher" {
  topic  = google_pubsub_topic.ad_jobs_dead_letter.name
  role   = "roles/pubsub.publisher"
  member = local.pubsub_agent
}

resource "google_pubsub_subscription_iam_member" "dead_letter_subscriber" {
  subscription = google_pubsub_subscription.worker.name
  role         = "roles/pubsub.subscriber"
  member       = local.pubsub_agent
}

// ------------------------------------------------------------ Secret Manager
resource "google_secret_manager_secret" "secrets" {
  for_each  = local.secret_ids
  secret_id = each.value
  labels    = local.labels

  replication {
    user_managed {
      replicas {
        location = var.region
      }
    }
  }

  depends_on = [google_project_service.required]
}

// Versions are added out of band - `gcloud secrets versions add` - so a secret
// value never passes through Terraform state.

// -------------------------------------------------------- Artifact Registry
resource "google_artifact_registry_repository" "images" {
  location      = var.region
  repository_id = "${local.prefix}-images"
  format        = "DOCKER"
  description   = "Orchestrator and API images."
  labels        = local.labels

  docker_config {
    immutable_tags = true // a tag cannot be moved under a running revision
  }

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 20
    }
  }

  depends_on = [google_project_service.required]
}
