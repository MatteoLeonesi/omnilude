import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

from omnilude import store
from omnilude.research_spec import (
    RecordedDecision,
    ResearchSpec,
    assumption_hash,
    validate_ready,
)
from omnilude.snapshots import hash_files
from omnilude.spec import canonical_json, digest

EXCLUDED = {
    ".git",
    ".venv",
    ".omnilude",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    ".mypy_cache",
    ".tox",
}
PRELUDE = (
    "question",
    "prediction",
    "metrics",
    "researcher_source",
    "researcher_reference",
    "after_agent_suggestions",
)


def relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(
            "Paths must be relative and remain inside the workspace"
        )
    return path


def _write(path: Path, value: dict) -> None:
    path.write_text(canonical_json(value) + "\n")


def _location(root: Path, experiment_id: str) -> Path:
    get(root, experiment_id)
    return root / "experiments" / experiment_id


def get(root: Path, experiment_id: str) -> dict:
    result = store.get_experiment(root, experiment_id)
    ResearchSpec.model_validate(result["spec"])
    return result


def create(root: Path, spec: dict, workspace: Path) -> dict:
    parsed = ResearchSpec.model_validate(spec)
    if workspace.is_symlink() or not workspace.is_dir():
        raise ValueError("Workspace must be a directory, not a symlink")
    workspace = workspace.resolve()
    store.initialize(root)
    identifier = store.create_experiment(root, parsed)
    location = root / "experiments" / identifier
    location.mkdir(parents=True)
    _write(
        location / "origin.json",
        {
            "workspace": str(workspace),
            "parent": None,
            "after_results": False,
            "recorded_by": "assistant",
        },
    )
    store.append_action(
        root,
        identifier,
        None,
        "researcher_input",
        {
            "input": {key: parsed.model_dump()[key] for key in PRELUDE},
            "recorded_by": "assistant",
            "identity_verified": False,
        },
    )
    return get(root, identifier)


def update(root: Path, experiment_id: str, spec: dict) -> dict:
    old = get(root, experiment_id)
    parsed = ResearchSpec.model_validate(spec)
    updated = parsed.model_dump()
    if (
        old["spec"]["after_agent_suggestions"]
        and not parsed.after_agent_suggestions
    ):
        raise ValueError(
            "Prior agent suggestions cannot be removed from provenance"
        )
    previous_authors = {
        item["id"]: item["author"] for item in old["spec"]["assumptions"]
    }
    if any(
        previous_authors.get(item.id) == "agent"
        and item.author == "researcher"
        for item in parsed.assumptions
    ):
        raise ValueError(
            "An agent proposal cannot be relabeled as researcher-authored"
        )
    prelude_changed = any(old["spec"][key] != updated[key] for key in PRELUDE)
    if prelude_changed and (
        old["spec"]["researcher_source"] == parsed.researcher_source
        and old["spec"]["researcher_reference"] == parsed.researcher_reference
    ):
        raise ValueError(
            "A researcher-input revision needs a new source statement "
            "or reference"
        )
    store.save_draft(root, experiment_id, parsed)
    return get(root, experiment_id)


def fork(root: Path, experiment_id: str) -> dict:
    parent = get(root, experiment_id)
    origin = json.loads(
        (_location(root, experiment_id) / "origin.json").read_text()
    )
    spec = ResearchSpec.model_validate(parent["spec"])
    after_results = origin["after_results"] or bool(
        store.list_runs(root, experiment_id)
    )
    if after_results:
        spec.mode = "exploration"
        spec.fresh_data_statement = None
    result = create(root, spec.model_dump(), Path(origin["workspace"]))
    _write(
        _location(root, result["id"]) / "origin.json",
        {
            **origin,
            "parent": experiment_id,
            "after_results": after_results,
        },
    )
    store.append_action(
        root,
        result["id"],
        None,
        "revision",
        {
            "parent": experiment_id,
            "parent_spec_hash": parent["spec_hash"],
            "after_results": after_results,
            "recorded_by": "assistant",
        },
    )
    return get(root, result["id"])


def _excluded(relative: Path) -> bool:
    return any(
        part in EXCLUDED or part == ".env" or part.startswith(".env.")
        for part in relative.parts
    )


def _collect(
    path: Path,
    prefix: Path,
    result: dict[str, Path],
    excluded: bool = False,
    store_root: Path | None = None,
) -> None:
    if excluded and _excluded(prefix):
        return
    if store_root is not None and path.absolute().is_relative_to(store_root):
        return
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        raise ValueError(f"Symlinks are not allowed: {path}")
    if not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise ValueError(f"Special files are not allowed: {path}")
    result[prefix.as_posix()] = path
    if stat.S_ISDIR(mode):
        for child in sorted(path.iterdir()):
            _collect(child, prefix / child.name, result, excluded, store_root)


def _workspace_entries(
    root: Path, workspace: Path, paths: list[str]
) -> dict[str, Path]:
    entries = {".": workspace}
    for value in paths:
        relative = relative_path(value)
        if _excluded(relative):
            raise ValueError(f"Excluded workspace path: {value}")
        source = workspace / relative
        if source.absolute().is_relative_to(root.resolve()):
            raise ValueError("The run store cannot be a workspace input")
        for ancestor in reversed(relative.parents):
            parent = workspace / ancestor
            if parent.is_symlink():
                raise ValueError(f"Symlinks are not allowed: {parent}")
            entries[ancestor.as_posix()] = parent
        _collect(source, relative, entries, True, root.resolve())
    return entries


def _input_entries(
    root: Path, workspace: Path, spec: ResearchSpec
) -> tuple[dict, dict]:
    entries, manifest = {}, {}
    for item in spec.inputs:
        source = Path(item.path).expanduser()
        source = source if source.is_absolute() else workspace / source
        for component in (source, *source.parents):
            if component.is_symlink():
                raise ValueError(f"Symlinks are not allowed: {component}")
        source = source.resolve(strict=True)
        if source.is_relative_to(
            root.resolve()
        ) or root.resolve().is_relative_to(source):
            raise ValueError("Inputs must be separate from the run store")
        destination = Path(item.name)
        if source.is_file():
            entries[item.name] = source.parent
            destination /= source.name
        _collect(source, destination, entries)
        manifest[item.name] = {
            "source": str(source),
            "path": destination.as_posix(),
            "role": item.role,
            "usage": item.usage,
        }
    return entries, manifest


def _hash_entries(entries: dict[str, Path]) -> dict:
    result = {}
    for name, path in sorted(entries.items()):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            result[name] = {"type": "directory", "mode": stat.S_IMODE(mode)}
        elif stat.S_ISREG(mode):
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as stream:
                actual = os.fstat(stream.fileno()).st_mode
                if not stat.S_ISREG(actual):
                    raise ValueError(f"Not a regular file: {path}")
                result[name] = {
                    "type": "file",
                    "mode": stat.S_IMODE(actual),
                    "sha256": hashlib.file_digest(
                        stream, "sha256"
                    ).hexdigest(),
                }
        else:
            raise ValueError(f"Not a regular file or directory: {path}")
    return result


def _prepared(
    root: Path, experiment_id: str, method: str
) -> tuple[dict, dict, dict]:
    experiment = get(root, experiment_id)
    if experiment["state"] != "draft":
        raise ValueError("Only a draft can be frozen")
    spec = ResearchSpec.model_validate(experiment["spec"])
    location = _location(root, experiment_id)
    origin = json.loads((location / "origin.json").read_text())
    for item in spec.artifacts:
        relative_path(item.path)
    if spec.metrics_path is not None:
        relative_path(spec.metrics_path)
    workspace = Path(origin["workspace"])
    workspace_entries = _workspace_entries(
        root, workspace, spec.workspace_paths
    )
    input_entries, input_manifest = _input_entries(root, workspace, spec)
    workspace_hashes = _hash_entries(workspace_entries)
    data_hashes = _hash_entries(input_entries)
    protocol_hash = digest(
        {
            "spec": experiment["spec"],
            "method": method,
            "data_hashes": data_hashes,
            "workspace_hashes": workspace_hashes,
        }
    )
    result = {
        "experiment_id": experiment_id,
        "protocol_hash": protocol_hash,
        "spec_hash": experiment["spec_hash"],
        "method": method,
        "workspace_hashes": workspace_hashes,
        "data_hashes": data_hashes,
        "input_manifest": input_manifest,
        "assumption_hashes": {
            item.id: assumption_hash(item) for item in spec.assumptions
        },
        "approval_required": spec.tier == "full",
        "recorded_by": "assistant",
        "identity_verified": False,
    }
    return result, workspace_entries, input_entries


def preview(root: Path, experiment_id: str, method: str) -> dict:
    result = _prepared(root, experiment_id, method)[0]
    try:
        _ready(root, experiment_id, method)
    except ValueError as error:
        return result | {"ready": False, "blocking_reason": str(error)}
    return result | {"ready": True, "blocking_reason": None}


def _ready(root: Path, experiment_id: str, method: str) -> None:
    spec = ResearchSpec.model_validate(get(root, experiment_id)["spec"])
    validate_ready(spec, method)
    origin = json.loads(
        (_location(root, experiment_id) / "origin.json").read_text()
    )
    if (
        origin["after_results"]
        and spec.mode == "confirmation"
        and not spec.fresh_data_statement
    ):
        raise ValueError(
            "Confirmation after results needs a fresh-data declaration"
        )


def _copy_entries(entries: dict[str, Path], destination: Path) -> None:
    destination.mkdir()
    for name, source in sorted(
        entries.items(), key=lambda item: (len(Path(item[0]).parts), item[0])
    ):
        target = destination / name
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target, follow_symlinks=False)
            shutil.copystat(source, target, follow_symlinks=False)
    for name, source in entries.items():
        if source.is_dir():
            shutil.copystat(source, destination / name, follow_symlinks=False)


def freeze(
    root: Path, experiment_id: str, method: str, approval: dict | None = None
) -> dict:
    _ready(root, experiment_id, method)
    prepared, workspace_entries, input_entries = _prepared(
        root, experiment_id, method
    )
    decision = (
        RecordedDecision.model_validate(approval)
        if approval is not None
        else None
    )
    if prepared["approval_required"] and decision is None:
        raise ValueError(
            "Full tier requires approval of the exact protocol_hash"
        )
    if (
        decision is not None
        and decision.subject_hash != prepared["protocol_hash"]
    ):
        raise ValueError(
            "Method approval is stale; preview the current protocol"
        )
    location = _location(root, experiment_id)
    if any(
        (location / name).exists()
        for name in ("workspace", "inputs", "frozen.json")
    ):
        raise ValueError(
            "Freeze artifacts already exist; inspect before retrying"
        )
    temporary = Path(tempfile.mkdtemp(prefix=".freeze-", dir=location))
    try:
        _copy_entries(workspace_entries, temporary / "workspace")
        _copy_entries(input_entries, temporary / "inputs")
        workspace_hashes = hash_files(temporary / "workspace", metadata=True)
        data_hashes = hash_files(temporary / "inputs", metadata=True)
        data_hashes.pop(".")
        if (
            workspace_hashes != prepared["workspace_hashes"]
            or data_hashes != prepared["data_hashes"]
        ):
            raise ValueError("Files changed while freezing; preview again")
        if (
            _prepared(root, experiment_id, method)[0]["protocol_hash"]
            != prepared["protocol_hash"]
        ):
            raise ValueError("Protocol or source files changed while freezing")
        for name in ("workspace", "inputs"):
            (temporary / name).rename(location / name)
        (location / "METHOD.md").write_text(method)
        _write(
            location / "frozen.json",
            prepared
            | {
                "approval": decision.model_dump() if decision else None,
            },
        )
        return store.freeze_spec(
            root,
            experiment_id,
            method,
            approved=decision is not None,
            data_hashes=data_hashes,
            workspace_hashes=workspace_hashes,
            expected_spec_hash=prepared["spec_hash"],
            approval_record=decision.model_dump() if decision else None,
        )
    finally:
        shutil.rmtree(temporary)


def verify_frozen(root: Path, experiment_id: str) -> list[str]:
    experiment = get(root, experiment_id)
    if experiment["state"] != "frozen":
        return ["Experiment is not frozen"]
    location = _location(root, experiment_id)
    findings = []
    try:
        frozen = json.loads((location / "frozen.json").read_text())
        if not isinstance(frozen, dict):
            raise ValueError("Frozen receipt must be an object")
        method = (location / "METHOD.md").read_text()
        workspace_hashes = hash_files(location / "workspace", metadata=True)
        data_hashes = hash_files(location / "inputs", metadata=True)
        data_hashes.pop(".")
        if method != experiment["method"]:
            findings.append("Frozen method changed")
        freeze_action = next(
            (
                action
                for action in store.experiment_actions(root, experiment_id)
                if action["kind"] == "freeze"
            ),
            None,
        )
        if freeze_action is None:
            findings.append("Freeze action missing from the record")
        elif frozen.get("approval") != freeze_action["payload"].get(
            "approval"
        ):
            findings.append("Frozen approval record changed")
        if workspace_hashes != frozen["workspace_hashes"]:
            findings.append("Frozen workspace changed")
        if data_hashes != experiment["data_hashes"]:
            findings.append("Frozen inputs changed")
        expected = digest(
            {
                "spec": experiment["spec"],
                "method": method,
                "data_hashes": data_hashes,
                "workspace_hashes": workspace_hashes,
            }
        )
        if (
            expected != experiment["frozen_hash"]
            or frozen["protocol_hash"] != expected
        ):
            findings.append("Frozen protocol hash mismatch")
    except (OSError, ValueError, KeyError) as error:
        findings.append(f"Cannot verify frozen experiment: {error}")
    return findings
