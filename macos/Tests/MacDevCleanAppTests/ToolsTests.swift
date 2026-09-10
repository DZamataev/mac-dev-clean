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

@Test func toolManagedPageAndCommandPreviewAreExplicit() {
    #expect(SidebarPage.tools.rawValue == "Tool-managed")
    #expect(SidebarPage.tools.symbol == "wrench.and.screwdriver")

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

    let report = try CleanupBackend.toolApplyReport(from: result)

    #expect(report.results.count == 1)
    #expect(report.results[0].succeeded == false)
    #expect(report.results[0].error == "recommendation not found in the current scan")
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

private actor StubToolBackendState {
    private var reports: [ToolReport]
    let applyReport: ToolApplyReport
    private(set) var appliedIds: [String] = []
    private(set) var loadCount = 0

    init(reports: [ToolReport], applyReport: ToolApplyReport) {
        self.reports = reports
        self.applyReport = applyReport
    }

    func load() -> ToolReport {
        loadCount += 1
        return reports.removeFirst()
    }

    func apply(id: String) -> ToolApplyReport {
        appliedIds.append(id)
        return applyReport
    }
}

private struct StubToolBackend: ToolBackendProtocol {
    let state: StubToolBackendState

    func loadTools() async throws -> ToolReport {
        await state.load()
    }

    func applyTool(id: String) async throws -> ToolApplyReport {
        await state.apply(id: id)
    }
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
