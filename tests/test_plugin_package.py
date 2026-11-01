import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from scripts.package_plugin import package


@pytest.fixture
def source(tmp_path):
    root = Path(__file__).resolve().parents[1]
    destination = tmp_path / "source"
    shutil.copytree(
        root,
        destination,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".omnilude",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            "dist",
        ),
    )
    return destination


def test_bundle_excludes_private_state_and_imports_runtime(source, tmp_path):
    secrets = (
        ".env",
        ".git/config",
        ".venv/lib/private.txt",
        ".omnilude/records/private.txt",
        "runs/private.json",
        "omnilude/__pycache__/private.py",
        "omnilude/private.env",
        "omnilude/debug_dump.py",
        "skills/omnilude/.env",
    )
    for relative in secrets:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("PRIVATE-SENTINEL")
    destination = package(source, tmp_path / "bundle with spaces")
    assert not any((destination / relative).exists() for relative in secrets)
    assert not (destination / "tests").exists()
    assert not (destination / "scripts").exists()
    assert not (destination / "images").exists()
    assert not (destination / "omnilude/adapters").exists()
    assert (destination / "skills/omnilude/SKILL.md").is_file()
    marketplace = json.loads(
        (destination / ".agents/plugins/marketplace.json").read_text()
    )
    assert marketplace["plugins"][0]["source"]["path"] == "./"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from omnilude import mcp, research_spec; "
            "print(json.dumps([mcp.__file__, research_spec.__file__]))",
        ],
        cwd=destination,
        capture_output=True,
        text=True,
        check=True,
    )
    assert all(
        Path(path).is_relative_to(destination)
        for path in json.loads(result.stdout)
    )


def test_default_bundle_paths_are_unique(source):
    first = package(source)
    second = package(source)
    assert first != second
    assert first.parent == source / "dist"
    assert second.parent == source / "dist"


@pytest.mark.parametrize(
    "relative", ["omnilude/cli.py", "skills/omnilude/SKILL.md"]
)
def test_missing_required_file_rejects_before_output_creation(
    source, tmp_path, relative
):
    (source / relative).unlink()
    destination = tmp_path / "bundle"
    with pytest.raises(ValueError, match="Missing package file"):
        package(source, destination)
    assert not destination.exists()


def test_nonempty_output_is_not_changed(source, tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep")
    with pytest.raises(ValueError, match="new or empty"):
        package(source, destination)
    assert list(destination.iterdir()) == [sentinel]
    assert sentinel.read_text() == "keep"


@pytest.mark.parametrize("relative", [".mcp.json", "omnilude/mcp.py"])
def test_selected_symlink_is_rejected(source, tmp_path, relative):
    path = source / relative
    original = tmp_path / "original"
    path.rename(original)
    path.symlink_to(original)
    destination = tmp_path / "bundle"
    with pytest.raises(ValueError, match="Symlinks"):
        package(source, destination)
    assert not destination.exists()


def test_output_symlink_is_rejected(source, tmp_path):
    destination = tmp_path / "real"
    destination.mkdir()
    link = tmp_path / "link"
    link.symlink_to(destination, target_is_directory=True)
    with pytest.raises(ValueError, match="Symlinks"):
        package(source, link)
    assert not list(destination.iterdir())


def test_client_manifests_share_identity_and_local_server(source, tmp_path):
    destination = package(source, tmp_path / "bundle")
    portable = json.loads((destination / "plugin.json").read_text())
    codex = json.loads((destination / ".codex-plugin/plugin.json").read_text())
    claude = json.loads(
        (destination / ".claude-plugin/plugin.json").read_text()
    )
    project = tomllib.loads((destination / "pyproject.toml").read_text())
    assert portable["$schema"].endswith("/1.0.0/plugin.schema.json")
    assert {item["version"] for item in (portable, codex, claude)} == {
        project["project"]["version"]
    }
    assert {item["name"] for item in (portable, codex, claude)} == {"omnilude"}
    extension = portable["extensions"]["com.openai"]
    assert extension["requires_local_executor"] is True
    assert extension["interface"] == codex["interface"]
    assert len(extension["interface"]["shortDescription"]) <= 30
    mcp = json.loads((destination / "mcp.json").read_text())
    assert mcp["$schema"].endswith("/1.0.0/mcp.schema.json")
    server = mcp["mcpServers"]["omnilude"]
    compatible = json.loads((destination / codex["mcpServers"]).read_text())[
        "mcpServers"
    ]["omnilude"]
    assert all(compatible[key] == value for key, value in server.items())
    alternate = claude["mcpServers"]["omnilude"]
    assert server["command"] == alternate["command"] == "uv"
    assert server["args"] == [
        value.replace("${CLAUDE_PLUGIN_ROOT}", "${PLUGIN_ROOT}")
        for value in alternate["args"]
    ]
    assert server["args"][-2:] == ["omnilude", "mcp"]
    marketplace = json.loads(
        (destination / ".claude-plugin/marketplace.json").read_text()
    )
    assert marketplace["plugins"][0]["source"] == "./"
