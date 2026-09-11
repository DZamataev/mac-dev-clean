import AppKit
import Foundation
import SwiftUI

@MainActor
final class AppModel: ObservableObject {
    enum Activity: Equatable, CaseIterable {
        case idle
        case scanning
        case cleaning
        case deepScanning
        case applying
        case loadingTools
        case applyingTool

        var showsDeepScanIndicator: Bool {
            switch self {
            case .deepScanning: true
            case .idle, .scanning, .cleaning, .applying, .loadingTools, .applyingTool: false
            }
        }

        var showsToolScanIndicator: Bool {
            switch self {
            case .loadingTools: true
            case .idle, .scanning, .cleaning, .deepScanning, .applying, .applyingTool: false
            }
        }

        var message: String {
            switch self {
            case .idle: "Ready"
            case .scanning: "Scanning developer storage…"
            case .cleaning: "Cleaning selected categories…"
            case .deepScanning: "Analysing projects…"
            case .applying: "Removing selected project artifacts…"
            case .loadingTools: "Checking tool-managed storage…"
            case .applyingTool: "Running tool-managed cleanup…"
            }
        }
    }

    @Published private(set) var report: ScanReport?
    @Published private(set) var diskSpace: DiskSpace?
    @Published private(set) var activity: Activity = .idle
    @Published private(set) var deepScanState = DeepScanState()
    @Published private(set) var toolReport: ToolReport?
    @Published var selectedFlags: Set<String> = []
    @Published var errorMessage: String?
    @Published var warningMessage: String?
    @Published var noticeMessage: String?

    private let backend: (any CleanupBackendProtocol)?
    private let deepScanBackend: (any DeepScanBackendProtocol)?
    private let toolBackend: (any ToolBackendProtocol)?
    private let startupError: Error?
    private var deepScanTask: Task<Void, Never>?

    init(
        backend: (any CleanupBackendProtocol)? = nil,
        deepScanBackend: (any DeepScanBackendProtocol)? = nil,
        toolBackend: (any ToolBackendProtocol)? = nil
    ) {
        diskSpace = try? DiskSpace.current()
        if let backend {
            self.backend = backend
            self.deepScanBackend = deepScanBackend
            self.toolBackend = toolBackend ?? (backend as? any ToolBackendProtocol)
            self.startupError = nil
        } else {
            do {
                let cleanupBackend = try CleanupBackend()
                self.backend = cleanupBackend
                self.deepScanBackend = deepScanBackend ?? (try? DeepScanBackend())
                self.toolBackend = toolBackend ?? cleanupBackend
                startupError = nil
            } catch {
                self.backend = nil
                self.deepScanBackend = nil
                self.toolBackend = nil
                startupError = error
            }
        }
    }

    var groups: [CleanupGroup] {
        CleanupGroup.make(from: report?.items ?? [])
    }

    var reviewItems: [ScanItem] {
        (report?.items ?? [])
            .filter { !$0.cleanable }
            .sorted { $0.sizeBytes > $1.sizeBytes }
    }

    var selectedGroups: [CleanupGroup] {
        groups.filter { selectedFlags.contains($0.rule.flag) }
    }

    var selectedBytes: Int64 {
        selectedGroups.reduce(0) { $0 + $1.totalBytes }
    }

    var selectedLocationCount: Int {
        selectedGroups.reduce(0) { $0 + $1.items.count }
    }

    var selectedSummary: String {
        let size = ByteFormatter.string(selectedBytes)
        let unknown = selectedGroups.contains { $0.hasUnknownSize }
        return unknown ? "\(size) plus shared simulator data" : size
    }

    var isBusy: Bool { activity != .idle }
    var toolScanButtonTitle: String { toolReport == nil ? "Scan Tools" : "Refresh Tools" }

    func scanIfNeeded() async {
        guard report == nil else { return }
        await scan()
    }

    func scan() async {
        await scan(preservingMessages: false)
    }

    private func scan(preservingMessages: Bool) async {
        guard !isBusy else { return }
        guard let backend else {
            errorMessage = startupError?.localizedDescription ?? "The cleanup engine is unavailable."
            warningMessage = nil
            noticeMessage = nil
            return
        }

        activity = .scanning
        if !preservingMessages {
            dismissMessage()
        }
        refreshDiskSpace()
        do {
            let newReport = try await backend.scan()
            report = newReport
            let validFlags = Set(CleanupGroup.make(from: newReport.items).map(\.rule.flag))
            if selectedFlags.isEmpty {
                selectedFlags = validFlags
            } else {
                selectedFlags.formIntersection(validFlags)
            }
        } catch {
            let refreshFailure = error.localizedDescription
            if preservingMessages, let errorMessage {
                self.errorMessage = "\(errorMessage)\n\nStorage totals could not refresh:\n\(refreshFailure)"
            } else if let warningMessage {
                errorMessage = "\(warningMessage)\n\nThe cleanup finished, but storage totals could not refresh:\n\(refreshFailure)"
                self.warningMessage = nil
            } else if noticeMessage != nil {
                errorMessage = "Cleanup finished, but storage totals could not refresh:\n\(refreshFailure)"
                noticeMessage = nil
            } else {
                errorMessage = refreshFailure
            }
        }
        refreshDiskSpace()
        activity = .idle
    }

    func loadTools() async {
        await loadTools(preservingMessages: false)
    }

    private func loadTools(preservingMessages: Bool) async {
        guard !isBusy else { return }
        toolReport = nil
        guard let toolBackend else {
            errorMessage = startupError?.localizedDescription ?? "The tool inventory is unavailable."
            warningMessage = nil
            noticeMessage = nil
            return
        }

        activity = .loadingTools
        if !preservingMessages {
            dismissMessage()
        }
        do {
            toolReport = try await toolBackend.loadTools()
        } catch {
            let refreshFailure = error.localizedDescription
            if preservingMessages, let errorMessage {
                self.errorMessage = "\(errorMessage)\n\nTool inventory could not refresh:\n\(refreshFailure)"
            } else if let warningMessage {
                errorMessage = "\(warningMessage)\n\nTool inventory could not refresh:\n\(refreshFailure)"
                self.warningMessage = nil
            } else if noticeMessage != nil {
                errorMessage = "Tool cleanup finished, but inventory could not refresh:\n\(refreshFailure)"
                noticeMessage = nil
            } else {
                errorMessage = refreshFailure
            }
        }
        refreshDiskSpace()
        activity = .idle
    }

    func applyTool(id: String) async {
        guard !isBusy, let toolBackend else { return }
        guard let recommendation = toolReport?.recommendations.first(where: { $0.id == id }) else {
            return
        }

        activity = .applyingTool
        dismissMessage()
        var requiresRefresh = true
        do {
            let report = try await toolBackend.applyTool(id: recommendation.id)
            guard let result = report.results.first(where: { $0.id == recommendation.id }) else {
                throw BackendError.invalidOutput("Tool cleanup returned a mismatched result.")
            }
            requiresRefresh = false
            let warnings = [result.journalWarning, report.warning ?? ""].filter { !$0.isEmpty }
            let resultLabel = result.label.trimmingCharacters(in: .whitespacesAndNewlines)
            let reviewedLabel = recommendation.label.trimmingCharacters(in: .whitespacesAndNewlines)
            let label = !resultLabel.isEmpty
                ? resultLabel
                : (reviewedLabel.isEmpty ? "Tool action" : reviewedLabel)
            if result.succeeded {
                if let currentReport = toolReport {
                    let remaining = currentReport.recommendations.filter { $0.id != result.id }
                    toolReport = ToolReport(
                        statuses: currentReport.statuses,
                        recommendations: remaining,
                        reclaimableTotalBytes: remaining.reduce(0) { $0 + $1.reclaimableBytes }
                    )
                }
            }
            if result.succeeded, warnings.isEmpty {
                let reported = result.reported.isEmpty ? result.size : result.reported
                noticeMessage = "Tool cleanup finished for \(label). \(reported)"
            } else if result.succeeded {
                warningMessage = "Tool cleanup finished for \(label). \(warnings.joined(separator: " "))"
            } else {
                let reason = result.error.isEmpty ? "The action was not run." : result.error
                let details = ([reason] + warnings).joined(separator: " ")
                warningMessage = "\(label) was not changed. \(details)"
            }
        } catch {
            errorMessage = error.localizedDescription
        }
        activity = .idle
        refreshDiskSpace()
        if requiresRefresh {
            await loadTools(preservingMessages: true)
        }
    }

    func cleanSelected() async {
        guard !isBusy, let backend else { return }
        let flags = selectedGroups.map(\.rule.flag).sorted()
        guard !flags.isEmpty else { return }

        report = nil
        selectedFlags.removeAll()
        activity = .cleaning
        errorMessage = nil
        warningMessage = nil
        noticeMessage = nil
        do {
            let result = try await backend.clean(flags: flags)
            let failures = result.items.filter { !$0.error.isEmpty }
            if failures.isEmpty {
                noticeMessage = "Cleanup finished. Selected \(result.total) across \(result.count) location(s)."
            } else {
                warningMessage = Self.cleanupWarning(for: result)
            }
        } catch {
            errorMessage = error.localizedDescription
            warningMessage = nil
            noticeMessage = nil
        }
        activity = .idle
        await scan(preservingMessages: true)
    }

    var deepScanSelectionSummary: String {
        ByteFormatter.string(deepScanState.selectedBytes)
    }

    func toggleDeepScanItem(_ id: String) {
        deepScanState.toggle(id)
    }

    func startDeepScan() async {
        await startDeepScan(preservingMessages: false)
    }

    /// Mirrors the existing `scan(preservingMessages:)` pattern: the rescan that
    /// follows a cleanup must not wipe the warning describing what was skipped.
    private func startDeepScan(preservingMessages: Bool) async {
        guard !isBusy, let deepScanBackend else { return }

        activity = .deepScanning
        if !preservingMessages {
            dismissMessage()
        }
        deepScanState.reset()

        // `self` is captured once, immutably, before the Task is formed. Capturing
        // the @MainActor model directly inside the @Sendable event callback would
        // be a concurrency error under Swift 6 strict checking.
        let task = Task { @MainActor [self] in
            let stream = AsyncStream<DeepScanEvent> { continuation in
                // Kept detached (rather than a plain, MainActor-inheriting `Task {}`)
                // so the blocking Process/Pipe I/O in `deepScan` never runs on the
                // main actor's executor. Detached tasks do not inherit cancellation,
                // so `onTermination` below is what actually propagates it: cancelling
                // the outer `deepScanTask` cancels this stream's iteration, which
                // fires `onTermination`, which cancels `inner`, which is what
                // `deepScanBackend.deepScan`'s `withTaskCancellationHandler` observes.
                let inner = Task.detached {
                    do {
                        try await deepScanBackend.deepScan { event in
                            continuation.yield(event)
                        }
                    } catch {
                        // The terminal event never arrived; `isRunning` is cleared
                        // when the stream finishes below.
                    }
                    continuation.finish()
                }
                continuation.onTermination = { @Sendable _ in
                    inner.cancel()
                }
            }
            for await event in stream {
                deepScanState.apply(event)
            }
        }
        deepScanTask = task
        await task.value
        deepScanTask = nil
        activity = .idle
        refreshDiskSpace()
    }

    func cancelDeepScan() {
        deepScanTask?.cancel()
        deepScanTask = nil
    }

    func applyDeepScanSelection() async {
        guard !isBusy, let deepScanBackend else { return }
        let ids = deepScanState.items
            .filter { deepScanState.selectedIds.contains($0.id) }
            .map(\.id)
        guard !ids.isEmpty else { return }

        activity = .applying
        dismissMessage()
        do {
            let results = try await deepScanBackend.apply(ids: ids, dryRun: false)
            let failures = results.filter { !$0.succeeded }
            if failures.isEmpty {
                let removed = results.reduce(Int64(0)) { $0 + $1.reclaimableBytes }
                noticeMessage = "Removed \(ByteFormatter.string(removed)) across \(results.count) location(s)."
            } else {
                warningMessage = Self.applyWarning(for: results)
            }
        } catch {
            errorMessage = error.localizedDescription
        }
        activity = .idle
        refreshDiskSpace()
        await startDeepScan(preservingMessages: true)
    }

    static func applyWarning(for results: [ApplyResultItem]) -> String? {
        let failures = results.filter { !$0.succeeded }
        guard !failures.isEmpty else { return nil }

        let removed = results.filter(\.succeeded)
        let removedBytes = removed.reduce(Int64(0)) { $0 + $1.reclaimableBytes }
        let itemWord = failures.count == 1 ? "item was" : "items were"
        let success = removed.isEmpty
            ? "Nothing was removed."
            : "Removed \(ByteFormatter.string(removedBytes)) across \(removed.count) location(s)."
        let details = failures.map { "• \($0.label): \($0.error)\n  \($0.path)" }
            .joined(separator: "\n")

        return "\(failures.count) \(itemWord) skipped. \(success) No additional files were removed.\n\n\(details)"
    }

    func selectAll() {
        selectedFlags = Set(groups.map(\.rule.flag))
    }

    func clearSelection() {
        selectedFlags.removeAll()
    }

    func dismissMessage() {
        errorMessage = nil
        warningMessage = nil
        noticeMessage = nil
    }

    static func cleanupWarning(for report: CleanReport) -> String? {
        let failures = report.items.filter { !$0.error.isEmpty }
        guard !failures.isEmpty else { return nil }

        let removedCount = report.items.filter { $0.removed && $0.error.isEmpty }.count
        let itemWord = failures.count == 1 ? "item was" : "items were"
        let locationWord = removedCount == 1 ? "location" : "locations"
        let success = removedCount == 0
            ? "No locations were cleaned."
            : "Cleaned \(report.total) across \(removedCount) \(locationWord)."
        let details = failures.map { item in
            "• \(item.label): \(item.error)\n  \(item.path)"
        }.joined(separator: "\n")

        return "Cleanup finished, but \(failures.count) \(itemWord) skipped. \(success) No additional files will be removed.\n\n\(details)"
    }

    func reveal(_ item: ScanItem) {
        NSWorkspace.shared.selectFile(item.path, inFileViewerRootedAtPath: "")
    }

    func openICloudDrive() {
        let path = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Mobile Documents/com~apple~CloudDocs")
        NSWorkspace.shared.open(path)
    }

    private func refreshDiskSpace() {
        diskSpace = try? DiskSpace.current()
    }
}
