// Cloud Run services, and the internal load balancer with IAP in front of the API.
//
// Two shapes, chosen by var.worker_image:
//
//   empty (default)  one service, the all-in-one image: the API process also
//                    runs the run workers (ALM_WORKER_CONCURRENCY).
//   set              the API (the api image, no browser, no workers) plus a
//                    worker service (the worker image) that scales on its own,
//                    from var.worker_min_instances to var.worker_max_instances.
//
// Whichever runs the workers needs two settings:
//
//   min_instance_count >= 1  something must be up to claim jobs and schedule
//                            the sweep and the retention purge.
//   cpu_idle = false         Cloud Run throttles CPU between requests by
//                            default, which would freeze the workers, the
//                            scheduler and any run waiting on a permission poll.
//
// Instances can scale out: run state lives in Postgres, a thread is never
// handed to two workers, and a dead instance's runs are taken over by another.

locals {
  split_workers = var.worker_image != ""

  // Every setting the API and the workers share.
  run_env = {
    ALM_ENVIRONMENT      = upper(var.environment)
    ALM_SHADOW_MODE      = tostring(var.shadow_mode)
    ALM_ORCHESTRATION    = var.orchestration
    ALM_REGION           = var.region
    ALM_LLM_PROVIDER     = var.llm_provider
    ALM_AGENT_MODEL      = var.agent_model
    ALM_SUPERVISOR_MODEL = var.supervisor_model
    EWM_SERVER           = var.ewm_server
    JTS_SERVER           = var.jts_server
    ALM_IAP_AUDIENCE     = var.iap_audience
    CID                  = var.service_account_cid
    ALM_CA_BUNDLE        = "/etc/ssl/certs/corporate-ca.pem"
    ALM_AD_JOB_TRANSPORT = "pubsub"
    ALM_PUBSUB_TOPIC     = google_pubsub_topic.ad_jobs.name
    ALM_SECRET_BACKEND   = "gcp" // pragma: allowlist secret - names the secret store, holds no secret
    ALM_AUTH_MODE        = "iap"
    ALM_ROLE_MAP         = var.role_map
    ALM_RETENTION_DAYS   = tostring(var.retention_days)
    // Spans and metrics over OTLP/HTTP, to a collector that forwards them to
    // Cloud Trace and Managed Prometheus. Off when no endpoint is set.
    ALM_OTEL_ENABLED            = tostring(var.otel_endpoint != "")
    OTEL_EXPORTER_OTLP_ENDPOINT = var.otel_endpoint
    ALM_DB_AUTH                 = "gcp_iam"
    ALM_APPROVAL_BASE_URL       = "https://${local.api_hostname}"
    // IAM database authentication: no password in the DSN. The application
    // appends a short-lived access token at connect time.
    ALM_POSTGRES_DSN = join("", [
      "postgresql://",
      trimsuffix(google_service_account.run.email, ".gserviceaccount.com"),
      "@", google_sql_database_instance.main.private_ip_address,
      ":5432/", google_sql_database.alm.name,
      "?sslmode=require",
    ])
  }
}

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
      max_instance_count = var.max_instances
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

    // Requests are short (they only queue work); runs happen in the workers.
    timeout = "300s"

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

      // The same settings as the worker service (local.run_env), so the two
      // cannot drift. This service runs workers only when there is no worker
      // service.
      dynamic "env" {
        for_each = merge(local.run_env, {
          ALM_WORKER_CONCURRENCY = local.split_workers ? "0" : tostring(var.worker_concurrency)
        })
        content {
          name  = env.key
          value = env.value
        }
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
        failure_threshold     = 12 // the all-in-one image's Chromium makes cold start slow
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

// ------------------------------------------------------------ run workers
// Only with var.worker_image. Nothing calls it: no ingress beyond the VPC and
// no invoker binding. It answers its own liveness probe on /healthz.
resource "google_cloud_run_v2_service" "worker" {
  count    = local.split_workers ? 1 : 0
  name     = "${local.prefix}-worker"
  location = var.region
  labels   = local.labels
  ingress  = "INGRESS_TRAFFIC_INTERNAL_ONLY"

  deletion_protection = var.environment == "prod"

  template {
    service_account = google_service_account.run.email

    scaling {
      min_instance_count = var.worker_min_instances
      max_instance_count = var.worker_max_instances
    }

    vpc_access {
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.run.id
      }
      egress = "ALL_TRAFFIC"
    }

    // A worker drains on SIGTERM: Cloud Run allows ten seconds, so a step in
    // progress may be cut short. Its run is taken over from the checkpoint and
    // the ledger turns any finished write into a replay.
    containers {
      image = var.worker_image

      resources {
        limits = {
          cpu    = "2"
          memory = "4Gi"
        }
        cpu_idle          = false // workers poll the queue between requests
        startup_cpu_boost = true
      }

      ports {
        container_port = 8080
      }

      dynamic "env" {
        for_each = merge(local.run_env, {
          ALM_WORKER_CONCURRENCY = tostring(var.worker_concurrency)
          ALM_WORKER_HEALTH_PORT = "8080"
        })
        content {
          name  = env.key
          value = env.value
        }
      }

      volume_mounts {
        name       = "secrets"
        mount_path = "/secrets"
      }

      startup_probe {
        http_get {
          path = "/healthz"
          port = 8080
        }
        initial_delay_seconds = 5
        period_seconds        = 10
        failure_threshold     = 12
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

// ------------------------------------------------------- synthetic check
// Only with var.synthetic_work_item (TEST). Every hour, queue a dry run of one
// known work item; the workers run it like any other. A dry run writes
// nothing, so it is safe against the real estate, and it proves sign-in to
// EWM and JTS, the model and the queue end to end. ops/alerts.md alerts when
// no run finishes for two hours. The deploy workflow also runs it once.
resource "google_cloud_run_v2_job" "synthetic" {
  count    = var.synthetic_work_item != "" ? 1 : 0
  name     = "${local.prefix}-synthetic"
  location = var.region
  labels   = local.labels

  deletion_protection = false

  template {
    task_count = 1
    template {
      service_account = google_service_account.run.email
      max_retries     = 0
      timeout         = "120s"

      vpc_access {
        network_interfaces {
          network    = google_compute_network.vpc.id
          subnetwork = google_compute_subnetwork.run.id
        }
        egress = "ALL_TRAFFIC"
      }

      containers {
        image   = local.split_workers ? var.worker_image : var.container_image
        command = ["python", "-m", "alm_agents.worker", "synthetic", var.synthetic_work_item]

        dynamic "env" {
          for_each = local.run_env
          content {
            name  = env.key
            value = env.value
          }
        }
      }
    }
  }

  depends_on = [google_project_iam_member.run]
}

resource "google_service_account" "scheduler" {
  count        = var.synthetic_work_item != "" ? 1 : 0
  account_id   = "${local.prefix}-scheduler"
  display_name = "ALM synthetic check trigger (Cloud Scheduler)"
}

resource "google_cloud_run_v2_job_iam_member" "scheduler" {
  count    = var.synthetic_work_item != "" ? 1 : 0
  name     = google_cloud_run_v2_job.synthetic[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.scheduler[0].email}"
}

resource "google_cloud_scheduler_job" "synthetic" {
  count     = var.synthetic_work_item != "" ? 1 : 0
  name      = "${local.prefix}-synthetic"
  region    = var.region
  schedule  = "17 * * * *"
  time_zone = "Etc/UTC"

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.synthetic[0].name}:run"
    oauth_token {
      service_account_email = google_service_account.scheduler[0].email
    }
  }

  depends_on = [google_project_service.required]
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
