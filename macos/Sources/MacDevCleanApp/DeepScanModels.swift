import Foundation

struct EvidenceItem: Decodable, Hashable, Sendable {
    let code: String
    let detail: String
}

struct RecommendationItem: Decodable, Identifiable, Hashable, Sendable {
    let id: String
    let detectorId: String
    let category: String
    let label: String
    let path: String
    let action: String
    let allocatedBytes: Int64
    let reclaimableBytes: Int64
    let size: String
    let confidence: String
    let restoration: String
    let selectedByDefault: Bool
    let evidence: [EvidenceItem]
    let safetyRoot: String
    let reason: String
    let warning: String
    let generation: Int
    let lastActivityAt: String?

    var displayPath: String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        if path == home { return "~" }
        if path.hasPrefix(home + "/") {
            return "~" + path.dropFirst(home.count)
        }
        return path
    }

    var restorationSummary: String {
        switch restoration {
        case "rebuild": "Rebuilt by the next build"
        case "redownload": "Reinstalled from the lock file"
        case "reinstall": "Reinstalled manually"
        case "external_state": "Managed by another tool"
        default: "No restore needed"
        }
    }

    enum CodingKeys: String, CodingKey {
        case id
        case detectorId = "detector_id"
        case category
        case label
        case path
        case action
        case allocatedBytes = "allocated_bytes"
        case reclaimableBytes = "reclaimable_bytes"
        case size
        case confidence
        case restoration
        case selectedByDefault = "selected_by_default"
        case evidence
        case safetyRoot = "safety_root"
        case reason
        case warning
        case generation
        case lastActivityAt = "last_activity_at"
    }
}

struct ToolStatus: Decodable, Identifiable, Hashable, Sendable {
    let tool: String
    let available: Bool
    let reason: String

    var id: String { tool }
}

struct ToolActionPayload: Decodable, Hashable, Sendable {
    let tool: String
    let resource: String
    let argv: [String]
    let previewArgv: [String]
    let reported: String

    var displayCommand: String {
        argv.map(Self.quoteForDisplay).joined(separator: " ")
    }

    private static func quoteForDisplay(_ argument: String) -> String {
        let safe = CharacterSet(charactersIn: "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_@%+=:,./-")
        if !argument.isEmpty, argument.unicodeScalars.allSatisfy(safe.contains) {
            return argument
        }
        return "'" + argument.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    private enum CodingKeys: String, CodingKey {
        case tool
        case resource
        case argv
        case reported
        case previewArgv = "preview_argv"
    }
}

struct ToolUsageSection: Hashable, Sendable {
    let title: String
    let paths: [String]
}

enum ToolUsageState: String, Decodable, Hashable, Sendable {
    case matched
    case unreferenced
    case unknown
}

struct ToolUsagePayload: Decodable, Hashable, Sendable {
    let state: ToolUsageState
    let projects: [String]
    let unpinnedProjects: [String]
    let scanCompletedAt: String?

    private enum CodingKeys: String, CodingKey {
        case state, projects
        case unpinnedProjects = "unpinned_projects"
        case scanCompletedAt = "scan_completed_at"
    }
}

struct ToolRecommendation: Decodable, Identifiable, Hashable, Sendable {
    let id: String
    let detectorId: String
    let category: String
    let label: String
    let size: String
    let reclaimableBytes: Int64
    let reason: String
    let warning: String
    let selectedByDefault: Bool
    let toolAction: ToolActionPayload?
    let toolUsage: ToolUsagePayload?

    nonisolated var usageSummary: String? {
        guard let toolUsage else { return nil }
        switch toolUsage.state {
        case .matched:
            let count = toolUsage.projects.count
            return "Used by \(count) project\(count == 1 ? "" : "s")"
        case .unreferenced:
            return "Not referenced by scanned projects"
        case .unknown:
            return "Usage unknown — run Deep Scan"
        }
    }

    nonisolated var usageSections: [ToolUsageSection] {
        guard let toolUsage else { return [] }
        var sections: [ToolUsageSection] = []
        if !toolUsage.projects.isEmpty {
            sections.append(ToolUsageSection(title: "Matching version", paths: toolUsage.projects))
        }
        if !toolUsage.unpinnedProjects.isEmpty {
            sections.append(
                ToolUsageSection(
                    title: "Uses an unpinned NDK",
                    paths: toolUsage.unpinnedProjects
                )
            )
        }
        return sections
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case category
        case label
        case size
        case reason
        case warning
        case detectorId = "detector_id"
        case reclaimableBytes = "reclaimable_bytes"
        case selectedByDefault = "selected_by_default"
        case toolAction = "tool_action"
        case toolUsage = "tool_usage"
    }
}

struct ToolReport: Decodable, Sendable {
    let statuses: [ToolStatus]
    let recommendations: [ToolRecommendation]
    let reclaimableTotalBytes: Int64

    private enum CodingKeys: String, CodingKey {
        case statuses
        case recommendations
        case reclaimableTotalBytes = "reclaimable_total_bytes"
    }
}

struct ToolApplyResult: Decodable, Sendable {
    let id: String
    let label: String
    let path: String
    let reclaimableBytes: Int64
    let size: String
    let outcome: String
    let dryRun: Bool
    let error: String
    let reported: String
    let journalWarning: String

    var succeeded: Bool { outcome == "invoked" }

    private enum CodingKeys: String, CodingKey {
        case id
        case label
        case path
        case size
        case outcome
        case error
        case reported
        case reclaimableBytes = "reclaimable_bytes"
        case dryRun = "dry_run"
        case journalWarning = "journal_warning"
    }
}

struct ToolApplyReport: Decodable, Sendable {
    let results: [ToolApplyResult]
    let warning: String?
}

struct ApplyResultItem: Decodable, Sendable {
    let id: String
    let label: String
    let path: String
    let reclaimableBytes: Int64
    let size: String
    let outcome: String
    let dryRun: Bool
    let error: String

    var succeeded: Bool { outcome == "removed" }

    enum CodingKeys: String, CodingKey {
        case id
        case label
        case path
        case reclaimableBytes = "reclaimable_bytes"
        case size
        case outcome
        case dryRun = "dry_run"
        case error
    }
}

struct ApplyReport: Decodable, Sendable {
    let results: [ApplyResultItem]
}

enum DeepScanEvent: Sendable {
    case started(roots: [String], incremental: Bool)
    case rootStarted(path: String)
    case progress(path: String, scanned: Int)
    case candidateFound(RecommendationItem)
    case rootFinished(path: String, repositories: Int)
    case permissionRequired(path: String, folder: String)
    case warning(message: String, path: String)
    case completed(reclaimableBytes: Int64, count: Int)
    case cancelled(reclaimableBytes: Int64, count: Int)
}

enum DeepScanEventParser {
    static let supportedProtocolVersion = 1

    /// Decode one NDJSON line. Returns nil for blank lines, malformed JSON,
    /// unknown event names, and incompatible protocol versions so a future
    /// engine can add events without breaking an older app build.
    static func parse(line: String) -> DeepScanEvent? {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let data = trimmed.data(using: .utf8) else { return nil }
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        guard let version = object["protocol_version"] as? Int,
              version == supportedProtocolVersion,
              let event = object["event"] as? String
        else { return nil }

        switch event {
        case "scan_started":
            return .started(
                roots: object["roots"] as? [String] ?? [],
                incremental: object["incremental"] as? Bool ?? false
            )
        case "root_started":
            return .rootStarted(path: object["path"] as? String ?? "")
        case "progress":
            return .progress(
                path: object["path"] as? String ?? "",
                scanned: object["scanned"] as? Int ?? 0
            )
        case "candidate_found":
            guard let raw = object["recommendation"],
                  let payload = try? JSONSerialization.data(withJSONObject: raw),
                  let item = try? JSONDecoder().decode(RecommendationItem.self, from: payload)
            else { return nil }
            return .candidateFound(item)
        case "root_finished":
            return .rootFinished(
                path: object["path"] as? String ?? "",
                repositories: object["repositories"] as? Int ?? 0
            )
        case "permission_required":
            return .permissionRequired(
                path: object["path"] as? String ?? "",
                folder: object["folder"] as? String ?? ""
            )
        case "warning":
            return .warning(
                message: object["message"] as? String ?? "",
                path: object["path"] as? String ?? ""
            )
        case "scan_completed":
            return .completed(
                reclaimableBytes: (object["reclaimable_bytes"] as? NSNumber)?.int64Value ?? 0,
                count: object["count"] as? Int ?? 0
            )
        case "scan_cancelled":
            return .cancelled(
                reclaimableBytes: (object["reclaimable_bytes"] as? NSNumber)?.int64Value ?? 0,
                count: object["count"] as? Int ?? 0
            )
        default:
            return nil
        }
    }
}

/// Buffers raw stdout bytes into complete NDJSON lines and parses each into a
/// `DeepScanEvent`. Extracted from `DeepScanBackend.deepScan`'s read loop so
/// the "trailing line without a newline" edge case can be unit tested without
/// spawning a real process.
struct NDJSONEventAccumulator {
    private var buffer = Data()

    /// Appends a raw chunk (as delivered by `FileHandle.availableData`) and
    /// returns every complete, parseable event found in `buffer` so far.
    /// Bytes after the last newline stay buffered for the next call.
    mutating func ingest(_ chunk: Data) -> [DeepScanEvent] {
        buffer.append(chunk)
        var events: [DeepScanEvent] = []
        while let newline = buffer.firstIndex(of: UInt8(ascii: "\n")) {
            let lineData = buffer[buffer.startIndex..<newline]
            buffer.removeSubrange(buffer.startIndex...newline)
            if let line = String(data: lineData, encoding: .utf8),
               let event = DeepScanEventParser.parse(line: line)
            {
                events.append(event)
            }
        }
        return events
    }

    /// Call once after the source has hit EOF. Parses and returns whatever is
    /// left in `buffer` as a final line -- the tail write may never have had
    /// a trailing newline -- and clears the buffer. Returns `nil` if nothing
    /// residual parses to an event.
    mutating func finish() -> DeepScanEvent? {
        defer { buffer.removeAll() }
        guard !buffer.isEmpty,
              let line = String(data: buffer, encoding: .utf8)
        else { return nil }
        return DeepScanEventParser.parse(line: line)
    }
}

struct DeepScanWarning: Identifiable, Hashable, Sendable {
    let id = UUID()
    let message: String
    let path: String
}

struct DeepScanState: Sendable {
    private(set) var items: [RecommendationItem] = []
    private(set) var selectedIds: Set<String> = []
    private(set) var warnings: [DeepScanWarning] = []
    private(set) var blockedFolders: [String] = []
    private(set) var currentPath: String = ""
    private(set) var scannedCount: Int = 0
    private(set) var isRunning: Bool = false
    private(set) var wasCancelled: Bool = false
    private(set) var isIncremental: Bool = false

    var selectedBytes: Int64 {
        items.filter { selectedIds.contains($0.id) }
            .reduce(0) { $0 + $1.reclaimableBytes }
    }

    var totalBytes: Int64 {
        items.reduce(0) { $0 + $1.reclaimableBytes }
    }

    mutating func reset() {
        self = DeepScanState()
    }

    mutating func toggle(_ id: String) {
        if selectedIds.contains(id) {
            selectedIds.remove(id)
        } else if items.contains(where: { $0.id == id }) {
            selectedIds.insert(id)
        }
    }

    mutating func apply(_ event: DeepScanEvent) {
        switch event {
        case let .started(_, incremental):
            isRunning = true
            wasCancelled = false
            isIncremental = incremental
        case let .rootStarted(path):
            currentPath = path
        case let .progress(path, scanned):
            currentPath = path
            scannedCount = scanned
        case let .candidateFound(item):
            items.append(item)
            items.sort { $0.reclaimableBytes > $1.reclaimableBytes }
            if item.selectedByDefault {
                selectedIds.insert(item.id)
            }
        case .rootFinished:
            break
        case let .permissionRequired(_, folder):
            if !blockedFolders.contains(folder) {
                blockedFolders.append(folder)
            }
        case let .warning(message, path):
            warnings.append(DeepScanWarning(message: message, path: path))
        case .completed:
            isRunning = false
        case .cancelled:
            isRunning = false
            wasCancelled = true
        }
    }
}
