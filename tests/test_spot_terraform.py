from pathlib import Path
import re


TERRAFORM = Path(__file__).resolve().parents[1] / "terraform" / "spot_queue.tf"


def resource_block(source: str, declaration: str) -> str:
    start = source.index(declaration)
    opening = source.index("{", start)
    depth = 0
    for offset, character in enumerate(source[opening:], opening):
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[start:offset + 1]
    raise AssertionError(f"unterminated Terraform block: {declaration}")


def test_spot_scheduler_posts_to_the_controller_reconcile_route():
    scheduler = resource_block(
        TERRAFORM.read_text(),
        'resource "google_cloud_scheduler_job" "spot_controller"',
    )

    assert 'uri         = "${google_cloud_run_v2_service.spot_controller.uri}/reconcile"' \
        in scheduler
    assert re.search(
        r"audience\s*=\s*google_cloud_run_v2_service\.spot_controller\.uri",
        scheduler,
    )


def test_spot_queue_wakes_the_controller_on_each_publish_while_idle_sweep_is_infrequent():
    source = TERRAFORM.read_text()
    wake = resource_block(source, 'resource "google_pubsub_subscription" "spot_controller_wake"')
    scheduler = resource_block(source, 'resource "google_cloud_scheduler_job" "spot_controller"')

    assert re.search(r'topic\s*=\s*google_pubsub_topic\.spot_requests\.id', wake)
    assert 'push_endpoint = "${google_cloud_run_v2_service.spot_controller.uri}/reconcile"' in wake
    assert 'service_account_email = google_service_account.spot_controller.email' in wake
    assert 'schedule         = "*/5 * * * *"' in scheduler


def test_spot_worker_can_only_mutate_state_and_worker_prefixes():
    source = TERRAFORM.read_text()
    immutable = resource_block(
        source,
        'resource "google_storage_bucket_iam_member" "spot_worker_immutable_reader"',
    )
    mutable = resource_block(
        source,
        'resource "google_storage_bucket_iam_member" "spot_worker_mutable_state"',
    )

    assert 'role   = "roles/storage.objectViewer"' in immutable
    assert 'objects/sources/' in immutable
    assert 'objects/spot/v1/requests/' in immutable
    assert 'role   = "roles/storage.objectUser"' in mutable
    assert 'objects/spot/v1/states/' in mutable
    assert 'objects/spot/v1/workers/' in mutable
    assert 'objects/sources/' not in mutable
    assert 'objects/spot/v1/requests/' not in mutable
    assert 'resource "google_storage_bucket_iam_member" "spot_worker_storage"' not in source


def test_spot_iam_separates_routine_operators_from_live_smoke_administrators():
    source = TERRAFORM.read_text()
    variables = (TERRAFORM.parent / "variables.tf").read_text()
    assert 'variable "spot_smoke_admin_members"' in variables
    assert 'for_each = toset(var.spot_smoke_admin_members)' in resource_block(
        source, 'resource "google_project_iam_member" "spot_smoke_compute"'
    )
    assert 'for_each = toset(var.spot_smoke_admin_members)' in resource_block(
        source, 'resource "google_project_iam_member" "spot_smoke_os_admin"'
    )
    controller = resource_block(source, 'resource "google_storage_bucket_iam_member" "spot_controller_state_reader"')
    assert 'objects/spot/v1/workers/index/current.json' in controller


def test_spot_subscription_has_a_dead_letter_policy_and_service_agent_permissions():
    source = TERRAFORM.read_text()
    subscription = resource_block(source, 'resource "google_pubsub_subscription" "spot_workers"')
    assert re.search(r"dead_letter_topic\s*=\s*google_pubsub_topic\.spot_dead_letters\.id", subscription)
    assert 'max_delivery_attempts = 5' in subscription
    assert 'resource "google_pubsub_topic" "spot_dead_letters"' in source
    assert 'gcp-sa-pubsub.iam.gserviceaccount.com' in source
    assert re.search(r'role\s*=\s*"roles/pubsub.publisher"', source)
