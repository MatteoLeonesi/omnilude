import json
import sys
import time

import pytest

from omnilude import core, local, research, store


@pytest.fixture
def experiment(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    workspace = tmp_path / "project"
    workspace.mkdir()
    root = workspace / ".omnilude"
    spec = {
        "title": "Local measurement",
        "question": "Does the program produce the declared measurement?",
        "prediction": "A value of 42 units",
        "metrics": [{"name": "value", "definition": "Measured value"}],
        "researcher_source": "I expect 42; measure value.",
        "after_agent_suggestions": False,
        "workspace_paths": ["main.py"],
        "data_policy": "No external data",
        "selection_rule": "Report every attempt",
        "success_criteria": "Measurement artifact contains value",
        "constraints": [],
        "stopping_rule": "One run with a five-second command limit",
        "timeout_seconds": 5,
        "command": [sys.executable, "main.py"],
        "artifacts": [{"path": "output/metrics.json"}],
        "metrics_path": "output/metrics.json",
    }

    def create(source=None, **changes):
        (workspace / "main.py").write_text(
            source
            or "from pathlib import Path\n"
            "Path('output/metrics.json').write_text('{\"value\":42}')\n"
            "print('measured')\n"
        )
        draft = research.create(root, spec | changes, workspace)
        research.freeze(root, draft["id"], "Run the declared measurement.")
        return root, draft["id"]

    return create


def complete(root, identifier):
    run_id = local.start(root, identifier, background=False)["run_id"]
    local.evaluate(root, run_id)
    return run_id, local.report(root, run_id)


def wait_until(predicate, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if result := predicate():
            return result
        time.sleep(0.02)
    pytest.fail("Timed out waiting for the local worker")


def test_complete_local_run_without_model_api(experiment):
    root, identifier = experiment()
    run_id, report = complete(root, identifier)
    assert report["metrics"] == {"value": 42}
    assert report["hypothesis_status"] == "not_assessed"
    assert report["manifest"]["model"] is None
    assert report["manifest"]["isolation"] == "none"
    assert (
        "No data or process isolation from the current assistant chat"
        in report["limitations"]
    )
    assert core.verify(root, run_id) == []
    assert (
        "measured"
        in local.read_evidence(root, run_id, "execution/command/stdout.log")[
            "text"
        ]
    )
    first = local.evaluate(root, run_id)
    assert local.evaluate(root, run_id) == first
    with pytest.raises(ValueError, match="cap"):
        local.start(root, identifier, background=False)


@pytest.mark.parametrize(
    ("code", "expected", "reason"),
    [(0, 0, "completed"), (7, 7, "completed"), (7, 0, "failed")],
)
def test_expected_and_unexpected_exit(experiment, code, expected, reason):
    root, identifier = experiment(
        f"raise SystemExit({code})\n", expected_exit=expected
    )
    run_id, report = complete(root, identifier)
    assert report["manifest"]["stop_reason"] == reason
    assert report["manifest"]["exit_code"] == code
    assert (
        next(row for row in report["rows"] if row["id"] == "metrics")["status"]
        == "failed"
    )
    assert core.verify(root, run_id) == []


def test_checks_run_on_separate_workspace_and_show_authorship(experiment):
    assumptions = [
        {
            "id": "value_present",
            "statement": "The output contains the measurement",
            "warrant": "Reading the output checks its presence",
            "check": "Read the metrics artifact",
            "expected": "Exit zero",
            "check_command": [
                sys.executable,
                "-c",
                "from pathlib import Path; "
                "assert Path('output/metrics.json').exists(); "
                "Path('output/metrics.json').write_text('changed')",
            ],
            "author": "researcher",
        },
        {
            "id": "deliberate_failure",
            "statement": "A control catches a failure",
            "warrant": "A nonzero exit is detectable",
            "check": "Exit one",
            "expected": "Exit zero",
            "check_command": [sys.executable, "-c", "raise SystemExit(1)"],
            "author": "researcher",
        },
    ]
    root, identifier = experiment(assumptions=assumptions)
    _, report = complete(root, identifier)
    rows = {row["id"]: row for row in report["rows"]}
    assert rows["value_present"]["status"] == "supported"
    assert rows["value_present"]["author"] == "researcher"
    assert rows["deliberate_failure"]["status"] == "failed"
    assert report["metrics"] == {"value": 42}


@pytest.mark.parametrize("value", ["true", "NaN", "Infinity", '"42"'])
def test_invalid_metrics_preserve_raw_artifact(experiment, value):
    source = (
        "from pathlib import Path\n"
        f"Path('output/metrics.json').write_text('{{\"value\":{value}}}')\n"
    )
    root, identifier = experiment(source)
    run_id, report = complete(root, identifier)
    assert report["metrics"] == {}
    assert (
        next(row for row in report["rows"] if row["id"] == "metrics")["status"]
        == "failed"
    )
    assert (
        value
        in local.read_evidence(
            root, run_id, "execution/workspace/output/metrics.json"
        )["text"]
    )


def test_large_integer_metric_is_finite(experiment):
    value = 10**400
    root, identifier = experiment(
        "from pathlib import Path\n"
        f"Path('output/metrics.json').write_text('{{\"value\":{value}}}')\n"
    )
    _, report = complete(root, identifier)
    assert report["metrics"]["value"] == value
    assert report["evaluation_state"] == "completed"


def test_timeout_keeps_partial_output(experiment):
    root, identifier = experiment(
        "import time\nprint('partial', flush=True)\ntime.sleep(30)\n",
        timeout_seconds=1,
    )
    run_id, report = complete(root, identifier)
    assert report["manifest"]["stop_reason"] == "timeout"
    assert (
        "partial"
        in local.read_evidence(root, run_id, "execution/command/stdout.log")[
            "text"
        ]
    )
    assert core.verify(root, run_id) == []


def test_background_stop_and_recovery_do_not_repeat(experiment):
    root, identifier = experiment(
        "import time\nprint('started', flush=True)\ntime.sleep(30)\n",
        timeout_seconds=60,
    )
    run_id = local.start(root, identifier)["run_id"]
    try:
        wait_until(
            lambda: (root / "records" / run_id / "process.json").exists()
        )
        with pytest.raises(RuntimeError, match="active"):
            local.recover(root, run_id)
        local.stop(root, run_id, "Test cancellation")
        wait_until(lambda: local.status(root, run_id)["state"] == "sealed")
        assert (
            local.status(root, run_id)["manifest"]["stop_reason"]
            == "cancelled"
        )
        events = store.events(root, run_id)
        local.recover(root, run_id)
        assert store.events(root, run_id) == events
    finally:
        if local.status(root, run_id)["state"] in {"created", "running"}:
            local.stop(root, run_id)
            wait_until(lambda: local.status(root, run_id)["state"] == "sealed")


def test_unstarted_worker_recovery_is_retained(experiment):
    root, identifier = experiment()
    run_id = store.new_run(root, identifier, harness="local")
    (root / "records" / run_id).mkdir(parents=True)
    result = local.recover(root, run_id)
    assert result["state"] == "sealed"
    assert result["manifest"]["stop_reason"] == "interrupted"
    assert not (root / "records" / run_id / "execution/workspace").exists()
    with pytest.raises(ValueError, match="cap"):
        local.start(root, identifier)


def test_env_values_not_logged_and_unlisted_secrets_not_inherited(
    experiment, monkeypatch
):
    monkeypatch.setenv("OMNILUDE_TEST_ALLOWED", "allowed-value")
    monkeypatch.setenv("OMNILUDE_TEST_SECRET", "secret-value")
    root, identifier = experiment(
        "import os\nfrom pathlib import Path\n"
        "assert os.environ['OMNILUDE_TEST_ALLOWED'] == 'allowed-value'\n"
        "assert 'OMNILUDE_TEST_SECRET' not in os.environ\n"
        "Path('output/metrics.json').write_text('{\"value\":42}')\n",
        env_vars=["OMNILUDE_TEST_ALLOWED"],
    )
    run_id, report = complete(root, identifier)
    assert report["metrics"] == {"value": 42}
    assert "allowed-value" not in json.dumps(report["manifest"])
    assert "secret-value" not in json.dumps(store.events(root, run_id))


def test_missing_required_env_seals_failure(experiment, monkeypatch):
    monkeypatch.delenv("OMNILUDE_MISSING", raising=False)
    root, identifier = experiment(env_vars=["OMNILUDE_MISSING"])
    run_id, report = complete(root, identifier)
    assert report["manifest"]["stop_reason"] == "infrastructure_error"
    assert "OMNILUDE_MISSING" in report["manifest"]["error"]
    assert core.verify(root, run_id) == []


def test_modified_seal_and_unsafe_evidence(experiment, tmp_path):
    root, identifier = experiment()
    run_id, _ = complete(root, identifier)
    artifact = (
        root / "records" / run_id / "execution/workspace/output/metrics.json"
    )
    artifact.write_text('{"value":99}')
    report = local.report(root, run_id)
    assert report["deviations"]
    assert report["stored_evaluation_stale"]
    assert report["rows"][0]["status"] == "failed"
    with pytest.raises(ValueError, match="relative"):
        local.read_evidence(root, run_id, "../../omnilude.sqlite3")
    secret = tmp_path / "outside.txt"
    secret.write_text("outside")
    (root / "records" / run_id / "outside").symlink_to(secret)
    with pytest.raises(ValueError, match="symlink"):
        local.read_evidence(root, run_id, "outside")


def test_agent_account_cannot_become_user_approval(experiment):
    root, identifier = experiment()
    run_id, _ = complete(root, identifier)
    with pytest.raises(ValueError, match="Record"):
        local.record(root, run_id, "approval", {"actor": "user"})
    local.record(
        root, run_id, "interpretation", {"actor": "user", "text": "claim"}
    )
    action = local.report(root, run_id)["agent_accounts"][-1]
    assert action["payload"]["actor"] == "assistant"
    assert action["payload"]["content"]["actor"] == "user"


def test_ps_unavailable_does_not_lose_process_cleanup(experiment, monkeypatch):
    root, identifier = experiment()
    original = local.subprocess.run

    def run(args, **kwargs):
        if args[0] == "ps":
            raise PermissionError("No process listing")
        return original(args, **kwargs)

    monkeypatch.setattr(local.subprocess, "run", run)
    run_id, report = complete(root, identifier)
    assert report["metrics"] == {"value": 42}
    assert not (root / "records" / run_id / "process.json").exists()


def test_input_mutation_is_reported(experiment, tmp_path):
    data = tmp_path / "data.txt"
    data.write_text("input")
    root, identifier = experiment(
        "import os\nfrom pathlib import Path\n"
        "(Path(os.environ['OMNILUDE_INPUTS_DIR'])/'sample/data.txt')"
        ".write_text('modified')\n"
        "Path('output/metrics.json').write_text('{\"value\":42}')\n",
        inputs=[
            {
                "name": "sample",
                "path": str(data),
                "role": "development",
                "usage": "read only",
            }
        ],
        data_policy="Read the declared input without changing it",
    )
    _, report = complete(root, identifier)
    assert (
        next(row for row in report["rows"] if row["id"] == "inputs")["status"]
        == "failed"
    )
    assert data.read_text() == "input"
