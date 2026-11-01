import hashlib
import json
from pathlib import Path

from omnilude import store
from omnilude.snapshots import hash_files
from omnilude.spec import canonical_json, digest


def write_json(path: Path, content: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(content) + "\n")


def run_path(root: Path, run_id: str) -> Path:
    store.get_run(root, run_id)
    return root / "records" / run_id


def omnilude_hash(package: Path | None = None) -> str:
    package = package or Path(__file__).resolve().parent
    files = {
        path.relative_to(package).as_posix(): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(package.rglob("*.py"))
    }
    return digest(files)


def seal_run(root: Path, run_id: str, manifest: dict) -> dict:
    if not manifest.get("writers_stopped"):
        raise ValueError("Confirm command writers stopped before sealing")
    location = run_path(root, run_id)
    execution = location / "execution"
    captured = store.events(root, run_id)
    manifest["event_cutoff"] = captured[-1]["seq"] if captured else 0
    write_json(execution / "events.json", captured)
    write_json(execution / "manifest.json", manifest)
    seal = {
        "version": 2,
        "files": hash_files(execution, metadata=True),
        "event_cutoff": manifest["event_cutoff"],
    }
    write_json(location / "seal.json", seal)
    manifest["seal_hash"] = digest(seal)
    store.update_run(root, run_id, "sealed", manifest)
    return store.get_run(root, run_id)


def verify(root: Path, run_id: str) -> list[str]:
    run = store.get_run(root, run_id)
    location = run_path(root, run_id)
    if run["state"] in {"created", "running"}:
        return ["Run has not been sealed"]
    try:
        seal = json.loads((location / "seal.json").read_text())
        if digest(seal) != run["manifest"].get("seal_hash"):
            return ["Seal digest differs from the recorded seal"]
        if seal.get("version") != 2:
            return ["Seal lacks directory and permission records"]
        actual = hash_files(location / "execution", metadata=True)
        expected = seal["files"]
        problems = [
            f"Changed or missing: {name}"
            for name in expected
            if actual.get(name) != expected[name]
        ]
        problems.extend(
            f"Unexpected file: {name}"
            for name in actual.keys() - expected.keys()
        )
        current = [
            event
            for event in store.events(root, run_id)
            if event["seq"] <= seal["event_cutoff"]
        ]
        recorded = json.loads((location / "execution/events.json").read_text())
        if current != recorded:
            problems.append("Execution events differ from the sealed prefix")
        return problems
    except (OSError, ValueError, KeyError, TypeError) as error:
        return [f"Seal cannot be verified: {error}"]
