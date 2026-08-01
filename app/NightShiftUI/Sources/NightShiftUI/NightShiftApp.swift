// The app itself: a menu bar agent (LSUIElement) with two on-demand windows.
//
// `MenuBarExtra(.window)` rather than a plain menu — the Phase 11 rumps menu could only
// offer a list of items and an alert, and the two things Phase 17 exists to improve
// (reviewing a draft reply, reading a transcript) both need a real view.

import SwiftUI

@main
struct NightShiftApp: App {
    // `NightShiftStore.shared`, not a fresh instance: every scene below must observe the
    // object that is actually polling. See the comment on `shared`.
    @StateObject private var store = NightShiftStore.shared

    init() {
        // Ask for notification permission once, at launch, before there is anything to
        // announce: a permission prompt that arrives *with* the first banner is a
        // permission prompt that eats it. `begin()` then starts the poll loop.
        Task { @MainActor in NightShiftStore.shared.begin() }
    }

    var body: some Scene {
        MenuBarExtra {
            MenuView().environmentObject(store)
        } label: {
            // Text, not an SF Symbol image: the daemon's `AppState.icon` already encodes the
            // four states, and a label that renders the same glyph the rumps menu shows
            // keeps the two clients recognisably one app.
            Text(store.state.icon)
        }
        .menuBarExtraStyle(.window)

        Window("Approvals", id: WindowID.approvals.rawValue) {
            ApprovalsView().environmentObject(store)
        }
        .defaultSize(width: 820, height: 520)

        Window("Run history", id: WindowID.history.rawValue) {
            HistoryView().environmentObject(store)
        }
        .defaultSize(width: 1000, height: 600)
    }
}
