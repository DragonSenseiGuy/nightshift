// The menu bar panel. Rendering only — every sentence in it was written by the daemon.

import AppKit
import SwiftUI

struct MenuView: View {
    @EnvironmentObject private var store: NightShiftStore
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            header

            if store.daemonMode == .demo {
                // Said once, at the top, on every launch: everything below this line is a
                // canned night, and no click in this window can send or merge anything.
                Callout(
                    text: "Demo mode — a canned night from the fixtures. Nothing here is "
                        + "your real mail, and approving sends nothing.",
                    tone: .warning,
                    symbol: "theatermasks"
                )
            }

            if !store.connectionError.isEmpty {
                Callout(text: store.connectionError, tone: .error, symbol: "bolt.horizontal.circle")
            }
            if !store.state.warning.isEmpty {
                Callout(text: store.state.warning, tone: .warning, symbol: "powerplug")
            }
            if !store.state.queueError.isEmpty {
                Callout(text: store.state.queueError, tone: .error, symbol: "tray.full")
            }

            if store.state.pending > 0 {
                Divider()
                pendingSection
            }

            Divider()
            controls

            if !store.lastActionResult.isEmpty {
                Text(store.lastActionResult)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(3)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(14)
        .frame(width: 340)
        .task { await store.refresh() }
    }

    private var header: some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: store.state.status.symbol)
                .font(.title2)
                .foregroundStyle(store.state.status == .failed ? Color.orange : Color.accentColor)
            VStack(alignment: .leading, spacing: 2) {
                Text("Night Shift").font(.headline)
                Text(store.connected ? store.state.summary : "Connecting to the daemon…")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer()
        }
    }

    private var pendingSection: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("\(store.state.pending) waiting for approval")
                .font(.subheadline.weight(.medium))
            // The menu lists them but never decides them: approving is a two-step act that
            // happens in the review window, where the whole effect sentence and body fit.
            ForEach(store.actions.prefix(4)) { action in
                Label {
                    Text(action.title).lineLimit(1)
                } icon: {
                    Image(systemName: action.type.symbol)
                }
                .font(.callout)
                .foregroundStyle(.secondary)
            }
            Button("Review approvals…") { open(.approvals) }
                .buttonStyle(.borderedProminent)
        }
    }

    private var controls: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button {
                Task { await store.runNow() }
            } label: {
                Label(
                    store.state.run.active ? "Run in progress…" : "Run tonight's shift now",
                    systemImage: "play.circle"
                )
            }
            .disabled(store.state.run.active || store.busyAction == "run" || !store.connected)

            Button {
                store.openBriefing()
            } label: {
                Label("Open last briefing", systemImage: "doc.richtext")
            }
            .disabled(!store.state.briefingAvailable)

            Button { open(.approvals) } label: {
                Label("Approvals\(store.state.pending > 0 ? " (\(store.state.pending))" : "")",
                      systemImage: "checkmark.seal")
            }

            Button { open(.history) } label: {
                Label("Run history & transcripts", systemImage: "clock.arrow.circlepath")
            }

            Divider()
            Button(role: .destructive) { NSApp.terminate(nil) } label: {
                Label("Quit Night Shift", systemImage: "power")
            }
        }
        .buttonStyle(.plain)
        .labelStyle(.titleAndIcon)
    }

    /// Opening a window from a `MenuBarExtra` needs the app activated, or the window comes
    /// up behind everything and looks like nothing happened.
    private func open(_ window: WindowID) {
        NSApp.activate(ignoringOtherApps: true)
        openWindow(id: window.rawValue)
    }
}

enum WindowID: String {
    case approvals
    case history
}

/// An empty pane that says why it is empty — hand-built rather than
/// `ContentUnavailableView`.
///
/// The system view was observed drawing nothing at all in this app's auxiliary `Window`
/// scenes (macOS 26.4, menu bar agent), which is the worst possible failure for an empty
/// state: a blank window reads as "this app is broken" at exactly the moment it is trying
/// to say "there is nothing here yet". An image and two labels cannot fail that way.
struct EmptyState: View {
    let title: String
    var message: String = ""
    var symbol: String = "moon.stars"

    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: symbol)
                .font(.system(size: 32))
                .foregroundStyle(.tertiary)
            Text(title)
                .font(.headline)
                .foregroundStyle(.secondary)
            if !message.isEmpty {
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.tertiary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct Callout: View {
    enum Tone { case warning, error }

    let text: String
    let tone: Tone
    var symbol: String = "exclamationmark.triangle"

    var body: some View {
        HStack(alignment: .top, spacing: 6) {
            Image(systemName: symbol)
            Text(text).fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
        }
        .font(.caption)
        .foregroundStyle(tone == .error ? Color.red : Color.orange)
        .padding(8)
        .background(
            RoundedRectangle(cornerRadius: 6)
                .fill((tone == .error ? Color.red : Color.orange).opacity(0.12))
        )
    }
}
