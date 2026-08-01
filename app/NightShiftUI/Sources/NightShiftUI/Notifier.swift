// Native UserNotifications — the half of Phase 17 that `orchestrator/notify.py` cannot do.
//
// The daemon's banner comes from a background process and has to shell out to
// `terminal-notifier`/`osascript`; a real app can post through UNUserNotificationCenter and
// get the system's own permission model, notification centre history and Do Not Disturb
// handling for free.
//
// Two rules carried over unchanged from `orchestrator/notify.py`:
//
// - **Counts and host-authored words only.** Never a subject line, sender, branch name or
//   any other agent-derived string. A banner is drawn by the system, outside every escape
//   `briefing.py` performs, and it is the one surface a user reads without opening
//   anything.
// - **It never raises.** A missing entitlement, a denied permission or an unbundled binary
//   must cost a banner, not a night's worth of state — every failure here degrades to a
//   line on stderr.

import Foundation
import UserNotifications

final class Notifier: @unchecked Sendable {
    private let center: UNUserNotificationCenter?

    init() {
        // `UNUserNotificationCenter.current()` traps when the executable is not in a bundle
        // with an identifier, which is exactly what `swift run` produces. Detecting that
        // keeps the app debuggable from a terminal instead of crashing on launch.
        if Bundle.main.bundleIdentifier != nil {
            center = UNUserNotificationCenter.current()
        } else {
            center = nil
            FileHandle.standardError.write(
                Data("nightshift: no bundle identifier — notifications disabled\n".utf8)
            )
        }
    }

    func requestAuthorization() {
        guard let center else { return }
        center.requestAuthorization(options: [.alert, .sound]) { granted, error in
            if let error {
                FileHandle.standardError.write(
                    Data("nightshift: notification permission error: \(error)\n".utf8)
                )
            } else if !granted {
                FileHandle.standardError.write(
                    Data("nightshift: notifications not permitted; the menu bar still works\n".utf8)
                )
            }
        }
    }

    func post(title: String, body: String) {
        guard let center else { return }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        let request = UNNotificationRequest(
            identifier: UUID().uuidString, content: content, trigger: nil
        )
        center.add(request) { error in
            if let error {
                FileHandle.standardError.write(
                    Data("nightshift: could not post notification: \(error)\n".utf8)
                )
            }
        }
    }
}
