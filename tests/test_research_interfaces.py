import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from omnilude import research_cli


def test_public_research_cli_entrypoint():
    result = subprocess.run(
        [sys.executable, "-m", "omnilude", "research", "schema"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    schema = json.loads(result.stdout)
    assert schema["properties"]["kind"]["const"] == "research"
    assert not result.stderr


def test_cli_requires_explicit_location(capsys):
    with pytest.raises(SystemExit) as error:
        research_cli.main(["list"])
    assert error.value.code == 1
    assert "explicit project or store path" in capsys.readouterr().err


def test_read_only_listing_does_not_create_store(tmp_path, capsys):
    research_cli.main(["--project", str(tmp_path), "list"])
    result = json.loads(capsys.readouterr().out)
    assert result == {"experiments": [], "runs": []}
    assert not (tmp_path / ".omnilude").exists()


def test_relative_project_and_redirected_store_rejected(tmp_path):
    with pytest.raises(ValueError, match="absolute"):
        research_cli.project_store(".")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / ".omnilude").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        research_cli.project_store(str(tmp_path))


def test_cli_preserves_method_and_reported_approval(tmp_path, monkeypatch):
    method = tmp_path / "method.md"
    method.write_text("Measure Δ without executing `anything`.\n")
    approval = tmp_path / "approval.json"
    source = {
        "subject_hash": "a" * 64,
        "statement": "Approvo questa versione.",
        "reference": "chat:2",
    }
    approval.write_text(json.dumps(source))
    received = []

    def dispatch(action, root, **arguments):
        received.append((action, root, arguments))
        return {"state": "frozen"}

    monkeypatch.setattr(research_cli, "dispatch", dispatch)
    research_cli.main(
        [
            "--project",
            str(tmp_path),
            "freeze",
            "experiment-id",
            "--method-file",
            str(method),
            "--approval",
            str(approval),
        ]
    )
    assert received == [
        (
            "freeze",
            tmp_path / ".omnilude",
            {
                "experiment_id": "experiment-id",
                "method": method.read_text(),
                "approval": source,
            },
        )
    ]


def test_real_stdio_discovery_and_read_only_calls(tmp_path):
    async def exercise():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "omnilude.mcp"],
            cwd=str(Path(__file__).resolve().parents[1]),
        )
        async with (
            asyncio.timeout(30),
            stdio_client(parameters) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            initialized = await session.initialize()
            assert initialized.serverInfo.name == "Omnilude"
            listed = await session.list_tools()
            by_name = {tool.name: tool for tool in listed.tools}
            assert {
                "research_create",
                "research_update",
                "research_fork",
                "research_get",
                "research_preview",
                "research_freeze",
                "research_run",
                "research_status",
                "research_stop",
                "research_recover",
                "research_verify",
                "research_report",
                "research_evidence",
                "research_record",
                "research_list",
                "research_schema",
            } == set(by_name)
            assert by_name["research_report"].annotations.readOnlyHint
            assert not by_name["research_run"].annotations.readOnlyHint
            assert by_name["research_run"].annotations.openWorldHint
            for name in ("research_run", "research_stop", "research_recover"):
                assert by_name[name].annotations.destructiveHint
                assert not by_name[name].annotations.readOnlyHint
            schema = await session.call_tool("research_schema", {})
            assert not schema.isError
            assert (
                schema.structuredContent["properties"]["kind"]["const"]
                == "research"
            )
            result = await session.call_tool(
                "research_list", {"project": str(tmp_path)}
            )
            assert not result.isError
            assert result.structuredContent == {
                "experiments": [],
                "runs": [],
            }
            invalid = await session.call_tool(
                "research_list", {"project": "."}
            )
            assert invalid.isError
            assert "absolute" in invalid.content[0].text

    asyncio.run(exercise())
    assert not (tmp_path / ".omnilude").exists()


def test_real_stdio_research_lifecycle_without_docker_or_api(tmp_path):
    (tmp_path / "main.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "Path('metrics.json').write_text(json.dumps({'value': 3}))\n"
    )
    spec = {
        "title": "Local MCP probe",
        "question": "Does the program emit the expected value?",
        "prediction": "The value will be 3.",
        "metrics": [{"name": "value", "definition": "Computed value"}],
        "researcher_source": "User: emit a value; I predict 3; report value.",
        "researcher_reference": "chat:1",
        "after_agent_suggestions": False,
    }

    async def exercise():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "omnilude", "mcp"],
            cwd=str(Path(__file__).resolve().parents[1]),
            env={"PATH": "/usr/bin:/bin", "OPENAI_API_KEY": ""},
        )
        async with (
            asyncio.timeout(30),
            stdio_client(parameters) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()

            async def call(name, **arguments):
                result = await session.call_tool(
                    "research_" + name,
                    {"project": str(tmp_path), **arguments},
                )
                assert not result.isError, result.content
                return result.structuredContent

            created = await call("create", spec=spec, workspace=str(tmp_path))
            experiment_id = created["id"]
            premature = await session.call_tool(
                "research_run",
                {"project": str(tmp_path), "experiment_id": experiment_id},
            )
            assert premature.isError
            assert "Freeze" in premature.content[0].text
            complete = created["spec"] | {
                "data_policy": "No external data",
                "selection_rule": "Report every run",
                "success_criteria": "The value is exactly 3",
                "constraints": [],
                "stopping_rule": "One run with a 10-second timeout",
                "timeout_seconds": 10,
                "command": [sys.executable, "main.py"],
                "workspace_paths": ["main.py"],
                "metrics_path": "metrics.json",
                "artifacts": [{"path": "metrics.json"}],
                "assumptions": [
                    {
                        "id": "number",
                        "statement": "The recorded value is numeric",
                        "warrant": "A type assertion checks the value",
                        "check": "Read metrics.json and assert int or float",
                        "expected": "The assertion exits successfully",
                        "check_command": [
                            sys.executable,
                            "-c",
                            "import json; "
                            "v=json.load(open('metrics.json'))['value']; "
                            "assert isinstance(v, (int, float))",
                        ],
                        "author": "agent",
                    }
                ],
            }
            await call("update", experiment_id=experiment_id, spec=complete)
            method = "Emit the numeric value, then check its recorded type."
            preview = await call(
                "preview", experiment_id=experiment_id, method=method
            )
            assert not preview["ready"]
            assert "acceptance" in preview["blocking_reason"]
            complete["assumptions"][0]["acceptance"] = {
                "statement": "I accept the numeric-value assumption.",
                "reference": "chat:2",
                "subject_hash": preview["assumption_hashes"]["number"],
            }
            await call("update", experiment_id=experiment_id, spec=complete)
            frozen = await call(
                "freeze", experiment_id=experiment_id, method=method
            )
            assert frozen["state"] == "frozen"
            started = await call("run", experiment_id=experiment_id)
            run_id = started["run_id"]
            while True:
                state = await call("status", run_id=run_id)
                if state["state"] not in {"created", "running"}:
                    break
                await asyncio.sleep(0.05)
            assert state["state"] == "sealed", state
            await call("verify", run_id=run_id)
            report = await call("report", run_id=run_id)
            assert report["state"] == "checked"
            assert report["metrics"] == {"value": 3}
            assert report["hypothesis_status"] == "not_assessed"
            rows = {row["id"]: row for row in report["rows"]}
            assert rows["number"]["status"] == "unsupported"
            assert rows["number"]["check_status"] == "passed"
            evidence = await call(
                "evidence",
                run_id=run_id,
                ref="execution/workspace/metrics.json",
            )
            assert json.loads(evidence["text"]) == {"value": 3}
            assert not evidence["truncated"]
            all_records = await call("list")
            assert len(all_records["runs"]) == 1

    asyncio.run(exercise())
