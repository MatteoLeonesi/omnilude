import hashlib
import os
import shutil
import stat
import tempfile
from pathlib import Path


def _entries(directory: Path) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("Expected a directory, not a symlink")
    paths = [directory]
    for path in sorted(directory.rglob("*")):
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode):
            raise ValueError(f"Symlinks are not allowed: {path}")
        if stat.S_ISREG(mode) or stat.S_ISDIR(mode):
            paths.append(path)
        else:
            raise ValueError(f"Special files are not allowed: {path}")
    return paths


def hash_files(directory: Path, *, metadata: bool = False) -> dict:
    result = {}
    for path in _entries(directory):
        name = path.relative_to(directory).as_posix()
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            if metadata:
                result[name] = {
                    "type": "directory",
                    "mode": stat.S_IMODE(mode),
                }
            continue
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            mode = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(mode):
                raise ValueError(f"Not a regular file: {path}")
            content = hashlib.file_digest(stream, "sha256").hexdigest()
            result[name] = (
                {"type": "file", "mode": stat.S_IMODE(mode), "sha256": content}
                if metadata
                else content
            )
    return result


def snapshot(
    source: Path, destination: Path, *, metadata: bool = False
) -> dict:
    if source.is_symlink():
        raise ValueError("Snapshot source cannot be a symlink")
    source = source.resolve(strict=True)
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(destination)
    if destination.resolve().is_relative_to(source):
        raise ValueError("Snapshot destination cannot be inside its source")
    before = hash_files(source, metadata=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(
        tempfile.mkdtemp(dir=destination.parent, prefix=".snapshot-")
    )
    try:
        staged = temporary / "files"
        shutil.copytree(source, staged, symlinks=True)
        after = hash_files(staged, metadata=True)
        if before != after or before != hash_files(source, metadata=True):
            raise ValueError("Source changed during snapshot")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(destination)
        staged.rename(destination)
        return (
            after
            if metadata
            else {
                name: entry["sha256"]
                for name, entry in after.items()
                if entry["type"] == "file"
            }
        )
    finally:
        shutil.rmtree(temporary)
