# Omnilude — agent instructions

Omnilude is a local research plugin: a conversational skill and MCP tools
for protocols, execution and evidence review. Read `README.md` before design
work; do not claim empirically validated verification benefits.

## Commands

- Install: `uv sync`
- Lint: `uv run ruff check .`
- Format: `uv run ruff format .`
- Test: `uv run pytest`

Run lint, format, and tests before calling a change done.

## Documentation

- `README.md` is the single project reference: purpose, design principles,
  usage, contracts and current limits.
- `AGENTS.md` holds coding instructions; `CLAUDE.md` points here.
- Update the README when lasting behavior or design changes.
- Use Git commits for project history. Do not add separate plans, progress
  logs, dated reports, Q&A files or duplicate documentation.
- Keep experiment provenance in the run records, not in documentation logs.

## Rules

- LESS IS MORE: write the smallest clear, correct solution. Minimize code,
  files, dependencies, and abstractions. Never trade correctness, security,
  accessibility, or maintainability for fewer lines.
- Build only what the current task requires; no speculative layers.
- Prefer functions and composition over classes and inheritance.
- No code comments or commented-out code. Docstrings only when asked.
- Catch specific exceptions; never hide failures.
- Resolve ambiguity by asking, not guessing.
- Style is enforced by Ruff (`pyproject.toml`); do not add Black.

## Stack

- Core: Python functions + Pydantic, shared typed models for specs,
  manifests, and results.
- Database: SQLite via `sqlite3`, explicit SQL, a few data-access functions.
- Interface: the current assistant chat through the skill and MCP tools,
  with CLI commands using the same core functions. Keep shared instructions
  independent of the client. Do not add a nested model agent, API gateway,
  Docker workflow or terminal UI.
- Tests: pytest, focused on verification, state transitions, and result
  integrity.
- Dependencies: uv.

Adopt a design pattern only when it simplifies a concrete need; use
[Python Patterns](https://python-patterns.guide/) as the reference.
