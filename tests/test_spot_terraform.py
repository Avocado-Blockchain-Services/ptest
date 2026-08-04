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
