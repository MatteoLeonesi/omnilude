import argparse
import json
import sqlite3
import subprocess
from pathlib import Path

from omnilude import store


def absolute_directory(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError("Use an explicit absolute project or store path")
    return path.resolve()


def project_store(project: str) -> Path:
    path = absolute_directory(project)
    if not path.is_dir():
        raise ValueError("Project must be an existing directory")
    root = path / ".omnilude"
    if root.is_symlink():
        raise ValueError("The project store must not be a symlink")
    return root


def dispatch(action: str, root: Path | None = None, **arguments):
    if action == "schema":
        from omnilude.research_spec import ResearchSpec

        return ResearchSpec.model_json_schema()
    if root is None:
        raise ValueError("An explicit project or store path is required")
    root = absolute_directory(root)
    if action == "list":
        if not (root / "omnilude.sqlite3").is_file():
            return {"experiments": [], "runs": []}
        experiments = [
            item
            for item in store.list_experiments(root)
            if item["spec"].get("kind") == "research"
        ]
        identifiers = {item["id"] for item in experiments}
        return {
            "experiments": experiments,
            "runs": [
                item
                for item in store.list_runs(root)
                if item["experiment_id"] in identifiers
            ],
        }
    if action in {"create", "update", "fork", "get", "preview", "freeze"}:
        from omnilude import research

        if action == "create":
            arguments["workspace"] = absolute_directory(arguments["workspace"])
        operation = getattr(research, action)
    else:
        from omnilude import local

        operations = {
            "run": local.start,
            "status": local.status,
            "stop": local.stop,
            "recover": local.recover,
            "verify": local.evaluate,
            "report": local.report,
            "evidence": local.read_evidence,
            "record": local.record,
            "worker": local.worker,
        }
        if action not in operations:
            raise ValueError(f"Unknown research operation: {action}")
        operation = operations[action]
    return operation(root, **arguments)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="omnilude research")
    location = result.add_mutually_exclusive_group()
    location.add_argument("--project", type=Path)
    location.add_argument("--root", type=Path)
    commands = result.add_subparsers(dest="action", required=True)
    commands.add_parser("schema", help="print the research JSON schema")
    commands.add_parser("list", help="list research experiments and runs")
    create = commands.add_parser("create", help="save a researcher prelude")
    create.add_argument("--spec", type=Path, required=True)
    create.add_argument("--workspace", type=Path, required=True)
    update = commands.add_parser("update", help="update an unfrozen draft")
    update.add_argument("experiment_id")
    update.add_argument("--spec", type=Path, required=True)
    for name in ("get", "fork", "preview", "freeze", "run"):
        command = commands.add_parser(name)
        command.add_argument("experiment_id")
        if name in {"preview", "freeze"}:
            command.add_argument("--method-file", type=Path, required=True)
        if name == "freeze":
            command.add_argument("--approval", type=Path)
        if name == "run":
            command.add_argument("--foreground", action="store_true")
    for name in (
        "status",
        "stop",
        "recover",
        "verify",
        "report",
        "evidence",
        "record",
        "worker",
    ):
        command = commands.add_parser(name)
        command.add_argument("run_id")
        if name == "stop":
            command.add_argument("--reason", default="")
        if name == "evidence":
            command.add_argument("ref")
            command.add_argument("--max-chars", type=int, default=20000)
        if name == "record":
            command.add_argument(
                "kind", choices=["decision", "observation", "interpretation"]
            )
            command.add_argument("--payload", type=Path, required=True)
    return result


def main(argv=None) -> int:
    arguments = parser()
    options = vars(arguments.parse_args(argv))
    action = options.pop("action")
    project = options.pop("project")
    explicit_root = options.pop("root")
    try:
        root = project_store(str(project)) if project is not None else None
        if explicit_root is not None:
            root = absolute_directory(explicit_root)
        for field in ("spec", "approval", "payload"):
            if options.get(field) is not None:
                options[field] = json.loads(
                    options[field].read_text(encoding="utf-8")
                )
        if "method_file" in options:
            options["method"] = options.pop("method_file").read_text(
                encoding="utf-8"
            )
        if "foreground" in options:
            options["background"] = not options.pop("foreground")
        result = dispatch(action, root, **options)
        print(json.dumps(result, indent=2, allow_nan=False))
    except (
        ValueError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ) as error:
        arguments.exit(1, f"omnilude research: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
