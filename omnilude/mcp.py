from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from omnilude.research_cli import dispatch, project_store

READ = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)
EXECUTE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

server = FastMCP(
    "Omnilude",
    instructions=(
        "Use the research skill. Record the researcher's question, prediction "
        "and metrics before suggestions. Preserve their source and disclose "
        "prior agent suggestions. Freeze the protocol before experiment "
        "execution; full tier needs approval for the exact protocol hash. "
        "Pass an explicit absolute project directory to every project tool. "
        "Local runs are not isolated; capture covers Omnilude tools only. "
        "Respect host permissions and do not use tools to bypass approvals. "
        "User decisions quoted by the assistant are reported, not "
        "independently authenticated. Use research_schema for the contract."
    ),
)


@server.tool(
    description="Read the generic research specification JSON schema.",
    annotations=READ,
)
def research_schema() -> dict[str, Any]:
    return dispatch("schema")


@server.tool(
    description=(
        "Save a researcher-first draft and its source workspace. Supply the "
        "researcher's question, prediction and metrics before proposals."
    ),
    annotations=WRITE,
)
def research_create(
    project: str, spec: dict[str, Any], workspace: str
) -> dict[str, Any]:
    return dispatch(
        "create", project_store(project), spec=spec, workspace=workspace
    )


@server.tool(
    description=(
        "Revise an unfrozen draft. Keep proposal authorship and reported "
        "user acceptance explicit; never invent user decisions."
    ),
    annotations=WRITE,
)
def research_update(
    project: str, experiment_id: str, spec: dict[str, Any]
) -> dict[str, Any]:
    return dispatch(
        "update",
        project_store(project),
        experiment_id=experiment_id,
        spec=spec,
    )


@server.tool(
    description="Fork a retained experiment into a new editable revision.",
    annotations=WRITE,
)
def research_fork(project: str, experiment_id: str) -> dict[str, Any]:
    return dispatch(
        "fork", project_store(project), experiment_id=experiment_id
    )


@server.tool(
    description="Read a research draft or frozen protocol and its provenance.",
    annotations=READ,
)
def research_get(project: str, experiment_id: str) -> dict[str, Any]:
    return dispatch("get", project_store(project), experiment_id=experiment_id)


@server.tool(
    description=(
        "Preview the results-free method and exact protocol hash before "
        "freezing. Present this version for full-tier user approval."
    ),
    annotations=READ,
)
def research_preview(
    project: str, experiment_id: str, method: str
) -> dict[str, Any]:
    return dispatch(
        "preview",
        project_store(project),
        experiment_id=experiment_id,
        method=method,
    )


@server.tool(
    description=(
        "Freeze the protocol and results-free method before execution. "
        "Full-tier approval must quote an actual user decision for the "
        "previewed hash: {statement, reference, subject_hash}. Light tier "
        "requires no extra method approval."
    ),
    annotations=WRITE,
)
def research_freeze(
    project: str,
    experiment_id: str,
    method: str,
    approval: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return dispatch(
        "freeze",
        project_store(project),
        experiment_id=experiment_id,
        method=method,
        approval=approval,
    )


@server.tool(
    description=(
        "Launch the frozen commands locally and return a run ID immediately. "
        "Executes real code with host access: honor the user's authorized "
        "scope and host-client permissions. Never retry an uncertain "
        "launch blindly."
    ),
    annotations=EXECUTE,
)
def research_run(project: str, experiment_id: str) -> dict[str, Any]:
    return dispatch("run", project_store(project), experiment_id=experiment_id)


@server.tool(
    description="Read a retained local run's state without restarting it.",
    annotations=READ,
)
def research_status(project: str, run_id: str) -> dict[str, Any]:
    return dispatch("status", project_store(project), run_id=run_id)


@server.tool(
    description="Request cancellation of the selected Omnilude run.",
    annotations=EXECUTE,
)
def research_stop(
    project: str, run_id: str, reason: str = ""
) -> dict[str, Any]:
    return dispatch(
        "stop", project_store(project), run_id=run_id, reason=reason
    )


@server.tool(
    description=(
        "Retain partial evidence from an interrupted run, stopping any "
        "verified orphan process group. Recovery does not repeat commands "
        "or restart the experiment."
    ),
    annotations=EXECUTE,
)
def research_recover(project: str, run_id: str) -> dict[str, Any]:
    return dispatch("recover", project_store(project), run_id=run_id)


@server.tool(
    description=(
        "Verify a sealed run's retained artifacts and check outcomes. "
        "Persist the mechanical assessment; passing checks does not establish "
        "the research hypothesis or independent review."
    ),
    annotations=WRITE,
)
def research_verify(project: str, run_id: str) -> dict[str, Any]:
    return dispatch("verify", project_store(project), run_id=run_id)


@server.tool(
    description=(
        "Read results against the prediction, deviations, assumption checks "
        "and linked evidence. Keep interpretation separate from observations."
    ),
    annotations=READ,
)
def research_report(project: str, run_id: str) -> dict[str, Any]:
    return dispatch("report", project_store(project), run_id=run_id)


@server.tool(
    description="Read a bounded text artifact referenced by a run's report.",
    annotations=READ,
)
def research_evidence(
    project: str, run_id: str, ref: str, max_chars: int = 20000
) -> dict[str, Any]:
    return dispatch(
        "evidence",
        project_store(project),
        run_id=run_id,
        ref=ref,
        max_chars=max_chars,
    )


@server.tool(
    description=(
        "Append an assistant-reported decision, observation or interpretation "
        "with its evidence. This never authenticates a user decision."
    ),
    annotations=WRITE,
)
def research_record(
    project: str, run_id: str, kind: str, payload: dict[str, Any]
) -> dict[str, Any]:
    return dispatch(
        "record",
        project_store(project),
        run_id=run_id,
        kind=kind,
        payload=payload,
    )


@server.tool(
    description="List all retained research experiments and attempts.",
    annotations=READ,
)
def research_list(project: str) -> dict[str, Any]:
    return dispatch("list", project_store(project))


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
