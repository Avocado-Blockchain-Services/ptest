# Opt-in Spot queue infrastructure. This file is declarative only: operators
# build the images, review a plan, and apply manually after validating IAM.

resource "google_project_service" "spot_required" {
  for_each           = toset(["pubsub.googleapis.com", "compute.googleapis.com", "cloudscheduler.googleapis.com", "monitoring.googleapis.com"])
  service            = each.key
  disable_on_destroy = false
}

resource "google_pubsub_topic" "spot_requests" {
  name       = "ptest-spot-requests"
  depends_on = [google_project_service.spot_required]
}

resource "google_pubsub_subscription" "spot_workers" {
  name                         = "ptest-spot-workers"
  topic                        = google_pubsub_topic.spot_requests.id
  ack_deadline_seconds         = 60
  message_retention_duration   = "86400s"
  enable_exactly_once_delivery = false
}

resource "google_service_account" "spot_worker" {
  account_id   = "ptest-spot-worker"
  display_name = "ptest Spot queue worker"
}

resource "google_service_account" "spot_controller" {
  account_id   = "ptest-spot-controller"
  display_name = "ptest Spot queue controller"
}

resource "google_storage_bucket_iam_member" "spot_worker_storage" {
  bucket = google_storage_bucket.src.name
  role   = "roles/storage.objectUser" # source + generation-safe queue records in this bucket only
  member = "serviceAccount:${google_service_account.spot_worker.email}"
}

resource "google_pubsub_subscription_iam_member" "spot_worker_subscriber" {
  subscription = google_pubsub_subscription.spot_workers.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:${google_service_account.spot_worker.email}"
}

resource "google_pubsub_subscription_iam_member" "spot_operator_smoke_subscriber" {
  for_each     = toset(var.operator_members)
  subscription = google_pubsub_subscription.spot_workers.name
  role         = "roles/pubsub.subscriber"
  member       = each.key
}

resource "google_pubsub_topic_iam_member" "spot_operator_publisher" {
  for_each = toset(var.operator_members)
  topic    = google_pubsub_topic.spot_requests.name
  role     = "roles/pubsub.publisher"
  member   = each.key
}

resource "google_artifact_registry_repository_iam_member" "spot_worker_reader" {
  location   = var.region
  repository = var.ar_repo
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.spot_worker.email}"
}

resource "google_project_iam_custom_role" "spot_controller_mig" {
  project = var.project_id
  role_id = "ptestSpotMigController"
  title   = "ptest Spot MIG controller"
  permissions = [
    "compute.regionInstanceGroupManagers.get",
    "compute.regionInstanceGroupManagers.update",
    "compute.regionOperations.get",
    "compute.instances.get",
    "compute.instances.list",
  ]
}

resource "google_project_iam_member" "spot_controller_compute" {
  project = var.project_id
  role    = google_project_iam_custom_role.spot_controller_mig.name
  member  = "serviceAccount:${google_service_account.spot_controller.email}"
}

resource "google_project_iam_custom_role" "spot_smoke_operator" {
  project = var.project_id
  role_id = "ptestSpotSmokeOperator"
  title   = "ptest Spot smoke operator"
  permissions = [
    "compute.instances.get",
    "compute.instances.list",
    "compute.regionInstanceGroupManagers.get",
  ]
}

resource "google_project_iam_member" "spot_smoke_compute" {
  for_each = toset(var.operator_members)
  project  = var.project_id
  role     = google_project_iam_custom_role.spot_smoke_operator.name
  member   = each.key
}

resource "google_project_iam_member" "spot_smoke_os_admin" {
  for_each = toset(var.operator_members)
  project  = var.project_id
  role     = "roles/compute.osAdminLogin"
  member   = each.key
}

resource "google_project_iam_member" "spot_controller_monitoring_viewer" {
  project = var.project_id
  role    = "roles/monitoring.viewer"
  member  = "serviceAccount:${google_service_account.spot_controller.email}"
}

resource "google_storage_bucket_iam_member" "spot_controller_leases" {
  bucket = google_storage_bucket.src.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.spot_controller.email}"
}

resource "google_compute_health_check" "spot_worker" {
  name = "ptest-spot-worker"
  tcp_health_check { port = 8080 }
}

resource "google_compute_firewall" "spot_worker_health" {
  name          = "ptest-spot-worker-health"
  network       = "default"
  target_tags   = ["ptest-spot-worker"]
  source_ranges = ["35.191.0.0/16", "130.211.0.0/22"]
  allow {
    protocol = "tcp"
    ports    = ["8080"]
  }
}

resource "google_compute_instance_template" "spot_worker" {
  name_prefix  = "ptest-spot-worker-"
  machine_type = var.spot_machine_type
  tags         = ["ptest-spot-worker"]

  disk {
    source_image = "projects/debian-cloud/global/images/family/debian-12"
    boot         = true
    auto_delete  = true
  }
  network_interface {
    network = "default"
    access_config {}
  }
  service_account {
    email  = google_service_account.spot_worker.email
    scopes = ["cloud-platform"]
  }
  scheduling {
    preemptible        = true
    automatic_restart  = false
    provisioning_model = "SPOT"
  }

  metadata_startup_script = <<-EOT
    #!/bin/bash
    set -euo pipefail
    apt-get update && apt-get install -y docker.io google-cloud-cli-gke-gcloud-auth-plugin
    systemctl enable --now docker
    gcloud auth configure-docker ${var.region}-docker.pkg.dev --quiet
    docker run --detach --restart=always --name ptest-spot-worker --publish 8080:8080 \
      --security-opt=no-new-privileges:true --security-opt=seccomp=unconfined \
      --env SPOT_PROJECT=${var.project_id} --env SPOT_REGION=${var.region} \
      --env SPOT_TOPIC=${google_pubsub_topic.spot_requests.name} \
      --env SPOT_SUBSCRIPTION=${google_pubsub_subscription.spot_workers.name} \
      --env SPOT_BUCKET=${google_storage_bucket.src.name} \
      --env SPOT_LEASE_SECONDS=120 --env SPOT_TASK_TIMEOUT_SECONDS=1800 \
      ${var.spot_worker_image}
  EOT
}

resource "google_compute_region_instance_group_manager" "spot_workers" {
  name               = "ptest-spot-workers"
  region             = var.region
  base_instance_name = "ptest-spot"
  target_size        = 0

  lifecycle { ignore_changes = [target_size] }

  version {
    instance_template = google_compute_instance_template.spot_worker.id
  }
  auto_healing_policies {
    health_check      = google_compute_health_check.spot_worker.id
    initial_delay_sec = 300
  }
}

resource "google_cloud_run_v2_service" "spot_controller" {
  name     = "ptest-spot-controller"
  location = var.region
  template {
    service_account = google_service_account.spot_controller.email
    containers {
      image = var.spot_controller_image
      env {
        name  = "SPOT_MAX_WORKERS"
        value = tostring(var.spot_max_workers)
      }
      env {
        name  = "SPOT_IDLE_SECONDS"
        value = tostring(var.spot_idle_seconds)
      }
      env {
        name  = "SPOT_PROJECT"
        value = var.project_id
      }
      env {
        name  = "SPOT_REGION"
        value = var.region
      }
      env {
        name  = "SPOT_MIG"
        value = google_compute_region_instance_group_manager.spot_workers.name
      }
      env {
        name  = "SPOT_SUBSCRIPTION"
        value = google_pubsub_subscription.spot_workers.name
      }
      env {
        name  = "SPOT_BUCKET"
        value = google_storage_bucket.src.name
      }
      env {
        name  = "SPOT_OVERFLOW_TO_CLOUDRUN"
        value = tostring(var.spot_overflow_to_cloudrun)
      }
    }
  }
}

resource "google_cloud_run_v2_service_iam_member" "spot_controller_invoker" {
  name     = google_cloud_run_v2_service.spot_controller.name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.spot_controller.email}"
}

resource "google_cloud_run_v2_service_iam_member" "spot_controller_operator_invoker" {
  for_each = toset(var.operator_members)
  name     = google_cloud_run_v2_service.spot_controller.name
  location = var.region
  role     = "roles/run.invoker"
  member   = each.key
}

resource "google_cloud_scheduler_job" "spot_controller" {
  name             = "ptest-spot-controller"
  schedule         = "* * * * *"
  time_zone        = "Etc/UTC"
  attempt_deadline = "60s"
  http_target {
    http_method = "POST"
    uri         = google_cloud_run_v2_service.spot_controller.uri
    oidc_token {
      service_account_email = google_service_account.spot_controller.email
    }
  }
}
