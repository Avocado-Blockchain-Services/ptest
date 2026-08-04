from pathlib import Path


SPOT_ROOT = Path(__file__).resolve().parents[1] / "terraform" / "spot"


def test_spot_only_deployment_references_shared_bucket_and_registry_without_managing_them():
    main = (SPOT_ROOT / "main.tf").read_text()

    assert 'data "google_storage_bucket" "source"' in main
    assert 'data "google_artifact_registry_repository" "images"' in main
    assert 'resource "google_storage_bucket"' not in main
    assert 'resource "google_cloud_run_v2_job"' not in main


def test_spot_only_deployment_uses_its_own_remote_state_prefix():
    backend = (SPOT_ROOT / "backend.tf").read_text()

    assert 'prefix = "ptest-spot"' in backend


def test_spot_only_deployment_uses_valid_compute_iam_permission_names():
    main = (SPOT_ROOT / "main.tf").read_text()

    assert '"compute.instanceGroupManagers.get"' in main
    assert '"compute.instanceGroupManagers.update"' in main
    assert "compute.regionInstanceGroupManagers" not in main


def test_spot_only_deployment_uses_a_non_controller_identity_for_operator_invocation():
    main = (SPOT_ROOT / "main.tf").read_text()

    assert 'resource "google_service_account" "invoker"' in main
    assert 'resource "google_service_account_iam_member" "operator_invoker_token_creator"' in main
    assert 'serviceAccount:${google_service_account.invoker.email}' in main


def test_spot_only_deployment_declares_the_provider_spot_termination_default():
    main = (SPOT_ROOT / "main.tf").read_text()

    assert 'instance_termination_action = "STOP"' in main


def test_worker_container_uses_the_required_host_user_namespace_for_bubblewrap():
    startup = (Path(__file__).resolve().parents[1] / "scripts" / "spot-worker-startup.sh").read_text()

    assert "--privileged --userns=host" in startup
    assert "--security-opt=no-new-privileges:true" in startup
