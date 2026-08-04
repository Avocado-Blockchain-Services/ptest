variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "bucket_name" { type = string }
variable "ar_repo" { type = string }
variable "spot_worker_image" { type = string }
variable "spot_controller_image" { type = string }
variable "operator_members" {
  type    = list(string)
  default = []
}
variable "spot_smoke_admin_members" {
  type    = list(string)
  default = []
}
variable "spot_max_workers" {
  type    = number
  default = 5
}
variable "spot_idle_seconds" {
  type    = number
  default = 3600
}
variable "spot_overflow_reject_enabled" {
  type    = bool
  default = true
}
variable "spot_machine_type" {
  type    = string
  default = "e2-standard-8"
}
