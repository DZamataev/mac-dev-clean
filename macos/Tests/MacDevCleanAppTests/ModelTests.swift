import Foundation
import Testing
@testable import MacDevCleanApp

@Test func scanReportDecodesPythonJSON() throws {
    let json = #"""
    {
      "total_bytes": 1073741824,
      "total": "1.1 GB",
      "cleanable_total_bytes": 1073741824,
      "cleanable_total": "1.1 GB",
      "report_only_total_bytes": 0,
      "report_only_total": "0 B",
      "count": 1,
      "items": [{
        "category": "browser-cache",
        "label": "Chrome model",
        "path": "/Users/test/Library/Caches/Chrome",
        "size_bytes": 1073741824,
        "size": "1.1 GB",
        "modified_at": "2026-07-11T12:00:00+00:00",
        "cleanable": true,
        "delete_mode": "contents",
        "note": "Downloaded model"
      }]
    }
    """#

    let report = try JSONDecoder().decode(ScanReport.self, from: Data(json.utf8))

    #expect(report.cleanableTotal == "1.1 GB")
    #expect(report.items.first?.category == "browser-cache")
    #expect(report.items.first?.cleanable == true)
}

@Test func cleanupGroupsCombineCategoriesThatShareAFlag() {
    let editor = fixture(category: "editor-cache", size: 50 * 1024 * 1024)
    let updater = fixture(category: "updater-cache", size: 60 * 1024 * 1024)

    let groups = CleanupGroup.make(from: [editor, updater])

    #expect(groups.count == 1)
    #expect(groups[0].rule.flag == "--editor-caches")
    #expect(groups[0].totalBytes == 110 * 1024 * 1024)
    #expect(groups[0].items.first?.category == "updater-cache")
}

@Test func cleanupGroupsBelowOneHundredMegabytesAreNotOffered() {
    let item = fixture(
        category: "python-cache",
        size: CleanupGroup.minimumOfferedBytes - 1
    )

    #expect(CleanupGroup.make(from: [item]).isEmpty)
}

@Test func cleanupGroupsAtOneHundredMegabytesAreOffered() {
    let item = fixture(
        category: "python-cache",
        size: CleanupGroup.minimumOfferedBytes
    )

    #expect(CleanupGroup.make(from: [item]).count == 1)
}

@Test func reportOnlyItemsNeverBecomeCleanupGroups() {
    let item = ScanItem(
        category: "xcode-archives",
        label: "Xcode Archives",
        path: "/Users/test/Archives",
        sizeBytes: 1000,
        size: "1000 B",
        modifiedAt: nil,
        cleanable: false,
        deleteMode: "none",
        note: "Keep"
    )

    #expect(CleanupGroup.make(from: [item]).isEmpty)
}

@Test func xctestCloneSizeIsPresentedAsShared() {
    let item = fixture(category: "xcode-test-devices", size: 22 * 1024 * 1024)
    let group = CleanupGroup.make(from: [item])[0]

    #expect(item.displaySize == "Shared / unknown")
    #expect(group.displaySize == "Shared / unknown")
    #expect(group.totalBytes == 0)
}

@Test func byteFormattingMatchesThePythonCLI() {
    #expect(ByteFormatter.string(699 * 1024 * 1024) == "733.0 MB")
    #expect(ByteFormatter.string(1024 * 1024 * 1024) == "1.1 GB")
    #expect(ByteFormatter.string(-1_000) == "-1.0 KB")
    #expect(ByteFormatter.string(-1_000_000) == "-1.0 MB")
}

@Test func diskSpaceFormatsFreeAndTotalCapacity() {
    let diskSpace = DiskSpace(
        freeBytes: 250 * 1024 * 1024 * 1024,
        totalBytes: 1_000 * 1024 * 1024 * 1024
    )

    #expect(diskSpace.free == "268.4 GB")
    #expect(diskSpace.total == "1.1 TB")
}

@Test func ravenVectorWebsiteUsesSecureCanonicalURL() {
    #expect(AppMetadata.ravenVectorWebsite.scheme == "https")
    #expect(AppMetadata.ravenVectorWebsite.host == "ravenvector.com")
}

@Test func backendDoesNotInheritPythonCodeInjectionSettings() {
    let environment = CleanupBackend.pythonEnvironment(
        base: [
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": "/tmp/untrusted-modules",
            "PYTHONINSPECT": "1",
            "PYTHONSTARTUP": "/tmp/startup.py",
            "PYTHONHOME": "/tmp/untrusted-runtime",
        ],
        pythonPath: URL(fileURLWithPath: "/Applications/mac-dev-clean.app/Contents/Resources/python")
    )

    #expect(environment["PATH"] == "/usr/bin:/bin")
    #expect(environment["PYTHONPATH"] == "/Applications/mac-dev-clean.app/Contents/Resources/python")
    #expect(environment["PYTHONNOUSERSITE"] == "1")
    #expect(environment["PYTHONDONTWRITEBYTECODE"] == "1")
    #expect(environment["PYTHONINSPECT"] == nil)
    #expect(environment["PYTHONSTARTUP"] == nil)
    #expect(environment["PYTHONHOME"] == nil)
}

@Test func backendDecodesCleanupDetailsFromPartialFailureExit() throws {
    let json = #"""
    {
      "total_bytes": 104857600,
      "total": "104.9 MB",
      "count": 2,
      "items": [
        {
          "category": "browser-cache",
          "label": "Google browser caches",
          "path": "/Users/test/Library/Caches/Google",
          "size_bytes": 104857600,
          "size": "104.9 MB",
          "removed": true,
          "error": ""
        },
        {
          "category": "npm-cache",
          "label": "npm download cache",
          "path": "/Users/test/.npm/_cacache",
          "size_bytes": 209715200,
          "size": "209.7 MB",
          "removed": false,
          "error": "Operation not permitted"
        }
      ]
    }
    """#
    let result = CommandResult(
        stdout: Data(json.utf8),
        stderr: "Scanning developer cache locations. This can take a moment...",
        terminationStatus: 1
    )

    let report = try CleanupBackend.cleanReport(from: result)

    #expect(report.items.count == 2)
    #expect(report.items[1].error == "Operation not permitted")
}

@Test func backendShowsDiagnosticsWhenPartialFailureHasNoValidReport() {
    let result = CommandResult(
        stdout: Data(),
        stderr: "Permission denied while reading the selected cache.",
        terminationStatus: 1
    )

    do {
        _ = try CleanupBackend.cleanReport(from: result)
        Issue.record("Expected malformed cleanup output to throw")
    } catch {
        #expect(error.localizedDescription.contains("Permission denied"))
        #expect(error.localizedDescription.contains("No additional files will be removed"))
    }
}

@Test @MainActor func partialCleanupWarningSurvivesRefreshAndCanBeDismissed() async {
    let scanItem = fixture(category: "browser-cache", size: 200 * 1024 * 1024)
    let scanReport = ScanReport(
        totalBytes: scanItem.sizeBytes,
        total: scanItem.size,
        cleanableTotalBytes: scanItem.sizeBytes,
        cleanableTotal: scanItem.size,
        reportOnlyTotalBytes: 0,
        reportOnlyTotal: "0 B",
        count: 1,
        items: [scanItem]
    )
    let cleanReport = CleanReport(
        totalBytes: 0,
        total: "0 B",
        count: 1,
        items: [
            CleanResultItem(
                category: "browser-cache",
                label: "Google browser caches",
                path: "/Users/test/Library/Caches/Google",
                sizeBytes: scanItem.sizeBytes,
                size: scanItem.size,
                removed: false,
                error: "The browser is still using this cache"
            ),
        ]
    )
    let model = AppModel(
        backend: StubBackend(scanReport: scanReport, cleanReport: cleanReport)
    )

    await model.scan()
    model.selectedFlags = ["--browser-caches"]
    await model.cleanSelected()

    #expect(model.errorMessage == nil)
    #expect(model.warningMessage?.contains("1 item was skipped") == true)
    #expect(model.warningMessage?.contains("The browser is still using this cache") == true)
    #expect(model.warningMessage?.contains("/Users/test/Library/Caches/Google") == true)

    model.dismissMessage()

    #expect(model.errorMessage == nil)
    #expect(model.warningMessage == nil)
    #expect(model.noticeMessage == nil)

    model.errorMessage = "Old error"
    model.warningMessage = "Old warning"
    model.noticeMessage = "Old notice"
    await model.scan()

    #expect(model.errorMessage == nil)
    #expect(model.warningMessage == nil)
    #expect(model.noticeMessage == nil)
}

/// Leaving the Cleanup tab cancels the SwiftUI `.task(id:)` that started the
/// initial scan. That cancellation must not reach the user as a raw
/// `Swift.CancellationError`.
@Test @MainActor func leavingTheCleanupTabDuringTheInitialScanNeverSurfacesACancellationError() async {
    let gate = ScanGate()
    let model = AppModel(backend: GatedScanBackend(gate: gate))

    let viewTask = Task { await model.scanIfNeeded() }
    while await !gate.started { await Task.yield() }

    // The user switches to Deep Scan / Tool-managed: SwiftUI cancels the task.
    viewTask.cancel()
    await gate.release()
    await viewTask.value
    while model.isBusy { await Task.yield() }

    #expect(model.errorMessage == nil)
    #expect(model.report != nil)
    #expect(model.activity == .idle)
}

/// The manual-scan tabs disable their start button while another scan owns the
/// backend, so they must say why instead of leaving a dead control.
@Test @MainActor func manualScanTabsExplainWhyTheirStartButtonIsDisabled() async {
    let gate = ScanGate()
    let model = AppModel(backend: GatedScanBackend(gate: gate))

    #expect(model.manualScanBlockedReason == nil)

    let scanTask = Task { await model.scan() }
    while await !gate.started { await Task.yield() }

    #expect(model.manualScanBlockedReason?.contains("initial scan") == true)

    await gate.release()
    await scanTask.value
    while model.isBusy { await Task.yield() }

    #expect(model.manualScanBlockedReason == nil)
}

private actor ScanGate {
    private(set) var started = false
    private var continuation: CheckedContinuation<Void, Never>?

    func markStarted() { started = true }

    func waitForRelease() async {
        await withCheckedContinuation { continuation = $0 }
    }

    func release() {
        continuation?.resume()
        continuation = nil
    }
}

private struct GatedScanBackend: CleanupBackendProtocol {
    let gate: ScanGate

    func scan() async throws -> ScanReport {
        await gate.markStarted()
        await gate.waitForRelease()
        try Task.checkCancellation()
        let item = fixture(category: "browser-cache", size: 200 * 1024 * 1024)
        return ScanReport(
            totalBytes: item.sizeBytes,
            total: item.size,
            cleanableTotalBytes: item.sizeBytes,
            cleanableTotal: item.size,
            reportOnlyTotalBytes: 0,
            reportOnlyTotal: "0 B",
            count: 1,
            items: [item]
        )
    }

    func clean(flags: [String]) async throws -> CleanReport {
        CleanReport(totalBytes: 0, total: "0 B", count: 0, items: [])
    }
}

private func fixture(category: String, size: Int64) -> ScanItem {
    ScanItem(
        category: category,
        label: category,
        path: "/Users/test/\(category)",
        sizeBytes: size,
        size: ByteFormatter.string(size),
        modifiedAt: nil,
        cleanable: true,
        deleteMode: "contents",
        note: ""
    )
}

private struct StubBackend: CleanupBackendProtocol {
    let scanReport: ScanReport
    let cleanReport: CleanReport

    func scan() async throws -> ScanReport {
        scanReport
    }

    func clean(flags: [String]) async throws -> CleanReport {
        cleanReport
    }
}
