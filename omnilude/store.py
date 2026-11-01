import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from omnilude.research_spec import ResearchSpec
from omnilude.spec import canonical_json, digest

TRANSITIONS = {
    "created": "running",
    "running": "sealed",
    "sealed": "checked",
}


def _now():
    return datetime.now(UTC).isoformat()


@contextmanager
def _db(root, write=False):
    connection = sqlite3.connect(Path(root) / "omnilude.sqlite3", timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
    finally:
        connection.close()


def initialize(root):
    Path(root).mkdir(parents=True, exist_ok=True)
    with _db(root) as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS experiments (
                id TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS specs (
                experiment_id TEXT PRIMARY KEY REFERENCES experiments(id),
                content TEXT NOT NULL, method TEXT, spec_hash TEXT NOT NULL,
                frozen_hash TEXT, data_hashes TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL REFERENCES experiments(id),
                state TEXT NOT NULL, harness TEXT NOT NULL,
                manifest TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_run
                ON runs(experiment_id) WHERE state IN ('created', 'running');
            CREATE TABLE IF NOT EXISTS events (
                run_id TEXT NOT NULL REFERENCES runs(id), seq INTEGER NOT NULL,
                ts TEXT NOT NULL, witness TEXT NOT NULL, kind TEXT NOT NULL,
                payload TEXT NOT NULL, raw_ref TEXT,
                PRIMARY KEY(run_id, seq)
            );
            CREATE TABLE IF NOT EXISTS actions (
                id TEXT PRIMARY KEY,
                experiment_id TEXT NOT NULL REFERENCES experiments(id),
                run_id TEXT REFERENCES runs(id), kind TEXT NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS evaluations (
                run_id TEXT PRIMARY KEY REFERENCES runs(id),
                state TEXT NOT NULL, result TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TRIGGER IF NOT EXISTS frozen_spec_update
                BEFORE UPDATE ON specs WHEN OLD.frozen_hash IS NOT NULL
                BEGIN SELECT RAISE(ABORT, 'frozen spec is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS frozen_spec_delete
                BEFORE DELETE ON specs WHEN OLD.frozen_hash IS NOT NULL
                BEGIN SELECT RAISE(ABORT, 'frozen spec is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS sealed_manifest_update
                BEFORE UPDATE OF manifest ON runs
                WHEN OLD.state IN ('sealed', 'checked')
                    AND NEW.manifest != OLD.manifest
                BEGIN SELECT RAISE(ABORT, 'sealed manifest is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS completed_evaluation_update
                BEFORE UPDATE ON evaluations WHEN OLD.state = 'completed'
                BEGIN SELECT RAISE(ABORT, 'evaluation is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS evaluation_delete
                BEFORE DELETE ON evaluations
                BEGIN SELECT RAISE(ABORT, 'evaluation cannot be deleted'); END;
        """)
        for table in ("events", "actions"):
            for operation in ("UPDATE", "DELETE"):
                db.execute(f"""
                    CREATE TRIGGER IF NOT EXISTS {table}_{operation.lower()}
                    BEFORE {operation} ON {table}
                    BEGIN SELECT RAISE(ABORT, 'append-only table'); END
                """)


def _required(db, table, identifier):
    row = db.execute(
        f"SELECT * FROM {table} WHERE id = ?", (identifier,)
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown {table} ID: {identifier}")
    if table == "runs" and row["harness"] != "local":
        raise ValueError("Unsupported run type")
    return row


def _experiment(db, identifier):
    item = dict(_required(db, "experiments", identifier))
    spec = db.execute(
        "SELECT * FROM specs WHERE experiment_id = ?", (identifier,)
    ).fetchone()
    content = json.loads(spec["content"])
    if content.get("kind") != "research":
        raise ValueError("Unsupported experiment type")
    item.update(
        {
            "spec": content,
            "method": spec["method"],
            "spec_hash": spec["spec_hash"],
            "frozen_hash": spec["frozen_hash"],
            "data_hashes": json.loads(spec["data_hashes"]),
        }
    )
    return item


def create_experiment(root, spec):
    spec = ResearchSpec.model_validate(spec)
    identifier = uuid4().hex
    with _db(root, True) as db:
        db.execute(
            "INSERT INTO experiments VALUES (?, 'draft', ?)",
            (identifier, _now()),
        )
        db.execute(
            "INSERT INTO specs (experiment_id, content, spec_hash) "
            "VALUES (?, ?, ?)",
            (identifier, canonical_json(spec), digest(spec)),
        )
    return identifier


def get_experiment(root, identifier):
    with _db(root) as db:
        return _experiment(db, identifier)


def list_experiments(root):
    with _db(root) as db:
        ids = db.execute(
            "SELECT experiments.id FROM experiments "
            "JOIN specs ON specs.experiment_id = experiments.id "
            "WHERE json_extract(specs.content, '$.kind') = 'research' "
            "ORDER BY experiments.created_at, experiments.id"
        )
        return [_experiment(db, row["id"]) for row in ids.fetchall()]


def save_draft(root, identifier, spec):
    spec = ResearchSpec.model_validate(spec)
    with _db(root, True) as db:
        if _required(db, "experiments", identifier)["state"] != "draft":
            raise ValueError("frozen specs cannot be edited")
        previous = _experiment(db, identifier)["spec"]
        _action(
            db,
            identifier,
            None,
            "draft_revision",
            {
                "previous_spec": previous,
                "spec": spec.model_dump(),
                "recorded_by": "assistant",
                "identity_verified": False,
            },
        )
        db.execute(
            "UPDATE specs SET content = ?, spec_hash = ? "
            "WHERE experiment_id = ?",
            (canonical_json(spec), digest(spec), identifier),
        )


def freeze_spec(
    root,
    identifier,
    method,
    approved=False,
    data_hashes=None,
    workspace_hashes=None,
    expected_spec_hash=None,
    approval_record=None,
):
    if not isinstance(method, str) or not method.strip():
        raise ValueError("a nonempty method is required")
    with _db(root, True) as db:
        experiment = _experiment(db, identifier)
        if experiment["state"] != "draft":
            raise ValueError("spec is already frozen")
        if (
            expected_spec_hash is not None
            and experiment["spec_hash"] != expected_spec_hash
        ):
            raise ValueError("Draft changed while freezing")
        if experiment["spec"]["tier"] == "full" and not approved:
            raise ValueError("full tier requires approval of the exact method")
        data_hashes = data_hashes or {}
        workspace_hashes = workspace_hashes or {}
        frozen_hash = digest(
            {
                "spec": experiment["spec"],
                "method": method,
                "data_hashes": data_hashes,
                "workspace_hashes": workspace_hashes,
            }
        )
        db.execute(
            "UPDATE specs SET method = ?, frozen_hash = ?, data_hashes = ? "
            "WHERE experiment_id = ?",
            (method, frozen_hash, canonical_json(data_hashes), identifier),
        )
        db.execute(
            "UPDATE experiments SET state = 'frozen' WHERE id = ?",
            (identifier,),
        )
        _action(
            db,
            identifier,
            None,
            "freeze",
            {
                "frozen_hash": frozen_hash,
                "method_hash": digest(method),
                "workspace_hashes": workspace_hashes,
                "approved": approved,
                "approval": approval_record,
                "recorded_by": "assistant",
                "identity_verified": False,
            },
        )
        return _experiment(db, identifier)


def new_run(root, experiment_id, harness="local"):
    if harness != "local":
        raise ValueError("Only local research runs are supported")
    identifier, now = uuid4().hex, _now()
    with _db(root, True) as db:
        if _required(db, "experiments", experiment_id)["state"] != "frozen":
            raise ValueError("freeze the experiment before starting a run")
        active = db.execute(
            "SELECT id FROM runs WHERE experiment_id = ? "
            "AND state IN ('created', 'running')",
            (experiment_id,),
        ).fetchone()
        if active:
            raise ValueError("experiment already has an active run")
        spec = _experiment(db, experiment_id)["spec"]
        maximum = spec.get("max_runs", 1)
        count = db.execute(
            "SELECT COUNT(*) FROM runs WHERE experiment_id = ?",
            (experiment_id,),
        ).fetchone()[0]
        if count >= maximum:
            raise ValueError(
                f"run cap reached ({count} of {maximum}); "
                "fork the experiment to run again"
            )
        db.execute(
            "INSERT INTO runs (id, experiment_id, state, harness, created_at, "
            "updated_at) VALUES (?, ?, 'created', ?, ?, ?)",
            (identifier, experiment_id, harness, now, now),
        )
    return identifier


def _run(row):
    item = dict(row)
    item["manifest"] = json.loads(item["manifest"])
    return item


def get_run(root, run_id):
    with _db(root) as db:
        return _run(_required(db, "runs", run_id))


def list_runs(root, experiment_id=None):
    with _db(root) as db:
        query, params = "SELECT * FROM runs WHERE harness = 'local'", ()
        if experiment_id is not None:
            _experiment(db, experiment_id)
            query += " AND experiment_id = ?"
            params = (experiment_id,)
        return [
            _run(row)
            for row in db.execute(query + " ORDER BY created_at, id", params)
        ]


def update_run(root, run_id, state, manifest=None):
    with _db(root, True) as db:
        run = _required(db, "runs", run_id)
        current = run["state"]
        if state != current and TRANSITIONS.get(current) != state:
            raise ValueError(f"invalid run transition: {current} -> {state}")
        content = (
            run["manifest"] if manifest is None else canonical_json(manifest)
        )
        if current in ("sealed", "checked"):
            if content != run["manifest"]:
                raise ValueError("sealed manifest is immutable")
            if current == state:
                raise ValueError("run has already completed this transition")
        cursor = db.execute(
            "UPDATE runs SET state = ?, manifest = ?, updated_at = ? "
            "WHERE id = ? AND state = ?",
            (state, content, _now(), run_id, current),
        )
        if cursor.rowcount != 1:
            raise ValueError("run state changed concurrently")


def append_event(root, run_id, witness, kind, payload, raw_ref=None):
    if witness != "harness":
        raise ValueError("invalid event witness")
    if kind not in {"session", "tool_call", "tool_result", "error"}:
        raise ValueError("invalid event kind")
    with _db(root, True) as db:
        _required(db, "runs", run_id)
        sequence = db.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?",
            (run_id,),
        ).fetchone()[0]
        db.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                sequence,
                _now(),
                witness,
                kind,
                canonical_json(payload),
                canonical_json(raw_ref),
            ),
        )
    return sequence


def events(root, run_id):
    with _db(root) as db:
        _required(db, "runs", run_id)
        rows = db.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY seq", (run_id,)
        )
        return [
            dict(row)
            | {
                "payload": json.loads(row["payload"]),
                "raw_ref": json.loads(row["raw_ref"]),
            }
            for row in rows
        ]


def _action(db, experiment_id, run_id, kind, payload):
    _experiment(db, experiment_id)
    if run_id is not None:
        run = _required(db, "runs", run_id)
        if run["experiment_id"] != experiment_id:
            raise ValueError("run belongs to another experiment")
    identifier = uuid4().hex
    db.execute(
        "INSERT INTO actions VALUES (?, ?, ?, ?, ?, ?)",
        (
            identifier,
            experiment_id,
            run_id,
            kind,
            canonical_json(payload),
            _now(),
        ),
    )
    return identifier


def append_action(root, experiment_id, run_id, kind, payload):
    with _db(root, True) as db:
        return _action(db, experiment_id, run_id, kind, payload)


def actions(root, run_id):
    with _db(root) as db:
        run = _required(db, "runs", run_id)
        rows = db.execute(
            "SELECT * FROM actions WHERE run_id = ? OR "
            "(experiment_id = ? AND run_id IS NULL) "
            "ORDER BY created_at, rowid",
            (run_id, run["experiment_id"]),
        )
        return [
            dict(row) | {"payload": json.loads(row["payload"])} for row in rows
        ]


def experiment_actions(root, experiment_id):
    with _db(root) as db:
        _experiment(db, experiment_id)
        rows = db.execute(
            "SELECT * FROM actions WHERE experiment_id = ? "
            "AND run_id IS NULL ORDER BY created_at, rowid",
            (experiment_id,),
        )
        return [
            dict(row) | {"payload": json.loads(row["payload"])} for row in rows
        ]


def reserve_evaluation(root, run_id):
    with _db(root, True) as db:
        run = _required(db, "runs", run_id)
        if run["state"] not in ("sealed", "checked"):
            raise ValueError("seal the run before evaluation")
        now = _now()
        cursor = db.execute(
            "INSERT OR IGNORE INTO evaluations VALUES "
            "(?, 'reserved', NULL, ?, ?)",
            (run_id, now, now),
        )
        return cursor.rowcount == 1


def finish_evaluation(root, run_id, result):
    with _db(root, True) as db:
        _required(db, "runs", run_id)
        cursor = db.execute(
            "UPDATE evaluations SET state = 'completed', result = ?, "
            "updated_at = ? WHERE run_id = ? AND state = 'reserved'",
            (canonical_json(result), _now(), run_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("evaluation must be reserved and unfinished")
        cursor = db.execute(
            "UPDATE runs SET state = 'checked', updated_at = ? "
            "WHERE id = ? AND state = 'sealed'",
            (_now(), run_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("only a sealed run can finish evaluation")


def get_evaluation(root, run_id):
    with _db(root) as db:
        _required(db, "runs", run_id)
        row = db.execute(
            "SELECT * FROM evaluations WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            return None
        return dict(row) | {
            "result": json.loads(row["result"])
            if row["result"] is not None
            else None
        }
