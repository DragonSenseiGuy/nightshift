// The wire models — one Swift struct per Pydantic model on the daemon side.
//
// These are deliberately *transcriptions*, not a second model layer: `AppState` here has
// the fields `app/service.py:AppState` has and nothing else, and every decision it encodes
// (which icon, which summary sentence, whether it is bedtime) arrives already made. The
// client's job is to render. If a view wants a fact that isn't here, the fix is a field on
// the Python model, so the rumps menu and this one cannot drift apart.

import Foundation

enum Status: String, Codable {
    case idle, attention, failed, running

    /// SF Symbol per state. The daemon also sends an emoji `icon` for rumps; a native
    /// client gets a template symbol that tints itself correctly in both menu bars.
    var symbol: String {
        switch self {
        case .idle: return "moon.stars"
        case .attention: return "moon.badge.plus"
        case .failed: return "moon.circle.fill"
        case .running: return "moon.zzz.fill"
        }
    }
}

enum RunPhase: String, Codable {
    case never, running, succeeded, failed
}

struct RunSnapshot: Codable, Equatable {
    var phase: RunPhase = .never
    var startedAt: Date?
    var finishedAt: Date?
    var exitCode: Int?
    var logPath: String = ""

    var active: Bool { phase == .running }

    enum CodingKeys: String, CodingKey {
        case phase
        case startedAt = "started_at"
        case finishedAt = "finished_at"
        case exitCode = "exit_code"
        case logPath = "log_path"
    }
}

struct AppState: Codable, Equatable {
    var status: Status = .idle
    var icon: String = "🌙"
    var summary: String = ""
    var pending: Int = 0
    var run: RunSnapshot = RunSnapshot()
    var briefingPath: String = ""
    var briefingAvailable: Bool = false
    var warning: String = ""
    var queueError: String = ""

    enum CodingKeys: String, CodingKey {
        case status, icon, summary, pending, run, warning
        case briefingPath = "briefing_path"
        case briefingAvailable = "briefing_available"
        case queueError = "queue_error"
    }
}

enum ActionType: String, Codable {
    case sendEmail = "send_email"
    case draftReply = "draft_reply"
    case mergeBranch = "merge_branch"

    var label: String {
        switch self {
        case .sendEmail: return "Send email"
        case .draftReply: return "Draft reply"
        case .mergeBranch: return "Merge branch"
        }
    }

    var symbol: String {
        switch self {
        case .sendEmail, .draftReply: return "envelope"
        case .mergeBranch: return "arrow.triangle.merge"
        }
    }
}

/// One pending side effect, rendered for the human about to decide it.
///
/// `effect` is the sentence security rule 3 exists for, and the views below treat it as
/// non-negotiable: it is shown next to the action, again in the confirmation, and it is
/// never truncated. `detail` and `title` are email-derived — always plain `Text`, never
/// markdown, never HTML, never anything that interprets them.
struct ActionPreview: Codable, Identifiable, Equatable {
    var id: String
    var type: ActionType
    var title: String
    var effect: String
    var detail: String = ""
    var tainted: Bool = false
    var origin: String = ""
}

// MARK: - Run history

struct TokenUsage: Codable, Equatable {
    var promptTokens: Int = 0
    var completionTokens: Int = 0
    /// True when the provider omitted usage and the numbers were reconstructed worst-case.
    /// Shown next to the cost, because a guessed figure must never read as a metered one.
    var estimated: Bool = false

    var totalTokens: Int { promptTokens + completionTokens }

    enum CodingKeys: String, CodingKey {
        case estimated
        case promptTokens = "prompt_tokens"
        case completionTokens = "completion_tokens"
    }
}

struct NightRecord: Codable, Identifiable, Equatable {
    var id: String
    var startedAt: Date?
    var finishedAt: Date?
    var outcome: String = ""
    /// The power guard's reason, when a night did not happen. A refused night must look
    /// different from a quiet one here too, not just in the briefing.
    var refused: String = ""
    var failures: Int = 0
    var stages: [String] = []
    var seconds: Double = 0
    var note: String = ""
    /// Summed from the night's agent runs by the daemon — `NightRunRecord` has no cost of
    /// its own, and a history view without the spend is the one number people open it for.
    var costUsd: Double = 0

    enum CodingKeys: String, CodingKey {
        case id, outcome, refused, failures, stages, seconds, note
        case startedAt = "started_at"
        case finishedAt = "finished_at"
        case costUsd = "cost_usd"
    }
}

/// A stored agent run. The list view decodes this without `messages`/`transcript`; opening
/// one fetches the same shape with them filled in.
struct RunRecord: Codable, Identifiable, Equatable {
    var id: String
    var nightId: String = ""
    var agent: String
    var model: String = ""
    var source: String = "host"
    var project: String = ""
    var startedAt: Date?
    var finishedAt: Date?
    var stopReason: String = "completed"
    var steps: Int = 0
    var usage: TokenUsage = TokenUsage()
    var costUsd: Double = 0
    var taint: [String] = []
    var text: String = ""
    var transcript: [ToolCallRecord] = []

    /// Whether this run touched untrusted input. Drives the UNTRUSTED banner in the viewer:
    /// a transcript is the most thoroughly untrusted text in the system and must never be
    /// presented as if the agent's own words were authoritative.
    var tainted: Bool { !taint.isEmpty }

    enum CodingKeys: String, CodingKey {
        case id, agent, model, source, project, steps, usage, taint, text, transcript
        case nightId = "night_id"
        case startedAt = "started_at"
        case finishedAt = "finished_at"
        case stopReason = "stop_reason"
        case costUsd = "cost_usd"
    }
}

struct ToolCallRecord: Codable, Equatable, Identifiable {
    var id: String { "\(step)-\(tool)" }
    var step: Int = 0
    var tool: String = ""
    var arguments: String = ""
    var ok: Bool = true
    var result: String = ""
    var error: String = ""
    var taint: [String] = []

    enum CodingKeys: String, CodingKey {
        case step, tool, arguments, ok, result, error, taint
    }

    /// Hand-written because the daemon stores arguments as a JSON object and results as
    /// text; a mismatch here must degrade to "show it as a string", not fail the whole
    /// transcript and leave the morning with no way to see what the agent did.
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        step = (try? container.decode(Int.self, forKey: .step)) ?? 0
        tool = (try? container.decode(String.self, forKey: .tool)) ?? ""
        ok = (try? container.decode(Bool.self, forKey: .ok)) ?? true
        result = (try? container.decode(String.self, forKey: .result)) ?? ""
        error = (try? container.decode(String.self, forKey: .error)) ?? ""
        taint = (try? container.decode([String].self, forKey: .taint)) ?? []
        if let text = try? container.decode(String.self, forKey: .arguments) {
            arguments = text
        } else if let object = try? container.decode([String: AnyCodable].self, forKey: .arguments),
                  let data = try? JSONSerialization.data(
                      withJSONObject: object.mapValues(\.value), options: [.sortedKeys]
                  ) {
            arguments = String(decoding: data, as: UTF8.self)
        }
    }
}

/// Minimal `Any` box, only for re-encoding a tool call's argument object for display.
struct AnyCodable: Codable {
    let value: Any

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if let value = try? container.decode(Bool.self) { self.value = value }
        else if let value = try? container.decode(Int.self) { self.value = value }
        else if let value = try? container.decode(Double.self) { self.value = value }
        else if let value = try? container.decode(String.self) { self.value = value }
        else if let value = try? container.decode([AnyCodable].self) {
            self.value = value.map(\.value)
        } else if let value = try? container.decode([String: AnyCodable].self) {
            self.value = value.mapValues(\.value)
        } else { self.value = "" }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        try container.encode(String(describing: value))
    }
}
