from pathlib import Path


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
