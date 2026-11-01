import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from omnilude import research, store
from omnilude.research_spec import ResearchSpec


@pytest.fixture
def workspace(tmp_path):
    directory = tmp_path / "project"
    directory.mkdir()
    (directory / "main.py").write_text("print(3)\n")
    return directory


@pytest.fixture
def root(tmp_path):
    return tmp_path / "records"


@pytest.fixture
def draft():
    return {
        "title": "Research probe",
        "question": "Does the calculation give 3?",
        "prediction": "The measured value will equal 3.",
        "metrics": [{"name": "value", "definition": "Computed value"}],
        "researcher_source": "I predict 3. Please report the computed value.",
        "researcher_reference": "chat:1",
        "after_agent_suggestions": False,
    }


@pytest.fixture
def complete(draft):
    return draft | {
        "workspace_paths": ["main.py"],
        "command": ["python", "main.py"],
        "data_policy": "No external dataset.",
        "selection_rule": "Report all attempts.",
        "success_criteria": "The computed value equals 3.",
        "constraints": [],
        "stopping_rule": "One attempt within 60 seconds.",
    }


@pytest.fixture
def proposal():
    return {
        "id": "numeric_contract",
        "statement": "The output is a finite numeric value.",
        "warrant": "The checker rejects nonnumeric and nonfinite output.",
        "check": "Run the numeric output checker.",
        "expected": "Exit code zero.",
        "check_command": ["python", "check.py"],
        "author": "agent",
    }


@pytest.mark.parametrize(
    "field",
    [
        "question",
        "prediction",
        "metrics",
        "researcher_source",
        "after_agent_suggestions",
    ],
)
def test_researcher_input_is_required_before_draft(
    root, workspace, draft, field
):
    draft.pop(field)
    with pytest.raises(ValidationError):
        research.create(root, draft, workspace)
    assert not root.exists()


def test_draft_and_prior_suggestions_are_retained(root, workspace, draft):
    draft["after_agent_suggestions"] = True
    experiment = research.create(root, draft, workspace)
    assert experiment["spec"]["after_agent_suggestions"] is True
    actions = store.experiment_actions(root, experiment["id"])
    assert (
        actions[0]["payload"]["input"]["researcher_source"]
        == draft["researcher_source"]
    )
    assert actions[0]["payload"]["recorded_by"] == "assistant"
    assert actions[0]["payload"]["identity_verified"] is False
    with pytest.raises(ValueError, match="data_policy"):
        research.freeze(root, experiment["id"], "Print the result.")
    with pytest.raises(ValueError, match="freeze"):
        store.new_run(root, experiment["id"], harness="local")


def test_prelude_cannot_be_silently_replaced(root, workspace, draft):
    experiment = research.create(root, draft, workspace)
    changed = experiment["spec"] | {"prediction": "Four instead."}
    with pytest.raises(ValueError, match="new source"):
        research.update(root, experiment["id"], changed)
    changed["researcher_reference"] = "chat:2"
    updated = research.update(root, experiment["id"], changed)
    assert updated["spec"]["prediction"] == "Four instead."
    actions = store.experiment_actions(root, experiment["id"])
    assert (
        actions[-1]["payload"]["previous_spec"]["prediction"]
        == draft["prediction"]
    )


def test_prior_suggestions_cannot_be_erased(root, workspace, draft):
    draft["after_agent_suggestions"] = True
    identifier = research.create(root, draft, workspace)["id"]
    draft |= {
        "after_agent_suggestions": False,
        "researcher_reference": "chat:2",
    }
    with pytest.raises(ValueError, match="Prior agent suggestions"):
        research.update(root, identifier, draft)


def test_agent_proposal_requires_acceptance_bound_to_content(
    root, workspace, complete, proposal
):
    complete["assumptions"] = [proposal]
    experiment = research.create(root, complete, workspace)
    identifier = experiment["id"]
    preview = research.preview(root, identifier, "Print the result.")
    assert preview["ready"] is False
    with pytest.raises(ValueError, match="needs user acceptance"):
        research.freeze(root, identifier, "Print the result.")
    proposal["acceptance"] = {
        "statement": "I accept this assumption and check.",
        "reference": "chat:3",
        "subject_hash": preview["assumption_hashes"][proposal["id"]],
    }
    research.update(root, identifier, complete)
    proposal["expected"] = "A different expectation."
    research.update(root, identifier, complete)
    with pytest.raises(ValueError, match="stale"):
        research.freeze(root, identifier, "Print the result.")
    proposal["expected"] = "Exit code zero."
    research.update(root, identifier, complete)
    frozen = research.freeze(root, identifier, "Print the result.")
    assert frozen["state"] == "frozen"
    revision = research.fork(root, identifier)
    assert (
        revision["spec"]["assumptions"][0]["acceptance"]
        == proposal["acceptance"]
    )


def test_agent_authorship_cannot_be_silently_replaced(
    root, workspace, complete, proposal
):
    complete["assumptions"] = [proposal]
    identifier = research.create(root, complete, workspace)["id"]
    proposal["author"] = "researcher"
    with pytest.raises(ValueError, match="relabeled"):
        research.update(root, identifier, complete)


def test_light_freeze_needs_no_method_approval(root, workspace, complete):
    experiment = research.create(root, complete, workspace)
    frozen = research.freeze(root, experiment["id"], "Print the result.")
    assert frozen["state"] == "frozen"
    assert research.verify_frozen(root, experiment["id"]) == []
    with pytest.raises(ValueError, match="frozen"):
        research.update(root, experiment["id"], complete)


@pytest.mark.parametrize("receipt", [[], None, 42, "invalid receipt"])
def test_malformed_freeze_receipt_returns_integrity_finding(
    root, workspace, complete, receipt
):
    identifier = research.create(root, complete, workspace)["id"]
    research.freeze(root, identifier, "Print the result.")
    (root / "experiments" / identifier / "frozen.json").write_text(
        json.dumps(receipt)
    )
    assert research.verify_frozen(root, identifier) == [
        "Cannot verify frozen experiment: Frozen receipt must be an object"
    ]


def test_missing_freeze_action_returns_integrity_finding(
    root, workspace, complete, monkeypatch
):
    identifier = research.create(root, complete, workspace)["id"]
    research.freeze(root, identifier, "Print the result.")
    actions = store.experiment_actions(root, identifier)
    monkeypatch.setattr(
        store,
        "experiment_actions",
        lambda *args: [
            action for action in actions if action["kind"] != "freeze"
        ],
    )
    assert research.verify_frozen(root, identifier) == [
        "Freeze action missing from the record"
    ]


def test_full_approval_binds_method_code_and_spec(root, workspace, complete):
    complete |= {
        "tier": "full",
        "known_outcome_check": "The exact arithmetic result is 3.",
    }
    identifier = research.create(root, complete, workspace)["id"]
    preview = research.preview(root, identifier, "Print the result.")
    with pytest.raises(ValueError, match="requires approval"):
        research.freeze(root, identifier, "Print the result.")
    approval = {
        "statement": "Approved.",
        "reference": "chat:2",
        "subject_hash": preview["protocol_hash"],
    }
    with pytest.raises(ValueError, match="stale"):
        research.freeze(root, identifier, "Print the value twice.", approval)
    (workspace / "main.py").write_text("print(4)\n")
    with pytest.raises(ValueError, match="stale"):
        research.freeze(root, identifier, "Print the result.", approval)
    approval["subject_hash"] = research.preview(
        root, identifier, "Print the result."
    )["protocol_hash"]
    research.freeze(root, identifier, "Print the result.", approval)
    receipt = json.loads(
        (root / "experiments" / identifier / "frozen.json").read_text()
    )
    assert receipt["approval"] == approval
    assert receipt["identity_verified"] is False
    freeze_action = store.experiment_actions(root, identifier)[-1]
    assert freeze_action["payload"]["approval"] == approval
    receipt["approval"]["statement"] = "Altered approval."
    (root / "experiments" / identifier / "frozen.json").write_text(
        json.dumps(receipt)
    )
    assert "Frozen approval record changed" in research.verify_frozen(
        root, identifier
    )


def test_confirmation_requires_protocol_and_new_data_after_results(
    root, workspace, complete
):
    complete["mode"] = "confirmation"
    identifier = research.create(root, complete, workspace)["id"]
    with pytest.raises(ValueError, match="hypothesis and analysis_plan"):
        research.freeze(root, identifier, "Print the result.")
    complete |= {
        "hypothesis": "The result is 3.",
        "analysis_plan": "Compare exact integer output.",
    }
    research.update(root, identifier, complete)
    research.freeze(root, identifier, "Print the result.")
    store.new_run(root, identifier, harness="local")
    revision = research.fork(root, identifier)
    assert revision["spec"]["mode"] == "exploration"
    changed = revision["spec"] | {"mode": "confirmation"}
    research.update(root, revision["id"], changed)
    with pytest.raises(ValueError, match="fresh-data"):
        research.freeze(root, revision["id"], "Print the result.")
    changed["fresh_data_statement"] = (
        "This is a new declared independent input set."
    )
    research.update(root, revision["id"], changed)
    research.freeze(root, revision["id"], "Print the result.")
    assert research.get(root, identifier)["state"] == "frozen"


def test_declared_snapshot_excludes_caches_and_preserves_input_identity(
    root, workspace, complete
):
    (workspace / ".venv").mkdir()
    (workspace / ".venv" / "python").symlink_to("/bin/sh")
    (workspace / ".env").write_text("SECRET=not-in-snapshot")
    (workspace / "data.csv").write_text("value\n3\n")
    complete["workspace_paths"] = ["."]
    complete["inputs"] = [
        {
            "name": "sample",
            "path": "data.csv",
            "role": "evaluation",
            "usage": "Use all rows.",
        }
    ]
    identifier = research.create(root, complete, workspace)["id"]
    research.freeze(root, identifier, "Print the result.")
    location = root / "experiments" / identifier
    assert not (location / "workspace" / ".venv").exists()
    assert not (location / "workspace" / ".env").exists()
    assert (
        location / "inputs" / "sample" / "data.csv"
    ).read_text() == "value\n3\n"
    (workspace / "main.py").write_text("print(4)\n")
    assert research.verify_frozen(root, identifier) == []
    (location / "workspace" / "main.py").write_text("print(5)\n")
    assert "Frozen workspace changed" in research.verify_frozen(
        root, identifier
    )


@pytest.mark.parametrize("path", ["../outside", "/tmp/outside", ".env"])
def test_workspace_paths_cannot_escape_or_select_secrets(
    root, workspace, complete, path
):
    complete["workspace_paths"] = [path]
    identifier = research.create(root, complete, workspace)["id"]
    with pytest.raises(ValueError):
        research.freeze(root, identifier, "Print the result.")


def test_workspace_symlinks_are_rejected(root, workspace, complete):
    (workspace / "linked").symlink_to(workspace / "main.py")
    complete["workspace_paths"] = ["linked"]
    identifier = research.create(root, complete, workspace)["id"]
    with pytest.raises(ValueError, match="Symlinks"):
        research.freeze(root, identifier, "Print the result.")


def test_input_symlink_ancestor_is_rejected(root, workspace, complete):
    (workspace / "data").mkdir()
    (workspace / "data" / "sample.csv").write_text("3\n")
    (workspace / "link").symlink_to(
        workspace / "data", target_is_directory=True
    )
    complete["inputs"] = [
        {
            "name": "sample",
            "path": "link/sample.csv",
            "role": "evaluation",
            "usage": "Use all data.",
        }
    ]
    identifier = research.create(root, complete, workspace)["id"]
    with pytest.raises(ValueError, match="Symlinks"):
        research.freeze(root, identifier, "Print the result.")


def test_input_directory_cannot_include_run_store(workspace, complete):
    root = workspace / ".omnilude"
    complete["inputs"] = [
        {
            "name": "sample",
            "path": ".",
            "role": "evaluation",
            "usage": "All data.",
        }
    ]
    identifier = research.create(root, complete, workspace)["id"]
    with pytest.raises(ValueError, match="separate from the run store"):
        research.freeze(root, identifier, "Print the result.")


def test_empty_workspace_and_inputs_can_freeze(root, workspace, complete):
    complete["workspace_paths"] = []
    complete["command"] = ["python", "-c", "print(3)"]
    identifier = research.create(root, complete, workspace)["id"]
    research.freeze(root, identifier, "Print the result.")
    assert research.verify_frozen(root, identifier) == []


def test_all_research_attempts_count_toward_cap(root, workspace, complete):
    identifier = research.create(root, complete, workspace)["id"]
    research.freeze(root, identifier, "Print the result.")
    run_id = store.new_run(root, identifier, harness="local")
    store.update_run(
        root,
        run_id,
        "running",
        {"stop_reason": "infrastructure_error", "exit_code": 1},
    )
    store.update_run(root, run_id, "sealed")
    with pytest.raises(ValueError, match="run cap reached"):
        store.new_run(root, identifier, harness="local")


def test_light_assumption_limit_and_general_numeric_metric_names(
    complete, proposal
):
    complete["metrics"] = [
        {
            "name": "duration_ms",
            "definition": "Elapsed milliseconds",
            "unit": "ms",
        }
    ]
    assert (
        ResearchSpec.model_validate(complete).metrics[0].name == "duration_ms"
    )
    complete["assumptions"] = [
        deepcopy(proposal) | {"id": f"check{number}"} for number in range(4)
    ]
    with pytest.raises(ValidationError, match="three assumptions"):
        ResearchSpec.model_validate(complete)
