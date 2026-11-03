terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
  # For a team, keep state in a bucket:
  # backend "gcs" { bucket = "<project>-tfstate" prefix = "ctx" }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

data "google_project" "this" {}

locals {
  run_url  = "https://${var.service_name}-${data.google_project.this.number}.${var.region}.run.app"
  base_url = var.public_base_url != "" ? var.public_base_url : local.run_url
  apis = [
    "run.googleapis.com", "sqladmin.googleapis.com", "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com", "cloudtasks.googleapis.com", "aiplatform.googleapis.com",
    "servicenetworking.googleapis.com", "compute.googleapis.com", "iam.googleapis.com",
    "iamcredentials.googleapis.com", "sts.googleapis.com", "cloudtrace.googleapis.com",
    "logging.googleapis.com", "storage.googleapis.com", "cloudbuild.googleapis.com",
  ]
  optional_secrets = {
    "google-client-secret" = var.google_oauth_client_secret
    "github-client-secret" = var.github_oauth_client_secret
    "anthropic-api-key"    = var.anthropic_api_key
  }
  # Only the names of the secrets that were provided; the values stay sensitive.
  present_secrets = nonsensitive(toset([for k, v in local.optional_secrets : k if v != ""]))
}

resource "google_project_service" "apis" {
  for_each           = toset(local.apis)
  service            = each.value
  disable_on_destroy = false
}

# ---------------------------------------------------------------- network (private Cloud SQL)

resource "google_compute_network" "vpc" {
  name                    = "${var.service_name}-vpc"
  auto_create_subnetworks = false
  depends_on              = [google_project_service.apis]
}

resource "google_compute_subnetwork" "run" {
  name                     = "${var.service_name}-run"
  network                  = google_compute_network.vpc.id
  region                   = var.region
  ip_cidr_range            = "10.8.0.0/24"
  private_ip_google_access = true
}

resource "google_compute_global_address" "sql_range" {
  name          = "${var.service_name}-sql-range"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
  network       = google_compute_network.vpc.id
}

resource "google_service_networking_connection" "sql" {
  network                 = google_compute_network.vpc.id
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.sql_range.name]
}

# ---------------------------------------------------------------- database

resource "random_password" "db" {
  length  = 32
  special = false
}

resource "google_sql_database_instance" "pg" {
  name                = "${var.service_name}-pg"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = var.db_deletion_protection
  depends_on          = [google_service_networking_connection.sql]

  settings {
    tier              = var.db_tier
    edition           = "ENTERPRISE"
    availability_type = "ZONAL"
    disk_autoresize   = true
    disk_size         = 10
    ip_configuration {
      ipv4_enabled    = false
      private_network = google_compute_network.vpc.id
      ssl_mode        = "ALLOW_UNENCRYPTED_AND_ENCRYPTED" # private network only
    }
    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }
    insights_config {
      query_insights_enabled = true
    }
  }
}

resource "google_sql_database" "ctx" {
  name     = "ctx"
  instance = google_sql_database_instance.pg.name
}

resource "google_sql_user" "ctx" {
  name     = "ctx"
  instance = google_sql_database_instance.pg.name
  password = random_password.db.result
}

# ---------------------------------------------------------------- secrets

resource "random_password" "session" {
  length  = 48
  special = false
}

resource "google_secret_manager_secret" "db_url" {
  secret_id = "${var.service_name}-database-url"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "db_url" {
  secret      = google_secret_manager_secret.db_url.id
  secret_data = "postgresql+asyncpg://ctx:${random_password.db.result}@${google_sql_database_instance.pg.private_ip_address}:5432/ctx"
}

resource "google_secret_manager_secret" "session" {
  secret_id = "${var.service_name}-session-secret"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "session" {
  secret      = google_secret_manager_secret.session.id
  secret_data = random_password.session.result
}

resource "google_secret_manager_secret" "optional" {
  for_each  = local.present_secrets
  secret_id = "${var.service_name}-${each.key}"
  replication {
    auto {}
  }
  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "optional" {
  for_each    = local.present_secrets
  secret      = google_secret_manager_secret.optional[each.key].id
  secret_data = local.optional_secrets[each.key]
}

# ---------------------------------------------------------------- storage and tasks

resource "google_storage_bucket" "uploads" {
  name                        = "${var.project_id}-${var.service_name}-uploads"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false
  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }
  depends_on = [google_project_service.apis]
}

resource "google_cloud_tasks_queue" "jobs" {
  name     = "${var.service_name}-jobs"
  location = var.region
  rate_limits {
    max_concurrent_dispatches = 5
    max_dispatches_per_second = 2
  }
  retry_config {
    max_attempts  = 3
    min_backoff   = "10s"
    max_backoff   = "300s"
    max_doublings = 3
  }
  depends_on = [google_project_service.apis]
}

resource "google_artifact_registry_repository" "images" {
  repository_id = var.service_name
  location      = var.region
  format        = "DOCKER"
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 10
    }
  }
  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      older_than = "2592000s"
    }
  }
  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------------- identities (least privilege)

resource "google_service_account" "run" {
  account_id   = "${var.service_name}-run"
  display_name = "ctx Cloud Run runtime"
}

resource "google_service_account" "tasks" {
  account_id   = "${var.service_name}-tasks"
  display_name = "ctx Cloud Tasks invoker"
}

resource "google_project_iam_member" "run_roles" {
  for_each = toset([
    "roles/aiplatform.user", "roles/cloudtrace.agent", "roles/logging.logWriter",
    "roles/cloudtasks.enqueuer",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.run.email}"
}

resource "google_storage_bucket_iam_member" "run_uploads" {
  bucket = google_storage_bucket.uploads.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.run.email}"
}

resource "google_secret_manager_secret_iam_member" "run_secrets" {
  for_each = merge(
    { db_url = google_secret_manager_secret.db_url.id, session = google_secret_manager_secret.session.id },
    { for k, s in google_secret_manager_secret.optional : k => s.id },
  )
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.run.email}"
}

# The runtime creates tasks that carry an OIDC token for the tasks identity.
resource "google_service_account_iam_member" "run_acts_as_tasks" {
  service_account_id = google_service_account.tasks.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.run.email}"
}

# ---------------------------------------------------------------- Cloud Run

resource "google_cloud_run_v2_service" "ctx" {
  name                = var.service_name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false
  depends_on          = [google_project_service.apis, google_secret_manager_secret_version.db_url]

  template {
    service_account                  = google_service_account.run.email
    timeout                          = "300s"
    max_instance_request_concurrency = 40
    scaling {
      min_instance_count = 0
      max_instance_count = var.max_instances
    }
    vpc_access {
      egress = "PRIVATE_RANGES_ONLY"
      network_interfaces {
        network    = google_compute_network.vpc.id
        subnetwork = google_compute_subnetwork.run.id
      }
    }
    containers {
      image = var.image
      ports {
        container_port = 8080
      }
      resources {
        limits            = { cpu = "1", memory = "1Gi" }
        cpu_idle          = true
        startup_cpu_boost = true
      }
      startup_probe {
        http_get {
          path = "/healthz"
        }
        initial_delay_seconds = 2
        period_seconds        = 3
        failure_threshold     = 40
      }

      dynamic "env" {
        for_each = {
          CTX_ENV                     = "prod"
          CTX_PUBLIC_BASE_URL         = local.base_url
          CTX_GCP_PROJECT             = var.project_id
          CTX_VERTEX_LOCATION         = var.vertex_location
          CTX_EMBEDDING_PROVIDER      = "vertex"
          CTX_EMBEDDING_MODEL         = var.embedding_model
          CTX_OTEL_ENABLED            = "true"
          CTX_GOOGLE_CLIENT_ID        = var.google_oauth_client_id
          CTX_GITHUB_CLIENT_ID        = var.github_oauth_client_id
          CTX_RUN_MIGRATIONS_ON_START = "true"
        }
        content {
          name  = env.key
          value = env.value
        }
      }
      env {
        name = "CTX_DATABASE_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.db_url.secret_id
            version = "latest"
          }
        }
      }
      env {
        name = "CTX_SESSION_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.session.secret_id
            version = "latest"
          }
        }
      }
      dynamic "env" {
        for_each = {
          for k, env_name in {
            "google-client-secret" = "CTX_GOOGLE_CLIENT_SECRET"
            "github-client-secret" = "CTX_GITHUB_CLIENT_SECRET"
            "anthropic-api-key"    = "CTX_ANTHROPIC_API_KEY"
          } : k => env_name if contains(local.present_secrets, k)
        }
        content {
          name = env.value
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.optional[env.key].secret_id
              version = "latest"
            }
          }
        }
      }
    }
  }

  lifecycle {
    # CI deploys new images; Cloud Run fills service-level scaling defaults on deploy.
    ignore_changes = [template[0].containers[0].image, client, client_version, scaling]
  }
}

# Public: the app authenticates every request itself (OAuth bearer tokens).
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.ctx.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "allUsers"
}

resource "google_cloud_run_v2_service_iam_member" "tasks_invoker" {
  name     = google_cloud_run_v2_service.ctx.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.tasks.email}"
}

# ---------------------------------------------------------------- GitHub Actions deploys (keyless)

resource "google_iam_workload_identity_pool" "github" {
  count                     = var.github_repo != "" ? 1 : 0
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"
  depends_on                = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count                              = var.github_repo != "" ? 1 : 0
  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  attribute_condition = "assertion.repository == '${var.github_repo}'"
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "deploy" {
  count        = var.github_repo != "" ? 1 : 0
  account_id   = "${var.service_name}-deploy"
  display_name = "ctx GitHub deploy"
}

resource "google_service_account_iam_member" "deploy_wif" {
  count              = var.github_repo != "" ? 1 : 0
  service_account_id = google_service_account.deploy[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repository/${var.github_repo}"
}

resource "google_project_iam_member" "deploy_roles" {
  for_each = var.github_repo != "" ? toset(["roles/run.developer", "roles/artifactregistry.writer", "roles/aiplatform.user"]) : toset([])
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.deploy[0].email}"
}

resource "google_service_account_iam_member" "deploy_acts_as_run" {
  count              = var.github_repo != "" ? 1 : 0
  service_account_id = google_service_account.run.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.deploy[0].email}"
}
