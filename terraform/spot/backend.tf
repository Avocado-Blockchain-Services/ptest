terraform {
  backend "gcs" {
    bucket = "seed-staging-b9508c89-tfstate"
    prefix = "ptest-spot"
  }
}
