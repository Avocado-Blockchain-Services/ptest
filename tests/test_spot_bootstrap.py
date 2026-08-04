import os
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "spot-worker-startup.sh"


def test_spot_worker_image_contains_the_universal_runner_toolchain():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile.spot-worker").read_text()
    assert "postgresql-15" in dockerfile
    assert "postgresql-client-15" in dockerfile
    assert "ENV PATH=/usr/lib/postgresql/15/bin:${PATH}" in dockerfile
    assert "COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv" in dockerfile
    assert "bubblewrap" in dockerfile
    assert "npm install --global vitest" not in dockerfile


def test_spot_worker_image_provides_bubblewrap_bind_target():
    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile.spot-worker").read_text()

    assert "/work" in dockerfile


def test_debian_bootstrap_configures_cloud_repo_before_installing_gcloud(tmp_path):
    log = tmp_path / "commands.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for command in ("apt-get", "curl", "gpg", "systemctl", "gcloud", "docker", "hostname"):
        path = fake_bin / command
        if command == "hostname":
            path.write_text("#!/bin/sh\necho spot-vm\n")
        else:
            path.write_text(f"#!/bin/sh\nprintf '%s %s\\n' {command} \"$*\" >> \"$SPOT_TEST_LOG\"\n")
        path.chmod(0o755)

    result = subprocess.run(
        ["/bin/bash", str(SCRIPT)], text=True, capture_output=True,
        env={**os.environ, "PATH": str(fake_bin) + ":/usr/bin:/bin", "SPOT_TEST_LOG": str(log),
             "SPOT_APT_ROOT": str(tmp_path / "apt"), "SPOT_WORKER_IMAGE": "example/worker:tag",
             "SPOT_PROJECT": "project", "SPOT_REGION": "us-central1", "SPOT_TOPIC": "topic",
             "SPOT_SUBSCRIPTION": "subscription", "SPOT_BUCKET": "bucket"},
    )

    assert result.returncode == 0, result.stderr
    commands = log.read_text().splitlines()
    assert commands.index("apt-get update") < commands.index("apt-get install -y --no-install-recommends ca-certificates curl gnupg docker.io")
    assert commands.index("apt-get update") < next(i for i, value in enumerate(commands) if "google-cloud-cli" in value)
    assert not any("gke-gcloud-auth-plugin" in command for command in commands)
    assert any(command.startswith("gcloud auth configure-docker") for command in commands)
    assert any(command.startswith("docker run ") and "--restart=always" in command for command in commands)
