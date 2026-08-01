"""The SwiftUI client's contract with the daemon (Phase 17).

Two kinds of check, because the client is compiled code the Python suite cannot import:

- **the wire contract**, tested here for real: `Models.swift` transcribes Pydantic models by
  hand, and a field renamed on the Python side would otherwise be discovered by a user
  looking at a menu that silently lost a line. Parsing the Swift `CodingKeys` and comparing
  them to `model_fields` catches that drift in the suite that runs on every commit,
  offline, with no toolchain;
- **the build**, marked `swift` and skipped unless NIGHTSHIFT_SWIFT_TESTS=1, because it
  needs the Swift toolchain and takes seconds rather than milliseconds.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from app.service import ActionPreview, AppState, RunSnapshot
from models import Action, ActionType
from runner.observe import AgentRunRecord
from transcripts import NightRunRecord

REPO_ROOT = Path(__file__).resolve().parent.parent
SWIFT_ROOT = REPO_ROOT / "app" / "NightShiftUI"
SOURCES = SWIFT_ROOT / "Sources" / "NightShiftUI"


def swift_wire_names(struct: str) -> set[str]:
    """The JSON keys one Swift struct decodes.

    A property with no `CodingKeys` entry decodes under its own name, so both halves count:
    the enum's `case x = "y"` mappings, and the plain `case x` / bare stored properties.
    """
    text = (SOURCES / "Models.swift").read_text(encoding="utf-8")
    body = re.search(rf"struct {struct}: [^{{]+{{(.*?)\n}}", text, re.S)
    assert body, f"no struct {struct} in Models.swift"
    block = body.group(1)

    keys_enum = re.search(r"enum CodingKeys: String, CodingKey \{(.*?)\}", block, re.S)
    if keys_enum:
        names: set[str] = set()
        for line in keys_enum.group(1).splitlines():
            line = line.strip().removeprefix("case ").strip()
            if not line or line.startswith("//"):
                continue
            for item in line.split(","):
                item = item.strip()
                if "=" in item:
                    names.add(item.split("=")[1].strip().strip('"'))
                elif item:
                    names.add(item)
        return names

    return {
        match.group(1)
        for match in re.finditer(r"^\s*var (\w+)", block, re.M)
    }


@pytest.mark.parametrize(
    "struct, model",
    [
        ("AppState", AppState),
        ("RunSnapshot", RunSnapshot),
        ("ActionPreview", ActionPreview),
    ],
)
def test_the_client_decodes_every_field_the_daemon_sends(struct, model):
    """The Swift struct must know about every field on the Pydantic model.

    Extra Swift keys are allowed (a client may keep a computed convenience); missing ones
    are not, because that is a fact the daemon decided and the UI silently dropped.
    """
    missing = set(model.model_fields) - swift_wire_names(struct)
    assert not missing, f"{struct} does not decode {sorted(missing)}"


def test_the_transcript_viewer_decodes_the_stored_record():
    """`RunRecord`/`NightRecord` mirror the stored history rows.

    `messages` is deliberately absent from the Swift struct — the viewer shows the tool
    transcript and fetches the full replay as *text*, so the whole conversation never has
    to be decoded twice. Everything else must be there.
    """
    run_keys = swift_wire_names("RunRecord")
    assert not (set(AgentRunRecord.model_fields) - run_keys - {"messages"})

    night_keys = swift_wire_names("NightRecord")
    # `briefing_path` is the daemon's own path and the client opens the briefing through
    # `AppState`, so it is the one field the history view has no use for.
    assert not (set(NightRunRecord.model_fields) - night_keys - {"briefing_path"})


def test_the_client_knows_every_action_type():
    """A queued action type the client cannot decode is an approval it cannot show — and an
    action nobody approves is a side effect that silently never happens."""
    text = (SOURCES / "Models.swift").read_text(encoding="utf-8")
    for action_type in ActionType:
        assert f'"{action_type.value}"' in text, f"the client cannot decode {action_type}"


def test_the_action_status_vocabulary_is_not_duplicated_in_swift():
    """The client shows *pending* actions and nothing else; it must not reimplement the
    queue's state machine, which is the thing that guarantees one send per approval."""
    text = "\n".join(path.read_text(encoding="utf-8") for path in SOURCES.rglob("*.swift"))
    assert "case approved" not in text and "case done" not in text


def test_the_bundle_is_a_menu_bar_agent():
    """LSUIElement and a bundle identifier are not cosmetics.

    Without the identifier `UNUserNotificationCenter.current()` traps on launch; without
    LSUIElement the client takes a Dock icon and an app menu it has no windows for.
    """
    build = (SWIFT_ROOT / "build.sh").read_text(encoding="utf-8")
    assert "LSUIElement" in build
    assert "CFBundleIdentifier" in build
    assert "codesign" in build  # a stable identity, so a granted permission survives rebuilds
    assert os.access(SWIFT_ROOT / "build.sh", os.X_OK)


@pytest.mark.swift
@pytest.mark.skipif(
    not os.environ.get("NIGHTSHIFT_SWIFT_TESTS"),
    reason="needs the Swift toolchain; set NIGHTSHIFT_SWIFT_TESTS=1",
)
def test_the_client_compiles(tmp_path: Path):
    # Its own scratch path: pytest may be running under a different architecture than the
    # developer's shell (uv's interpreter can be x86_64 on an arm64 Mac), and a test that
    # shared `.build/` would leave the real one half-written for the wrong arch.
    result = subprocess.run(
        ["swift", "build", "-c", "release", "--scratch-path", str(tmp_path / "build")],
        cwd=SWIFT_ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stderr[-4000:]
