import json
import sqlite3

import pytest

from omnilude import core, store
from omnilude.research_spec import ResearchSpec


@pytest.fixture
def retained_run(tmp_path):
    store.initialize(tmp_path)
    spec = ResearchSpec(
        title="Record integrity",
        question="Does the retained output match its sealed record?",
        prediction="The bytes will remain unchanged.",
        metrics=[{"name": "value", "definition": "Recorded integer"}],
        researcher_source="Software verification fixture.",
        after_agent_suggestions=True,
    )
    experiment = store.create_experiment(tmp_path, spec)
    store.freeze_spec(tmp_path, experiment, "Write an output and retain it.")
    run_id = store.new_run(tmp_path, experiment, harness="local")
    store.update_run(tmp_path, run_id, "running")
    location = core.run_path(tmp_path, run_id)
    execution = location / "execution"
    execution.mkdir(parents=True)
    (execution / "output.json").write_text('{"value":42}')
    (execution / "empty").mkdir()
    store.append_event(
        tmp_path, run_id, "harness", "session", {"phase": "start"}
    )
    return tmp_path, run_id, execution


def seal(retained_run):
    root, run_id, execution = retained_run
    core.seal_run(root, run_id, {"writers_stopped": True})
    assert core.verify(root, run_id) == []
    return root, run_id, execution


def test_sealing_requires_stopped_writers(retained_run):
    root, run_id, execution = retained_run
    with pytest.raises(ValueError, match="writers stopped"):
        core.seal_run(root, run_id, {})
    assert store.get_run(root, run_id)["state"] == "running"
    assert not (execution.parent / "seal.json").exists()
    assert core.verify(root, run_id) == ["Run has not been sealed"]


@pytest.mark.parametrize("mutation", ["content", "mode", "directory", "extra"])
def test_seal_detects_artifact_mutations(retained_run, mutation):
    root, run_id, execution = seal(retained_run)
    output = execution / "output.json"
    if mutation == "content":
        output.write_text('{"value":43}')
    elif mutation == "mode":
        output.chmod(output.stat().st_mode ^ 0o100)
    elif mutation == "directory":
        (execution / "empty").rmdir()
    else:
        (execution / "unexpected").write_text("new")
    assert core.verify(root, run_id)


def test_seal_file_tampering_cannot_replace_stored_digest(retained_run):
    root, run_id, execution = seal(retained_run)
    receipt = execution.parent / "seal.json"
    value = json.loads(receipt.read_text())
    value["event_cutoff"] += 1
    receipt.write_text(json.dumps(value))
    assert core.verify(root, run_id) == [
        "Seal digest differs from the recorded seal"
    ]


def test_seal_checks_original_event_prefix_not_later_events(
    retained_run,
):
    root, run_id, _ = seal(retained_run)
    store.append_event(root, run_id, "harness", "error", {"note": "Later"})
    assert core.verify(root, run_id) == []
    with sqlite3.connect(root / "omnilude.sqlite3") as db:
        db.execute("DROP TRIGGER events_update")
        db.execute(
            "UPDATE events SET payload = ? WHERE run_id = ? AND seq = 1",
            ('{"phase":"changed"}', run_id),
        )
    assert core.verify(root, run_id) == [
        "Execution events differ from the sealed prefix"
    ]


def test_package_hash_tracks_nested_python_sources(tmp_path):
    (tmp_path / "module.py").write_text("value = 1\n")
    before = core.omnilude_hash(tmp_path)
    nested = tmp_path / "nested"
    nested.mkdir()
    source = nested / "module.py"
    source.write_text("value = 2\n")
    assert core.omnilude_hash(tmp_path) != before
    changed = core.omnilude_hash(tmp_path)
    source.write_text("value = 3\n")
    assert core.omnilude_hash(tmp_path) != changed
