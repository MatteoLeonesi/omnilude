import json

import pytest

from omnilude.cli import main


def test_default_command_shows_help_without_creating_a_store(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0
    output = capsys.readouterr().out
    assert "research" in output
    assert "mcp" in output
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "command",
    [
        "tui",
        "demo",
        "import",
        "freeze",
        "run",
        "evaluate",
        "doctor",
        "prepare",
    ],
)
def test_removed_commands_are_rejected_without_side_effects(
    command, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as error:
        main([command])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_research_schema_routes_to_the_shared_cli(capsys):
    assert main(["research", "schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["properties"]["kind"]["const"] == "research"


def test_mcp_help_does_not_start_the_server(capsys):
    with pytest.raises(SystemExit) as error:
        main(["mcp", "--help"])
    assert error.value.code == 0
    assert "STDIO" in capsys.readouterr().out
