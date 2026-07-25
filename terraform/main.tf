locals {
  image_pytest = "${var.region}-docker.pkg.dev/${var.project_id}/${var.ar_repo}/ptest-runner:latest"
  image_node   = "${var.region}-docker.pkg.dev/${var.project_id}/${var.ar_repo}/ptest-runner-node:latest"
}

resource "google_project_service" "required" {
  for_each = toset([
    "run.googleapis.com",
    "cloudbuild.googleapis.com",
    "artifactregistry.googleapis.com",
    "storage.googleapis.com",
  ])

  service            = each.key
  disable_on_destroy = false
}

# ── Source drop ──────────────────────────────────────────────────────────────

resource "google_storage_bucket" "src" {
  name                        = var.bucket_name
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  # Every object here is a disposable snapshot of a repo. Nothing is worth
  # keeping, and `terraform destroy` should not need a manual empty-the-bucket
  # step.
  force_destroy = true

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = var.src_retention_days
    }
  }

  depends_on = [google_project_service.required]
}

# ── Runner identity ──────────────────────────────────────────────────────────
# Least privilege on purpose. This identity executes arbitrary test code from a
# developer's working tree, so it must never be able to reach production
# buckets, secrets, or databases. Read the source drop; write logs. Nothing else.

resource "google_service_account" "runner" {
  account_id   = "ptest-runner"
  display_name = "ptest remote test runner"
}

resource "google_storage_bucket_iam_member" "runner_read" {
  bucket = google_storage_bucket.src.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.runner.email}"
}

resource "google_project_iam_member" "runner_logs" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.runner.email}"
}

# ── Operators ────────────────────────────────────────────────────────────────

resource "google_storage_bucket_iam_member" "operator_upload" {
  for_each = toset(var.operator_members)

  bucket = google_storage_bucket.src.name
  role   = "roles/storage.objectAdmin"
  member = each.key
}

resource "google_project_iam_member" "operator_run" {
  for_each = toset(var.operator_members)

  project = var.project_id
  role    = "roles/run.developer"
  member  = each.key
}

# ── Jobs ─────────────────────────────────────────────────────────────────────
# PTEST_SRC / PTEST_CMD are placeholders. The dispatcher overrides both per
# execution via `gcloud run jobs execute --update-env-vars`, so Terraform owns
# the shape of the job and never the payload of a run.

resource "google_cloud_run_v2_job" "pytest" {
  name     = var.pytest_job_name
  location = var.region

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.runner.email

      # CRITICAL: Cloud Run Jobs default to THREE retries. Left at the default,
      # a single runaway submission becomes four billed executions of a suite
      # that was already failing.
      max_retries = 0
      timeout     = var.task_timeout

      containers {
        image = local.image_pytest

        resources {
          limits = {
            cpu    = var.cpu
            memory = var.memory
          }
        }

        env {
          name  = "PTEST_SRC"
          value = "unset"
        }
        env {
          name  = "PTEST_CMD"
          value = "unset"
        }
      }
    }
  }

  depends_on = [google_project_service.required]

  lifecycle {
    # The dispatcher rewrites env on every execution; without this, each
    # `terraform plan` shows spurious drift against the last run's values.
    ignore_changes = [template[0].template[0].containers[0].env]
  }
}

resource "google_cloud_run_v2_job" "vitest" {
  name     = var.vitest_job_name
  location = var.region

  template {
    task_count  = 1
    parallelism = 1

    template {
      service_account = google_service_account.runner.email
      max_retries     = 0
      timeout         = var.task_timeout

      containers {
        image = local.image_node

        resources {
          limits = {
            cpu    = var.cpu
            memory = var.memory
          }
        }

        env {
          name  = "PTEST_SRC"
          value = "unset"
        }
        env {
          name  = "PTEST_CMD"
          value = "unset"
        }
      }
    }
  }

  depends_on = [google_project_service.required]

  lifecycle {
    ignore_changes = [template[0].template[0].containers[0].env]
  }
}
