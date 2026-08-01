"""Power guard: is this machine in a state where a night's work is a good idea? (Phase 10)

A nightly run boots a Linux VM, holds the CPU busy for tens of minutes and blocks sleep.
On mains that is fine. On battery it is a laptop that is dead by morning — and the user
never asked for that, they asked for a briefing. So the guard defaults to refusing, and
the refusal is recorded rather than swallowed: a night that did not happen must be
visible in the morning, exactly like a night that failed (the failures-are-never-
silent rule, applied one step earlier).

What is actually detectable, honestly:

- **AC vs battery** — `pmset -g batt` prints ``Now drawing from 'AC Power'`` or
  ``'Battery Power'``. Reliable, cheap, and the one signal the guard blocks on.
- **Lid** — `ioreg -k AppleClamshellState` reports Yes/No. Present on laptops, absent on
  a Mac mini/Studio, where "the lid" is not a thing; absent reads as *unknown*, never as
  closed, because a desktop must not be refused for lacking a lid.
- **External display** — `system_profiler SPDisplaysDataType -json`, counting displays
  whose `spdisplays_connection_type` is not internal. ~0.3s.

The lid/display pair only *matters* together: a closed lid with no external display means
macOS sleeps, and `caffeinate` cannot override clamshell sleep. But note that if this code
is executing at all, the machine is already awake — so that combination is reported as a
warning, not a refusal. AC is the hard gate; everything else is advice printed into the
run log and (Phase 11) the bedtime warning in the UI.

Every probe is parsed from text by a pure function, so the tests cover both an AC machine
and a battery machine without owning either.
"""

from __future__ import annotations

import json
import re
import subprocess

from pydantic import BaseModel, ConfigDict, Field

# Probes are read-only and fast; a hung one must not hold the night hostage.
PROBE_TIMEOUT = 10.0

_PERCENT = re.compile(r"(\d{1,3})%")


class PowerState(BaseModel):
    """What we could learn about the machine's power situation. `None` means unknown."""

    model_config = ConfigDict(extra="forbid")

    on_ac: bool | None = Field(default=None, description="True on mains, False on battery")
    battery_percent: int | None = Field(default=None, ge=0, le=100)
    charging: bool | None = Field(default=None)
    lid_closed: bool | None = Field(default=None, description="None on machines with no lid")
    external_display: bool | None = Field(default=None)
    source: str = Field(default="", max_length=200, description="pmset's power-source line")
    notes: list[str] = Field(default_factory=list, max_length=20)

    def describe(self) -> str:
        bits = [
            "AC power" if self.on_ac else ("battery" if self.on_ac is False else "unknown source")
        ]
        if self.battery_percent is not None:
            bits.append(f"{self.battery_percent}%")
        if self.lid_closed is not None:
            bits.append("lid closed" if self.lid_closed else "lid open")
        if self.external_display is not None:
            bits.append("external display" if self.external_display else "no external display")
        return ", ".join(bits)


def parse_pmset_batt(text: str) -> tuple[bool | None, int | None, bool | None, str]:
    """Parse `pmset -g batt` (or `-g ps`) into (on_ac, percent, charging, source line).

    Matching on the quoted source name rather than the word "AC" anywhere in the output:
    the battery line itself can contain "AC" adjacent words, and a false "we're on mains"
    is the one mistake this function must not make.
    """
    source_line = ""
    on_ac: bool | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("now drawing from"):
            source_line = stripped[:200]
            lowered = stripped.lower()
            if "'ac power'" in lowered:
                on_ac = True
            elif "'battery power'" in lowered:
                on_ac = False
            break

    percent = None
    match = _PERCENT.search(text)
    if match:
        value = int(match.group(1))
        percent = value if 0 <= value <= 100 else None

    charging: bool | None = None
    lowered = text.lower()
    if "; charging" in lowered or "charged" in lowered:
        charging = True
    elif "discharging" in lowered:
        charging = False

    return on_ac, percent, charging, source_line


def parse_clamshell(text: str) -> bool | None:
    """Parse `ioreg -r -k AppleClamshellState -d 4`. Absent key ⇒ None (no lid / unknown)."""
    for line in text.splitlines():
        if "AppleClamshellState" in line and "CausesSleep" not in line:
            value = line.split("=", 1)[-1].strip().strip('"').lower()
            if value in {"yes", "true"}:
                return True
            if value in {"no", "false"}:
                return False
    return None


def parse_displays(text: str) -> bool | None:
    """Parse `system_profiler SPDisplaysDataType -json` → is a non-internal display attached?"""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    entries = data.get("SPDisplaysDataType")
    if not isinstance(entries, list):
        return None

    seen_any = False
    for gpu in entries:
        for display in (gpu or {}).get("spdisplays_ndrvs", []) or []:
            seen_any = True
            connection = str(display.get("spdisplays_connection_type", ""))
            if "internal" not in connection and "builtin" not in connection:
                return True
    return False if seen_any else None


def _probe(command: list[str]) -> str:
    """Run a read-only probe, returning "" for anything that goes wrong.

    A missing binary, a permissions change or a hang must degrade to "unknown", never to
    an exception: the guard exists to protect the run, not to become a new way to lose it.
    """
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=PROBE_TIMEOUT, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout or ""


def read_power_state(
    *,
    pmset_text: str | None = None,
    clamshell_text: str | None = None,
    displays_text: str | None = None,
) -> PowerState:
    """Probe the machine (or accept canned probe output, which is how the tests drive it)."""
    pmset_text = _probe(["pmset", "-g", "batt"]) if pmset_text is None else pmset_text
    clamshell_text = (
        _probe(["ioreg", "-r", "-k", "AppleClamshellState", "-d", "4"])
        if clamshell_text is None
        else clamshell_text
    )
    displays_text = (
        _probe(["system_profiler", "SPDisplaysDataType", "-json"])
        if displays_text is None
        else displays_text
    )

    on_ac, percent, charging, source = parse_pmset_batt(pmset_text)
    state = PowerState(
        on_ac=on_ac,
        battery_percent=percent,
        charging=charging,
        lid_closed=parse_clamshell(clamshell_text),
        external_display=parse_displays(displays_text),
        source=source,
    )
    if on_ac is None:
        state.notes.append("Could not read the power source from pmset; treating it as unknown.")
    if state.lid_closed and not state.external_display:
        # Not a refusal: we are demonstrably awake if this line runs. It is a warning
        # because macOS may sleep the machine mid-run and caffeinate cannot stop it.
        state.notes.append(
            "The lid appears closed with no external display; macOS may sleep mid-run."
        )
    return state


def power_refusal(state: PowerState, *, require_ac: bool = True) -> str | None:
    """The reason not to run, or None. The single decision the daemon acts on.

    An *unknown* power source is allowed through deliberately. Refusing on "I could not
    tell" would let a future macOS wording change silently cancel every night, and a
    cancelled night looks exactly like a quiet one until you read the briefing.
    """
    if not require_ac:
        return None
    if state.on_ac is False:
        percent = f" ({state.battery_percent}% battery)" if state.battery_percent else ""
        return (
            f"On battery power{percent}. NightShift needs AC power to run a full night; "
            "plug in, or set `require_ac = false` under [schedule] to override."
        )
    return None


def main() -> int:
    """`python -m orchestrator power` — print what the guard sees, and what it would do."""
    state = read_power_state()
    print(state.describe())
    for note in state.notes:
        print(f"  note: {note}")
    refusal = power_refusal(state)
    print(f"  verdict: {refusal or 'clear to run'}")
    return 1 if refusal else 0
