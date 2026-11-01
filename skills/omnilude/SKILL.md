---
name: omnilude
description: Guide Omnilude research through Prelude (define the Blueprint), Interlude (run and record), and Postlude (review evidence) in the current chat. Use for exploratory probes, confirmation studies, or resuming an existing Omnilude experiment.
---

# Omnilude

Use this chat as the research assistant and the bundled Omnilude MCP tools
as the local record and runner. One skill covers all three phases. When
resuming work, read the existing protocol and run state before asking for
decisions again or starting an attempt. Read the Research workflow and Design
traceability sections in [README.md](../../README.md) before the first
protocol. Use `research_schema` for the current typed contract; inspect
tool schemas instead of inventing arguments. If the tools are unavailable,
continue clarifying the idea and report that execution is not connected.
Keep research assistance in this chat; do not start another model agent or
API gateway.

## Prelude — define the Blueprint

### Researcher first

Before suggesting a hypothesis, expected result, metric, method or option
list, collect the user's own question, prediction and exact metrics, using
what they already wrote. Ask for only missing fields, in free text. The
initial idea is not permission to supply their prediction for them. If
they do not know yet, retain that statement and ask whether they want
proposals; label subsequent proposals as assistant-suggested. Never invent
an independent user prediction from a suggested value.

If the user has already seen suggestions or results, preserve that prior
exposure. Do not label the position unprompted, blinded or prospective when
it is not. The current chat sees the prediction: historical requirement
P8 is explicitly waived for this local workflow. Keep experiment wording
neutral; an expected result is not a target to achieve.

### Build and freeze the Blueprint

Choose the smallest experiment that resolves a useful uncertainty. Keep
one question and a clear outcome per protocol; identify what is exploratory
and what is a confirmation. Confirmations need a hypothesis, prediction,
analysis and stopping rule before execution. Revisions after inspecting
results are exploratory; preserve the old version and use fresh or held-out
data for a later confirmation.

Pin the exact metrics and definitions, data identity and permitted use,
selection and aggregation rule, all-run reporting, success criteria,
tolerances, excluded shortcuts, and the command that reproduces the
outcome. Prefer few changed variables and the smallest adequate model and
dataset. State what the measurement cannot establish.

Keep assumptions few and ranked. Each needs a self-contained statement,
its warrant, the check/evidence that would test it, and its expected
outcome. Include a known-outcome control where feasible, such as a baseline,
planted effect or justified ceiling; record why it is absent otherwise.
Passing an agent-written check does not establish its warrant or independence.

Assistant proposals stay pending until the user accepts them. Preserve
their original author after acceptance. Do not silently promote a proposed
assumption, metric or substantive method choice into an accepted pin.
Group related choices to avoid repeated confirmation of the same decision.
Within accepted boundaries, make routine implementation choices and record
the choice, evidence and rejected alternative. Stop for a change to a pin.

Use the light tier for a small probe: at most three assumptions and no
separate method-approval step. Use full when the user chooses deeper review
for a costly or consequential result. Write a method without results before
running experiment code. A full-tier freeze requires the user's approval
of the exact previewed protocol hash, which includes the method and pinned
files; a changed preview needs a new approval.
Accepting assistant proposals and approving a full-tier method are different
actions. Do not add full-tier approval to a light experiment.

Pass an explicit absolute `project` directory to the tools; they keep the
store in `<project>/.omnilude`, outside the selected workspace inputs and
the plugin installation. Keep raw inputs,
outputs and previous attempts. `research_create`, `research_get`,
`research_update` and `research_preview` prepare the draft;
`research_freeze` pins it. Use `research_fork` for a revised frozen protocol.
Prepare code needed by the method without executing the experiment before
freeze. Declare the needed `workspace_paths`; an empty list copies no code.

## Interlude — run and retain

Use argument lists without shell interpolation. Commands run in the copied
workspace; read `OMNILUDE_INPUTS_DIR` and write `OMNILUDE_OUTPUT_DIR` as needed.
Artifact paths and `metrics_path` are relative to that workspace. A metrics
file is a JSON object with exactly the declared metric names and finite
numeric values. Do not run a failed attempt outside Omnilude to conceal it.

`research_run` starts the frozen command and declared checks locally;
`research_status` follows it. The worker retains stdout/stderr, process
outcomes and artifacts and seals the run. `research_stop` requests a stop;
`research_recover` retains an interrupted attempt. Do not automatically
rerun an uncertain or failed attempt. Check the recorded state first.

Use `research_record` for decisions, observations, deviations and
interpretations. Tool-reported process facts and an assistant's account are
different evidence. Statements attributed to the user from chat remain
reported provenance, not authenticated human actions. Never fabricate an
approval, identity, model snapshot, sampling setting or usage figure.
Record unavailable metadata as unavailable. Commands or actions outside
Omnilude are reported observations, not verified runner events.

Local commands run with the host user's access. Omnilude does not hide final
data, constrain the whole chat, meter its model usage, or independently
capture all tools and transient edits. Copies and hashes detect some changes;
the copies are writable and do not create a security boundary. Do not claim
otherwise.

## Postlude — inspect and explain

Use `research_verify` to validate the sealed record without rerunning the
experiment, then `research_report` and `research_evidence`. Inspect raw
examples as well as metrics, including failures, apparent negatives and
near misses; use `research_list` to include retained attempts. A nonzero
exit is a process result, and can be expected by a declared negative test;
it is not evidence that the research hypothesis is false.

Show the observed result first, method next, interpretation last. Link each
claim to the artifact or event that supports it. Separate record integrity,
method fidelity, and scientific meaning. Metric schema validation is not
independent scoring. A completed run or passed check does not make the
hypothesis supported. Keep evidence status distinct from work status, using
the recorded labels; do not upgrade a label from fluent explanation alone.

Keep unexpected results as anomalies while their validity is unresolved.
Before a negative scientific conclusion, check prerequisite capability,
conditions, measurement sensitivity and known examples. Name the main
unresolved assumption and cheapest informative follow-up when inconclusive.
Preserve rare-behaviour transcripts and scope claims to the tested conditions.
Any automated review is a finding for inspection, never an LLM validity grade.
