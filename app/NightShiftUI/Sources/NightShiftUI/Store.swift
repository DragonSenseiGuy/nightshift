// The one observable object every view reads. Polls the daemon; owns nothing else.
//
// The client mirrors `app/service.py`'s discipline one level up: no view computes a status,
// a warning or an effect sentence. They read what the daemon already decided, so the menu
// bar, the approvals window and the notification banner can never say three different
// things about the same night.

import Foundation
import SwiftUI

@MainActor
final class NightShiftStore: ObservableObject {
    @Published private(set) var state = AppState()
    @Published private(set) var actions: [ActionPreview] = []
    @Published private(set) var nights: [NightRecord] = []
    @Published private(set) var connectionError: String = ""
    @Published private(set) var lastActionResult: String = ""
    @Published private(set) var busyAction: String? = nil

    /// True until the first successful poll, so a cold launch shows "Connecting…" rather
    /// than an idle moon that claims a night is scheduled when the daemon is not running.
    @Published private(set) var connected = false

    /// Which daemon this client ended up talking to. `.demo` must be visible in the UI:
    /// every number on screen is then canned, and nothing a click does leaves the machine.
    @Published private(set) var daemonMode: DaemonMode = .external

    private let client: DaemonClient
    private let notifier: Notifier
    private var timer: Task<Void, Never>?
    private var lastRunPhase: RunPhase = .never
    private var lastPending = 0

    init(client: DaemonClient = DaemonClient(), notifier: Notifier = Notifier()) {
        self.client = client
        self.notifier = notifier
    }

    /// The one store the app runs on.
    ///
    /// SwiftUI is free to initialise an `App` value more than once, and `@StateObject`
    /// keeps only the first wrapped instance. A `Task` in `App.init()` that captures its
    /// *local* store can therefore end up polling an object no view observes — a live
    /// socket to the daemon and a window that never fills in, which is indistinguishable
    /// from a broken window. Owning the instance here removes the question.
    static let shared = NightShiftStore()

    /// Ask for notification permission once, work out which daemon to talk to, then start
    /// polling. Idempotent, so calling it from more than one place (or a re-initialised
    /// `App`) is safe.
    ///
    /// The daemon question is asked *before* the first poll: a downloaded copy of this app
    /// has no daemon running and would otherwise spend its first five seconds showing a
    /// connection error to someone who has done nothing wrong.
    func begin() {
        notifier.requestAuthorization()
        Task { @MainActor in
            let mode = await DaemonSupervisor.shared.ensure()
            self.daemonMode = mode
            if mode != .external {
                await client.reconfigure(
                    port: DaemonSupervisor.shared.port,
                    tokenPath: DaemonSupervisor.shared.tokenPath
                )
            }
            self.start()
        }
    }

    // MARK: - Polling

    /// Five seconds: fast enough that "Run now" feels like it did something, slow enough
    /// that an idle Mac is doing 12 loopback reads a minute and nothing else. The daemon's
    /// `/state` is one SQLite read and a `pmset` probe, so this stays cheap.
    func start(interval: Duration = .seconds(5)) {
        guard timer == nil else { return }
        timer = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: interval)
            }
        }
    }

    func stop() {
        timer?.cancel()
        timer = nil
    }

    func refresh() async {
        do {
            let state = try await client.state()
            let actions = try await client.actions()
            self.state = state
            self.actions = actions
            self.connectionError = ""
            self.connected = true
            announce(state: state, pending: actions.count)
        } catch {
            self.connectionError = error.localizedDescription
            self.connected = false
        }
    }

    func refreshHistory() async {
        do {
            nights = try await client.nights()
        } catch {
            connectionError = error.localizedDescription
        }
    }

    // MARK: - Notifications

    /// Native banners, on the two transitions worth interrupting someone for: a night that
    /// just ended, and work that has appeared in the approval queue.
    ///
    /// Counts and host-authored words only — never a subject line, a branch name or any
    /// other agent- or email-derived string. That is the same rule
    /// `orchestrator/notify.py:headline` follows, and it holds here for the same reason: a
    /// notification is rendered by the system, outside every escape this repo controls.
    private func announce(state: AppState, pending: Int) {
        let phase = state.run.phase
        if phase != lastRunPhase, lastRunPhase == .running {
            switch phase {
            case .succeeded:
                notifier.post(
                    title: "Night Shift finished",
                    body: pending > 0
                        ? "\(pending) action(s) waiting for approval."
                        : "Your briefing is ready."
                )
            case .failed:
                notifier.post(
                    title: "Night Shift failed",
                    body: "The run exited with an error. Check the log."
                )
            default:
                break
            }
        }
        if pending > lastPending, phase != .running {
            notifier.post(
                title: "Approvals waiting",
                body: "\(pending) action(s) need your decision before anything is sent."
            )
        }
        lastRunPhase = phase
        lastPending = pending
    }

    // MARK: - Commands

    func runNow() async {
        busyAction = "run"
        defer { busyAction = nil }
        do {
            _ = try await client.runNow()
            lastActionResult = "Started tonight's run."
            await refresh()
        } catch {
            lastActionResult = error.localizedDescription
        }
    }

    /// Perform one approved side effect. Called only from a view that has already shown
    /// `preview.effect` and taken a second, explicit confirmation.
    func approve(_ preview: ActionPreview) async {
        busyAction = preview.id
        defer { busyAction = nil }
        do {
            try await client.approve(preview.id)
            lastActionResult = "Approved: \(preview.effect)"
            await refresh()
        } catch {
            lastActionResult = error.localizedDescription
        }
    }

    func reject(_ preview: ActionPreview, reason: String = "") async {
        busyAction = preview.id
        defer { busyAction = nil }
        do {
            try await client.reject(preview.id, reason: reason)
            lastActionResult = "Rejected: \(preview.title)"
            await refresh()
        } catch {
            lastActionResult = error.localizedDescription
        }
    }

    func openBriefing() {
        guard state.briefingAvailable else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: state.briefingPath))
    }

    // MARK: - Reads the history views make directly

    func runs(night: String?) async throws -> [RunRecord] {
        try await client.runs(night: night)
    }

    func run(_ id: String) async throws -> RunRecord { try await client.run(id) }

    func replay(_ id: String) async throws -> String { try await client.replay(id, full: true) }
}
