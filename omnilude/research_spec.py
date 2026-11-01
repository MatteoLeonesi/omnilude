from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnilude.spec import Text, digest

Name = Annotated[str, Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$")]


class ResearchMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    definition: Text
    unit: str = ""


class RecordedDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: Text
    reference: Text
    subject_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class ResearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Name
    path: Text
    role: Text
    usage: Text


class ResearchArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: Text
    required: bool = True


class ResearchAssumption(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Name
    statement: Text
    warrant: Text
    check: Text
    expected: Text
    check_command: list[Text] | None = Field(default=None, min_length=1)
    expected_exit: int = Field(default=0, strict=True)
    author: Literal["researcher", "agent"]
    critical: bool = True
    acceptance: RecordedDecision | None = None


class ResearchSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", revalidate_instances="always")

    kind: Literal["research"] = "research"
    schema_version: Literal[1] = 1
    title: Text
    question: Text
    prediction: Text
    metrics: list[ResearchMetric] = Field(min_length=1)
    researcher_source: Text
    researcher_reference: Text | None = None
    after_agent_suggestions: bool = Field(strict=True)
    mode: Literal["exploration", "confirmation"] = "exploration"
    tier: Literal["light", "full"] = "light"
    workspace_paths: list[Text] = Field(default_factory=list)
    inputs: list[ResearchInput] = Field(default_factory=list)
    data_policy: str = ""
    selection_rule: str = ""
    success_criteria: str = ""
    constraints: list[Text] | None = None
    stopping_rule: str = ""
    hypothesis: Text | None = None
    analysis_plan: Text | None = None
    fresh_data_statement: Text | None = None
    known_outcome_check: Text | None = None
    known_outcome_not_applicable: Text | None = None
    command: list[Text] = Field(default_factory=list)
    expected_exit: int = Field(default=0, strict=True)
    timeout_seconds: int = Field(default=60, gt=0, strict=True)
    max_runs: int = Field(default=1, ge=1, strict=True)
    budget_usd: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = (
        None
    )
    env_vars: list[
        Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")]
    ] = Field(default_factory=list)
    artifacts: list[ResearchArtifact] = Field(default_factory=list)
    metrics_path: Text | None = None
    assumptions: list[ResearchAssumption] = Field(default_factory=list)

    @model_validator(mode="after")
    def distinct(self) -> Self:
        groups = (
            [metric.name for metric in self.metrics],
            [item.name for item in self.inputs],
            [item.id for item in self.assumptions],
            [item.path for item in self.artifacts],
            self.env_vars,
        )
        if any(len(items) != len(set(items)) for items in groups):
            raise ValueError(
                "metric, input, assumption and artifact names must be unique"
            )
        if self.tier == "light" and len(self.assumptions) > 3:
            raise ValueError("light tier permits at most three assumptions")
        reserved = {
            "integrity",
            "execution",
            "outputs",
            "method",
            "completeness",
        }
        if reserved.intersection(item.id for item in self.assumptions):
            raise ValueError("assumption IDs must not use reserved check IDs")
        return self


def assumption_hash(assumption: ResearchAssumption) -> str:
    return digest(assumption.model_dump(exclude={"acceptance"}))


def validate_ready(spec: ResearchSpec, method: str) -> None:
    if not method.strip():
        raise ValueError("A results-free method description is required")
    for field in (
        "data_policy",
        "selection_rule",
        "success_criteria",
        "stopping_rule",
    ):
        if not getattr(spec, field).strip():
            raise ValueError(f"Set {field} before freezing")
    if spec.constraints is None:
        raise ValueError("Declare constraints before freezing; [] means none")
    if not spec.command:
        raise ValueError("Declare the experiment command before freezing")
    if spec.mode == "confirmation" and (
        not spec.hypothesis or not spec.analysis_plan
    ):
        raise ValueError("Confirmation requires hypothesis and analysis_plan")
    if spec.tier == "full" and not (
        spec.known_outcome_check or spec.known_outcome_not_applicable
    ):
        raise ValueError(
            "Full tier needs a known-outcome check or its inapplicability"
        )
    for assumption in spec.assumptions:
        if assumption.author == "agent":
            if assumption.acceptance is None:
                raise ValueError(
                    f"Agent proposal {assumption.id} needs user acceptance"
                )
            if assumption.acceptance.subject_hash != assumption_hash(
                assumption
            ):
                raise ValueError(f"Acceptance for {assumption.id} is stale")
