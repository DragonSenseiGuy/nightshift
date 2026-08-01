"""Standing instructions (Phase 5): resolution, precedence, defaults, and reach.

The point of this phase is that a human can change agent behaviour by editing a TOML file
— so these tests pin the four ways that can go wrong: the file's values not winning, a
missing file taking the night down, a typo failing silently or opaquely, and the
priorities/style never actually reaching the model. Everything here is offline; the LLM
call is stubbed and no test may open a socket.
"""

from __future__ import annotations

import textwrap

import pytest

from config import (
    DEFAULT_CONFIG_PATH,
    AgentsConfig,
    ConfigError,
    StandingInstructions,
    load_config,
    resolve_config_path,
)

SAMPLE = textwrap.dedent(
    """
    version = 1
    priorities = ["Money first", "Then blocked humans"]

    [style]
    tone = "Terse."
    max_summary_sentences = 1
    notes = ["Numbers over adjectives."]

    [agents.email_agent]
    model = "test/email-model"
    max_tokens = 1234

    [agents.calendar_agent]
    model = "test/calendar-model"

    [agents.project_agent]
    model = "test/project-model"
    max_tokens = 4321

    [llm]
    base_url = "https://example.invalid/v1"

    [[projects]]
    name = "nightshift"
    path = "/tmp/nightshift"
    priority = 90
    goals = ["Ship phase 5"]

    [[projects]]
    name = "dormant"
    active = false
    """
)


@pytest.fixture
def sample_config(tmp_path):
    path = tmp_path / "standing_instructions.toml"
    path.write_text(SAMPLE, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """No ambient config path or model env leaking in from the developer's shell."""
    monkeypatch.delenv("NIGHTSHIFT_CONFIG", raising=False)
    monkeypatch.delenv("OPENROUTER_MODEL", raising=False)
    monkeypatch.delenv("OPENROUTER_BASE_URL", raising=False)


# --- per-agent model resolution (the phase's Verify step) -------------------------------


def test_each_agent_resolves_its_own_model(sample_config):
    config = load_config(sample_config)

    assert config.agent("email_agent").model == "test/email-model"
    assert config.agent("calendar_agent").model == "test/calendar-model"
    assert config.agent("project_agent").model == "test/project-model"

    # Budgets are per-agent too, and an omitted one falls back to the default.
    assert config.agent("email_agent").max_tokens == 1234
    assert config.agent("project_agent").max_tokens == 4321
    assert config.agent("calendar_agent").max_tokens == 16000


def test_unknown_agent_name_is_a_clear_error(sample_config):
    with pytest.raises(ConfigError) as exc:
        load_config(sample_config).agent("weather_agent")
    assert "weather_agent" in str(exc.value)
    assert "email_agent" in str(exc.value)


def test_committed_default_config_is_valid_and_names_all_three_agents():
    """The shipped file must work out of the box — it's the thing users edit."""
    config = load_config(DEFAULT_CONFIG_PATH)
    for name in AgentsConfig.model_fields:
        assert config.agent(name).model
    assert config.priorities
    assert config.source_path == str(DEFAULT_CONFIG_PATH)


# --- defaults / missing files -----------------------------------------------------------


def test_missing_default_file_falls_back_to_validated_defaults(monkeypatch, tmp_path):
    """A 3am run must not die because the config file is gone."""
    monkeypatch.setattr("config.DEFAULT_CONFIG_PATH", tmp_path / "nope.toml")
    config = load_config()

    assert isinstance(config, StandingInstructions)
    assert config.source_path == ""
    assert config.agent("email_agent").model  # a usable slug, not empty
    assert config.priorities == []


def test_missing_explicit_config_is_an_error(tmp_path):
    """An explicitly requested file that isn't there is a mistake, not a default."""
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "absent.toml")
    assert "not found" in str(exc.value)


# --- malformed input --------------------------------------------------------------------


def test_invalid_toml_raises_a_readable_error(tmp_path):
    path = tmp_path / "broken.toml"
    path.write_text('priorities = ["unclosed\n', encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "not valid TOML" in str(exc.value)
    assert str(path) in str(exc.value)


def test_wrong_types_name_the_offending_field(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text(
        '[agents.email_agent]\nmodel = 12\nmax_tokens = -5\n', encoding="utf-8"
    )

    with pytest.raises(ConfigError) as exc:
        load_config(path)
    message = str(exc.value)
    assert "agents.email_agent.model" in message
    assert "agents.email_agent.max_tokens" in message


def test_unknown_key_is_rejected_rather_than_silently_ignored(tmp_path):
    """A typo'd key must not leave the user wondering why nothing changed."""
    path = tmp_path / "typo.toml"
    path.write_text('priorties = ["oops"]\n', encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(path)
    assert "priorties" in str(exc.value)


# --- precedence: file > env > built-in --------------------------------------------------


def test_config_file_beats_env(monkeypatch, sample_config):
    monkeypatch.setenv("OPENROUTER_MODEL", "env/should-lose")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://env.invalid/v1")

    config = load_config(sample_config)
    assert config.agent("email_agent").model == "test/email-model"
    assert config.llm.base_url == "https://example.invalid/v1"


def test_env_is_the_fallback_when_the_file_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr("config.DEFAULT_CONFIG_PATH", tmp_path / "nope.toml")
    monkeypatch.setenv("OPENROUTER_MODEL", "env/model")
    monkeypatch.setenv("OPENROUTER_BASE_URL", "https://env.invalid/v1")

    config = load_config()
    assert config.agent("email_agent").model == "env/model"
    assert config.agent("project_agent").model == "env/model"
    assert config.llm.base_url == "https://env.invalid/v1"


def test_env_fills_fields_the_file_omits(monkeypatch, tmp_path):
    path = tmp_path / "partial.toml"
    path.write_text('[agents.email_agent]\nmodel = "file/email"\n', encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_MODEL", "env/model")

    config = load_config(path)
    assert config.agent("email_agent").model == "file/email"
    assert config.agent("calendar_agent").model == "env/model"


def test_nightshift_config_env_names_the_file(monkeypatch, sample_config):
    monkeypatch.setenv("NIGHTSHIFT_CONFIG", str(sample_config))
    assert resolve_config_path() == sample_config
    assert load_config().agent("email_agent").model == "test/email-model"


def test_explicit_path_beats_the_env_var(monkeypatch, sample_config, tmp_path):
    other = tmp_path / "other.toml"
    other.write_text('[agents.email_agent]\nmodel = "other/model"\n', encoding="utf-8")
    monkeypatch.setenv("NIGHTSHIFT_CONFIG", str(sample_config))

    assert load_config(other).agent("email_agent").model == "other/model"


# --- projects ---------------------------------------------------------------------------


def test_active_projects_are_filtered_and_ranked(sample_config):
    projects = load_config(sample_config).active_projects()
    assert [p.name for p in projects] == ["nightshift"]
    assert projects[0].goals == ["Ship phase 5"]


# --- the config actually reaches the model ----------------------------------------------


def test_priorities_and_style_reach_the_system_prompt(sample_config):
    from summarise import build_system_prompt

    prompt = build_system_prompt(load_config(sample_config))
    assert "Money first" in prompt
    assert "Then blocked humans" in prompt
    assert "Terse." in prompt
    assert "at most 1 sentence" in prompt
    assert "Numbers over adjectives." in prompt
    assert "nightshift" in prompt
    assert "dormant" not in prompt  # inactive projects stay out of the prompt
    # The untrusted-input warning must survive the append.
    assert "untrusted data" in prompt


def test_draft_replies_can_be_switched_off(tmp_path):
    path = tmp_path / "no_drafts.toml"
    path.write_text("[style]\ndraft_replies = false\n", encoding="utf-8")

    from summarise import build_system_prompt

    assert "always set draft_reply to null" in build_system_prompt(load_config(path))


def test_summariser_uses_the_configured_model_and_endpoint(monkeypatch, sample_config):
    """End-to-end through `build_digest`, with the OpenAI client stubbed out.

    This is the acceptance criterion in test form: change the slug in the file, and the
    next run requests a different model with no code edit.
    """
    import summarise
    from runner import backends
    from fixtures.mock_emails import mock_emails

    seen: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs):
            seen.update(kwargs)

            class Message:
                content = '{"overview": "ok", "emails": []}'

            class Choice:
                message = Message()

            class Response:
                choices = [Choice()]

            return Response()

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    def fake_openai(*, base_url, api_key):
        seen["base_url"] = base_url
        return FakeClient()

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    # Since Phase 6 the LLM client is constructed in exactly one place, `runner.backends`
    # — `summarise` no longer imports an LLM SDK at all.
    monkeypatch.setattr(backends, "OpenAI", fake_openai)

    config = load_config(sample_config)
    digest = summarise.build_digest(mock_emails(), since="2h", config=config)

    assert seen["model"] == "test/email-model"
    assert seen["max_tokens"] == 1234
    assert seen["base_url"] == "https://example.invalid/v1"
    assert "Money first" in seen["messages"][0]["content"]
    # The model returned nothing per-email, so every fixture email is backfilled.
    assert digest.count == len(mock_emails())


# --- how config reaches the sandbox ------------------------------------------------------


def test_config_is_staged_into_the_worktree_for_the_sandbox(tmp_path, sample_config):
    """The sandbox reads config as a file in its mounted worktree — no new channel."""
    from sandbox import orchestrator

    worktree = tmp_path / "worktree"
    worktree.mkdir()

    assert orchestrator.stage_config(worktree, sample_config) is True
    staged = worktree / orchestrator.WORKTREE_CONFIG_REL
    assert staged.read_text(encoding="utf-8") == sample_config.read_text(encoding="utf-8")

    env = orchestrator.sandbox_environment(since="2h", config_in_worktree=True)
    assert env["NIGHTSHIFT_CONFIG"] == orchestrator.SANDBOX_CONFIG_PATH
    assert orchestrator.SANDBOX_CONFIG_PATH.startswith(orchestrator.WORKSPACE)
    # Staging config must not smuggle anything else in.
    assert "GOOGLE_CLIENT_SECRET" not in env
    assert "NIGHTSHIFT_API_URL" not in env


def test_staging_a_malformed_config_fails_on_the_host(tmp_path):
    """Better to blow up here than inside a container at 3am."""
    from sandbox import orchestrator

    bad = tmp_path / "bad.toml"
    bad.write_text('[agents.email_agent]\nmodel = 3\n', encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    with pytest.raises(ConfigError):
        orchestrator.stage_config(worktree, bad)


def test_absent_config_leaves_the_sandbox_on_defaults(tmp_path, monkeypatch):
    from sandbox import orchestrator

    monkeypatch.setattr("config.DEFAULT_CONFIG_PATH", tmp_path / "nope.toml")
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    assert orchestrator.stage_config(worktree, None) is False
    assert "NIGHTSHIFT_CONFIG" not in orchestrator.sandbox_environment(since="2h")


def test_active_config_is_installable_and_resettable(sample_config):
    from config import active_config, reset_config, use_config

    try:
        use_config(load_config(sample_config))
        assert active_config().agent("email_agent").model == "test/email-model"
    finally:
        reset_config()
