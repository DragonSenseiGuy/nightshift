// Run history and the transcript viewer — three columns: nights → agent runs → the run.
//
// `transcripts.py replay <id>` already prints this, and the briefing prints that command
// because an emailed HTML file cannot host a button. A real app can, and this window is
// that button: the same records, browsable, with the tool calls as a structured list
// rather than a wall of text.
//
// One rule the viewer never bends: a transcript is the most thoroughly untrusted text in
// the system. Taint labels ride on the stored row, a tainted run shows an UNTRUSTED banner
// before any of its content, every string is plain `Text`, and nothing here can copy a
// transcript anywhere a model would read it. Feeding a stored run back into an agent would
// be security rule 2 with an extra hop.

import SwiftUI

struct HistoryView: View {
    @EnvironmentObject private var store: NightShiftStore
    @State private var selectedNight: NightRecord.ID?
    @State private var runs: [RunRecord] = []
    @State private var selectedRun: RunRecord.ID?
    @State private var openRun: RunRecord?
    @State private var loadError = ""

    var body: some View {
        NavigationSplitView {
            List(store.nights, selection: $selectedNight) { night in
                VStack(alignment: .leading, spacing: 2) {
                    Text(night.id).font(.headline)
                    HStack(spacing: 6) {
                        OutcomeTag(outcome: night.outcome)
                        if night.failures > 0 {
                            Text("\(night.failures) failure(s)")
                                .font(.caption)
                                .foregroundStyle(.orange)
                        }
                        Text(money(night.costUsd))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    if !night.refused.isEmpty {
                        // A night that did not happen must never read like a quiet one.
                        Text(night.refused)
                            .font(.caption2)
                            .foregroundStyle(.orange)
                            .lineLimit(2)
                    }
                }
                .padding(.vertical, 2)
                .tag(night.id)
            }
            .navigationSplitViewColumnWidth(min: 200, ideal: 240)
            .overlay {
                if store.nights.isEmpty {
                    EmptyState(
                        title: "No nights yet",
                        message: "Run history appears here after the first night.",
                        symbol: "moon.zzz"
                    )
                }
            }
        } content: {
            List(runs, selection: $selectedRun) { run in
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(run.agent).font(.headline)
                        if run.tainted {
                            Image(systemName: "eye.trianglebadge.exclamationmark")
                                .foregroundStyle(.orange)
                                .help("Touched untrusted input: \(run.taint.joined(separator: ", "))")
                        }
                    }
                    Text(runLine(run))
                        .font(.caption)
                        .foregroundStyle(run.stopReason == "completed" ? Color.secondary : Color.orange)
                    if !run.model.isEmpty {
                        Text(run.model).font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                    }
                }
                .padding(.vertical, 2)
                .tag(run.id)
            }
            .navigationSplitViewColumnWidth(min: 220, ideal: 280)
            .overlay {
                if runs.isEmpty {
                    EmptyState(title: "No agent runs", symbol: "list.bullet.rectangle")
                }
            }
        } detail: {
            if let openRun {
                RunDetailView(run: openRun)
            } else if !loadError.isEmpty {
                EmptyState(
                    title: "Could not load",
                    message: loadError,
                    symbol: "exclamationmark.triangle"
                )
            } else {
                EmptyState(title: "Select a run", symbol: "doc.text.magnifyingglass")
            }
        }
        .navigationTitle("Run history")
        .frame(minWidth: 900, minHeight: 520)
        .task { await store.refreshHistory() }
        .onChange(of: selectedNight) { _, night in
            Task { await loadRuns(night: night) }
        }
        .onChange(of: selectedRun) { _, id in
            Task { await loadRun(id) }
        }
    }

    private func loadRuns(night: String?) async {
        selectedRun = nil
        openRun = nil
        do {
            runs = try await store.runs(night: night)
        } catch {
            loadError = error.localizedDescription
        }
    }

    /// The list is fetched without `messages`/`transcript` (a night of conversations is
    /// megabytes); opening one run fetches that run in full.
    private func loadRun(_ id: String?) async {
        guard let id else { openRun = nil; return }
        do {
            openRun = try await store.run(id)
            loadError = ""
        } catch {
            openRun = nil
            loadError = error.localizedDescription
        }
    }
}

struct RunDetailView: View {
    let run: RunRecord
    @EnvironmentObject private var store: NightShiftStore
    @State private var replay = ""
    @State private var showingReplay = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                header

                if run.tainted {
                    Callout(
                        text: "UNTRUSTED — this run read \(run.taint.joined(separator: ", ")). "
                            + "Everything below is data an attacker may have written. It is "
                            + "displayed, never executed, and never fed back to a model.",
                        tone: .error,
                        symbol: "eye.trianglebadge.exclamationmark"
                    )
                }

                if !run.text.isEmpty {
                    Section2("Final message") {
                        Text(run.text)
                            .font(.system(.body, design: .monospaced))
                            .textSelection(.enabled)
                    }
                }

                Section2("Tool calls (\(run.transcript.count))") {
                    if run.transcript.isEmpty {
                        Text("No tools were called.").foregroundStyle(.secondary)
                    } else {
                        VStack(alignment: .leading, spacing: 10) {
                            ForEach(run.transcript) { call in
                                ToolCallRow(call: call)
                            }
                        }
                    }
                }

                if showingReplay, !replay.isEmpty {
                    Section2("Full replay") {
                        Text(replay)
                            .font(.system(.caption, design: .monospaced))
                            .textSelection(.enabled)
                    }
                }
            }
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .toolbar {
            Button {
                Task {
                    replay = (try? await store.replay(run.id)) ?? "Could not load the replay."
                    showingReplay = true
                }
            } label: {
                Label("Full replay", systemImage: "text.alignleft")
            }
            .help("The same text `uv run python transcripts.py replay \(run.id)` prints")
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(run.agent).font(.title3.weight(.semibold))
            Text(run.id).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            Text("\(run.model) · \(run.source) · \(run.steps) step(s) · \(run.stopReason)")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(
                "\(run.usage.totalTokens) tokens · \(money(run.costUsd))"
                    + (run.usage.estimated ? " (estimated — the provider reported no usage)" : "")
            )
            .font(.caption)
            .foregroundStyle(run.usage.estimated ? .orange : .secondary)
        }
    }
}

struct ToolCallRow: View {
    let call: ToolCallRecord
    @State private var expanded = false

    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            VStack(alignment: .leading, spacing: 6) {
                if !call.arguments.isEmpty {
                    LabeledBlock(title: "arguments", body: call.arguments)
                }
                if !call.error.isEmpty {
                    LabeledBlock(title: "error", body: call.error, tint: .red)
                }
                if !call.result.isEmpty {
                    LabeledBlock(title: "result", body: call.result)
                }
            }
            .padding(.top, 4)
        } label: {
            HStack(spacing: 6) {
                Image(systemName: call.ok ? "wrench.and.screwdriver" : "xmark.octagon")
                    .foregroundStyle(call.ok ? Color.secondary : Color.red)
                Text("\(call.step). \(call.tool)").font(.callout.weight(.medium))
                if !call.taint.isEmpty {
                    Text(call.taint.joined(separator: ","))
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(.orange)
                }
            }
        }
    }
}

struct LabeledBlock: View {
    let title: String
    let body_: String
    var tint: Color = .secondary

    init(title: String, body: String, tint: Color = .secondary) {
        self.title = title
        self.body_ = body
        self.tint = tint
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption2.weight(.semibold)).foregroundStyle(tint)
            Text(body_)
                .font(.system(.caption, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
                .background(RoundedRectangle(cornerRadius: 6).fill(Color.secondary.opacity(0.08)))
        }
    }
}

struct Section2<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content

    init(_ title: String, @ViewBuilder content: () -> Content) {
        self.title = title
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.headline)
            content.frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

struct OutcomeTag: View {
    let outcome: String

    var body: some View {
        Text(outcome)
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(color.opacity(0.18)))
            .foregroundStyle(color)
    }

    private var color: Color {
        switch outcome {
        case "completed": return .green
        case "running": return .blue
        case "refused": return .orange
        default: return .red
        }
    }
}

/// Built outside the view body: string interpolation of four differently-typed values is
/// the classic SwiftUI "unable to type-check in reasonable time" trap.
func runLine(_ run: RunRecord) -> String {
    let steps = "\(run.steps) step(s)"
    return steps + " · " + run.stopReason + " · " + money(run.costUsd)
}

func money(_ usd: Double) -> String {
    usd < 0.01 && usd > 0 ? "<$0.01" : String(format: "$%.2f", usd)
}
