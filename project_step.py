"""The project agent, run *inside* the sandbox container (Phase 9).

The summariser's sibling (`summarise_step.py`), with one important difference: this
container has **no broker socket at all**. The project agent has no broker tools, so
mounting the bridge would create exactly the channel Phase 6 took away — the one agent
with a shell would gain a route to the process that holds the Gmail credential. So the
only things mounted here are:

- ``/workspace``  — the project's disposable git worktree, already on tonight's
  ``agent/<date>`` branch (rw). This is the agent's whole world.
- ``/opt/nightshift`` — an explicit copy of the NightShift modules the agent needs, plus
  the resolved standing instructions (ro). Copied file by file host-side rather than
  mounting the repo, because the repo root contains ``.env``.
- ``/run/nightshift-out`` — a directory for the structured report (rw).

and the only network route is the egress proxy to the LLM.

**Write-back is a file, not a broker call.** The agent's "what I did" lands as
``project_work.json`` in the out directory and the host reads it back after the container
exits — the same worktree/file pattern the summariser uses for ``briefing.html``. That is
the simpler of the two options *and* the safer one: contributing through the broker's
``add_to_briefing`` would mean giving this agent a socket it currently cannot reach.

Everything with a consequence happens on the host afterwards: the commit sweep, the diff
artifact, the push under the ``agent/*``-restricted deploy key, and the queued (never
automatic) merge. See ``nightly_project.py``.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from config import ConfigError, load_config, use_config
from models import AgentWorkReport
from runner.agent_runner import AgentRunner
from runner.agents import project_agent
from runner.taint import PromptPart
from runner.tools_project import WorkSink, WorktreeScope

WORKSPACE = Path(os.getenv("NIGHTSHIFT_WORKSPACE", "/workspace"))
REPORT_PATH = Path(os.getenv("NIGHTSHIFT_WORK_REPORT", "/run/nightshift-out/project_work.json"))
PROJECT_NAME = os.getenv("NIGHTSHIFT_PROJECT", "")


def build_goal(project) -> str:
    """The night's instructions: host-authored goals only.

    Assembled from `[[projects]].goals` in the standing instructions and nothing else. No
    email, no calendar, no repo content — `project_agent` declares `accepts_taint=frozenset()`
    and the runner would refuse the prompt if any of it carried a label.
    """
    goals = [g.strip() for g in project.goals if g.strip()]
    if not goals:
        return "No specific goal was configured tonight. Improve the project modestly."
    return "\n".join(f"- {goal}" for goal in goals)


def run_project_agent(config, project, *, workspace: Path, backend, max_steps: int | None = None):
    """Run one project agent against `workspace`. Returns `(AgentResult, AgentWorkReport)`.

    Split out from `main()` so tests can drive it with a stub backend and no container.
    """
    scope = WorktreeScope(workspace)
    sink = WorkSink()
    spec = project_agent(
        config,
        scope=scope,
        goal=build_goal(project),
        sink=sink,
        max_steps=max_steps or project.max_steps,
    )
    prompt = [
        PromptPart.trusted(
            f"Project: {project.name}\n"
            f"You are on tonight's agent branch in a disposable worktree at {workspace}.",
            label="tonight's assignment",
        )
    ]
    result = AgentRunner(backend).run(spec, prompt)

    report = sink.report
    if report is None:
        # No `report_work` call: fall back to the final message so the briefing still says
        # something, clearly marked. Silence would read as "nothing happened".
        report = AgentWorkReport(
            summary=(result.text or "The agent finished without reporting what it did.")[:4000],
            completed=False,
        )
    return result, report


def main() -> int:
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"project_step: config unusable ({exc}); refusing to run blind.")
        return 2
    use_config(config)

    if not PROJECT_NAME:
        print("project_step: NIGHTSHIFT_PROJECT is not set.")
        return 2
    try:
        project = config.project(PROJECT_NAME)
    except ConfigError as exc:
        print(f"project_step: {exc}")
        return 2

    print(
        f"project_step: project {project.name!r}, model "
        f"{config.agent('project_agent').model}, worktree {WORKSPACE}"
    )

    # Imported here so the module is importable (and testable) without an API key.
    from runner.backends import backend_for

    result, report = run_project_agent(
        config, project, workspace=WORKSPACE, backend=backend_for(config)
    )
    print(
        f"project_step: {result.steps} step(s), stop reason {result.stop_reason}, "
        f"{len(result.transcript)} tool call(s), {result.usage.total_tokens} token(s), "
        f"${result.cost_usd:.4f}."
    )

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    print(f"project_step: wrote work report to {REPORT_PATH}.")

    # A step-limit stop is not a crash: the work so far is still committed and reviewed.
    # It is reported so the briefing can say the goal was cut short.
    if result.stop_reason != "completed":
        print(f"project_step: note — run ended on {result.stop_reason}.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 - the host needs the reason, not a traceback race
        print(f"project_step: failed: {exc!r}")
        # Emit an empty report so the host can distinguish "ran and failed" from
        # "never produced anything".
        try:
            REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
            REPORT_PATH.write_text(json.dumps({"summary": f"failed: {exc!r}"}), encoding="utf-8")
        except OSError:
            pass
        sys.exit(1)
