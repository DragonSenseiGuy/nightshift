// The approval queue, reviewed properly — the draft-reply UX a menu could not give.
//
// This window is where a human exercises security rule 3, so its layout is an argument:
//
// - the **effect sentence** is the first thing in the detail pane, in full, never
//   truncated, and it is repeated verbatim in the confirmation dialog. A queue whose UI
//   says only "Approve?" is a queue that sends mail on a misclick;
// - a **tainted** action carries a standing banner, because "an agent wrote this after
//   reading untrusted email" is the fact that should slow the reader down;
// - the body is rendered as plain `Text` in a monospaced frame — never markdown, never
//   `AttributedString(markdown:)`, never a web view. It is email-derived text and this app
//   interprets none of it;
// - Approve and Reject are far apart and only Approve is destructive-confirmed. Rejecting
//   is recoverable; sending is not.

import SwiftUI

struct ApprovalsView: View {
    @EnvironmentObject private var store: NightShiftStore
    @State private var selection: ActionPreview.ID?
    @State private var columns: NavigationSplitViewVisibility = .all
    @State private var confirming: ActionPreview?
    @State private var rejecting: ActionPreview?
    @State private var rejectReason = ""

    private var selected: ActionPreview? {
        store.actions.first { $0.id == selection } ?? store.actions.first
    }

    var body: some View {
        // An explicit visibility binding, because a sidebar this window cannot show is a
        // window with no way back to the list.
        NavigationSplitView(columnVisibility: $columns) {
            List(store.actions, selection: $selection) { action in
                VStack(alignment: .leading, spacing: 3) {
                    Label(action.type.label, systemImage: action.type.symbol)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    Text(action.title)
                        .lineLimit(2)
                    if action.tainted {
                        Text("untrusted input")
                            .font(.caption2.weight(.semibold))
                            .foregroundStyle(.orange)
                    }
                }
                .padding(.vertical, 2)
                .tag(action.id)
            }
            .navigationSplitViewColumnWidth(min: 220, ideal: 260)
            .overlay {
                if store.actions.isEmpty {
                    // A queue that cannot be read and a queue that is empty are different
                    // facts, and only one of them means "nothing to do".
                    if !store.connected {
                        EmptyState(
                            title: "Not connected",
                            message: "Start the daemon: uv run python -m app serve",
                            symbol: "bolt.horizontal.circle"
                        )
                    } else {
                        EmptyState(
                            title: "Nothing waiting",
                            message: "Agents propose; nothing happens until you approve it.",
                            symbol: "checkmark.seal"
                        )
                    }
                }
            }
        } detail: {
            if let action = selected {
                detail(action)
            } else {
                EmptyState(title: "No action selected", symbol: "sidebar.left")
            }
        }
        .navigationTitle("Approvals")
        .frame(minWidth: 720, minHeight: 460)
        .task { await store.refresh() }
        .alert("Approve this action?", isPresented: confirmBinding, presenting: confirming) { action in
            Button("Approve", role: .destructive) {
                Task { await store.approve(action) }
            }
            Button("Cancel", role: .cancel) {}
        } message: { action in
            // The effect, in the dialog, every time — the same sentence the detail pane
            // showed. This is the last screen before something irreversible happens.
            Text(action.effect)
        }
        .alert("Reject this action?", isPresented: rejectBinding, presenting: rejecting) { action in
            TextField("Reason (optional)", text: $rejectReason)
            Button("Reject", role: .destructive) {
                let reason = rejectReason
                rejectReason = ""
                Task { await store.reject(action, reason: reason) }
            }
            Button("Cancel", role: .cancel) { rejectReason = "" }
        } message: { action in
            Text("Nothing will be sent or merged. The decision is kept on the row.\n\n\(action.title)")
        }
    }

    private func detail(_ action: ActionPreview) -> some View {
        VStack(alignment: .leading, spacing: 14) {
            VStack(alignment: .leading, spacing: 6) {
                Label(action.type.label, systemImage: action.type.symbol)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Text(action.title)
                    .font(.title3.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
            }

            Callout(text: action.effect, tone: .warning, symbol: "exclamationmark.triangle.fill")
                .font(.callout)

            if action.tainted {
                Callout(
                    text: "This was written by an agent after reading untrusted email. "
                        + "Read it before approving.",
                    tone: .error,
                    symbol: "eye.trianglebadge.exclamationmark"
                )
            }

            if !action.detail.isEmpty {
                ScrollView {
                    Text(action.detail)
                        .font(.system(.body, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(10)
                }
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.secondary.opacity(0.08)))
            }

            Spacer(minLength: 0)

            HStack {
                if !action.origin.isEmpty {
                    Text("Proposed by \(action.origin)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button("Reject") { rejecting = action }
                    .disabled(store.busyAction == action.id)
                Button("Approve…") { confirming = action }
                    .buttonStyle(.borderedProminent)
                    .disabled(store.busyAction == action.id)
            }
        }
        .padding(18)
    }

    private var confirmBinding: Binding<Bool> {
        Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } })
    }

    private var rejectBinding: Binding<Bool> {
        Binding(get: { rejecting != nil }, set: { if !$0 { rejecting = nil } })
    }
}
