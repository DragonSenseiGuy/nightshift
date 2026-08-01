"""Standing instructions: the versioned config every night's agents read (Phase 5).

TOML, not YAML: `tomllib` is in the stdlib on 3.13 so the sandbox image needs no extra
baked dependency, the format has no anchors/aliases or implicit type coercion to be
surprised by, and it comments well — this file is meant to be hand-edited by a human at
bedtime and read back in a diff.

**Trust.** This file is *host-authored* data. Unlike email, it is trusted, so its
priorities and style preferences are fed straight into agent prompts (see
`summarise.py:build_system_prompt`). That line must not blur: nothing an agent reads from
the outside world (email, web, repo contents) may be written back into this file, and no
secret belongs in it — API keys stay in `.env`/Keychain.

**Precedence** (highest first), per field:

1. the config file — `--config <path>`, else `$NIGHTSHIFT_CONFIG`, else
   `config/standing_instructions.toml` next to this module;
2. the matching environment variable, where one exists (`OPENROUTER_MODEL`,
   `OPENROUTER_BASE_URL`) — the pre-Phase-5 fallback, still honoured when the file omits
   the field or is missing entirely;
3. the built-in default baked into the models below.

So a value present in the file always wins, which is what makes the Phase 5 acceptance
criterion true: edit a model slug or a priority, and the next run behaves differently with
no code change.

**Absence is not failure.** A missing config file falls back to validated defaults — a
3am run must never die because someone moved a file. A *malformed* file is different: it
raises `ConfigError` with a readable, field-by-field message, because silently ignoring a
typo'd model slug would be worse than stopping.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = REPO_ROOT / "config" / "standing_instructions.toml"

# Env var naming the config file, honoured on host and (via the orchestrator) in the
# sandbox, where the file arrives as part of the mounted worktree.
CONFIG_PATH_ENV = "NIGHTSHIFT_CONFIG"

FALLBACK_MODEL = "google/gemini-3.5-flash"
FALLBACK_BASE_URL = "https://ai.hackclub.com/proxy/v1"


class ConfigError(RuntimeError):
    """Raised when a config file exists but cannot be read or validated."""


def _env_model() -> str:
    """`OPENROUTER_MODEL` as the per-agent default — layer 2 of the precedence order."""
    return os.getenv("OPENROUTER_MODEL") or FALLBACK_MODEL


def _env_base_url() -> str:
    return os.getenv("OPENROUTER_BASE_URL") or FALLBACK_BASE_URL


class _Strict(BaseModel):
    """Base for every config section: unknown keys are an error, not a shrug.

    A typo'd key in a hand-edited file (`priorties = [...]`) would otherwise be silently
    ignored and the user would spend a week wondering why nothing changed.
    """

    model_config = ConfigDict(extra="forbid")


class AgentConfig(_Strict):
    """Per-agent model choice and run budget.

    One block per agent rather than a single global model: triaging email and writing code
    have very different cost/quality trade-offs, and Phase 22's evaluation harness is meant
    to feed its results back into exactly these slugs.

    The three caps (Phase 12) are enforced in `AgentRunner.run` and stop a run cleanly with
    a recorded stop reason — they never raise, so partial work and the transcript survive.
    `0` means "no cap" for cost and time; `max_steps` has no off switch.
    """

    model: str = Field(
        default_factory=_env_model,
        min_length=1,
        description="OpenRouter model slug, e.g. 'google/gemini-3.5-flash'",
    )
    max_tokens: int = Field(
        default=16000, gt=0, description="Completion cap for this agent's calls"
    )
    max_steps: int = Field(
        default=8, gt=0, le=500, description="Agent loop iterations before the run is stopped"
    )
    max_cost_usd: float = Field(
        default=1.0,
        ge=0.0,
        description="USD this agent may spend in one run at [pricing] rates (0 = no cap)",
    )
    max_seconds: float = Field(
        default=900.0,
        ge=0.0,
        description="Wall-clock seconds one run may take before it is stopped (0 = no cap)",
    )


class AgentsConfig(_Strict):
    """The three agents the plan defines. Calendar and project land in Phases 14 and 9;
    their slugs are configurable now so those phases have nothing to invent."""

    email_agent: AgentConfig = Field(default_factory=AgentConfig)
    calendar_agent: AgentConfig = Field(default_factory=AgentConfig)
    project_agent: AgentConfig = Field(default_factory=AgentConfig)


class StyleConfig(_Strict):
    """How the briefing should read. Prompted verbatim, so keep it short and imperative."""

    tone: str = Field(
        default="Concise and factual. No filler, no flattery.",
        description="One line describing the voice the summaries should use",
    )
    max_summary_sentences: int = Field(
        default=2, gt=0, le=10, description="Upper bound on per-email summary length"
    )
    draft_replies: bool = Field(
        default=True,
        description="Whether agents should suggest draft replies (never sent; Phase 8 queues them)",
    )
    notes: list[str] = Field(
        default_factory=list, description="Extra free-form style rules for the agents"
    )


class ProjectConfig(_Strict):
    """A project the project agent may work on overnight (Phase 9).

    `goals` is host-authored text and the *only* thing from here that reaches the agent's
    prompt — see `runner/agents.py:project_agent`, which accepts no taint at all.

    `deploy_key` is a **path**, not key material: a path is not a secret, so it belongs in
    a committed config, while the key itself stays on the host filesystem and never enters
    the sandbox (the container has no remote access; `gitops.push_branch` pushes from the
    host). `push` defaults to False so a fresh checkout never writes to someone's remote
    before they have set the restricted key and the `agent/*` server-side rule up.
    """

    name: str = Field(min_length=1, description="Short human name for the project")
    path: str = Field(default="", description="Absolute path to the repo on the host")
    active: bool = Field(default=True, description="Include this project in nightly work")
    priority: int = Field(
        default=50, ge=0, le=100, description="Higher runs first when time is short"
    )
    branch_prefix: str = Field(
        default="agent/", description="Prefix for nightly branches (Phase 9 deploy-key scope)"
    )
    goals: list[str] = Field(
        default_factory=list, description="Standing goals for the overnight agent"
    )
    remote: str = Field(default="origin", description="Git remote the agent branch is pushed to")
    push: bool = Field(
        default=False, description="Push the nightly branch (needs an agent/*-restricted key)"
    )
    deploy_key: str = Field(
        default="",
        description="Path to the agent/*-restricted SSH key; else $NIGHTSHIFT_DEPLOY_KEY",
    )
    merge_into: str = Field(
        default="main", description="Branch an approved merge targets (never automatic)"
    )
    max_steps: int = Field(
        default=40, gt=0, le=500, description="Agent loop cap for this project's nightly run"
    )


class ScheduleConfig(_Strict):
    """When the night runs, and what it is allowed to do when it wakes up (Phase 10).

    This is the only config section the *daemon* reads before any agent starts, so it is
    deliberately boring: an hour, a minute, and a few refusals. `require_ac` defaults to
    True because a 40-minute container run on battery is how you wake up to a dead laptop;
    `caffeinate` defaults to True because a run that sleeps halfway through is worse than
    no run at all.

    `max_relaunches` bounds launchd's crash-restart loop. `KeepAlive = {SuccessfulExit:
    false}` re-runs a job that exits non-zero — correct for a crash, catastrophic for a
    crash that repeats, since each retry costs API budget and battery. The entrypoint
    counts its own attempts per night and gives up (exiting 0) once this many have burned.
    """

    hour: int = Field(default=3, ge=0, le=23, description="Local hour the night starts")
    minute: int = Field(default=0, ge=0, le=59)
    require_ac: bool = Field(
        default=True, description="Refuse to run on battery (Phase 10 power guard)"
    )
    caffeinate: bool = Field(
        default=True, description="Hold the machine awake for the run window only"
    )
    max_relaunches: int = Field(
        default=3, ge=0, le=20, description="Crash-relaunch budget per night before giving up"
    )
    window_minutes: int = Field(
        default=180,
        ge=0,
        le=1440,
        description="How long after the scheduled time a run may still start (0 = always)",
    )
    projects: bool = Field(default=True, description="Run the project agent as part of the night")
    send: bool = Field(default=True, description="Email the briefing when the run completes")
    since: str = Field(default="8h", min_length=1, description="Email lookback for a nightly run")


class NotificationsConfig(_Strict):
    """The wake-up nudge (Phase 15). One switch, because there is one decision to make.

    On by default: a briefing nobody is told about is a briefing nobody reads, and the
    notification is the only part of the system that reaches a user who is not already
    looking at the menu bar. `enabled = false` is for someone who has turned the whole
    machine's notifications into a discipline and does not want another source.

    What the banner may *say* is not configurable on purpose — it is counts and host-written
    words only (security rule 2), and a knob that let email text into it would be a knob
    that let a hostile email write a system dialog.
    """

    enabled: bool = Field(
        default=True, description="Post a local notification when a night finishes"
    )


class ModelPriceConfig(_Strict):
    """What one model costs, in USD per million tokens."""

    input_per_mtok: float = Field(default=0.0, ge=0.0)
    output_per_mtok: float = Field(default=0.0, ge=0.0)


class PricingConfig(_Strict):
    """The price table the per-agent cost caps are evaluated against (Phase 12).

    Prices live in config rather than in code because they are the provider's numbers, not
    ours: they change without a release, they differ per routing proxy, and the person who
    swaps a model slug is the person who should state what it costs.

    **An unpriced slug is billed at the `unknown_*` rate**, which defaults to frontier-tier
    list prices. That is deliberately pessimistic. Pricing an unknown model at zero would
    silently switch the cost cap off for precisely the case it exists to catch — a slug
    nobody has checked — while over-pricing one only ends a run early, visibly, with
    `cost_limit` in the briefing.
    """

    unknown_input_per_mtok: float = Field(
        default=15.0, ge=0.0, description="Input price assumed for a model not listed below"
    )
    unknown_output_per_mtok: float = Field(
        default=75.0, ge=0.0, description="Output price assumed for a model not listed below"
    )
    models: dict[str, ModelPriceConfig] = Field(
        default_factory=dict, description="Model slug → price, e.g. [pricing.models.\"a/b\"]"
    )


class RetentionConfig(_Strict):
    """How long stored history is kept (Phases 12 and 13).

    Transcripts are the only thing NightShift writes that grows without bound — one row
    per agent run per night, each carrying a whole model conversation. The nightly run
    prunes at the end of every night, so the default policy is finite rather than "until
    the disk fills". `0` disables pruning and means you have decided to manage the file
    yourself.

    Snapshots (Phase 13) grow the same way, one per project per night, but ageing them out
    has a sharper edge: the thing being deleted is the only way to undo a bad night. So
    `snapshot_keep` always spares that many most-recent snapshots per project regardless of
    `snapshot_days`, and the age policy only applies beyond them.
    """

    transcript_days: int = Field(
        default=30,
        ge=0,
        le=3650,
        description="Days of agent transcripts and run history to keep (0 = forever)",
    )
    snapshot_days: int = Field(
        default=30,
        ge=0,
        le=3650,
        description="Days of pre-run project snapshots to keep (0 = forever)",
    )
    snapshot_keep: int = Field(
        default=5,
        ge=0,
        le=1000,
        description="Most-recent snapshots per project never pruned, however old",
    )


class LLMConfig(_Strict):
    """LLM endpoint. The API *key* is deliberately absent — secrets stay out of config."""

    base_url: str = Field(
        default_factory=_env_base_url,
        min_length=1,
        description="OpenAI-compatible base URL; the egress proxy allowlists this host",
    )


class StandingInstructions(_Strict):
    """The whole config file, validated."""

    version: int = Field(default=1, ge=1, description="Schema version of this file")
    priorities: list[str] = Field(
        default_factory=list,
        description="What matters, most important first. Fed to every agent's prompt.",
    )
    style: StyleConfig = Field(default_factory=StyleConfig)
    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    pricing: PricingConfig = Field(default_factory=PricingConfig)
    retention: RetentionConfig = Field(default_factory=RetentionConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    schedule: ScheduleConfig = Field(default_factory=ScheduleConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    projects: list[ProjectConfig] = Field(default_factory=list)

    # Set by the loader so callers (and the briefing) can say where the config came from.
    source_path: str = Field(
        default="", description="File this config was loaded from; empty means defaults"
    )

    def agent(self, name: str) -> AgentConfig:
        """Resolve one agent's config by name, e.g. `agent('email_agent')`."""
        try:
            return getattr(self.agents, name)
        except AttributeError as exc:
            known = ", ".join(AgentsConfig.model_fields)
            raise ConfigError(f"Unknown agent {name!r}. Known agents: {known}.") from exc

    def active_projects(self) -> list[ProjectConfig]:
        """Active projects, highest priority first (Phase 9 consumes this)."""
        return sorted(
            (p for p in self.projects if p.active), key=lambda p: -p.priority
        )

    def project(self, name: str) -> ProjectConfig:
        """Look a project up by name, active or not.

        Used by the `merge_branch` approval executor, which must resolve a *reviewed*
        branch's repo path hours after the run — possibly after the project was
        deactivated. An unknown name is an error, never a guessed path: the alternative is
        running `git merge` somewhere nobody asked for.
        """
        for project in self.projects:
            if project.name == name:
                return project
        known = ", ".join(p.name for p in self.projects) or "(none configured)"
        raise ConfigError(f"Unknown project {name!r}. Configured projects: {known}.")


def _format_validation_error(path: Path, exc: ValidationError) -> str:
    """Turn Pydantic's error list into something a human editing TOML can act on."""
    lines = [f"{path} is not a valid standing-instructions file:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "(root)"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def resolve_config_path(path: Path | str | None = None) -> Path:
    """Where the config would be read from, applying the `--config` → env → default order."""
    if path is not None:
        return Path(path).expanduser()
    from_env = os.getenv(CONFIG_PATH_ENV)
    if from_env:
        return Path(from_env).expanduser()
    return DEFAULT_CONFIG_PATH


def load_config(path: Path | str | None = None) -> StandingInstructions:
    """Load and validate the standing instructions.

    A missing file at the *default* location is fine — you get validated defaults (with
    env still acting as the fallback layer). A file the caller asked for explicitly
    (`--config` or `$NIGHTSHIFT_CONFIG`) that isn't there is an error: silently ignoring
    it would run the night under settings the user thinks they replaced.
    """
    resolved = resolve_config_path(path)
    explicit = path is not None or bool(os.getenv(CONFIG_PATH_ENV))

    if not resolved.exists():
        if explicit:
            raise ConfigError(f"Config file not found: {resolved}")
        return StandingInstructions()

    try:
        raw = tomllib.loads(resolved.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{resolved} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"Could not read {resolved}: {exc}") from exc

    try:
        return StandingInstructions.model_validate({**raw, "source_path": str(resolved)})
    except ValidationError as exc:
        raise ConfigError(_format_validation_error(resolved, exc)) from exc


# The process-wide config. Entry points call `use_config(load_config(args.config))` once;
# everything downstream calls `active_config()` and stays free of path plumbing.
_active: StandingInstructions | None = None


def use_config(config: StandingInstructions) -> None:
    """Install the config the rest of this process should use."""
    global _active
    _active = config


def active_config() -> StandingInstructions:
    """The installed config, loading the default one on first use."""
    global _active
    if _active is None:
        _active = load_config()
    return _active


def reset_config() -> None:
    """Forget the installed config (tests; also lets a long-lived daemon reload)."""
    global _active
    _active = None
