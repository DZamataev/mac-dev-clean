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
