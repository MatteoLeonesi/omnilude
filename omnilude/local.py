import fcntl
import json
import math
import os
import platform
import signal
import subprocess
import sys
import time
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

from omnilude import core, research, store
from omnilude.research_spec import ResearchSpec
from omnilude.snapshots import hash_files, snapshot


def _now():
    return datetime.now(UTC).isoformat()


def _run(root, run_id):
    run = store.get_run(root, run_id)
    if run["harness"] != "local":
        raise ValueError("This command requires a local research run")
    return run


def _location(root, run_id):
    _run(root, run_id)
    return Path(root) / "records" / run_id


@contextmanager
def _lock(location):
    with (location / "worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("Run worker is still active") from error
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _environment(spec, location, workspace):
    missing = [key for key in spec.env_vars if key not in os.environ]
    if missing:
        raise ValueError(
            "Missing environment variables: " + ", ".join(missing)
        )
    keys = {"PATH", "LANG", "LC_ALL", "TMPDIR", *spec.env_vars}
    env = {key: os.environ[key] for key in keys if key in os.environ}
    env.update(
        OMNILUDE_RUN_ID=location.name,
        OMNILUDE_INPUTS_DIR=str(location / "execution/inputs"),
        OMNILUDE_OUTPUT_DIR=str(workspace / "output"),
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUNBUFFERED="1",
    )
    (workspace / "output").mkdir(exist_ok=True)
    return env


def _process_start(pid):
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _kill_group(pid):
    with suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGKILL)


def _wait_group(pid):
    deadline = time.monotonic() + 5
    while True:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("Managed process group has not exited")
        time.sleep(0.02)


def _command(root, run_id, argv, cwd, log_directory, timeout, expected, env):
    location = _location(root, run_id)
    log_directory.mkdir(parents=True, exist_ok=True)
    result = {
        "argv": argv,
        "cwd": str(cwd),
        "started_at": _now(),
        "expected_exit": expected,
        "exit_code": None,
        "stop_reason": "cancelled",
        "stdout": str((log_directory / "stdout.log").relative_to(location)),
        "stderr": str((log_directory / "stderr.log").relative_to(location)),
    }
    store.append_event(root, run_id, "harness", "tool_call", result)
    with (
        (log_directory / "stdout.log").open("xb") as stdout,
        (log_directory / "stderr.log").open("xb") as stderr,
    ):
        if not (location / "STOP").exists():
            core.write_json(location / "process.json", {"launching": True})
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=cwd,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
            except OSError:
                (location / "process.json").unlink()
                raise
            deadline = time.monotonic() + timeout
            try:
                core.write_json(
                    location / "process.json",
                    {"pid": process.pid, "start": _process_start(process.pid)},
                )
                while True:
                    cancelled = (location / "STOP").exists()
                    if cancelled or time.monotonic() >= deadline:
                        result["stop_reason"] = (
                            "cancelled" if cancelled else "timeout"
                        )
                        _kill_group(process.pid)
                        process.wait(timeout=5)
                        break
                    try:
                        process.wait(timeout=0.1)
                    except subprocess.TimeoutExpired:
                        continue
                    result["stop_reason"] = (
                        "completed"
                        if process.returncode == expected
                        else "failed"
                    )
                    break
            finally:
                _kill_group(process.pid)
                process.wait(timeout=5)
                _wait_group(process.pid)
                (location / "process.json").unlink(missing_ok=True)
            result["exit_code"] = process.returncode
    result["finished_at"] = _now()
    core.write_json(log_directory / "result.json", result)
    store.append_event(root, run_id, "harness", "tool_result", result)
    return result


def start(root: Path, experiment_id: str, background: bool = True) -> dict:
    root = Path(root).resolve()
    experiment = research.get(root, experiment_id)
    if experiment["state"] != "frozen":
        raise ValueError("Freeze the research experiment before starting")
    problems = research.verify_frozen(root, experiment_id)
    if problems:
        raise ValueError("; ".join(problems))
    run_id = store.new_run(root, experiment_id, harness="local")
    location = root / "records" / run_id
    location.mkdir(parents=True)
    if background:
        try:
            with (location / "worker.log").open("ab") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "omnilude.research_cli",
                        "--root",
                        str(root),
                        "worker",
                        run_id,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=log,
                    start_new_session=True,
                )
            core.write_json(location / "worker.json", {"pid": process.pid})
        except OSError as error:
            store.append_event(
                root, run_id, "harness", "error", {"message": str(error)}
            )
            recover(root, run_id)
            raise
    else:
        worker(root, run_id)
    return status(root, run_id)


def _seal(root, run_id, manifest):
    location = _location(root, run_id)
    if (location / "process.json").exists():
        raise RuntimeError("Managed processes must stop before sealing")
    execution = location / "execution"
    execution.mkdir(exist_ok=True)
    manifest["finished_at"] = _now()
    manifest["writers_stopped"] = True
    manifest["writer_scope"] = "process groups started by Omnilude only"
    store.append_event(root, run_id, "harness", "session", {"phase": "end"})
    return core.seal_run(root, run_id, manifest)


def worker(root: Path, run_id: str) -> dict:
    root = Path(root).resolve()
    location = _location(root, run_id)
    with _lock(location):
        run = _run(root, run_id)
        if run["state"] != "created":
            raise ValueError("A worker cannot repeat an already started run")
        experiment = research.get(root, run["experiment_id"])
        spec = ResearchSpec.model_validate(experiment["spec"])
        source = root / "experiments" / run["experiment_id"]
        execution = location / "execution"
        execution.mkdir()
        core.write_json(execution / "protocol.json", experiment)
        manifest = {
            "kind": "research",
            "started_at": _now(),
            "platform": platform.platform(),
            "python": sys.version,
            "python_executable": sys.executable,
            "omnilude_sha256": core.omnilude_hash(),
            "protocol_hash": experiment["frozen_hash"],
            "capture_scope": "Omnilude commands and declared artifacts only",
            "isolation": "none",
            "model": None,
            "token_usage": None,
            "cost_usd": None,
            "env_vars": spec.env_vars,
            "stop_reason": "interrupted",
            "exit_code": None,
            "checks": {},
        }
        store.update_run(root, run_id, "running", manifest)
        store.append_event(
            root, run_id, "harness", "session", {"phase": "start"}
        )
        try:
            problems = research.verify_frozen(root, run["experiment_id"])
            if problems:
                raise ValueError("; ".join(problems))
            snapshot(source / "workspace", location / "workspace")
            snapshot(source / "inputs", execution / "inputs")
            manifest["input_hashes"] = hash_files(
                execution / "inputs", metadata=True
            )
            workspace = location / "workspace"
            env = _environment(spec, location, workspace)
            outcome = _command(
                root,
                run_id,
                spec.command,
                workspace,
                execution / "command",
                spec.timeout_seconds,
                spec.expected_exit,
                env,
            )
            manifest.update(
                exit_code=outcome["exit_code"],
                stop_reason=outcome["stop_reason"],
            )
            snapshot(workspace, execution / "workspace")
            for assumption in spec.assumptions:
                if not assumption.check_command:
                    continue
                if (location / "STOP").exists():
                    break
                check_workspace = location / "checks" / assumption.id
                snapshot(execution / "workspace", check_workspace)
                check_env = _environment(spec, location, check_workspace)
                manifest["checks"][assumption.id] = _command(
                    root,
                    run_id,
                    assumption.check_command,
                    check_workspace,
                    execution / "checks" / assumption.id,
                    spec.timeout_seconds,
                    assumption.expected_exit,
                    check_env,
                )
            manifest["inputs_changed"] = manifest[
                "input_hashes"
            ] != hash_files(execution / "inputs", metadata=True)
        except (
            OSError,
            ValueError,
            RuntimeError,
            subprocess.SubprocessError,
        ) as error:
            manifest["stop_reason"] = "infrastructure_error"
            manifest["error"] = str(error)
            store.append_event(
                root, run_id, "harness", "error", {"message": str(error)}
            )
        return _seal(root, run_id, manifest)


def status(root: Path, run_id: str) -> dict:
    run = _run(root, run_id)
    return {
        "run_id": run_id,
        "experiment_id": run["experiment_id"],
        "state": run["state"],
        "manifest": run["manifest"],
        "stop_requested": (_location(root, run_id) / "STOP").exists(),
    }


def stop(root: Path, run_id: str, reason: str = "") -> dict:
    run = _run(root, run_id)
    if run["state"] not in {"created", "running"}:
        raise ValueError("Only an active run can be stopped")
    store.append_action(
        root,
        run["experiment_id"],
        run_id,
        "stop",
        {"reason": reason, "actor": "assistant"},
    )
    (_location(root, run_id) / "STOP").touch()
    return status(root, run_id)


def recover(root: Path, run_id: str) -> dict:
    location = _location(root, run_id)
    with _lock(location):
        run = _run(root, run_id)
        if run["state"] not in {"created", "running"}:
            return status(root, run_id)
        process_file = location / "process.json"
        if process_file.exists():
            process = json.loads(process_file.read_text())
            if process.get("launching"):
                raise RuntimeError(
                    "Interrupted during process launch; inspect the worker "
                    "before closing this run"
                )
            pid, started = process["pid"], process["start"]
            current = _process_start(pid)
            if current and current == started and os.getpgid(pid) == pid:
                _kill_group(pid)
                _wait_group(pid)
            else:
                try:
                    os.killpg(pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise RuntimeError(
                        "Cannot verify the orphan process identity; "
                        "wait for it to exit before recovering"
                    )
            process_file.unlink()
        if run["state"] == "created":
            store.update_run(root, run_id, "running", run["manifest"])
        manifest = run["manifest"] | {
            "stop_reason": "interrupted",
            "capture_scope": "partial; recovered without repeating commands",
            "isolation": "none",
        }
        execution = location / "execution"
        execution.mkdir(exist_ok=True)
        if (location / "workspace").exists() and not (
            execution / "workspace"
        ).exists():
            snapshot(location / "workspace", execution / "workspace")
        _seal(root, run_id, manifest)
        return status(root, run_id)


def _row(identifier, status_value, reason, evidence=None):
    return {
        "id": identifier,
        "status": status_value,
        "reason": reason,
        "evidence": evidence or [],
    }


def _safe_file(base, relative):
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts:
        raise ValueError("Evidence paths must be relative and contained")
    target = base / path
    if any(item.is_symlink() for item in (target, *target.parents)):
        raise ValueError("Evidence paths cannot traverse symlinks")
    if not target.resolve().is_relative_to(base.resolve()):
        raise ValueError("Evidence path escapes the run")
    return target


def evaluate(root: Path, run_id: str) -> dict:
    run = _run(root, run_id)
    if not store.reserve_evaluation(root, run_id):
        return store.get_evaluation(root, run_id)
    experiment = research.get(root, run["experiment_id"])
    spec = ResearchSpec.model_validate(experiment["spec"])
    execution = _location(root, run_id) / "execution"
    problems = core.verify(root, run_id) + research.verify_frozen(
        root, run["experiment_id"]
    )
    rows = [
        _row(
            "integrity",
            "failed" if problems else "supported",
            "; ".join(problems) or "Sealed bytes and recorded events match",
            ["seal.json"],
        ),
        _row(
            "execution",
            "supported"
            if run["manifest"].get("stop_reason") == "completed"
            else "failed",
            run["manifest"].get("stop_reason", "Unknown outcome"),
            ["execution/command/result.json"],
        ),
    ]
    for artifact in spec.artifacts:
        try:
            exists = _safe_file(
                execution / "workspace", artifact.path
            ).is_file()
        except ValueError:
            exists = False
        rows.append(
            _row(
                "artifact:" + artifact.path,
                "supported"
                if exists
                else "failed"
                if artifact.required
                else "unsupported",
                "File present" if exists else "File missing or unsafe",
                ["execution/workspace/" + artifact.path] if exists else [],
            )
        )
    for assumption in spec.assumptions:
        outcome = run["manifest"].get("checks", {}).get(assumption.id)
        passed = outcome and outcome["stop_reason"] == "completed"
        supported = passed and assumption.author == "researcher"
        rows.append(
            _row(
                assumption.id,
                "supported"
                if supported
                else "unsupported"
                if passed
                else "failed"
                if outcome
                else "unsupported",
                "Researcher-declared check passed; warrant needs review"
                if supported
                else "Agent-authored check passed; assumption needs review"
                if passed
                else "Declared check failed"
                if outcome
                else "No recorded executable check",
                [f"execution/checks/{assumption.id}/result.json"]
                if outcome
                else [],
            )
            | {
                "statement": assumption.statement,
                "warrant": assumption.warrant,
                "check": assumption.check,
                "expected": assumption.expected,
                "author": assumption.author,
                "check_status": "passed"
                if passed
                else "failed"
                if outcome
                else "not_run",
                "observed": outcome,
            }
        )
    metrics = {}
    if spec.metrics_path:
        try:
            path = _safe_file(execution / "workspace", spec.metrics_path)
            if path.stat().st_size > 8_000_000:
                raise ValueError("Metrics file exceeds 8 MB")
            content = json.loads(path.read_text())
            if not isinstance(content, dict):
                raise ValueError("Metrics must be a JSON object")
            required = {metric.name for metric in spec.metrics}
            if content.keys() != required:
                raise ValueError(
                    "Metrics must match every declared metric exactly"
                )
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or (isinstance(value, float) and not math.isfinite(value))
                for value in content.values()
            ):
                raise ValueError("Metric values must be finite numbers")
            metrics = content
            rows.append(
                _row(
                    "metrics",
                    "supported",
                    "Declared numeric fields present; "
                    "values are not independently reproduced",
                    ["execution/workspace/" + spec.metrics_path],
                )
            )
        except (OSError, ValueError) as error:
            rows.append(_row("metrics", "failed", str(error)))
    else:
        rows.append(
            _row("metrics", "unsupported", "No metrics artifact declared")
        )
    if run["manifest"].get("inputs_changed"):
        rows.append(
            _row("inputs", "failed", "Copied inputs changed during execution")
        )
    result = {
        "metrics": metrics,
        "rows": rows,
        "hypothesis_status": "not_assessed",
        "verification_scope": "artifact structure and declared check outcomes",
    }
    store.finish_evaluation(root, run_id, result)
    return store.get_evaluation(root, run_id)


def report(root: Path, run_id: str) -> dict:
    run = _run(root, run_id)
    experiment = research.get(root, run["experiment_id"])
    evaluation = store.get_evaluation(root, run_id)
    result = (evaluation or {}).get("result") or {}
    problems = (
        core.verify(root, run_id)
        + research.verify_frozen(root, run["experiment_id"])
        if run["state"] not in {"created", "running"}
        else []
    )
    actions = store.actions(root, run_id)
    spec = experiment["spec"]
    rows = [dict(row) for row in result.get("rows", [])]
    if problems:
        rows = [row for row in rows if row["id"] != "integrity"]
        rows.insert(0, _row("integrity", "failed", "; ".join(problems)))
    return {
        "title": spec["title"],
        "run_id": run_id,
        "state": run["state"],
        "work_status": "active"
        if run["state"] in {"created", "running"}
        else "completed",
        "evidence_status": "inconclusive",
        "prediction": spec["prediction"],
        "metrics": result.get("metrics", {}),
        "metric_definitions": spec["metrics"],
        "mode": spec["mode"],
        "after_agent_suggestions": spec["after_agent_suggestions"],
        "deviations": problems
        + ([run["manifest"]["error"]] if run["manifest"].get("error") else []),
        "rows": rows,
        "stored_evaluation_stale": bool(problems),
        "evaluation_state": (evaluation or {}).get("state", "not_started"),
        "hypothesis_status": "not_assessed",
        "method": experiment["method"],
        "manifest": run["manifest"],
        "all_runs": store.list_runs(root, run["experiment_id"]),
        "agent_accounts": [
            action for action in actions if action["kind"] == "agent_account"
        ],
        "reported_protocol_actions": [
            action for action in actions if action["kind"] != "agent_account"
        ],
        "limitations": [
            "No data or process isolation from the current assistant chat",
            "Only commands executed through Omnilude are captured",
            "Reported conversation decisions are not authenticated",
            "Passing checks does not establish hypothesis or method validity",
        ],
    }


def read_evidence(
    root: Path, run_id: str, ref: str, max_chars: int = 20000
) -> dict:
    if not 1 <= max_chars <= 100000:
        raise ValueError("max_chars must be between 1 and 100000")
    path = _safe_file(_location(root, run_id), ref)
    with path.open("rb") as stream:
        content = stream.read(max_chars * 4 + 1).decode(
            "utf-8", errors="replace"
        )
    return {
        "ref": ref,
        "text": content[:max_chars],
        "truncated": len(content) > max_chars,
    }


def record(root: Path, run_id: str, kind: str, payload: dict) -> dict:
    if kind not in {"decision", "observation", "interpretation"}:
        raise ValueError("Record a decision, observation, or interpretation")
    run = _run(root, run_id)
    if kind == "decision" and not all(
        isinstance(payload.get(key), str) and payload[key].strip()
        for key in ("choice", "reason", "rejected_alternative")
    ):
        raise ValueError(
            "Decisions need choice, reason, and rejected_alternative"
        )
    store.append_action(
        root,
        run["experiment_id"],
        run_id,
        "agent_account",
        {"actor": "assistant", "kind": kind, "content": payload},
    )
    return {"run_id": run_id, "kind": kind, "actor": "assistant"}
