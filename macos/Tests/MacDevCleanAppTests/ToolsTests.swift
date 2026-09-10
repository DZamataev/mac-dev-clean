import Foundation
import Testing
@testable import MacDevCleanApp

@Test func toolReportDecodesStatusesAndRecommendations() throws {
    let json = #"""
    {
      "reclaimable_total_bytes": 7499000000,
      "statuses": [
        {"tool": "docker", "available": true, "reason": ""},
        {"tool": "homebrew", "available": false, "reason": "not installed"}
      ],
      "recommendations": [
        {
          "id": "abc123",
          "detector_id": "docker-build-cache",
          "category": "tool-managed",
          "label": "Docker build cache",
          "path": "/Users/test/home",
          "action": "invoke_tool",
          "allocated_bytes": 18920000000,
          "reclaimable_bytes": 7499000000,
          "size": "7.0 GB",
          "confidence": "exact",
          "restoration": "external_state",
          "selected_by_default": false,
          "evidence": [{"code": "tool-report", "detail": "docker reports 7.499GB reclaimable"}],
          "safety_root": "/Users/test/home",
          "reason": "Docker reports reclaimable build cache.",
          "warning": "",
          "generation": 0,
          "last_activity_at": null,
          "tool_action": {
            "tool": "docker",
            "resource": "build-cache",
            "argv": ["docker", "builder", "prune", "-f"],
            "preview_argv": ["docker", "system", "df", "--format", "{{json .}}"],
            "reported": "7.499GB"
          }
        }
      ]
    }
    """#

    let report = try JSONDecoder().decode(ToolReport.self, from: Data(json.utf8))

    #expect(report.reclaimableTotalBytes == 7_499_000_000)
    #expect(report.statuses.count == 2)
    #expect(report.statuses[1].available == false)
    #expect(report.statuses[1].reason == "not installed")
    #expect(report.recommendations.count == 1)
    #expect(report.recommendations[0].toolAction?.argv == ["docker", "builder", "prune", "-f"])
}

@Test func unavailableToolsRemainInAnEmptyReport() throws {
    let json = #"""
    {"reclaimable_total_bytes": 0,
     "statuses": [{"tool": "docker", "available": false, "reason": "daemon stopped"}],
     "recommendations": []}
    """#

    let report = try JSONDecoder().decode(ToolReport.self, from: Data(json.utf8))

    #expect(report.statuses.count == 1)
    #expect(report.statuses[0].reason == "daemon stopped")
    #expect(report.recommendations.isEmpty)
}

@Test func toolRecommendationsAreNeverSelectedByDefault() throws {
    let json = #"""
    {"reclaimable_total_bytes": 1,
     "statuses": [],
     "recommendations": [
       {"id": "x", "detector_id": "d", "category": "tool-managed", "label": "L",
        "path": "/Users/test/home", "action": "invoke_tool", "allocated_bytes": 1,
        "reclaimable_bytes": 1, "size": "1 B", "confidence": "exact",
        "restoration": "external_state", "selected_by_default": false,
        "evidence": [], "safety_root": "/Users/test/home", "reason": "",
        "warning": "", "generation": 0, "last_activity_at": null,
        "tool_action": {"tool": "t", "resource": "r", "argv": ["t"],
                        "preview_argv": ["t"], "reported": ""}}
     ]}
    """#

    let report = try JSONDecoder().decode(ToolReport.self, from: Data(json.utf8))

    #expect(report.recommendations[0].selectedByDefault == false)
}

@Test func toolBackendBuildsDedicatedCommandsFromThePersistedId() {
    #expect(CleanupBackend.toolInventoryArguments == ["tools", "--json"])
    #expect(
        CleanupBackend.toolApplyArguments(id: "persisted-id")
            == ["tools-apply", "--id", "persisted-id", "--json"]
    )
}

@Test func toolBackendDrainsLargeInventoryBeforeWaitingForExit() async throws {
    let fileManager = FileManager.default
    let directory = fileManager.temporaryDirectory
        .appendingPathComponent("mac-dev-clean-tools-\(UUID().uuidString)", isDirectory: true)
    try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
    defer { try? fileManager.removeItem(at: directory) }

    let reason = String(repeating: "x", count: 200_000)
    let payload = #"{"reclaimable_total_bytes":0,"statuses":[{"tool":"docker","available":false,"reason":"\#(reason)"}],"recommendations":[]}"#
    let payloadURL = directory.appendingPathComponent("payload.json")
    try Data(payload.utf8).write(to: payloadURL)

    let executableURL = directory.appendingPathComponent("fake-python")
    let script = "#!/bin/sh\n/bin/cat \"\(payloadURL.path)\"\n"
    try Data(script.utf8).write(to: executableURL)
    try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executableURL.path)

    let backend = try CleanupBackend(
        location: BackendLocation(
            pythonURL: executableURL,
            pythonPath: directory,
            workingDirectory: directory
        )
    )

    let report = try await backend.loadTools()

    #expect(report.statuses.first?.reason.count == reason.count)
}

@Test func cancellingToolInventoryTerminatesTheBackendProcess() async throws {
    let fileManager = FileManager.default
    let directory = fileManager.temporaryDirectory
        .appendingPathComponent("mac-dev-clean-cancel-\(UUID().uuidString)", isDirectory: true)
    defer { try? fileManager.removeItem(at: directory) }
    let backend = try fakeBackend(
        in: directory,
        scriptBody: "exec /bin/sleep 30",
        commandTimeout: .seconds(10)
    )

    let task = Task { try await backend.loadTools() }
    try await Task.sleep(for: .milliseconds(50))
    task.cancel()

    do {
        _ = try await task.value
        Issue.record("Expected cancelled inventory to throw")
    } catch {
        #expect(error is CancellationError)
    }
}

@Test func toolInventoryTimesOutAHungBackendProcess() async throws {
    let fileManager = FileManager.default
    let directory = fileManager.temporaryDirectory
        .appendingPathComponent("mac-dev-clean-timeout-\(UUID().uuidString)", isDirectory: true)
    defer { try? fileManager.removeItem(at: directory) }
    let backend = try fakeBackend(
        in: directory,
        scriptBody: "trap '' TERM\nwhile :; do /bin/sleep 1; done",
        commandTimeout: .milliseconds(100)
    )

    do {
        _ = try await backend.loadTools()
        Issue.record("Expected timed-out inventory to throw")
    } catch {
        #expect(error.localizedDescription.contains("timed out"))
    }
}

@Test func cancellingToolApplyStillWaitsForItsAuthoritativeResult() async throws {
    let fileManager = FileManager.default
    let directory = fileManager.temporaryDirectory
        .appendingPathComponent("mac-dev-clean-apply-cancel-\(UUID().uuidString)", isDirectory: true)
    defer { try? fileManager.removeItem(at: directory) }
    let json = #"{"results":[{"id":"persisted-id","label":"Docker build cache","path":"docker:build-cache","reclaimable_bytes":10,"size":"10 B","outcome":"invoked","dry_run":false,"error":"","reported":"10 B","journal_warning":""}]}"#
    let backend = try fakeBackend(
        in: directory,
        scriptBody: "/bin/sleep 0.1\n/bin/echo '\(json)'",
        commandTimeout: .seconds(1)
    )

    let task = Task { try await backend.applyTool(id: "persisted-id") }
    try await Task.sleep(for: .milliseconds(20))
    task.cancel()
    let report = try await task.value

    #expect(report.results.count == 1)
    #expect(report.results[0].succeeded)
}

@Test func toolInventoryRejectsOversizedOutputWithoutEchoingIt() async throws {
    let fileManager = FileManager.default
    let directory = fileManager.temporaryDirectory
        .appendingPathComponent("mac-dev-clean-output-cap-\(UUID().uuidString)", isDirectory: true)
    try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
    defer { try? fileManager.removeItem(at: directory) }

    let reason = "SENSITIVE_START" + String(repeating: "x", count: 9_000_000)
    let payload = #"{"reclaimable_total_bytes":0,"statuses":[{"tool":"docker","available":false,"reason":"\#(reason)"}],"recommendations":[]}"#
    let payloadURL = directory.appendingPathComponent("payload.json")
    try Data(payload.utf8).write(to: payloadURL)
    let backend = try fakeBackend(
        in: directory,
        scriptBody: "/bin/cat \"\(payloadURL.path)\"",
        commandTimeout: .seconds(10)
    )

    do {
        _ = try await backend.loadTools()
        Issue.record("Expected oversized inventory to throw")
    } catch {
        #expect(error.localizedDescription.contains("safe output limit"))
        #expect(!error.localizedDescription.contains("SENSITIVE_START"))
        #expect(error.localizedDescription.count < 5_000)
    }
}

@Test func toolManagedPageAndCommandPreviewAreExplicit() {
    #expect(SidebarPage.tools.rawValue == "Tool-managed")
    #expect(SidebarPage.tools.symbol == "wrench.and.screwdriver")
    #expect(SidebarPage.cleanup.showsPrimaryScanProgress)
    #expect(SidebarPage.review.showsPrimaryScanProgress)
    #expect(!SidebarPage.tools.showsPrimaryScanProgress)

    let action = ToolActionPayload(
        tool: "docker",
        resource: "build-cache",
        argv: ["docker", "builder", "two words", "$HOME", "it's", ""],
        previewArgv: ["docker", "system", "df"],
        reported: ""
    )
    #expect(action.displayCommand == "docker builder 'two words' '$HOME' 'it'\\''s' ''")
}

@Test func toolApplyDecodesAnAuthoritativeRefusalFromExitOne() throws {
    let json = #"""
    {"results":[{
      "id":"persisted-id", "label":"Docker build cache", "path":"docker:build-cache",
      "reclaimable_bytes":10, "size":"10 B", "outcome":"failed", "dry_run":false,
      "error":"recommendation not found in the current scan", "reported":"",
      "journal_warning":""
    }]}
    """#
    let result = CommandResult(stdout: Data(json.utf8), stderr: "", terminationStatus: 1)

    let report = try CleanupBackend.toolApplyReport(from: result, requestedID: "persisted-id")

    #expect(report.results.count == 1)
    #expect(report.results[0].succeeded == false)
    #expect(report.results[0].error == "recommendation not found in the current scan")
}

@Test func toolApplyRejectsAmbiguousOrExitInconsistentResults() {
    let invoked = #"{"id":"persisted-id","label":"Docker build cache","path":"docker:build-cache","reclaimable_bytes":10,"size":"10 B","outcome":"invoked","dry_run":false,"error":"","reported":"10 B","journal_warning":""}"#
    let failed = #"{"id":"persisted-id","label":"Docker build cache","path":"docker:build-cache","reclaimable_bytes":10,"size":"10 B","outcome":"failed","dry_run":false,"error":"failed","reported":"","journal_warning":""}"#
    let duplicate = CommandResult(
        stdout: Data("{\"results\":[\(invoked),\(failed)]}".utf8),
        stderr: "",
        terminationStatus: 1
    )
    let inconsistent = CommandResult(
        stdout: Data("{\"results\":[\(invoked)]}".utf8),
        stderr: "",
        terminationStatus: 1
    )

    #expect(throws: BackendError.self) {
        try CleanupBackend.toolApplyReport(from: duplicate, requestedID: "persisted-id")
    }
    #expect(throws: BackendError.self) {
        try CleanupBackend.toolApplyReport(from: inconsistent, requestedID: "persisted-id")
    }
}

@MainActor
@Test func appModelLoadsToolStatusesWithoutSelectingRecommendations() async {
    let state = StubToolBackendState(
        reports: [toolReport(recommendations: [toolRecommendation()])],
        applyReport: invokedToolReport()
    )
    let model = AppModel(
        backend: ToolsEmptyCleanupBackend(),
        toolBackend: StubToolBackend(state: state)
    )

    await model.loadTools()

    #expect(model.toolReport?.statuses.count == 2)
    #expect(model.toolReport?.statuses.last?.available == false)
    #expect(model.toolReport?.recommendations.map(\.id) == ["persisted-id"])
    #expect(model.activity == .idle)
}

@MainActor
@Test func aCombinedInjectedBackendProvidesToolInventoryAutomatically() async {
    let state = StubToolBackendState(
        reports: [toolReport(recommendations: [])],
        applyReport: invokedToolReport()
    )
    let model = AppModel(backend: CombinedToolCleanupBackend(state: state))

    await model.loadTools()

    #expect(model.toolReport?.statuses.count == 2)
    #expect(await state.loadCount == 1)
}

@MainActor
@Test func appModelAppliesOneCurrentToolIdThenRefreshesTheGeneration() async {
    let state = StubToolBackendState(
        reports: [
            toolReport(recommendations: [toolRecommendation()]),
            toolReport(recommendations: []),
        ],
        applyReport: invokedToolReport()
    )
    let model = AppModel(
        backend: ToolsEmptyCleanupBackend(),
        toolBackend: StubToolBackend(state: state)
    )

    await model.loadTools()
    await model.applyTool(id: "persisted-id")

    #expect(await state.appliedIds == ["persisted-id"])
    #expect(await state.loadCount == 2)
    #expect(model.toolReport?.recommendations.isEmpty == true)
    #expect(model.noticeMessage?.contains("Docker build cache") == true)
    #expect(model.activity == .idle)
}

@MainActor
@Test func appModelRefusesAnIdThatIsNotInTheCurrentToolReport() async {
    let state = StubToolBackendState(
        reports: [toolReport(recommendations: [toolRecommendation()])],
        applyReport: invokedToolReport()
    )
    let model = AppModel(
        backend: ToolsEmptyCleanupBackend(),
        toolBackend: StubToolBackend(state: state)
    )

    await model.loadTools()
    await model.applyTool(id: "not-displayed")

    #expect(await state.appliedIds.isEmpty)
    #expect(await state.loadCount == 1)
}

@MainActor
@Test func appModelRefreshesAfterAMismatchedPostInvocationResult() async {
    let mismatched = ToolApplyReport(
        results: [
            ToolApplyResult(
                id: "unexpected-id",
                label: "Docker build cache",
                path: "docker:build-cache",
                reclaimableBytes: 10,
                size: "10 B",
                outcome: "invoked",
                dryRun: false,
                error: "",
                reported: "10 B",
                journalWarning: ""
            ),
        ],
        warning: nil
    )
    let state = StubToolBackendState(
        reports: [
            toolReport(recommendations: [toolRecommendation()]),
            toolReport(recommendations: []),
        ],
        applyReport: mismatched
    )
    let model = AppModel(
        backend: ToolsEmptyCleanupBackend(),
        toolBackend: StubToolBackend(state: state)
    )

    await model.loadTools()
    await model.applyTool(id: "persisted-id")

    #expect(await state.appliedIds == ["persisted-id"])
    #expect(await state.loadCount == 2)
    #expect(model.toolReport?.recommendations.isEmpty == true)
    #expect(model.errorMessage?.contains("mismatched result") == true)
}

@MainActor
@Test func failedToolRefreshInvalidatesTheOldActionGeneration() async {
    let state = StubToolBackendState(
        reports: [
            toolReport(recommendations: [toolRecommendation()]),
            toolReport(recommendations: []),
        ],
        applyReport: invokedToolReport()
    )
    let model = AppModel(
        backend: ToolsEmptyCleanupBackend(),
        toolBackend: StubToolBackend(state: state)
    )

    await model.loadTools()
    await state.failNextLoad()
    await model.loadTools()
    await model.applyTool(id: "persisted-id")

    #expect(model.toolReport == nil)
    #expect(await state.appliedIds.isEmpty)
    #expect(await state.loadCount == 2)
}

@MainActor
@Test func indeterminateToolApplyInvalidatesAndRefreshesTheGeneration() async {
    let state = StubToolBackendState(
        reports: [
            toolReport(recommendations: [toolRecommendation()]),
            toolReport(recommendations: []),
        ],
        applyReport: invokedToolReport()
    )
    let model = AppModel(
        backend: ToolsEmptyCleanupBackend(),
        toolBackend: StubToolBackend(state: state)
    )

    await model.loadTools()
    await state.failNextApply()
    await model.applyTool(id: "persisted-id")

    #expect(await state.appliedIds == ["persisted-id"])
    #expect(await state.loadCount == 2)
    #expect(model.toolReport?.recommendations.isEmpty == true)
    #expect(model.errorMessage?.contains("indeterminate tool apply") == true)
}

@MainActor
@Test func failedToolApplyKeepsAuditAndIndexWarningsWithALabelFallback() async {
    let failed = ToolApplyReport(
        results: [
            ToolApplyResult(
                id: "persisted-id",
                label: "",
                path: "docker:build-cache",
                reclaimableBytes: 10,
                size: "10 B",
                outcome: "failed",
                dryRun: false,
                error: "owner tool refused",
                reported: "",
                journalWarning: "audit journal warning"
            ),
        ],
        warning: "recommendation index warning"
    )
    let state = StubToolBackendState(
        reports: [
            toolReport(recommendations: [toolRecommendation()]),
            toolReport(recommendations: []),
        ],
        applyReport: failed
    )
    let model = AppModel(
        backend: ToolsEmptyCleanupBackend(),
        toolBackend: StubToolBackend(state: state)
    )

    await model.loadTools()
    await model.applyTool(id: "persisted-id")

    #expect(model.warningMessage?.contains("Docker build cache") == true)
    #expect(model.warningMessage?.contains("owner tool refused") == true)
    #expect(model.warningMessage?.contains("audit journal warning") == true)
    #expect(model.warningMessage?.contains("recommendation index warning") == true)
}

@MainActor
@Test func applyAndRefreshFailuresPreserveBothErrorsWithoutAStaleReport() async {
    let state = StubToolBackendState(
        reports: [toolReport(recommendations: [toolRecommendation()])],
        applyReport: invokedToolReport()
    )
    let model = AppModel(
        backend: ToolsEmptyCleanupBackend(),
        toolBackend: StubToolBackend(state: state)
    )

    await model.loadTools()
    await state.failNextApply()
    await state.failNextLoad()
    await model.applyTool(id: "persisted-id")

    #expect(model.toolReport == nil)
    #expect(model.errorMessage?.contains("indeterminate tool apply") == true)
    #expect(model.errorMessage?.contains("tool refresh failed") == true)
}

@MainActor
@Test func indeterminatePathCleanupInvalidatesAndRefreshesTheOldSelection() async {
    let state = IndeterminateCleanupState()
    let model = AppModel(backend: IndeterminateCleanupBackend(state: state))

    await model.scan()
    model.selectedFlags = ["--browser-caches"]
    await model.cleanSelected()
    await model.cleanSelected()

    #expect(await state.cleanCount == 1)
    #expect(await state.scanCount == 2)
    #expect(model.report?.items.isEmpty == true)
    #expect(model.selectedFlags.isEmpty)
    #expect(model.errorMessage?.contains("timed out") == true)
}

@MainActor
@Test func cleanupAndRescanFailuresPreserveBothErrorsWithoutAStaleReport() async {
    let state = IndeterminateCleanupState()
    let model = AppModel(backend: IndeterminateCleanupBackend(state: state))

    await model.scan()
    await state.failNextScan()
    model.selectedFlags = ["--browser-caches"]
    await model.cleanSelected()

    #expect(model.report == nil)
    #expect(model.errorMessage?.contains("timed out") == true)
    #expect(model.errorMessage?.contains("storage scan refresh failed") == true)
}

private func toolRecommendation() -> ToolRecommendation {
    ToolRecommendation(
        id: "persisted-id",
        detectorId: "docker-build-cache",
        category: "tool-managed",
        label: "Docker build cache",
        size: "10 B",
        reclaimableBytes: 10,
        reason: "Docker reports reclaimable build cache.",
        warning: "",
        selectedByDefault: false,
        toolAction: ToolActionPayload(
            tool: "docker",
            resource: "build-cache",
            argv: ["docker", "builder", "prune", "-f"],
            previewArgv: ["docker", "system", "df"],
            reported: "10 B"
        )
    )
}

private func toolReport(recommendations: [ToolRecommendation]) -> ToolReport {
    ToolReport(
        statuses: [
            ToolStatus(tool: "docker", available: true, reason: ""),
            ToolStatus(tool: "homebrew", available: false, reason: "not installed"),
        ],
        recommendations: recommendations,
        reclaimableTotalBytes: recommendations.reduce(0) { $0 + $1.reclaimableBytes }
    )
}

private func invokedToolReport() -> ToolApplyReport {
    ToolApplyReport(
        results: [
            ToolApplyResult(
                id: "persisted-id",
                label: "Docker build cache",
                path: "docker:build-cache",
                reclaimableBytes: 10,
                size: "10 B",
                outcome: "invoked",
                dryRun: false,
                error: "",
                reported: "Total reclaimed space: 10 B",
                journalWarning: ""
            ),
        ],
        warning: nil
    )
}

private actor IndeterminateCleanupState {
    private(set) var scanCount = 0
    private(set) var cleanCount = 0
    private var shouldFailNextScan = false

    func failNextScan() {
        shouldFailNextScan = true
    }

    func scan() throws -> ScanReport {
        scanCount += 1
        if shouldFailNextScan {
            shouldFailNextScan = false
            throw BackendError.invalidOutput("storage scan refresh failed")
        }
        let itemBytes: Int64 = 200 * 1_024 * 1_024
        let items: [ScanItem] = scanCount == 1
            ? [
                ScanItem(
                    category: "browser-cache",
                    label: "Browser caches",
                    path: "/tmp/browser-cache",
                    sizeBytes: itemBytes,
                    size: "200 MB",
                    modifiedAt: nil,
                    cleanable: true,
                    deleteMode: "trash",
                    note: ""
                ),
            ]
            : []
        return ScanReport(
            totalBytes: items.isEmpty ? 0 : itemBytes,
            total: items.isEmpty ? "0 B" : "200 MB",
            cleanableTotalBytes: items.isEmpty ? 0 : itemBytes,
            cleanableTotal: items.isEmpty ? "0 B" : "200 MB",
            reportOnlyTotalBytes: 0,
            reportOnlyTotal: "0 B",
            count: items.count,
            items: items
        )
    }

    func clean() throws -> CleanReport {
        cleanCount += 1
        throw BackendError.commandTimedOut
    }
}

private struct IndeterminateCleanupBackend: CleanupBackendProtocol {
    let state: IndeterminateCleanupState

    func scan() async throws -> ScanReport {
        try await state.scan()
    }

    func clean(flags: [String]) async throws -> CleanReport {
        try await state.clean()
    }
}

private actor StubToolBackendState {
    private var reports: [ToolReport]
    let applyReport: ToolApplyReport
    private var shouldFailNextLoad = false
    private var shouldFailNextApply = false
    private(set) var appliedIds: [String] = []
    private(set) var loadCount = 0

    init(reports: [ToolReport], applyReport: ToolApplyReport) {
        self.reports = reports
        self.applyReport = applyReport
    }

    func failNextLoad() {
        shouldFailNextLoad = true
    }

    func failNextApply() {
        shouldFailNextApply = true
    }

    func load() throws -> ToolReport {
        loadCount += 1
        if shouldFailNextLoad {
            shouldFailNextLoad = false
            throw ToolBackendStubError.load
        }
        return reports.removeFirst()
    }

    func apply(id: String) throws -> ToolApplyReport {
        appliedIds.append(id)
        if shouldFailNextApply {
            shouldFailNextApply = false
            throw ToolBackendStubError.apply
        }
        return applyReport
    }
}

private struct StubToolBackend: ToolBackendProtocol {
    let state: StubToolBackendState

    func loadTools() async throws -> ToolReport {
        try await state.load()
    }

    func applyTool(id: String) async throws -> ToolApplyReport {
        try await state.apply(id: id)
    }
}

private enum ToolBackendStubError: LocalizedError {
    case load
    case apply

    var errorDescription: String? {
        switch self {
        case .load:
            "tool refresh failed"
        case .apply:
            "indeterminate tool apply"
        }
    }
}

private func fakeBackend(
    in directory: URL,
    scriptBody: String,
    commandTimeout: Duration
) throws -> CleanupBackend {
    let fileManager = FileManager.default
    try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
    let executableURL = directory.appendingPathComponent("fake-python")
    let script = "#!/bin/sh\n\(scriptBody)\n"
    try Data(script.utf8).write(to: executableURL)
    try fileManager.setAttributes([.posixPermissions: 0o755], ofItemAtPath: executableURL.path)
    return try CleanupBackend(
        location: BackendLocation(
            pythonURL: executableURL,
            pythonPath: directory,
            workingDirectory: directory
        ),
        commandTimeout: commandTimeout
    )
}

private struct ToolsEmptyCleanupBackend: CleanupBackendProtocol {
    func scan() async throws -> ScanReport {
        ScanReport(
            totalBytes: 0,
            total: "0 B",
            cleanableTotalBytes: 0,
            cleanableTotal: "0 B",
            reportOnlyTotalBytes: 0,
            reportOnlyTotal: "0 B",
            count: 0,
            items: []
        )
    }

    func clean(flags: [String]) async throws -> CleanReport {
        CleanReport(totalBytes: 0, total: "0 B", count: 0, items: [])
    }
}

private struct CombinedToolCleanupBackend: CleanupBackendProtocol, ToolBackendProtocol {
    let state: StubToolBackendState

    func scan() async throws -> ScanReport {
        ScanReport(
            totalBytes: 0,
            total: "0 B",
            cleanableTotalBytes: 0,
            cleanableTotal: "0 B",
            reportOnlyTotalBytes: 0,
            reportOnlyTotal: "0 B",
            count: 0,
            items: []
        )
    }

    func clean(flags: [String]) async throws -> CleanReport {
        CleanReport(totalBytes: 0, total: "0 B", count: 0, items: [])
    }

    func loadTools() async throws -> ToolReport {
        try await state.load()
    }

    func applyTool(id: String) async throws -> ToolApplyReport {
        try await state.apply(id: id)
    }
}
