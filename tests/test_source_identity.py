import os
import subprocess
import tarfile
from pathlib import Path

import pytest


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def init_repo(repo: Path) -> None:
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "ptest@example.invalid")
    git(repo, "config", "user.name", "ptest")


@pytest.mark.parametrize(
    ("returncode", "stdout", "stderr", "expected"),
    [
        (0, "12345\n", "", True),
        (0, "", "", False),
        (1, "", "ERROR: (gcloud.storage.objects.describe) 404 Not Found", False),
        (1, "", "HTTPError 404: Not Found", False),
        (1, "", "status=404", False),
        (1, "", "404 Not Found", False),
        (1, "", "credentials not found", None),
        (1, "", "permission denied", None),
    ],
)
def test_source_object_exists_distinguishes_hits_misses_and_ambiguous_failures(
    ptest, monkeypatch, returncode, stdout, stderr, expected
):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, returncode, stdout, stderr)

    monkeypatch.setattr(ptest.subprocess, "run", run)

    assert ptest.source_object_exists(
        ["gcloud", "--project=test-project"], "private-bucket", "sources/a.tar.gz"
    ) is expected
    assert calls == [
        (["gcloud", "--project=test-project", "storage", "objects", "describe",
          "gs://private-bucket/sources/a.tar.gz", "--format=value(generation)"],
         {"capture_output": True, "text": True, "timeout": 60})
    ]


def test_source_object_exists_does_not_misclassify_an_echoed_404_uri(
    ptest, monkeypatch
):
    digest = "a" * 20 + "404" + "b" * 41

    def run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1, "",
            f"credentials not found while reading gs://private-bucket/sources/{digest}.tar.gz",
        )

    monkeypatch.setattr(ptest.subprocess, "run", run)

    assert ptest.source_object_exists(
        ["gcloud"], "private-bucket", f"sources/{digest}.tar.gz"
    ) is None


def _archive(tmp_path):
    directory = tmp_path / "ptest-source-archive"
    directory.mkdir()
    archive = directory / "src.tar.gz"
    archive.write_bytes(b"archive")
    return archive, directory


def _prepare_source_archive(ptest, monkeypatch, tmp_path, upload, digests):
    archive, directory = _archive(tmp_path)
    monkeypatch.setattr(ptest, "source_object_exists", lambda *args: False)
    monkeypatch.setattr(ptest, "pack_tree", lambda *args: archive)
    monkeypatch.setattr(ptest, "source_manifest", lambda root: ())
    monkeypatch.setattr(ptest, "tree_digest", lambda *args: next(digests))
    monkeypatch.setattr(ptest, "upload_source_once", lambda *args: upload)
    result = ptest.prepare_source_archive(
        tmp_path, "a" * 64, (), ["gcloud"], object(), "private-bucket"
    )
    return result, archive, directory


def test_prepare_source_archive_cleans_owned_archive_after_upload(
    ptest, monkeypatch, tmp_path
):
    result, archive, directory = _prepare_source_archive(
        ptest, monkeypatch, tmp_path, True, iter(["a" * 64])
    )

    assert result == Path("sources/" + "a" * 64 + ".tar.gz")
    assert not archive.exists()
    assert not directory.exists()


def test_prepare_source_archive_cleans_owned_archive_after_upload_failure(
    ptest, monkeypatch, tmp_path
):
    result, archive, directory = _prepare_source_archive(
        ptest, monkeypatch, tmp_path, False, iter(["a" * 64])
    )

    assert result is None
    assert not archive.exists()
    assert not directory.exists()


def test_prepare_source_archive_cleans_owned_archive_after_source_change(
    ptest, monkeypatch, tmp_path
):
    archive, directory = _archive(tmp_path)
    monkeypatch.setattr(ptest, "source_object_exists", lambda *args: False)
    monkeypatch.setattr(ptest, "pack_tree", lambda *args: archive)
    monkeypatch.setattr(ptest, "source_manifest", lambda root: ())
    monkeypatch.setattr(ptest, "tree_digest", lambda *args: "b" * 64)
    monkeypatch.setattr(ptest, "upload_source_once", lambda *args: True)

    with pytest.raises(ptest.SourceTreeChanged):
        ptest.prepare_source_archive(
            tmp_path, "a" * 64, (), ["gcloud"], object(), "private-bucket"
        )

    assert not archive.exists()
    assert not directory.exists()


def test_digest_is_independent_of_checkout_path_and_mtime(ptest, tmp_path):
    digests = []
    for name, mtime in (("one", 1_000_000_000), ("two", 2_000_000_000)):
        root = tmp_path / name
        root.mkdir()
        init_repo(root)
        source = root / "src" / "example.py"
        source.parent.mkdir()
        source.write_text("answer = 42\n")
        git(root, "add", ".")
        os.utime(source, (mtime, mtime))
        digests.append(ptest.tree_digest(root))

    assert digests[0] == digests[1]


def test_digest_tracks_source_bytes_untracked_files_and_executable_bit(ptest, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    init_repo(root)
    source = root / "script.sh"
    source.write_text("echo one\n")
    git(root, "add", "script.sh")
    original = ptest.tree_digest(root)

    source.write_text("echo two\n")
    edited = ptest.tree_digest(root)
    assert edited != original

    extra = root / "new-test.py"
    extra.write_text("assert True\n")
    with_untracked = ptest.tree_digest(root)
    assert with_untracked != edited

    extra.unlink()
    source.chmod(0o755)
    executable = ptest.tree_digest(root)
    assert executable != edited


def test_manifest_excludes_secrets_and_build_outputs_but_keeps_templates_and_lockfiles(
    ptest, tmp_path
):
    root = tmp_path / "repo"
    root.mkdir()
    init_repo(root)
    (root / ".gitignore").write_text("uv.lock\nnode_modules/\n")
    (root / "app.py").write_text("pass\n")
    (root / ".env").write_text("TOKEN=secret\n")
    (root / ".env.local").write_text("TOKEN=secret\n")
    (root / ".env.example").write_text("TOKEN=replace-me\n")
    (root / "uv.lock").write_text("version = 1\n")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "huge.js").write_text("ignored\n")
    git(root, "add", ".gitignore", "app.py", ".env.example")

    paths = {entry.relative_path for entry in ptest.source_manifest(root)}

    assert "app.py" in paths
    assert ".env.example" in paths
    assert "uv.lock" in paths
    assert ".env" not in paths
    assert ".env.local" not in paths
    assert "node_modules/huge.js" not in paths


def test_pack_tree_uses_exactly_the_manifest_entries(ptest, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    init_repo(root)
    (root / "tracked.txt").write_text("tracked\n")
    (root / "untracked.txt").write_text("untracked\n")
    git(root, "add", "tracked.txt")
    manifest = ptest.source_manifest(root)

    archive = ptest.pack_tree(root, manifest)
    try:
        with tarfile.open(archive, "r:gz") as packed:
            names = {member.name.removeprefix("./") for member in packed.getmembers()}
    finally:
        archive.unlink()
        archive.parent.rmdir()

    assert names == {entry.relative_path for entry in manifest}


def test_non_git_manifest_excludes_generated_directories(ptest, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("pass\n")
    (tmp_path / "build").mkdir()
    (tmp_path / "build" / "artifact.py").write_text("generated\n")

    paths = {entry.relative_path for entry in ptest.source_manifest(tmp_path)}

    assert paths == {"src/app.py"}


def test_manifest_skips_staged_deletions(ptest, tmp_path):
    init_repo(tmp_path)
    deleted = tmp_path / "deleted.py"
    deleted.write_text("pass\n")
    git(tmp_path, "add", "deleted.py")
    deleted.unlink()

    assert ptest.source_manifest(tmp_path) == ()


def test_git_manifest_keeps_tracked_files_in_generated_named_directories(ptest, tmp_path):
    init_repo(tmp_path)
    generated_named = tmp_path / "build" / "checked-in-fixture.txt"
    generated_named.parent.mkdir()
    generated_named.write_text("fixture\n")
    git(tmp_path, "add", ".")

    paths = {entry.relative_path for entry in ptest.source_manifest(tmp_path)}

    assert "build/checked-in-fixture.txt" in paths


def test_symlink_target_is_hashed_and_preserved_in_archive(ptest, tmp_path):
    init_repo(tmp_path)
    (tmp_path / "first.txt").write_text("same\n")
    (tmp_path / "second.txt").write_text("same\n")
    link = tmp_path / "current.txt"
    link.symlink_to("first.txt")
    git(tmp_path, "add", ".")
    first_digest = ptest.tree_digest(tmp_path)

    link.unlink()
    link.symlink_to("second.txt")
    second_digest = ptest.tree_digest(tmp_path)
    assert second_digest != first_digest

    manifest = ptest.source_manifest(tmp_path)
    archive = ptest.pack_tree(tmp_path, manifest)
    try:
        with tarfile.open(archive, "r:gz") as packed:
            member = packed.getmember("./current.txt")
            assert member.issym()
            assert member.linkname == "second.txt"
    finally:
        archive.unlink()
        archive.parent.rmdir()
