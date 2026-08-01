"""The rumps menu bar — rendering only (Phase 11).

Deliberately dull. Every decision (what the icon means, what a run's argv is, whether it is
bedtime, what approving an action will do) lives in `app.service`, and this file turns that
into `MenuItem`s and alerts. If a change here needs an `if`, it probably belongs next door:
Phase 17 replaces this file with SwiftUI and must not have to port logic out of it.

Two things this file *does* own, because they are properties of a menu bar and nothing else:

- **Never block the main thread.** rumps runs one NSApplication run loop; anything slow on
  a callback freezes the menu until it returns. "Run now" therefore only *spawns* (see
  `service._spawn_run`) and a `rumps.Timer` polls for the result.
- **Never approve on a single click.** A menu item opens an alert that states the exact
  side effect, with "Approve" as a deliberate second click and "Reject" as a third button.
  A misclick on the menu closes a dialog; it does not send mail (security rule 3).
"""

from __future__ import annotations

import sys
from pathlib import Path

import rumps

from app.service import ActionPreview, AppState, NightShiftService, ServiceError

REFRESH_SECONDS = 10

# NSAlert's return codes: default 1, alternate ("Cancel") 0, other -1.
APPROVE, CANCEL, REJECT = 1, 0, -1


def decision_from_alert(code: int) -> str:
    """Map an alert's return code to an intent. Anything unrecognised cancels.

    Failing closed on an unexpected code is the whole point: an unknown button must never
    resolve to "approve", and this mapping is where a rumps/AppKit change would show up.
    """
    if code == APPROVE:
        return "approve"
    if code == REJECT:
        return "reject"
    return "cancel"


class NightShiftApp(rumps.App):
    """The menu bar item. Holds a `NightShiftService` and renders whatever it says."""

    def __init__(self, service: NightShiftService | None = None) -> None:
        super().__init__("NightShift", title="🌙", quit_button=None)
        self.service = service or NightShiftService()
        self._timer = rumps.Timer(self._tick, REFRESH_SECONDS)
        self.refresh()
        self._timer.start()

    # -- rendering ---------------------------------------------------------------------

    def _tick(self, _timer) -> None:
        self.refresh()

    def refresh(self) -> None:
        """Rebuild the menu from one `AppState` snapshot.

        Rebuilt wholesale rather than patched: the pending list changes shape (items appear,
        get approved, fail), and a menu assembled from a single snapshot cannot end up
        showing an approve button for a row that is already gone.
        """
        state = self.service.state()
        self.title = state.icon
        self.menu.clear()
        self.menu.update(self.build_menu(state))

    def build_menu(self, state: AppState) -> list:
        """The menu as data. Separated from `refresh` so a test can assert its shape."""
        items: list = [self._label(state.summary)]

        if state.warning:
            items.append(self._label(f"⚡︎ {state.warning}"))
        if state.queue_error:
            items.append(self._label(f"⚠ {state.queue_error}"))

        items += [
            rumps.separator,
            rumps.MenuItem("Run now", callback=self.on_run_now),
            rumps.MenuItem(
                "Open last briefing",
                callback=self.on_open_briefing if state.briefing_available else None,
            ),
            rumps.separator,
            self._label(
                f"Approvals ({state.pending})" if state.pending else "Approvals — none waiting"
            ),
        ]
        items += [
            self._approval_item(index, preview)
            for index, preview in enumerate(self.service.previews(), start=1)
        ]
        items += [
            rumps.separator,
            rumps.MenuItem("Power…", callback=self.on_power),
            rumps.MenuItem("Quit NightShift", callback=rumps.quit_application),
        ]
        return items

    @staticmethod
    def _label(text: str) -> rumps.MenuItem:
        """A non-clickable status line (rumps disables items with no callback)."""
        return rumps.MenuItem(text[:200])

    def _approval_item(self, index: int, preview: ActionPreview) -> rumps.MenuItem:
        # Numbered because rumps keys menu items by title: two identical draft subjects
        # would otherwise collapse into one row, hiding a pending action.
        marker = "✉︎" if preview.tainted else "⎇"
        item = rumps.MenuItem(f"  {index}. {marker} {preview.title}", callback=self.on_decide)
        # Stash the id on the item: the callback receives the sender, and looking the row
        # up by title would break the moment two actions share a subject line.
        item.action_id = preview.id
        return item

    # -- callbacks ----------------------------------------------------------------------

    def on_run_now(self, _sender) -> None:
        try:
            self.service.run_now()
        except ServiceError as exc:
            rumps.alert("NightShift", str(exc))
        else:
            rumps.alert(
                "NightShift",
                "Started tonight's run in the background.\n"
                f"Progress is logged to {self.service.run_log}.",
            )
        self.refresh()

    def on_open_briefing(self, _sender) -> None:
        if not self.service.open_briefing():
            rumps.alert("NightShift", "No briefing yet — run a night first.")

    def on_power(self, _sender) -> None:
        warning = self.service.bedtime_warning()
        minutes = self.service.minutes_to_bedtime()
        rumps.alert(
            "NightShift power check",
            warning or f"Clear to run. Tonight's shift starts in {minutes} min.",
        )

    def on_decide(self, sender) -> None:
        """Show what approving would do, then do exactly what the human picked."""
        action_id = getattr(sender, "action_id", "")
        preview = next((p for p in self.service.previews() if p.id == action_id), None)
        if preview is None:
            rumps.alert("NightShift", "That action is no longer pending.")
            self.refresh()
            return

        decision = decision_from_alert(
            rumps.alert(
                title=preview.title,
                message=preview.confirmation(),
                ok="Approve",
                cancel="Cancel",
                other="Reject",
            )
        )
        try:
            if decision == "approve":
                action = self.service.approve(preview.id)
                rumps.alert("NightShift", f"{action.status}: {action.result or action.error}")
            elif decision == "reject":
                self.service.reject(preview.id, reason="rejected from the menu bar")
        except Exception as exc:  # noqa: BLE001 - a queue error is a dialog, not a crash
            rumps.alert("NightShift", f"That did not work: {exc}")
        self.refresh()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="app", description="NightShift menu bar (v1).")
    parser.add_argument("--config", default=None, help="Standing-instructions TOML.")
    parser.add_argument("--db", type=Path, default=None, help="Approval queue database.")
    args = parser.parse_args(argv)

    from approvals import ApprovalQueue

    service = NightShiftService(
        config_path=args.config,
        queue=ApprovalQueue(args.db) if args.db else None,
    )
    NightShiftApp(service).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
