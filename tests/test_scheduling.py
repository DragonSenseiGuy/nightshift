"""Scheduling, caffeinate, power guard and watchdog (Phase 10).

Four things can go wrong overnight in ways nobody is awake to see, and each has a test
here that does not need a Mac in any particular state:

- the **plist** telling launchd to loop a finished run (or to run at login), so it is
  parsed back with `plistlib` and its schedule/KeepAlive/RunAtLoad asserted;
- **caffeinate** leaking past the run and pinning the machine awake forever, so it is
  asserted released even when the block raises;
- the **power guard** misreading `pmset` and running a 40-minute job on battery, so both
  outputs are parsed from canned text;
- a **stage crashing** and taking the briefing with it, so a failing stage is asserted to
  still produce an artifact naming the failure.

Nothing here shells out to `launchctl`, `pmset` or `caffeinate`: every probe is a pure
parse and every process is injected. The one test that would need a real launchd is
marked `launchd` and skipped unless NIGHTSHIFT_LAUNCHD_TESTS=1.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import textwrap
from pathlib import Path

import pytest

from config import StandingInstructions, load_config
from models import Briefing
from orchestrator import caffeinate as caffeinate_mod
from orchestrator import launchd, nightly, power

# --------------------------------------------------------------------------------------
# pmset fixtures — real output from a laptop on each source.
# --------------------------------------------------------------------------------------

AC_BATT = (
    "Now drawing from 'AC Power'\n"
    " -InternalBattery-0 (id=23134307)\t84%; charging; (no estimate) present: true\n"
)
BATTERY_BATT = (
    "Now drawing from 'Battery Power'\n"
    " -InternalBattery-0 (id=23134307)\t61%; discharging; 3:42 remaining present: true\n"
)
DESKTOP_BATT = "Now drawing from 'AC Power'\n"

CLAMSHELL_OPEN = '  |   "AppleClamshellCausesSleep" = No\n  |   "AppleClamshellState" = No\n'
CLAMSHELL_CLOSED = '  |   "AppleClamshellCausesSleep" = Yes\n  |   "AppleClamshellState" = Yes\n'

INTERNAL_DISPLAY = """
{"SPDisplaysDataType": [{"_name": "Apple M4 Pro", "spdisplays_ndrvs": [
  {"_name": "Color LCD", "spdisplays_connection_type": "spdisplays_internal"}]}]}
"""
EXTERNAL_DISPLAY = """
{"SPDisplaysDataType": [{"_name": "Apple M4 Pro", "spdisplays_ndrvs": [
  {"_name": "Color LCD", "spdisplays_connection_type": "spdisplays_internal"},
  {"_name": "DELL U2720Q", "spdisplays_connection_type": "spdisplays_displayport_dongle"}]}]}
"""


# --------------------------------------------------------------------------------------
# Power guard
# --------------------------------------------------------------------------------------


def test_parses_ac_power():
    on_ac, percent, charging, source = power.parse_pmset_batt(AC_BATT)
    assert on_ac is True
    assert percent == 84
    assert charging is True
    assert "AC Power" in source


def test_parses_battery_power():
    on_ac, percent, charging, _ = power.parse_pmset_batt(BATTERY_BATT)
    assert on_ac is False
    assert percent == 61
    assert charging is False


def test_unreadable_pmset_is_unknown_not_battery():
    # A wording change in a future macOS must not read as "on battery" (that would cancel
    # every night) nor as "on AC" (that would run one on a draining laptop).
    on_ac, percent, _, source = power.parse_pmset_batt("some future format\n")
    assert on_ac is None and percent is None and source == ""


def test_clamshell_and_display_parsing():
    assert power.parse_clamshell(CLAMSHELL_OPEN) is False
    assert power.parse_clamshell(CLAMSHELL_CLOSED) is True
    assert power.parse_clamshell("") is None  # a Mac mini has no lid
    assert power.parse_displays(INTERNAL_DISPLAY) is False
    assert power.parse_displays(EXTERNAL_DISPLAY) is True
    assert power.parse_displays("not json") is None


def test_battery_run_is_refused_with_a_reason():
    state = power.read_power_state(
        pmset_text=BATTERY_BATT, clamshell_text=CLAMSHELL_OPEN, displays_text=INTERNAL_DISPLAY
    )
    refusal = power.power_refusal(state)
    assert refusal
    assert "battery" in refusal.lower()
    assert "61%" in refusal


def test_ac_run_is_allowed():
    state = power.read_power_state(
        pmset_text=AC_BATT, clamshell_text=CLAMSHELL_OPEN, displays_text=INTERNAL_DISPLAY
    )
    assert power.power_refusal(state) is None
    assert "AC power" in state.describe()


def test_require_ac_false_overrides_the_guard():
    state = power.read_power_state(
        pmset_text=BATTERY_BATT, clamshell_text=CLAMSHELL_OPEN, displays_text=INTERNAL_DISPLAY
    )
    assert power.power_refusal(state, require_ac=False) is None


def test_unknown_power_source_does_not_block_the_night():
    state = power.read_power_state(
        pmset_text="", clamshell_text="", displays_text=""
    )
    assert power.power_refusal(state) is None
    assert any("pmset" in note for note in state.notes)


def test_closed_lid_without_display_warns_but_does_not_refuse():
    state = power.read_power_state(
        pmset_text=AC_BATT, clamshell_text=CLAMSHELL_CLOSED, displays_text=INTERNAL_DISPLAY
    )
    assert power.power_refusal(state) is None
    assert any("sleep mid-run" in note for note in state.notes)


# --------------------------------------------------------------------------------------
# caffeinate
# --------------------------------------------------------------------------------------


class FakeProcess:
    """Stands in for the caffeinate child: records how it was ended."""

    def __init__(self, command):
        self.command = command
        self.terminated = False
        self.killed = False
        self._alive = True

    def poll(self):
        return None if self._alive else 0

    def terminate(self):
        self.terminated = True
        self._alive = False

    def kill(self):
        self.killed = True
        self._alive = False

    def wait(self, timeout=None):
        self._alive = False
        return 0


def test_caffeinate_flags_block_every_kind_of_sleep():
    command = caffeinate_mod.caffeinate_command(4242)
    assert command[:5] == ["caffeinate", "-d", "-i", "-m", "-s"]
    # -w <pid>: the child releases itself if this process is killed without unwinding.
    assert command[5:] == ["-w", "4242"]


def test_caffeinate_is_spawned_and_released():
    spawned = []
    with caffeinate_mod.keep_awake(spawn=lambda cmd: spawned.append(FakeProcess(cmd)) or spawned[-1]):
        assert spawned and spawned[0].poll() is None
    assert spawned[0].terminated is True


def test_caffeinate_is_released_when_the_run_raises():
    spawned = []

    with pytest.raises(RuntimeError):
        with caffeinate_mod.keep_awake(spawn=lambda cmd: spawned.append(FakeProcess(cmd)) or spawned[-1]):
            raise RuntimeError("the night exploded")

    # The whole point: a leaked assertion keeps the user's Mac awake indefinitely.
    assert spawned[0].terminated is True


def test_caffeinate_escalates_to_kill_when_terminate_hangs():
    class Stubborn(FakeProcess):
        def wait(self, timeout=None):
            if not self.killed:
                raise subprocess.TimeoutExpired(cmd="caffeinate", timeout=timeout)
            return 0

    process = Stubborn(["caffeinate"])
    caffeinate_mod.release(process)
    assert process.killed is True


def test_caffeinate_disabled_spawns_nothing():
    def explode(cmd):  # pragma: no cover - must never be called
        raise AssertionError("caffeinate spawned despite enabled=False")

    with caffeinate_mod.keep_awake(enabled=False, spawn=explode) as process:
        assert process is None


def test_missing_caffeinate_binary_does_not_kill_the_night():
    def missing(cmd):
        raise FileNotFoundError("caffeinate")

    with caffeinate_mod.keep_awake(spawn=missing) as process:
        assert process is None


# --------------------------------------------------------------------------------------
# launchd plist
# --------------------------------------------------------------------------------------


def _plist(**kwargs):
    kwargs.setdefault("uv_binary", "/Users/test/.local/bin/uv")
    kwargs.setdefault("repo_root", Path("/Users/test/NightShift"))
    kwargs.setdefault("home", Path("/Users/test"))
    kwargs.setdefault("log_dir", Path("/Users/test/Library/Logs/NightShift"))
    return plistlib.loads(launchd.plist_bytes(**kwargs))


def test_plist_is_valid_plist_xml():
    raw = launchd.plist_bytes(
        uv_binary="/Users/test/.local/bin/uv", repo_root=Path("/repo"), home=Path("/Users/test")
    )
    assert raw.startswith(b"<?xml")
    assert plistlib.loads(raw)["Label"] == launchd.LABEL


def test_plist_program_arguments_are_absolute_and_run_the_daemon():
    data = _plist()
    args = data["ProgramArguments"]
    assert args[0] == "/Users/test/.local/bin/uv"
    assert args[1:] == ["run", "python", "-m", "orchestrator", "run"]
    assert data["WorkingDirectory"] == "/Users/test/NightShift"
    # launchd hands the job a near-empty PATH; uv, colima and git all live outside it.
    assert "/Users/test/.local/bin" in data["EnvironmentVariables"]["PATH"]
    assert "/opt/homebrew/bin" in data["EnvironmentVariables"]["PATH"]
    assert data["EnvironmentVariables"]["HOME"] == "/Users/test"


def test_plist_schedule_is_the_requested_time():
    data = _plist(hour=3, minute=30)
    assert data["StartCalendarInterval"] == {"Hour": 3, "Minute": 30}


def test_plist_keepalive_relaunches_only_after_a_crash():
    data = _plist()
    # `KeepAlive: True` would restart a *successful* night immediately, forever.
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["ThrottleInterval"] >= 60
    # Installing the job (or logging in) must never start a night.
    assert data["RunAtLoad"] is False


def test_plist_logs_where_a_crashed_run_can_be_read_in_the_morning():
    data = _plist()
    assert data["StandardOutPath"].endswith("NightShift/nightly.out.log")
    assert data["StandardErrorPath"].endswith("NightShift/nightly.err.log")


def test_plist_carries_extra_run_arguments():
    data = _plist(arguments=["run", "--config", "/tmp/c.toml"])
    assert data["ProgramArguments"][-2:] == ["--config", "/tmp/c.toml"]


def test_plist_rejects_an_impossible_time():
    with pytest.raises(launchd.LaunchdError):
        _plist(hour=25)


def test_install_refuses_to_clobber_an_existing_plist(tmp_path):
    target = tmp_path / f"{launchd.LABEL}.plist"
    target.write_text("hand-tuned", encoding="utf-8")
    logs = tmp_path / "logs"

    with pytest.raises(launchd.LaunchdError) as exc:
        launchd.install(path=target, bootstrap=False, uv_binary="/bin/echo", log_dir=logs)
    assert "--force" in str(exc.value)
    assert target.read_text(encoding="utf-8") == "hand-tuned"

    launchd.install(path=target, bootstrap=False, force=True, uv_binary="/bin/echo", log_dir=logs)
    assert plistlib.loads(target.read_bytes())["Label"] == launchd.LABEL


def test_install_creates_the_log_directory_launchd_will_not(tmp_path):
    # A missing StandardOutPath directory makes launchd fail to spawn the job, and the
    # only record of that is in a log file it could not create.
    logs = tmp_path / "logs" / "NightShift"
    launchd.install(
        path=tmp_path / "j.plist", bootstrap=False, uv_binary="/bin/echo", log_dir=logs
    )
    assert logs.is_dir()


def test_uninstall_removes_the_plist(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(
        launchd,
        "_launchctl",
        lambda *args: calls.append(args)
        or subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr=""),
    )
    target = tmp_path / f"{launchd.LABEL}.plist"
    launchd.install(
        path=target, bootstrap=False, uv_binary="/bin/echo", log_dir=tmp_path / "logs"
    )

    assert launchd.uninstall(path=target) is True
    assert not target.exists()
    assert ("bootout", f"{launchd.domain()}/{launchd.LABEL}") in calls
    assert launchd.uninstall(path=target) is False  # idempotent


def test_schedule_time_comes_from_config(tmp_path):
    config_file = tmp_path / "c.toml"
    config_file.write_text(
        textwrap.dedent(
            """
            [schedule]
            hour = 2
            minute = 45
            require_ac = false
            """
        ),
        encoding="utf-8",
    )
    config = load_config(config_file)
    assert (config.schedule.hour, config.schedule.minute) == (2, 45)
    assert config.schedule.require_ac is False
    data = _plist(hour=config.schedule.hour, minute=config.schedule.minute)
    assert data["StartCalendarInterval"] == {"Hour": 2, "Minute": 45}


@pytest.mark.launchd
@pytest.mark.skipif(
    os.getenv("NIGHTSHIFT_LAUNCHD_TESTS") != "1",
    reason="needs a real launchctl and mutates the user's launchd domain",
)
def test_bootstrap_and_bootout_round_trip(tmp_path):
    label = "dev.nightshift.test-roundtrip"
    target = tmp_path / f"{label}.plist"
    launchd.install(label=label, path=target, uv_binary="/bin/echo", hour=4, minute=0)
    try:
        assert "loaded" in launchd.status(label=label, path=target)
    finally:
        launchd.uninstall(label=label, path=target)
    assert "not loaded" in launchd.status(label=label, path=target)


# --------------------------------------------------------------------------------------
# Watchdog
# --------------------------------------------------------------------------------------


def _no_caffeinate(monkeypatch):
    """Never pin the developer's Mac awake while the suite runs."""
    monkeypatch.setattr(nightly, "keep_awake", lambda **kwargs: caffeinate_mod.keep_awake(
        enabled=False, spawn=lambda cmd: None
    ))


def test_a_failing_stage_still_produces_a_briefing_naming_it(tmp_path, monkeypatch):
    _no_caffeinate(monkeypatch)
    monkeypatch.setattr(
        nightly, "_load_emails", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("broker down"))
    )
    out = tmp_path / "briefing.html"

    result = nightly.run_night(
        StandingInstructions(),
        out=out,
        send=False,
        projects=False,
        caffeinate=False,
        power_state=power.read_power_state(pmset_text=AC_BATT, clamshell_text="", displays_text=""),
    )

    assert result.failures == 1
    html = out.read_text(encoding="utf-8")
    assert "email_agent" in html
    assert "broker down" in html
    assert result.briefing_path == str(out)


def test_every_stage_failing_still_writes_the_artifact(tmp_path, monkeypatch):
    _no_caffeinate(monkeypatch)
    monkeypatch.setattr(
        nightly, "_load_emails", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no email"))
    )
    out = tmp_path / "briefing.html"

    result = nightly.run_night(
        StandingInstructions(projects=[]),
        out=out,
        send=False,
        projects=True,
        caffeinate=False,
        power_state=power.read_power_state(pmset_text=AC_BATT, clamshell_text="", displays_text=""),
    )

    assert out.exists()
    assert "Failures" in out.read_text(encoding="utf-8")
    assert result.ran is True


def test_battery_refuses_the_run_and_records_why(tmp_path, monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - must never run on battery
        raise AssertionError("the night ran on battery")

    monkeypatch.setattr(nightly, "_load_emails", explode)
    out = tmp_path / "briefing.html"

    result = nightly.run_night(
        StandingInstructions(),
        out=out,
        send=False,
        caffeinate=False,
        power_state=power.read_power_state(
            pmset_text=BATTERY_BATT, clamshell_text=CLAMSHELL_OPEN, displays_text=INTERNAL_DISPLAY
        ),
    )

    assert result.ran is False
    assert "battery" in result.refused.lower()
    html = out.read_text(encoding="utf-8")
    assert "Nightly run skipped" in html


def test_caffeinate_is_held_for_the_run_window_only(tmp_path, monkeypatch):
    spawned = []
    alive_during_run = []

    def fake_spawn(command):
        spawned.append(FakeProcess(command))
        return spawned[-1]

    monkeypatch.setattr(
        nightly, "keep_awake", lambda **kwargs: caffeinate_mod.keep_awake(
            enabled=kwargs.get("enabled", True), spawn=fake_spawn
        )
    )

    def observe(*args, **kwargs):
        alive_during_run.append(spawned[0].poll() is None)
        return []

    monkeypatch.setattr(nightly, "_load_emails", observe)

    nightly.run_night(
        StandingInstructions(),
        out=tmp_path / "briefing.html",
        send=False,
        projects=False,
        power_state=power.read_power_state(pmset_text=AC_BATT, clamshell_text="", displays_text=""),
    )

    assert alive_during_run == [True]
    assert spawned[0].terminated is True


def test_caffeinate_is_released_when_the_whole_night_crashes(tmp_path, monkeypatch):
    """A crash *inside* the run window must still release the sleep assertion.

    The per-stage guard cannot help here: this is an exception raised where no `stage`
    wraps it, which is exactly the shape of the bug that leaves a laptop awake for days.
    """
    from contextlib import contextmanager

    spawned = []
    monkeypatch.setattr(
        nightly,
        "keep_awake",
        lambda **kwargs: caffeinate_mod.keep_awake(
            spawn=lambda cmd: spawned.append(FakeProcess(cmd)) or spawned[-1]
        ),
    )

    @contextmanager
    def exploding_stage(*args, **kwargs):
        raise RuntimeError("the orchestrator itself fell over")
        yield  # pragma: no cover

    monkeypatch.setattr(nightly, "stage", exploding_stage)
    out = tmp_path / "briefing.html"

    with pytest.raises(RuntimeError):
        nightly.run_night(
            StandingInstructions(),
            out=out,
            send=False,
            projects=False,
            power_state=power.read_power_state(
                pmset_text=AC_BATT, clamshell_text="", displays_text=""
            ),
        )

    assert spawned[0].terminated is True
    # ... and the briefing is still on disk, because it is written in a `finally`.
    assert out.exists()


def test_no_caffeinate_when_the_run_is_refused(tmp_path, monkeypatch):
    def explode(command):  # pragma: no cover - must never be spawned
        raise AssertionError("caffeinated a run that never happened")

    monkeypatch.setattr(
        nightly, "keep_awake", lambda **kwargs: caffeinate_mod.keep_awake(spawn=explode)
    )
    nightly.run_night(
        StandingInstructions(),
        out=tmp_path / "briefing.html",
        send=False,
        power_state=power.read_power_state(pmset_text=BATTERY_BATT, clamshell_text="", displays_text=""),
    )


def test_mock_mode_never_sends(tmp_path, monkeypatch):
    _no_caffeinate(monkeypatch)
    monkeypatch.setattr(nightly, "_load_emails", lambda *a, **k: [])
    out = tmp_path / "briefing.html"

    result = nightly.run_night(
        StandingInstructions(schedule={"send": True}),
        out=out,
        mock=True,
        projects=False,
        caffeinate=False,
        power_state=power.read_power_state(pmset_text=AC_BATT, clamshell_text="", displays_text=""),
    )

    # If sending had been attempted, `send_emails` would need a Keychain credential.
    assert result.failures == 0
    assert out.exists()


def test_mock_mode_does_not_write_to_the_real_approval_queue(tmp_path, monkeypatch):
    # The queue is durable host state shared with the morning UI; a rehearsal must not
    # leave fabricated drafts in it. (`--queue-drafts` still forces it on.)
    _no_caffeinate(monkeypatch)

    def explode(*args, **kwargs):  # pragma: no cover - must never be constructed
        raise AssertionError("a mock run touched the approval queue")

    monkeypatch.setattr("approvals.ApprovalQueue", explode)
    from fixtures.mock_emails import mock_emails
    from models import EmailDigest

    monkeypatch.setattr(nightly, "_load_emails", lambda *a, **k: mock_emails())
    monkeypatch.setattr(
        "summarise.build_digest", lambda emails, **k: EmailDigest(since="8h", items=[])
    )

    result = nightly.run_night(
        StandingInstructions(),
        out=tmp_path / "briefing.html",
        mock=True,
        projects=False,
        caffeinate=False,
        power_state=power.read_power_state(pmset_text=AC_BATT, clamshell_text="", displays_text=""),
    )
    assert result.failures == 0


def test_stage_helper_records_the_failure_and_continues():
    briefing = Briefing(date="today")
    reached = []
    with nightly.stage(briefing, "email_agent", "Summarising email failed"):
        raise ValueError("kaboom")
    reached.append(True)

    assert reached == [True]
    assert briefing.failures[0].stage == "email_agent"
    assert "kaboom" in briefing.failures[0].detail


# --------------------------------------------------------------------------------------
# Relaunch budget — KeepAlive.SuccessfulExit=false must not become an infinite loop.
# --------------------------------------------------------------------------------------


def test_relaunch_attempts_accumulate_per_night(tmp_path):
    state = tmp_path / "relaunch.json"
    assert nightly.claim_attempt(state, "2026-07-24") == 1
    assert nightly.claim_attempt(state, "2026-07-24") == 2
    assert nightly.claim_attempt(state, "2026-07-24") == 3
    # A new night starts from scratch, however badly the last one went.
    assert nightly.claim_attempt(state, "2026-07-25") == 1


def test_a_clean_run_clears_the_budget(tmp_path):
    state = tmp_path / "relaunch.json"
    nightly.claim_attempt(state, "2026-07-24")
    nightly.clear_attempts(state)
    assert not state.exists()
    assert nightly.claim_attempt(state, "2026-07-24") == 1


def test_corrupt_relaunch_state_does_not_stop_the_night(tmp_path):
    state = tmp_path / "relaunch.json"
    state.write_text("{not json", encoding="utf-8")
    assert nightly.claim_attempt(state, "2026-07-24") == 1


def test_window_covers_the_scheduled_time_and_a_late_start():
    from datetime import datetime as dt

    assert nightly.within_window(dt(2026, 7, 24, 3, 0), 3, 0, 180) is True
    assert nightly.within_window(dt(2026, 7, 24, 3, 20), 3, 0, 180) is True  # crash relaunch
    assert nightly.within_window(dt(2026, 7, 24, 5, 59), 3, 0, 180) is True


def test_window_rejects_the_times_launchd_starts_a_job_uninvited():
    from datetime import datetime as dt

    # Bootstrapping the job at bedtime (KeepAlive starts it immediately) ...
    assert nightly.within_window(dt(2026, 7, 24, 21, 0), 3, 0, 180) is False
    # ... and a missed 3am interval firing when the Mac wakes at 09:00.
    assert nightly.within_window(dt(2026, 7, 24, 9, 0), 3, 0, 180) is False


def test_window_wraps_across_midnight():
    from datetime import datetime as dt

    # A 23:30 schedule with a 3h window is still open at 01:00 the next day.
    assert nightly.within_window(dt(2026, 7, 25, 1, 0), 23, 30, 180) is True
    assert nightly.within_window(dt(2026, 7, 25, 4, 0), 23, 30, 180) is False
    assert nightly.within_window(dt(2026, 7, 25, 4, 0), 23, 30, 0) is True  # 0 disables it


def test_uninvited_start_exits_zero_without_touching_the_briefing(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "RELAUNCH_STATE", tmp_path / "relaunch.json")
    monkeypatch.setattr(
        nightly, "run_night", lambda *a, **k: (_ for _ in ()).throw(AssertionError("ran"))
    )
    config_file = tmp_path / "c.toml"
    # A window that cannot contain "now", whenever the suite happens to run.
    config_file.write_text("[schedule]\nwindow_minutes = 1\n", encoding="utf-8")
    briefing = tmp_path / "briefing.html"
    briefing.write_text("last night's briefing", encoding="utf-8")

    from datetime import datetime as dt

    class Clock(dt):
        @classmethod
        def now(cls, tz=None):
            return dt(2026, 7, 24, 12, 0)

    monkeypatch.setattr(nightly, "datetime", Clock)
    assert nightly.main(["--config", str(config_file), "--out", str(briefing)]) == 0
    # Overwriting it with an empty briefing would destroy an artifact nobody has read.
    assert briefing.read_text(encoding="utf-8") == "last night's briefing"


def test_exhausted_budget_exits_zero_so_launchd_stops(tmp_path, monkeypatch):
    state = tmp_path / "relaunch.json"
    monkeypatch.setattr(nightly, "RELAUNCH_STATE", state)
    out = tmp_path / "briefing.html"

    def explode(*args, **kwargs):  # pragma: no cover - budget must be checked first
        raise AssertionError("ran the night despite an exhausted relaunch budget")

    monkeypatch.setattr(nightly, "run_night", explode)
    for _ in range(4):  # max_relaunches (3) + the scheduled run
        nightly.claim_attempt(state, nightly.datetime.now().date().isoformat())

    code = nightly.main(["--out", str(out), "--mock", "--now"])

    assert code == 0  # non-zero here would ask launchd to relaunch it again
    assert "abandoned" in out.read_text(encoding="utf-8")


def test_a_crash_exits_non_zero_so_launchd_relaunches(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "RELAUNCH_STATE", tmp_path / "relaunch.json")

    def crash(*args, **kwargs):
        raise RuntimeError("colima vanished")

    monkeypatch.setattr(nightly, "run_night", crash)
    assert nightly.main(["--out", str(tmp_path / "b.html"), "--mock", "--now"]) == 1


def test_a_night_with_failures_still_exits_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "RELAUNCH_STATE", tmp_path / "relaunch.json")
    monkeypatch.setattr(
        nightly, "run_night", lambda *a, **k: nightly.NightResult(failures=3, briefing_path="x")
    )
    # Failures are recorded in the briefing; re-running the night would double the spend
    # for the same answer.
    assert nightly.main(["--out", str(tmp_path / "b.html"), "--now"]) == 0


def test_a_broken_config_exits_zero_rather_than_looping(tmp_path, monkeypatch):
    monkeypatch.setattr(nightly, "RELAUNCH_STATE", tmp_path / "relaunch.json")
    broken = tmp_path / "broken.toml"
    broken.write_text("priorities = 5\n", encoding="utf-8")
    assert nightly.main(["--config", str(broken)]) == 0
