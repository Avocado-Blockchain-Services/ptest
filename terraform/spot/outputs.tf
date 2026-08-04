output "spot_topic" { value = google_pubsub_topic.requests.name }
output "spot_controller_url" { value = google_cloud_run_v2_service.controller.uri }
output "spot_worker_mig" { value = google_compute_region_instance_group_manager.workers.name }
