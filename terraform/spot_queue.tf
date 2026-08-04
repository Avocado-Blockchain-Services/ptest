# Opt-in Spot queue infrastructure. This file is declarative only: operators
# build the images, review a plan, and apply manually after validating IAM.

resource "google_project_service" "spot_required" {
  for_each           = toset(["pubsub.googleapis.com", "compute.googleapis.com", "cloudscheduler.googleapis.com"])
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
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.spot_worker.email}"
}

resource "google_project_iam_member" "spot_worker_subscriber" {
  project = var.project_id
  role    = "roles/pubsub.subscriber"
  member  = "serviceAccount:${google_service_account.spot_worker.email}"
}

resource "google_project_iam_member" "spot_controller_compute" {
  project = var.project_id
  role    = "roles/compute.instanceAdmin.v1"
  member  = "serviceAccount:${google_service_account.spot_controller.email}"
}

resource "google_compute_health_check" "spot_worker" {
  name = "ptest-spot-worker"
  tcp_health_check { port = 8080 }
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
    docker run --rm ${var.spot_worker_image}
  EOT
}

resource "google_compute_region_instance_group_manager" "spot_workers" {
  name               = "ptest-spot-workers"
  region             = var.region
  base_instance_name = "ptest-spot"
  target_size        = 0

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
        name  = "SPOT_OVERFLOW_TO_CLOUDRUN"
        value = tostring(var.spot_overflow_to_cloudrun)
      }
    }
  }
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
