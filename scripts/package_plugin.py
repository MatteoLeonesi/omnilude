import argparse
import shutil
import tempfile
from pathlib import Path

FILES = (
    "plugin.json",
    "mcp.json",
    ".claude-plugin/plugin.json",
    ".claude-plugin/marketplace.json",
    ".codex-plugin/plugin.json",
    ".mcp.json",
    ".agents/plugins/marketplace.json",
    "skills/omnilude/SKILL.md",
    "README.md",
    "pyproject.toml",
    "uv.lock",
    "omnilude/__init__.py",
    "omnilude/__main__.py",
    "omnilude/cli.py",
    "omnilude/core.py",
    "omnilude/local.py",
    "omnilude/mcp.py",
    "omnilude/research.py",
    "omnilude/research_cli.py",
    "omnilude/research_spec.py",
    "omnilude/snapshots.py",
    "omnilude/spec.py",
    "omnilude/store.py",
)


def reject_symlinks(path: Path) -> None:
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError(f"Symlinks are not allowed: {path}")


def package(source: Path, output: Path | None = None) -> Path:
    source = source.absolute()
    reject_symlinks(source)
    files = [source / name for name in FILES]
    metadata = source / "skills/omnilude/agents/openai.yaml"
    if metadata.exists() or metadata.is_symlink():
        files.append(metadata)
    for path in files:
        reject_symlinks(path)
        if not path.is_file():
            raise ValueError(f"Missing package file: {path}")
    if output is None:
        parent = source / "dist"
        reject_symlinks(parent)
        parent.mkdir(exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix="omnilude-plugin-", dir=parent))
    else:
        output = output.expanduser().absolute()
        reject_symlinks(output)
        if output.exists() and (not output.is_dir() or any(output.iterdir())):
            raise ValueError("Output must be a new or empty directory")
        output.mkdir(parents=True, exist_ok=True)
    for path in files:
        destination = output / path.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    return output


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build a clean local plugin")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args(argv)
    print(package(Path(__file__).resolve().parents[1], arguments.output))


if __name__ == "__main__":
    main()
