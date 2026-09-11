import Darwin
import Foundation
import Testing
@testable import MacDevCleanApp

@Test func tabScanLaunchPolicyKeepsDeepAndToolsManualOnly() {
    #expect(SidebarPage.cleanup.startsScanOnFocus)
    #expect(!SidebarPage.deepScan.startsScanOnFocus)
    #expect(!SidebarPage.tools.startsScanOnFocus)
    #expect(!SidebarPage.review.startsScanOnFocus)
    #expect(!SidebarPage.about.startsScanOnFocus)
}

@Test func nilSidebarSelectionUsesCleanupLifecyclePolicy() {
    let page = SidebarPage.resolved(nil)

    #expect(page == .cleanup)
    #expect(page.startsScanOnFocus)
    #expect(page.showsScanActivityIndicator(for: .scanning))
    #expect(!page.showsScanActivityIndicator(for: .cleaning))
}

@Test func sidebarActivityIndicatorIsScopedToTheSelectedTabsScan() {
    let expected: [SidebarPage: AppModel.Activity?] = [
        .cleanup: .scanning,
        .deepScan: .deepScanning,
        .tools: .loadingTools,
        .review: .scanning,
        .about: nil,
    ]

    for page in SidebarPage.allCases {
        for activity in AppModel.Activity.allCases {
            #expect(
                page.showsScanActivityIndicator(for: activity) == (expected[page] == activity),
                "Unexpected sidebar indicator for \(page.rawValue) during \(activity)"
            )
        }
    }
}

@Test func primaryScanPlaceholderNeverMisattributesOtherWork() {
    for page in SidebarPage.allCases {
        for activity in AppModel.Activity.allCases {
            for hasReport in [false, true] {
                let expected = !hasReport
                    && (page == .cleanup || page == .review)
                    && activity == .scanning
                #expect(
                    page.showsPrimaryScanPlaceholder(for: activity, hasReport: hasReport) == expected,
                    "Unexpected primary placeholder for \(page.rawValue), \(activity), report=\(hasReport)"
                )
            }
        }
    }
}

@Test func toolScanActivityIndicatorOnlyTracksToolInventory() {
    #expect(AppModel.Activity.loadingTools.showsToolScanIndicator)
    #expect(!AppModel.Activity.applyingTool.showsToolScanIndicator)
    #expect(!AppModel.Activity.deepScanning.showsToolScanIndicator)
    #expect(!AppModel.Activity.scanning.showsToolScanIndicator)
    #expect(!AppModel.Activity.idle.showsToolScanIndicator)
}

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
    #expect(report.recommendations[0].toolUsage == nil)
}

@Test func toolReportDecodesNdkUsageAndFormatsMatchedSummary() throws {
    let recommendation = try decodeToolRecommendation(toolUsage: #"""
    {
      "state": "matched",
      "projects": ["/Users/test/app-a", "/Users/test/app-b"],
      "unpinned_projects": ["/Users/test/app-c"],
      "scan_completed_at": "2026-09-11T00:00:00+00:00"
    }
    """#)

    #expect(recommendation.toolUsage?.state == .matched)
    #expect(recommendation.toolUsage?.projects == ["/Users/test/app-a", "/Users/test/app-b"])
    #expect(recommendation.toolUsage?.unpinnedProjects == ["/Users/test/app-c"])
    #expect(recommendation.toolUsage?.scanCompletedAt == "2026-09-11T00:00:00+00:00")
    #expect(recommendation.usageSummary == "Used by 2 projects")
    #expect(
        recommendation.usageSections == [
            ToolUsageSection(
                title: "Matching version",
                paths: ["/Users/test/app-a", "/Users/test/app-b"]
            ),
            ToolUsageSection(
                title: "Uses an unpinned NDK",
                paths: ["/Users/test/app-c"]
            ),
        ]
    )
}

@Test func toolReportDecodesNdkUsageSummaryStatesAndSingularCount() throws {
    let unreferenced = try decodeToolRecommendation(toolUsage: #"""
    {"state":"unreferenced","projects":[],"unpinned_projects":[],"scan_completed_at":"2026-09-11T00:00:00+00:00"}
    """#)
    let unknown = try decodeToolRecommendation(toolUsage: #"""
    {"state":"unknown","projects":[],"unpinned_projects":[],"scan_completed_at":null}
    """#)
    let singular = try decodeToolRecommendation(toolUsage: #"""
    {"state":"matched","projects":["/Users/test/app-a"],"unpinned_projects":[],"scan_completed_at":"2026-09-11T00:00:00+00:00"}
    """#)

    #expect(unreferenced.usageSummary == "Not referenced by scanned projects")
    #expect(unknown.usageSummary == "Usage unknown — run Deep Scan")
    #expect(singular.usageSummary == "Used by 1 project")
}

@Test func toolReportDecodesNdkUsageMissingKeyAsNil() throws {
    let recommendation = try decodeToolRecommendation(toolUsage: nil)

    #expect(recommendation.toolUsage == nil)
    #expect(recommendation.usageSummary == nil)
    #expect(recommendation.usageSections.isEmpty)
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

@Test func cancellingToolInventoryTerminatesItsProcessGroupAndSettlesPromptly() async throws {
    let fileManager = FileManager.default
    let directory = fileManager.temporaryDirectory
        .appendingPathComponent("mac dev clean process group \(UUID().uuidString)", isDirectory: true)
    defer { try? fileManager.removeItem(at: directory) }
    let unrelated = Process()
    unrelated.executableURL = URL(fileURLWithPath: "/bin/sleep")
    unrelated.arguments = ["30"]
    try unrelated.run()
    defer {
        if unrelated.isRunning {
            unrelated.terminate()
            unrelated.waitUntilExit()
        }
    }
    let pidURL = directory.appendingPathComponent("children.pid")
    let helperURL = try escapingProcessHelper(in: directory)
    let backend = try fakeBackend(
        in: directory,
        scriptBody: """
        /usr/bin/python3 '\(helperURL.path)' '\(pidURL.path)'
        /bin/sleep 30
        """,
        commandTimeout: .seconds(10)
    )
    let task = Task { try await backend.loadTools() }
    let clock = ContinuousClock()
    let readyDeadline = clock.now.advanced(by: .seconds(3))
    while !fileManager.fileExists(atPath: pidURL.path), clock.now < readyDeadline {
        try await Task.sleep(for: .milliseconds(10))
    }
    let pidText = try String(contentsOf: pidURL, encoding: .utf8)
    let childPIDs = pidText.split(separator: " ").compactMap { pid_t($0) }
    #expect(childPIDs.count == 2)

    let cancelStart = clock.now
    task.cancel()
    do {
        _ = try await task.value
        Issue.record("Expected cancelled inventory to throw")
    } catch {
        #expect(error is CancellationError)
    }
    #expect(cancelStart.duration(to: clock.now) < .seconds(2))
    #expect(unrelated.isRunning)

    for pid in childPIDs {
        let isAlive = kill(pid, 0) == 0 || errno == EPERM
        #expect(!isAlive)
        if isAlive {
            _ = kill(pid, SIGKILL)
        }
    }
}

@Test func toolInventoryTimesOutAHungBackendProcess() async throws {
    let fileManager = FileManager.default
    let directory = fileManager.temporaryDirectory
        .appendingPathComponent("mac-dev-clean-timeout-\(UUID().uuidString)", isDirectory: true)
    defer { try? fileManager.removeItem(at: directory) }
    let pidURL = directory.appendingPathComponent("children.pid")
    let helperURL = try escapingProcessHelper(in: directory)
    let backend = try fakeBackend(
        in: directory,
        scriptBody: """
        /usr/bin/python3 '\(helperURL.path)' '\(pidURL.path)'
        /bin/sleep 30
        """,
        commandTimeout: .seconds(3)
    )

    let clock = ContinuousClock()
    let start = clock.now
    let inventoryTask = Task { try await backend.loadTools() }
    let readyDeadline = clock.now.advanced(by: .seconds(2))
    while !fileManager.fileExists(atPath: pidURL.path), clock.now < readyDeadline {
        try await Task.sleep(for: .milliseconds(20))
    }
    #expect(fileManager.fileExists(atPath: pidURL.path))
    do {
        _ = try await inventoryTask.value
        Issue.record("Expected timed-out inventory to throw")
    } catch {
        #expect(error.localizedDescription.contains("timed out"))
    }
    #expect(start.duration(to: clock.now) < .seconds(5))
    let pidText = try String(contentsOf: pidURL, encoding: .utf8)
    for pid in pidText.split(separator: " ").compactMap({ pid_t($0) }) {
        let isAlive = kill(pid, 0) == 0 || errno == EPERM
        #expect(!isAlive)
        if isAlive {
            _ = kill(pid, SIGKILL)
        }
    }
}

@Test func completedToolInventoryReapsReparentedSentinelHolders() async throws {
    let fileManager = FileManager.default
    let directory = fileManager.temporaryDirectory
        .appendingPathComponent("mac-dev-clean-parent-exit-\(UUID().uuidString)", isDirectory: true)
    defer { try? fileManager.removeItem(at: directory) }
    let pidURL = directory.appendingPathComponent("children.pid")
    let json = #"{"reclaimable_total_bytes":0,"statuses":[],"recommendations":[]}"#
    let helperURL = try escapingProcessHelper(in: directory)
    let backend = try fakeBackend(
        in: directory,
        scriptBody: """
        sentinel_path=$(/usr/bin/python3 -c 'import fcntl; print(fcntl.fcntl(0, 50, bytes(1024)).split(b"\\0", 1)[0].decode())')
        /bin/rm -f "$sentinel_path"
        /usr/bin/python3 '\(helperURL.path)' '\(pidURL.path)'
        /bin/echo '\(json)'
        """,
        commandTimeout: .seconds(10)
    )

    let clock = ContinuousClock()
    let start = clock.now
    let report = try await backend.loadTools()
    #expect(report.recommendations.isEmpty)
    #expect(start.duration(to: clock.now) < .seconds(5))
    let pidText = try String(contentsOf: pidURL, encoding: .utf8)
    for pid in pidText.split(separator: " ").compactMap({ pid_t($0) }) {
        let isAlive = kill(pid, 0) == 0 || errno == EPERM
        #expect(!isAlive)
        if isAlive {
            _ = kill(pid, SIGKILL)
        }
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
        commandTimeout: .seconds(3)
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
@Test func toolScanActionStartsAsManualScanThenBecomesRefresh() async {
    let state = StubToolBackendState(
        reports: [toolReport(recommendations: [])],
        applyReport: invokedToolReport()
    )
    let model = AppModel(
        backend: ToolsEmptyCleanupBackend(),
        toolBackend: StubToolBackend(state: state)
    )

    #expect(model.toolScanButtonTitle == "Scan Tools")
    #expect(await state.loadCount == 0)

    await model.loadTools()

    #expect(model.toolScanButtonTitle == "Refresh Tools")
    #expect(await state.loadCount == 1)
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
@Test func appModelAppliesOneCurrentToolIdWithoutRefreshingAllTools() async {
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
    #expect(await state.loadCount == 1)
    #expect(model.toolReport?.recommendations.isEmpty == true)
    #expect(model.noticeMessage?.contains("Docker build cache") == true)
    #expect(model.activity == .idle)
}

@MainActor
@Test func authoritativeToolRefusalDoesNotRefreshInventory() async {
    let failed = ToolApplyReport(
        results: [
            ToolApplyResult(
                id: "persisted-id",
                label: "Docker build cache",
                path: "docker:build-cache",
                reclaimableBytes: 10,
                size: "10 B",
                outcome: "failed",
                dryRun: false,
                error: "owner tool refused",
                reported: "",
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
        applyReport: failed
    )
    let model = AppModel(
        backend: ToolsEmptyCleanupBackend(),
        toolBackend: StubToolBackend(state: state)
    )

    await model.loadTools()
    await model.applyTool(id: "persisted-id")

    #expect(await state.appliedIds == ["persisted-id"])
    #expect(await state.loadCount == 1)
    #expect(model.toolReport?.recommendations.map(\.id) == ["persisted-id"])
    #expect(model.warningMessage?.contains("owner tool refused") == true)
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
@Test func failedToolApplyUsesAGenericLabelWhenBothLabelsAreBlank() async {
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
                error: "refused",
                reported: "",
                journalWarning: ""
            ),
        ],
        warning: nil
    )
    let state = StubToolBackendState(
        reports: [
            toolReport(recommendations: [toolRecommendation(label: "   ")]),
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

    #expect(model.warningMessage == "Tool action was not changed. refused")
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

private func decodeToolRecommendation(toolUsage: String?) throws -> ToolRecommendation {
    let usageMember = toolUsage.map { ",\"tool_usage\":\($0)" } ?? ""
    let json = #"""
    {
      "reclaimable_total_bytes": 10,
      "statuses": [],
      "recommendations": [{
        "id": "ndk-27", "detector_id": "android-ndk", "category": "tool-managed",
        "label": "Android NDK 27.0.1", "reclaimable_bytes": 10, "size": "10 B",
        "reason": "Installed Android SDK package.", "warning": "",
        "selected_by_default": false,
        "tool_action": {
          "tool": "android", "resource": "ndk;27.0.1",
          "argv": ["sdkmanager", "--uninstall", "ndk;27.0.1"],
          "preview_argv": ["sdkmanager", "--list_installed"], "reported": "10 B"
        }\#(usageMember)
      }]
    }
    """#
    return try JSONDecoder().decode(ToolReport.self, from: Data(json.utf8)).recommendations[0]
}

private func toolRecommendation(label: String = "Docker build cache") -> ToolRecommendation {
    ToolRecommendation(
        id: "persisted-id",
        detectorId: "docker-build-cache",
        category: "tool-managed",
        label: label,
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
        ),
        toolUsage: nil
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

private func escapingProcessHelper(in directory: URL) throws -> URL {
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    let helperURL = directory.appendingPathComponent("double-fork-helper.py")
    let source = #"""
    import os
    import signal
    import sys
    import time

    pid_path = sys.argv[1]
    first = os.fork()
    if first:
        os.waitpid(first, 0)
        deadline = time.monotonic() + 2
        while not os.path.exists(pid_path) and time.monotonic() < deadline:
            time.sleep(0.01)
        os._exit(0 if os.path.exists(pid_path) else 2)

    os.setsid()
    second = os.fork()
    if second:
        os._exit(0)

    held = os.fork()
    if held == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        time.sleep(6)
        os._exit(0)

    detached = os.fork()
    if detached == 0:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)
        time.sleep(30)
        os._exit(0)

    with open(pid_path, "w", encoding="utf-8") as handle:
        handle.write(f"{held} {detached}")
    os._exit(0)
    """#
    try Data(source.utf8).write(to: helperURL)
    return helperURL
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
