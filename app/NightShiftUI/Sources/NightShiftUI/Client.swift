// The daemon client: HTTP to 127.0.0.1, bearer token from the 0600 file the daemon wrote.
//
// Nothing here decides anything. It fetches models, posts a decision, and surfaces the
// error when the daemon is not running — which is the failure this client has to handle
// *well*, because "the menu is empty" and "the daemon is down" must never look the same.

import Foundation

struct DaemonError: LocalizedError {
    let message: String
    var errorDescription: String? { message }
}

/// Where the token file lives. Mirrors `app/api.py:default_token_path`, including the
/// `NIGHTSHIFT_UI_TOKEN_FILE` override, so a client and a daemon started with the same
/// environment always agree.
func defaultTokenPath() -> URL {
    if let override = ProcessInfo.processInfo.environment["NIGHTSHIFT_UI_TOKEN_FILE"],
       !override.isEmpty {
        return URL(fileURLWithPath: (override as NSString).expandingTildeInPath)
    }
    return FileManager.default.homeDirectoryForCurrentUser
        .appendingPathComponent("Library/Application Support/NightShift/ui-token")
}

/// The daemon's loopback port, `NIGHTSHIFT_UI_PORT` overriding the default — the same
/// escape hatch `--port` gives the Python side, so a scratch daemon can be driven without
/// touching the real queue.
func defaultPort() -> Int {
    if let text = ProcessInfo.processInfo.environment["NIGHTSHIFT_UI_PORT"],
       let port = Int(text) {
        return port
    }
    return 8402
}

actor DaemonClient {
    private var baseURL: URL
    private var tokenPath: URL
    private let session: URLSession
    private let decoder: JSONDecoder

    init(port: Int = defaultPort(), tokenPath: URL = defaultTokenPath()) {
        self.baseURL = URL(string: "http://127.0.0.1:\(port)")!
        self.tokenPath = tokenPath

        let config = URLSessionConfiguration.ephemeral
        // A local daemon that has not answered in five seconds is not going to; a UI timer
        // must not pile up requests behind one stuck call.
        config.timeoutIntervalForRequest = 5
        config.waitsForConnectivity = false
        self.session = URLSession(configuration: config)

        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .custom { decoder in
            let text = try decoder.singleValueContainer().decode(String.self)
            if let date = isoDate(text) { return date }
            throw DecodingError.dataCorruptedError(
                in: try decoder.singleValueContainer(),
                debugDescription: "not an ISO-8601 timestamp: \(text)"
            )
        }
        self.decoder = decoder
    }

    /// Point the client at a different daemon. Called once at launch by `DaemonSupervisor`,
    /// which decides between the user's real daemon and the demo one inside the bundle; the
    /// alternative — constructing the client after that decision — would mean the store
    /// could not exist until the network answered.
    func reconfigure(port: Int, tokenPath: URL) {
        self.baseURL = URL(string: "http://127.0.0.1:\(port)")!
        self.tokenPath = tokenPath
    }

    /// Re-read on every request rather than cached: the daemon may be restarted (and mint a
    /// new token) while the client stays up, and a UI that needs relaunching to recover
    /// from that is a UI people quit.
    private func token() -> String {
        (try? String(contentsOf: tokenPath, encoding: .utf8))?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
    }

    private func request(_ path: String, method: String = "GET") -> URLRequest {
        var request = URLRequest(url: URL(string: path, relativeTo: baseURL)!)
        request.httpMethod = method
        request.setValue("Bearer \(token())", forHTTPHeaderField: "Authorization")
        return request
    }

    private func send(_ request: URLRequest) async throws -> Data {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: request)
        } catch {
            throw DaemonError(
                message: "Can't reach the NightShift daemon on \(baseURL.absoluteString). "
                    + "Start it with `uv run python -m app serve`."
            )
        }
        guard let http = response as? HTTPURLResponse else {
            throw DaemonError(message: "Unexpected response from the daemon.")
        }
        guard (200..<300).contains(http.statusCode) else {
            if http.statusCode == 401 {
                throw DaemonError(
                    message: "The daemon rejected this client's token (\(tokenPath.path))."
                )
            }
            throw DaemonError(message: detail(from: data) ?? "Daemon error \(http.statusCode).")
        }
        return data
    }

    private func detail(from data: Data) -> String? {
        guard
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
            let detail = object["detail"] as? String
        else { return nil }
        return detail
    }

    private func get<T: Decodable>(_ path: String) async throws -> T {
        try decoder.decode(T.self, from: try await send(request(path)))
    }

    // MARK: - The calls the UI makes

    func state() async throws -> AppState { try await get("/state") }

    func actions() async throws -> [ActionPreview] { try await get("/actions") }

    /// Approve — and therefore *perform* — one action. The human's click ends up here, and
    /// this is the only method in the client that causes anything to happen in the world.
    func approve(_ id: String) async throws {
        _ = try await send(request("/actions/\(id)/approve", method: "POST"))
    }

    func reject(_ id: String, reason: String = "") async throws {
        var path = "/actions/\(id)/reject"
        if !reason.isEmpty,
           let encoded = reason.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) {
            path += "?reason=\(encoded)"
        }
        _ = try await send(request(path, method: "POST"))
    }

    func runNow() async throws -> RunSnapshot {
        try decoder.decode(RunSnapshot.self, from: try await send(request("/run", method: "POST")))
    }

    func nights(limit: Int = 20) async throws -> [NightRecord] {
        try await get("/nights?limit=\(limit)")
    }

    func runs(night: String? = nil, limit: Int = 50) async throws -> [RunRecord] {
        var path = "/runs?limit=\(limit)"
        if let night, !night.isEmpty { path += "&night=\(night)" }
        return try await get(path)
    }

    func run(_ id: String) async throws -> RunRecord { try await get("/runs/\(id)") }

    func replay(_ id: String, full: Bool = false) async throws -> String {
        let data = try await send(request("/runs/\(id)/replay?full=\(full)"))
        return String(decoding: data, as: UTF8.self)
    }

    var briefingURL: URL { URL(string: "/briefing", relativeTo: baseURL)!.absoluteURL }

    func briefingRequest() -> URLRequest { request("/briefing") }
}

/// Python's `.isoformat()` emits fractional seconds sometimes and a timezone sometimes; a
/// single `ISO8601DateFormatter` handles neither variation on its own.
func isoDate(_ text: String) -> Date? {
    let withFraction = ISO8601DateFormatter()
    withFraction.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    if let date = withFraction.date(from: text) { return date }

    let plain = ISO8601DateFormatter()
    plain.formatOptions = [.withInternetDateTime]
    if let date = plain.date(from: text) { return date }

    // Naive timestamps (no offset) come from local-time writers; assume local rather than
    // silently shifting a 3am run into yesterday.
    let local = DateFormatter()
    local.locale = Locale(identifier: "en_US_POSIX")
    for format in ["yyyy-MM-dd'T'HH:mm:ss.SSSSSS", "yyyy-MM-dd'T'HH:mm:ss.SSS", "yyyy-MM-dd'T'HH:mm:ss"] {
        local.dateFormat = format
        if let date = local.date(from: text) { return date }
    }
    return nil
}
