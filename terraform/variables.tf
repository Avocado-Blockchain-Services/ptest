variable "project_id" {
  type        = string
  description = "GCP project that hosts the test runner. Prefer a dedicated project so a runaway test job cannot touch production resources."
}

variable "region" {
  type    = string
  default = "us-central1"
}

variable "bucket_name" {
  type        = string
  description = "Source-drop bucket for packed repo tarballs. Must be globally unique."
}

variable "ar_repo" {
  type        = string
  description = "Existing Artifact Registry Docker repository holding the runner images. Terraform does not build images — build them before apply."
}

variable "src_retention_days" {
  type        = number
  default     = 3
  description = "Tarballs are worthless once a run finishes. Without expiry they accumulate forever and storage becomes the most expensive part of the setup."
}

variable "task_timeout" {
  type        = string
  default     = "1800s"
  description = "Hard server-side kill. Aim for ~10x observed runtime; too tight and it kills legitimate runs under contention."
}

variable "cpu" {
  type    = string
  default = "8"
}

variable "memory" {
  type        = string
  default     = "16Gi"
  description = "Must cover the RAM-backed container filesystem (which holds PGDATA) plus one connection pool per test worker."
}

variable "pytest_job_name" {
  type    = string
  default = "ptest-api"
}

variable "vitest_job_name" {
  type    = string
  default = "ptest-front"
}

variable "operator_members" {
  type        = list(string)
  default     = []
  description = "Principals allowed to upload source and execute jobs, e.g. [\"user:you@example.com\"] or a group. Prefer a group."
}

variable "spot_max_workers" {
  type        = number
  default     = 5
  description = "Hard cap for the regional Spot worker MIG."
}

variable "spot_idle_seconds" {
  type        = number
  default     = 3600
  description = "Keep an idle worker until this exact timeout, unless it owns a lease."
}

variable "spot_worker_image" {
  type        = string
  description = "Prebuilt Spot worker image with Python and Node/Vitest dependencies."
}

variable "spot_controller_image" {
  type        = string
  description = "Prebuilt authenticated Spot controller image."
}

variable "spot_machine_type" {
  type    = string
  default = "e2-standard-8"
}

variable "spot_overflow_to_cloudrun" {
  type        = bool
  default     = false
  description = "Opt-in controller policy flag; no Cloud Run overflow is enabled by default."
}
