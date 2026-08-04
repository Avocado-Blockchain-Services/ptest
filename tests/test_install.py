import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def _spot_config(path: Path, project: Path) -> None:
    path.write_text(
        "[defaults]\n"
        'bucket = "private-bucket"\n'
        'gcp_project = "project-a"\n'
        "\n[projects.isolated]\n"
        f'root = "{project}"\n'
        'kind = "pytest"\n'
        'backend = "spot_queue"\n'
        'spot_topic = "ptest-spot-requests"\n'
    )


def test_installer_places_spot_companion_where_ptest_can_import_it(tmp_path):
    destination = tmp_path / "bin"
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "config.toml"
    _spot_config(config, project)

    installed = subprocess.run(
        [str(ROOT / "install.sh"), str(destination)], capture_output=True, text=True
    )
    assert installed.returncode == 0, installed.stderr
    assert (destination / "ptest").is_symlink()
    assert (destination / "ptest").resolve().with_name("spot_queue.py").is_file()
    assert (destination / "ptest").resolve().with_name("spot_controller.py").is_file()

    environment = os.environ.copy()
    environment["PTEST_CONFIG"] = str(config)
    environment["PYTHONPATH"] = ""
    doctor = subprocess.run(
        [str(destination / "ptest"), "doctor"], cwd=project,
        env=environment, capture_output=True, text=True,
    )

    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
    assert "companion   ✓" in doctor.stdout


def test_doctor_rejects_spot_backend_when_companion_is_absent(tmp_path):
    destination = tmp_path / "bin"
    destination.mkdir()
    runner = destination / "ptest"
    runner.write_bytes((ROOT / "ptest").read_bytes())
    runner.chmod(0o755)
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "config.toml"
    _spot_config(config, project)
    environment = os.environ.copy()
    environment["PTEST_CONFIG"] = str(config)
    environment["PYTHONPATH"] = ""

    doctor = subprocess.run(
        [str(runner), "doctor"], cwd=project, env=environment,
        capture_output=True, text=True,
    )

    assert doctor.returncode == 1
    assert "companion   ✗ spot_queue.py is not installed beside ptest" in doctor.stdout


def test_doctor_rejects_an_incompatible_spot_companion(tmp_path):
    destination = tmp_path / "bin"
    project = tmp_path / "project"
    project.mkdir()
    config = tmp_path / "config.toml"
    _spot_config(config, project)
    installed = subprocess.run(
        [str(ROOT / "install.sh"), str(destination)], capture_output=True, text=True
    )
    assert installed.returncode == 0, installed.stderr
    companion = (destination / "ptest").resolve().with_name("spot_queue.py")
    companion.write_text(companion.read_text().replace(
        "PTEST_SPOT_COMPANION_ABI = 1", "PTEST_SPOT_COMPANION_ABI = 999"
    ))
    environment = os.environ.copy()
    environment["PTEST_CONFIG"] = str(config)
    environment["PYTHONPATH"] = ""

    doctor = subprocess.run(
        [str(destination / "ptest"), "doctor"], cwd=project,
        env=environment, capture_output=True, text=True,
    )

    assert doctor.returncode == 1
    assert "companion   ✗ incompatible spot_queue.py" in doctor.stdout
