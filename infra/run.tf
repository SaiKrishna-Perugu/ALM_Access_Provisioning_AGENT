// Cloud Run service, and the internal load balancer with IAP in front of it.
//
// Two settings here are load-bearing and easy to get wrong:
//
//   min_instance_count = 1  the reconciliation sweep is an in-process timer.
//                           Scaled to zero, the queue stops being swept.
//   cpu_idle = false        Cloud Run throttles CPU between requests by default,
//                           which would freeze that timer and any run waiting on
//                           a 30-minute permission poll. "CPU always allocated"
//                           is what makes background work possible at all.

resource "google_cloud_run_v2_service" "api" {
  name     = "${local.prefix}-api"
  location = var.region
  labels   = local.labels

  // Internal only. The approval UI is published through the internal load
  // balancer with IAP; nothing here is reachable from the internet.
  ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER"

  deletion_protection = var.environment == "prod"

  template {
    service_account = google_service_account.run.email

    scaling {
      min_instance_count = 1
      // One instance, deliberately. Runs are keyed by work item and guarded by
      // the ledger, but a single writer keeps the failure modes obvious while
      // the pilot runs. Raise it once concurrency is measured.
      max_instance_count = 1
    }

    // Direct VPC egress: no Serverless VPC Access connector to size, pay for or
    // run out of capacity on.
    vpc_access {
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.run.id
      }
      egress = "ALL_TRAFFIC"
    }

    // Long enough for a run that waits on a permission poll to finish inside
    // one request when triggered synchronously; background tasks outlive it.
    timeout = "3600s"

    containers {
      image = var.container_image

      resources {
        limits = {
          cpu    = "2"
          memory = "4Gi"
        }
        cpu_idle          = false // CPU always allocated - see the header comment
        startup_cpu_boost = true
      }

      ports {
        container_port = 8080
      }

      env {
        name  = "ALM_ENVIRONMENT"
        value = upper(var.environment)
      }
      env {
        name  = "ALM_SHADOW_MODE"
        value = tostring(var.shadow_mode)
      }
      env {
        name  = "ALM_ORCHESTRATION"
        value = var.orchestration
      }
      env {
        name  = "ALM_REGION"
        value = var.region
      }
      env {
        name  = "ALM_AGENT_MODEL"
        value = var.agent_model
      }
      env {
        name  = "ALM_SUPERVISOR_MODEL"
        value = var.supervisor_model
      }
      env {
        name  = "EWM_SERVER"
        value = var.ewm_server
      }
      env {
        name  = "JTS_SERVER"
        value = var.jts_server
      }
      env {
        name  = "CID"
        value = var.service_account_cid
      }
      env {
        name  = "ALM_CA_BUNDLE"
        value = "/etc/ssl/certs/corporate-ca.pem"
      }
      env {
        name  = "ALM_PUBSUB_TOPIC"
        value = google_pubsub_topic.ad_jobs.name
      }
      env {
        name  = "ALM_APPROVAL_BASE_URL"
        value = "https://${local.api_hostname}"
      }
      env {
        // IAM database authentication: no password in the DSN. The application
        // appends a short-lived access token at connect time.
        name  = "ALM_POSTGRES_DSN"
        value = join("", [
          "postgresql://",
          trimsuffix(google_service_account.run.email, ".gserviceaccount.com"),
          "@", google_sql_database_instance.main.private_ip_address,
          ":5432/", google_sql_database.alm.name,
          "?sslmode=require",
        ])
      }

      // The service account password is mounted as a file rather than an
      // environment variable: a process listing exposes an environment.
      volume_mounts {
        name       = "secrets"
        mount_path = "/secrets"
      }

      startup_probe {
        http_get {
          path = "/readyz"
          port = 8080
        }
        initial_delay_seconds = 10
        period_seconds        = 10
        failure_threshold     = 12 // Playwright's chromium makes cold start slow
      }

      liveness_probe {
        http_get {
          path = "/healthz"
          port = 8080
        }
        period_seconds = 30
      }
    }

    volumes {
      name = "secrets"
      secret {
        secret = google_secret_manager_secret.secrets["password"].secret_id
        items {
          version = "latest"
          path    = local.secret_ids.password
          mode    = 0400
        }
      }
    }
  }

  depends_on = [
    google_project_iam_member.run,
    google_secret_manager_secret_iam_member.run,
  ]
}

// ------------------------------------------------- internal HTTPS + IAP
locals {
  api_hostname = "alm-${var.environment}.internal.${var.project_id}.example"
}

resource "google_compute_region_network_endpoint_group" "api" {
  name                  = "${local.prefix}-neg"
  region                = var.region
  network_endpoint_type = "SERVERLESS"

  cloud_run {
    service = google_cloud_run_v2_service.api.name
  }
}

resource "google_compute_backend_service" "api" {
  name                  = "${local.prefix}-backend"
  load_balancing_scheme = "INTERNAL_MANAGED"
  protocol              = "HTTPS"
  port_name             = "http"
  timeout_sec           = 3600

  backend {
    group = google_compute_region_network_endpoint_group.api.id
  }

  // Identity-Aware Proxy authenticates the human before the request reaches
  // the service. It is what makes the approver identity in the audit row
  // trustworthy - the application does not verify a JWT itself.
  iap {
    enabled = true
  }

  log_config {
    enable      = true
    sample_rate = 1.0
  }
}
