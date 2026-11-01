import hashlib
import os

import pytest

from omnilude import snapshots


def test_snapshot_preserves_nested_and_empty_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "nested").mkdir()
    (source / "nested" / "data").write_bytes(b"abc")
    (source / "empty").write_bytes(b"")
    (source / "empty-directory").mkdir()
    destination = tmp_path / "snapshot"
    manifest = snapshots.snapshot(source, destination)
    assert manifest == {
        "empty": hashlib.sha256(b"").hexdigest(),
        "nested/data": hashlib.sha256(b"abc").hexdigest(),
    }
    assert (destination / "empty-directory").is_dir()
    (source / "nested" / "data").write_bytes(b"changed")
    assert snapshots.hash_files(destination) == manifest
    assert snapshots.hash_files(source) != manifest


@pytest.mark.parametrize("kind", ["file_link", "directory_link", "fifo"])
def test_snapshot_rejects_unsafe_entries(tmp_path, kind):
    source = tmp_path / "source"
    source.mkdir()
    entry = source / "unsafe"
    if kind == "fifo":
        os.mkfifo(entry)
    else:
        target = tmp_path if kind == "directory_link" else tmp_path / "secret"
        if kind == "file_link":
            target.write_text("not part of the workspace")
        entry.symlink_to(target)
    with pytest.raises(ValueError):
        snapshots.snapshot(source, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()


def test_snapshot_rejects_overwrite_and_recursion(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    with pytest.raises(FileExistsError):
        snapshots.snapshot(source, source)
    with pytest.raises(ValueError):
        snapshots.snapshot(source, source / "nested")


@pytest.mark.parametrize("mutation", ["content", "directory", "mode"])
def test_snapshot_detects_source_mutation(tmp_path, monkeypatch, mutation):
    source = tmp_path / "source"
    source.mkdir()
    (source / "data").write_bytes(b"before")
    copytree = snapshots.shutil.copytree

    def changed_copy(*args, **kwargs):
        result = copytree(*args, **kwargs)
        if mutation == "content":
            (source / "data").write_bytes(b"after")
        elif mutation == "directory":
            (source / "empty").mkdir()
        else:
            path = source / "data"
            path.chmod(path.stat().st_mode ^ 0o100)
        return result

    monkeypatch.setattr(snapshots.shutil, "copytree", changed_copy)
    with pytest.raises(ValueError, match="changed"):
        snapshots.snapshot(source, tmp_path / "destination")
    assert not (tmp_path / "destination").exists()
    assert not list(tmp_path.glob(".snapshot-*"))


def test_snapshot_preserves_directory_and_file_modes(tmp_path):
    source = tmp_path / "source"
    source.mkdir(mode=0o750)
    (source / "empty").mkdir(mode=0o700)
    script = source / "run.sh"
    script.write_bytes(b"exit 0\n")
    script.chmod(0o751)
    destination = tmp_path / "snapshot"
    manifest = snapshots.snapshot(source, destination, metadata=True)
    assert manifest == {
        ".": {"type": "directory", "mode": 0o750},
        "empty": {"type": "directory", "mode": 0o700},
        "run.sh": {
            "type": "file",
            "mode": 0o751,
            "sha256": hashlib.sha256(b"exit 0\n").hexdigest(),
        },
    }
    assert snapshots.hash_files(destination, metadata=True) == manifest
    (destination / "run.sh").chmod(0o640)
    assert snapshots.hash_files(source) == snapshots.hash_files(destination)
    assert snapshots.hash_files(destination, metadata=True) != manifest
