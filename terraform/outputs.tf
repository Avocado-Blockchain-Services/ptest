output "bucket" {
  value       = google_storage_bucket.src.name
  description = "Set this as `bucket` in ~/.config/ptest/config.toml."
}

output "runner_service_account" {
  value = google_service_account.runner.email
}

output "pytest_job" {
  value       = google_cloud_run_v2_job.pytest.name
  description = "Set this as `job` on your pytest project stanza."
}

output "vitest_job" {
  value       = google_cloud_run_v2_job.vitest.name
  description = "Set this as `job` on your vitest project stanza."
}

output "config_stanza" {
  description = "Paste into ~/.config/ptest/config.toml."
  value       = <<-EOT
    [defaults]
    account     = "<your-account@example.com>"
    gcp_project = "${var.project_id}"
    bucket      = "${google_storage_bucket.src.name}"
    region      = "${var.region}"

    [projects.<name>]
    backend = "cloudrun"
    job     = "${google_cloud_run_v2_job.pytest.name}"
  EOT
}

output "spot_queue_topic" {
  value       = google_pubsub_topic.spot_requests.name
  description = "Set as spot_topic only after the worker/controller images are built and this static plan is manually applied."
}

output "spot_worker_mig" {
  value = google_compute_region_instance_group_manager.spot_workers.name
}
