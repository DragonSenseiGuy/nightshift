// Starting the daemon the client talks to, when nobody else has.
//
// Phase 17 assumed a developer: `uv run python -m app serve` in one terminal, the app in
// the other. A downloaded NightShift.app has neither the terminal nor the checkout, so the
// bundle carries a frozen copy of the daemon (`Contents/Resources/nightshiftd`, built by
// `packaging/build_daemon.sh`) and this file decides whether to run it.
//
// The order matters, and it is the whole design:
//
//   1. Is a daemon already answering on the normal port? Then that is the user's real
//      install — their queue, their briefing, their night. Attach to it and start nothing.
//   2. Otherwise, is there a frozen daemon inside this bundle? Start it in **demo mode**
//      on a different port, with its own token file. A first launch on a machine with no
//      Google account, no API key and no Docker then shows a real morning instead of an
//      error, and it cannot touch anything real: demo mode's queue is a separate database
//      whose effects are disarmed (`app/demo.py`).
//   3. Otherwise, change nothing and let the client report that the daemon is down.
//
// A demo daemon is never started on top of a real one, and a real daemon is never started
// by the UI at all — that would mean a menu bar app deciding to open a surface that can
// send mail, which is exactly the decision a human should be making.

import Foundation

enum DaemonMode: Equatable {
    /// A daemon that was already running: the user's real install.
    case external
    /// The frozen daemon inside this bundle, serving a canned night.
    case demo
    /// Nothing is running and this bundle has no daemon to start.
    case unavailable
}

/// The port the bundled demo daemon uses. Deliberately not 8402: if the real daemon is
/// started later, the two must not collide, and a demo must never occupy the port the real
/// client looks for.
let demoPort = 8412

@MainActor
final class DaemonSupervisor {
    static let shared = DaemonSupervisor()

    private(set) var mode: DaemonMode = .unavailable
    private(set) var port: Int = defaultPort()
    private(set) var tokenPath: URL = defaultTokenPath()

    private var child: Process?

    /// Where demo state lives. Mirrors `app/demo.py:default_demo_root`.
    private var demoRoot: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/NightShift/demo")
    }

    /// The frozen daemon inside this bundle, if this is a bundle and it has one.
    private var bundledDaemon: URL? {
        guard let url = Bundle.main.url(forResource: "nightshiftd", withExtension: nil),
              FileManager.default.isExecutableFile(atPath: url.path)
        else { return nil }
        return url
    }

    /// Resolve what to talk to, starting the bundled daemon if that is the answer.
    /// Returns the mode so the UI can say which one it is — a demo that does not announce
    /// itself is just a lie with a nice icon.
    @discardableResult
    func ensure() async -> DaemonMode {
        if await isHealthy(port: defaultPort()) {
            mode = .external
            port = defaultPort()
            tokenPath = defaultTokenPath()
            return mode
        }
        guard let daemon = bundledDaemon else {
            mode = .unavailable
            return mode
        }

        let demoToken = demoRoot.appendingPathComponent("ui-token")
        // A demo daemon from a previous launch is reused rather than duplicated: two
        // uvicorns on one port means one of them exits and the UI polls a corpse.
        if !(await isHealthy(port: demoPort)) {
            start(daemon: daemon, tokenPath: demoToken)
        }
        guard await waitForHealth(port: demoPort, seconds: 25) else {
            mode = .unavailable
            return mode
        }
        mode = .demo
        port = demoPort
        tokenPath = demoToken
        return mode
    }

    private func start(daemon: URL, tokenPath: URL) {
        let process = Process()
        process.executableURL = daemon
        process.arguments = [
            "demo",
            "--port", String(demoPort),
            "--demo-dir", demoRoot.path,
            "--token-file", tokenPath.path,
        ]
        // The log is the only place a frozen daemon's traceback can go; without it, "the
        // window is empty" has no explanation anywhere on the machine.
        let logDirectory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/NightShift")
        try? FileManager.default.createDirectory(at: logDirectory, withIntermediateDirectories: true)
        let log = logDirectory.appendingPathComponent("demo-daemon.log")
        FileManager.default.createFile(atPath: log.path, contents: nil)
        if let handle = try? FileHandle(forWritingTo: log) {
            handle.seekToEndOfFile()
            process.standardOutput = handle
            process.standardError = handle
        }

        do {
            try process.run()
            child = process
            // A demo server outliving the app it was started for would keep answering on a
            // loopback port with nothing to show it. Quit takes it with us.
            NotificationCenter.default.addObserver(
                forName: NSNotification.Name("NSApplicationWillTerminateNotification"),
                object: nil,
                queue: .main
            ) { _ in
                // The process, not `self.child`: the observer runs during teardown, when
                // reaching back into an actor-isolated property is neither safe nor needed.
                process.terminate()
            }
        } catch {
            child = nil
        }
    }

    // MARK: - Health

    private func isHealthy(port: Int) async -> Bool {
        guard let url = URL(string: "http://127.0.0.1:\(port)/health") else { return false }
        var request = URLRequest(url: url)
        request.timeoutInterval = 1.5
        guard let (_, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse
        else { return false }
        return http.statusCode == 200
    }

    private func waitForHealth(port: Int, seconds: Int) async -> Bool {
        // A frozen one-file binary unpacks itself on first launch, which on a cold disk is
        // several seconds — hence a generous window rather than a couple of retries.
        for _ in 0..<(seconds * 2) {
            if await isHealthy(port: port) { return true }
            try? await Task.sleep(for: .milliseconds(500))
        }
        return false
    }
}
