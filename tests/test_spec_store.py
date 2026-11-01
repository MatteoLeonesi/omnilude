import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from omnilude import store
from omnilude.research_spec import ResearchSpec
from omnilude.spec import canonical_json, digest


@pytest.fixture
def spec():
    return ResearchSpec(
        title="Research probe",
        question="Does the calculation produce the expected value?",
        prediction="The result should be 3.",
        metrics=[{"name": "value", "definition": "Calculated value"}],
        researcher_source="I predict 3; measure the calculated value.",
        after_agent_suggestions=False,
    )


@pytest.fixture
def root(tmp_path):
    store.initialize(tmp_path)
    return tmp_path


def frozen_run(root, spec):
    experiment_id = store.create_experiment(root, spec)
    store.freeze_spec(
        root, experiment_id, "Calculate and record the value once."
    )
    return experiment_id, store.new_run(root, experiment_id)


@pytest.mark.parametrize(
    "change",
    [
        {"prediction": " "},
        {"prediction": {"low": 0.8, "high": 1}},
        {"metrics": []},
        {"metrics": [{"name": "value", "definition": "Value"}] * 2},
        {"timeout_seconds": 0},
        {"timeout_seconds": True},
        {"timeout_seconds": "120"},
        {"max_runs": 0},
        {"max_runs": True},
        {"max_runs": "2"},
        {"command": [" "]},
        {"question": " "},
        {"budget_usd": float("inf")},
        {"host_path": "/private/labels"},
        {"kind": "unsupported"},
        {"schema_version": 2},
        {"after_agent_suggestions": "false"},
    ],
)
def test_invalid_specs(spec, change):
    with pytest.raises(ValidationError):
        ResearchSpec.model_validate(spec.model_dump() | change)


@pytest.mark.parametrize(
    "identifier",
    ["integrity", "execution", "outputs", "method", "completeness"],
)
def test_assumptions_cannot_replace_verification_rows(spec, identifier):
    with pytest.raises(ValidationError, match="reserved"):
        ResearchSpec.model_validate(
            spec.model_dump()
            | {
                "assumptions": [
                    {
                        "id": identifier,
                        "statement": "Statement",
                        "warrant": "Warrant",
                        "check": "Review evidence",
                        "expected": "Expected",
                        "author": "researcher",
                    }
                ]
            }
        )


def test_canonical_hash_is_order_independent():
    assert canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    with pytest.raises(ValueError):
        canonical_json({"a": float("nan")})


def test_freeze_approval_is_exact_and_immutable(root, spec):
    full = ResearchSpec.model_validate(spec.model_dump() | {"tier": "full"})
    identifier = store.create_experiment(root, full)
    store.save_draft(root, identifier, full)
    with pytest.raises(ValueError, match="approval"):
        store.freeze_spec(root, identifier, "Method")
    with pytest.raises(ValueError, match="nonempty"):
        store.freeze_spec(root, identifier, " ", approved=True)
    frozen = store.freeze_spec(
        root, identifier, "Method", approved=True, data_hashes={"final": "abc"}
    )
    assert frozen["frozen_hash"] == digest(
        {
            "spec": full.model_dump(),
            "method": "Method",
            "data_hashes": {"final": "abc"},
            "workspace_hashes": {},
        }
    )
    with sqlite3.connect(root / "omnilude.sqlite3") as db:
        approval = json.loads(
            db.execute(
                "SELECT payload FROM actions WHERE kind = 'freeze'"
            ).fetchone()[0]
        )
        assert approval["frozen_hash"] == frozen["frozen_hash"]
        assert approval["approved"] is True
        assert approval["workspace_hashes"] == {}
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("UPDATE specs SET method = 'Other'")
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            db.execute("DELETE FROM specs")
    with pytest.raises(ValueError, match="cannot be edited"):
        store.save_draft(root, identifier, spec)
    with pytest.raises(ValueError, match="already frozen"):
        store.freeze_spec(root, identifier, "Other", approved=True)
    assert store.get_experiment(root, identifier) == frozen
    assert store.list_experiments(root) == [frozen]


def test_frozen_hash_binds_data(root, spec):
    identifiers = [store.create_experiment(root, spec) for _ in range(2)]
    hashes = [
        store.freeze_spec(
            root,
            identifier,
            "Method",
            data_hashes={
                "final": str(index),
            },
        )["frozen_hash"]
        for index, identifier in enumerate(identifiers)
    ]
    assert hashes[0] != hashes[1]


def test_freeze_records_workspace_and_time_for_both_tiers(root, spec):
    hashes = []
    for tier, approved in (("light", False), ("full", True)):
        updated = ResearchSpec.model_validate(
            spec.model_dump() | {"tier": tier}
        )
        identifier = store.create_experiment(root, updated)
        workspace = {"main.py": f"{tier}-sha256"}
        frozen = store.freeze_spec(
            root,
            identifier,
            "Method",
            approved=approved,
            workspace_hashes=workspace,
        )
        actions = store.experiment_actions(root, identifier)
        assert len(actions) == 1
        assert actions[0]["kind"] == "freeze"
        assert actions[0]["created_at"]
        assert actions[0]["payload"] == {
            "frozen_hash": frozen["frozen_hash"],
            "method_hash": digest("Method"),
            "workspace_hashes": workspace,
            "approved": approved,
            "approval": None,
            "recorded_by": "assistant",
            "identity_verified": False,
        }
        hashes.append(frozen["frozen_hash"])
    assert hashes[0] != hashes[1]


def test_frozen_hash_binds_workspace(root, spec):
    hashes = [
        store.freeze_spec(
            root,
            store.create_experiment(root, spec),
            "Method",
            workspace_hashes={"main.py": content},
        )["frozen_hash"]
        for content in ("before", "after")
    ]
    assert hashes[0] != hashes[1]


def test_run_transitions_and_sealed_manifest(root, spec):
    spec = ResearchSpec.model_validate(spec.model_dump() | {"max_runs": 2})
    identifier = store.create_experiment(root, spec)
    with pytest.raises(ValueError, match="freeze"):
        store.new_run(root, identifier)
    store.freeze_spec(root, identifier, "Method")
    run_id = store.new_run(root, identifier)
    with pytest.raises(ValueError, match="active"):
        store.new_run(root, identifier)
    with pytest.raises(ValueError, match="transition"):
        store.update_run(root, run_id, "checked")
    store.update_run(root, run_id, "running", {"worker_pid": 42})
    store.update_run(root, run_id, "running", {"worker_pid": 43})
    store.update_run(root, run_id, "sealed", {"outcome": "failed"})
    with pytest.raises(ValueError, match="immutable"):
        store.update_run(root, run_id, "checked", {"outcome": "passed"})
    store.update_run(root, run_id, "checked")
    with pytest.raises(ValueError, match="transition"):
        store.update_run(root, run_id, "decided")
    second_run = store.new_run(root, identifier)
    assert store.get_run(root, run_id)["state"] == "checked"
    assert store.get_run(root, run_id)["manifest"] == {"outcome": "failed"}
    assert store.get_run(root, second_run)["state"] == "created"
    assert [run["id"] for run in store.list_runs(root, identifier)] == [
        run_id,
        second_run,
    ]


def test_events_are_sequential_and_append_only(root, spec):
    _, run_id = frozen_run(root, spec)
    with ThreadPoolExecutor(max_workers=8) as pool:
        sequences = list(
            pool.map(
                lambda i: store.append_event(
                    root,
                    run_id,
                    "harness",
                    "tool_result",
                    {"index": i},
                    {"path": "execution/command/stdout.log", "line": i + 1},
                ),
                range(24),
            )
        )
    assert sorted(sequences) == list(range(1, 25))
    records = store.events(root, run_id)
    assert [event["seq"] for event in records] == list(range(1, 25))
    assert {event["payload"]["index"] for event in records} == set(range(24))
    assert records[0]["raw_ref"]["path"] == "execution/command/stdout.log"
    with sqlite3.connect(root / "omnilude.sqlite3") as db:
        for command in ("DELETE FROM events", "UPDATE events SET seq = 99"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(command)
    with pytest.raises(ValueError, match="witness"):
        store.append_event(root, run_id, "unverified", "tool_result", {})
    with pytest.raises(ValueError, match="event kind"):
        store.append_event(root, run_id, "harness", "arbitrary", {})


def test_actions_cannot_cross_experiments_or_change(root, spec):
    identifier, run_id = frozen_run(root, spec)
    other = store.create_experiment(root, spec)
    with pytest.raises(ValueError, match="another experiment"):
        store.append_action(root, other, run_id, "agent_account", {})
    action_id = store.append_action(
        root,
        identifier,
        run_id,
        "agent_account",
        {
            "run_ids": [run_id],
        },
    )
    assert [
        action["id"]
        for action in store.actions(root, run_id)
        if action["kind"] == "agent_account"
    ] == [action_id]
    with sqlite3.connect(root / "omnilude.sqlite3") as db:
        for command in ("DELETE FROM actions", "UPDATE actions SET kind='x'"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(command)


def test_evaluation_is_reserved_once_even_concurrently(root, spec):
    _, run_id = frozen_run(root, spec)
    with pytest.raises(ValueError, match="seal"):
        store.reserve_evaluation(root, run_id)
    store.update_run(root, run_id, "running")
    store.update_run(root, run_id, "sealed")
    with ThreadPoolExecutor(max_workers=8) as pool:
        reservations = list(
            pool.map(
                lambda _: store.reserve_evaluation(root, run_id), range(24)
            )
        )
    assert sum(reservations) == 1
    assert store.get_evaluation(root, run_id)["state"] == "reserved"
    store.initialize(root)
    assert not store.reserve_evaluation(root, run_id)
    assert store.get_evaluation(root, run_id)["result"] is None
    store.finish_evaluation(root, run_id, {"value": 3})
    assert store.get_evaluation(root, run_id)["result"] == {"value": 3}
    assert store.get_run(root, run_id)["state"] == "checked"
    assert not store.reserve_evaluation(root, run_id)
    with pytest.raises(ValueError, match="unfinished"):
        store.finish_evaluation(root, run_id, {"value": 4})
    with (
        sqlite3.connect(root / "omnilude.sqlite3") as db,
        pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"),
    ):
        db.execute("DELETE FROM evaluations")


def test_evaluation_and_checked_state_commit_atomically(root, spec):
    _, run_id = frozen_run(root, spec)
    store.update_run(root, run_id, "running")
    store.update_run(root, run_id, "sealed")
    assert store.reserve_evaluation(root, run_id)
    with sqlite3.connect(root / "omnilude.sqlite3") as db:
        db.execute("""
            CREATE TRIGGER reject_checked BEFORE UPDATE OF state ON runs
            WHEN NEW.state = 'checked'
            BEGIN SELECT RAISE(ABORT, 'simulated state write failure'); END
        """)
    with pytest.raises(sqlite3.IntegrityError, match="simulated"):
        store.finish_evaluation(root, run_id, {"value": 3})
    assert store.get_run(root, run_id)["state"] == "sealed"
    assert store.get_evaluation(root, run_id)["state"] == "reserved"
    assert store.get_evaluation(root, run_id)["result"] is None
    with sqlite3.connect(root / "omnilude.sqlite3") as db:
        db.execute("DROP TRIGGER reject_checked")
    store.finish_evaluation(root, run_id, {"value": 3})
    assert store.get_run(root, run_id)["state"] == "checked"
    assert store.get_evaluation(root, run_id)["state"] == "completed"


def test_missing_ids_raise_value_errors(root):
    for function in (
        store.get_experiment,
        store.get_run,
        store.events,
        store.actions,
        store.experiment_actions,
        store.reserve_evaluation,
        store.get_evaluation,
    ):
        with pytest.raises(ValueError, match="unknown"):
            function(root, "missing")


def test_store_revalidates_mutated_model(root, spec):
    spec.metrics.clear()
    with pytest.raises(ValidationError):
        store.create_experiment(root, spec)


@pytest.mark.parametrize(
    "manifest",
    [
        {},
        {"exit_code": 0, "stop_reason": "completed"},
        {"exit_code": 1, "stop_reason": "failed"},
        {"exit_code": 137, "stop_reason": "timeout"},
        {"exit_code": 1, "stop_reason": "infrastructure_error"},
        {"exit_code": None, "stop_reason": "interrupted"},
        {"exit_code": 1, "stop_reason": "cancelled"},
    ],
)
def test_run_cap_counts_every_attempt(root, spec, manifest):
    identifier, run_id = frozen_run(root, spec)
    store.update_run(root, run_id, "running")
    store.update_run(root, run_id, "sealed", manifest)
    with pytest.raises(ValueError, match=r"run cap reached \(1 of 1\)"):
        store.new_run(root, identifier)
    assert len(store.list_runs(root, identifier)) == 1


def test_actions_preserve_insertion_order_when_times_tie(
    root, spec, monkeypatch
):
    monkeypatch.setattr(store, "_now", lambda: "2026-10-04T12:00:00+00:00")
    identifier, run_id = frozen_run(root, spec)
    other, other_run = frozen_run(root, spec)
    first = store.append_action(
        root, identifier, run_id, "agent_account", {"kind": "observation"}
    )
    second = store.append_action(root, identifier, None, "draft_revision", {})
    third = store.append_action(
        root, identifier, run_id, "agent_account", {"kind": "interpretation"}
    )
    store.append_action(root, other, other_run, "agent_account", {})
    actions = store.actions(root, run_id)
    assert [action["id"] for action in actions[1:]] == [first, second, third]
    assert [
        action["id"] for action in store.experiment_actions(root, identifier)
    ] == [
        actions[0]["id"],
        second,
    ]


def test_nonlocal_launch_is_rejected_without_allocating_run(root, spec):
    identifier = store.create_experiment(root, spec)
    store.freeze_spec(root, identifier, "Method")
    with pytest.raises(ValueError, match="Only local research"):
        store.new_run(root, identifier, harness="unsupported")
    assert store.list_runs(root, identifier) == []


def test_unknown_record_types_remain_untouched_and_unsupported(root, spec):
    identifier = store.create_experiment(root, spec)
    content = '{"kind":"unsupported","untouched":true}'
    with sqlite3.connect(root / "omnilude.sqlite3") as db:
        db.execute(
            "INSERT INTO experiments VALUES (?, 'draft', ?)",
            ("other", "2026-01-01"),
        )
        db.execute(
            "INSERT INTO specs (experiment_id,content,spec_hash) "
            "VALUES (?, ?, ?)",
            ("other", content, "stored-hash"),
        )
        db.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("other-run", "other", "sealed", "unsupported", "{}", "1", "1"),
        )
    assert [item["id"] for item in store.list_experiments(root)] == [
        identifier
    ]
    assert store.list_runs(root) == []
    with pytest.raises(ValueError, match="Unsupported experiment type"):
        store.get_experiment(root, "other")
    with pytest.raises(ValueError, match="Unsupported run type"):
        store.get_run(root, "other-run")
    with pytest.raises(ValueError, match="Unsupported experiment type"):
        store.save_draft(root, "other", spec)
    store.initialize(root)
    with sqlite3.connect(root / "omnilude.sqlite3") as db:
        assert db.execute(
            "SELECT content,spec_hash FROM specs WHERE experiment_id='other'"
        ).fetchone() == (content, "stored-hash")
        assert db.execute(
            "SELECT state,harness,manifest FROM runs WHERE id='other-run'"
        ).fetchone() == ("sealed", "unsupported", "{}")


def test_reopening_research_store_preserves_serialized_protocol(root, spec):
    identifier, run_id = frozen_run(root, spec)
    before = store.get_experiment(root, identifier)
    expected = canonical_json(spec)
    expected_hash = digest(spec)
    store.initialize(root)
    assert store.get_experiment(root, identifier) == before
    assert store.get_run(root, run_id)["harness"] == "local"
    with sqlite3.connect(root / "omnilude.sqlite3") as db:
        assert db.execute(
            "SELECT content,spec_hash FROM specs WHERE experiment_id=?",
            (identifier,),
        ).fetchone() == (expected, expected_hash)
