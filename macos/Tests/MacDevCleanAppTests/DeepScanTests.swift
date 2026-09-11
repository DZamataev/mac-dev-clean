import Foundation
import Testing
@testable import MacDevCleanApp

@Test func deepScanActivityIndicatorOnlyTracksProjectAnalysis() {
    #expect(AppModel.Activity.deepScanning.showsDeepScanIndicator)
    #expect(!AppModel.Activity.applying.showsDeepScanIndicator)
    #expect(!AppModel.Activity.loadingTools.showsDeepScanIndicator)
    #expect(!AppModel.Activity.scanning.showsDeepScanIndicator)
    #expect(!AppModel.Activity.idle.showsDeepScanIndicator)
}

@Test func parserDecodesACandidateEvent() throws {
    let line = #"""
    {"protocol_version":1,"generation":3,"event":"candidate_found","recommendation":{"id":"abc123","detector_id":"node-modules","category":"project-dependencies","label":"node_modules","path":"/Users/test/app/node_modules","action":"delete_tree","allocated_bytes":2048,"reclaimable_bytes":2048,"size":"2.0 KB","confidence":"strong","restoration":"redownload","selected_by_default":true,"evidence":[{"code":"lock-file","detail":"pnpm-lock.yaml exists"}],"safety_root":"/Users/test/app","reason":"Project has not changed in 143 days.","warning":"Close Xcode","generation":3,"last_activity_at":null}}
    """#

    let event = try #require(DeepScanEventParser.parse(line: line))

    guard case let .candidateFound(item) = event else {
        Issue.record("expected candidateFound, got \(event)")
        return
    }
    #expect(item.id == "abc123")
    #expect(item.selectedByDefault)
    #expect(item.action == "delete_tree")
    #expect(item.evidence.first?.detail == "pnpm-lock.yaml exists")
}

@Test func parserIgnoresBlankAndUnknownLines() throws {
    #expect(DeepScanEventParser.parse(line: "") == nil)
    #expect(DeepScanEventParser.parse(line: "   ") == nil)
    #expect(DeepScanEventParser.parse(line: "not json") == nil)

    let future = #"{"protocol_version":1,"generation":1,"event":"future_event_type"}"#
    #expect(DeepScanEventParser.parse(line: future) == nil)
}

@Test func parserRejectsAnIncompatibleProtocolVersion() throws {
    let line = #"{"protocol_version":99,"generation":1,"event":"scan_started","roots":["/Users/test/home"],"incremental":false}"#

    #expect(DeepScanEventParser.parse(line: line) == nil)
}

@Test func parserDecodesTerminalEvents() throws {
    let completed = #"{"protocol_version":1,"generation":1,"event":"scan_completed","reclaimable_bytes":100,"count":2}"#
    let cancelled = #"{"protocol_version":1,"generation":1,"event":"scan_cancelled","reclaimable_bytes":0,"count":0}"#

    guard case let .completed(bytes, count) = try #require(DeepScanEventParser.parse(line: completed)) else {
        Issue.record("expected completed")
        return
    }
    #expect(bytes == 100)
    #expect(count == 2)

    guard case .cancelled = try #require(DeepScanEventParser.parse(line: cancelled)) else {
        Issue.record("expected cancelled")
        return
    }
}

@Test func stateAccumulatesCandidatesAndTotals() {
    var state = DeepScanState()
    state.apply(.started(roots: ["/Users/test/home"], incremental: false))
    state.apply(.candidateFound(item(id: "a", bytes: 100, selected: true)))
    state.apply(.candidateFound(item(id: "b", bytes: 50, selected: false)))
    state.apply(.progress(path: "/Users/test/app", scanned: 25))

    #expect(state.isRunning)
    #expect(state.items.count == 2)
    #expect(state.selectedIds == ["a"])
    #expect(state.selectedBytes == 100)
    #expect(state.currentPath == "/Users/test/app")

    state.apply(.completed(reclaimableBytes: 150, count: 2))

    #expect(!state.isRunning)
    #expect(!state.wasCancelled)
}

@Test func stateSortsBySizeDescending() {
    var state = DeepScanState()
    state.apply(.candidateFound(item(id: "small", bytes: 10, selected: true)))
    state.apply(.candidateFound(item(id: "large", bytes: 900, selected: true)))

    #expect(state.items.map(\.id) == ["large", "small"])
}

@Test func stateNeverPreselectsAReportOnlyItem() {
    var state = DeepScanState()
    state.apply(.candidateFound(item(id: "a", bytes: 100, selected: false)))

    #expect(state.selectedIds.isEmpty)
    #expect(state.selectedBytes == 0)
}

@Test func cancellationIsReflectedInState() {
    var state = DeepScanState()
    state.apply(.started(roots: ["/Users/test/home"], incremental: false))
    state.apply(.cancelled(reclaimableBytes: 0, count: 0))

    #expect(!state.isRunning)
    #expect(state.wasCancelled)
}

@Test func warningsAreCollectedWithoutStoppingTheScan() {
    var state = DeepScanState()
    state.apply(.started(roots: ["/Users/test/home"], incremental: false))
    state.apply(.warning(message: "Permission denied", path: "/Users/test/locked"))
    state.apply(.candidateFound(item(id: "a", bytes: 10, selected: true)))
    state.apply(.completed(reclaimableBytes: 10, count: 1))

    #expect(state.warnings.count == 1)
    #expect(state.items.count == 1)
}

/// Defect 3: a final NDJSON line with no trailing newline (e.g. the child's
/// last write before exit) must not be silently dropped.
@Test func accumulatorRecoversAFinalLineWithNoTrailingNewline() throws {
    var accumulator = NDJSONEventAccumulator()
    let completedLine = #"{"protocol_version":1,"generation":1,"event":"scan_completed","reclaimable_bytes":100,"count":2}"#

    // No trailing "\n" -- this simulates the child's write buffer flushing
    // its very last bytes without a newline before the process exits.
    let midStream = accumulator.ingest(Data(completedLine.utf8))
    #expect(midStream.isEmpty, "a line with no newline yet must stay buffered, not be emitted early")

    let finalEvent = accumulator.finish()
    guard case let .completed(bytes, count) = try #require(finalEvent) else {
        Issue.record("expected the buffered line to be recovered as .completed on finish()")
        return
    }
    #expect(bytes == 100)
    #expect(count == 2)

    // finish() only recovers once; a second call must not re-emit or crash.
    #expect(accumulator.finish() == nil)
}

@Test func accumulatorSurvivesAMultiByteCharacterSplitAcrossReads() {
    // `availableData` chunks at arbitrary byte offsets, so a non-ASCII path --
    // Cyrillic, CJK, an emoji in a folder name -- can be cut mid-character.
    // Splitting on the newline BYTE and decoding only whole lines is what makes
    // that safe; decoding each chunk as it arrived would corrupt the path.
    var accumulator = NDJSONEventAccumulator()
    let path = "/Users/test/Разработка/проект/node_modules"
    let line = #"{"protocol_version":1,"generation":1,"event":"warning","message":"m","path":"\#(path)"}"#
    var bytes = Array(Data("\(line)\n".utf8))

    // Cut inside the first Cyrillic character: one byte of it lands in chunk 1.
    let firstCyrillicByte = bytes.firstIndex { $0 >= 0xD0 } ?? 0
    let cut = firstCyrillicByte + 1
    let chunk1 = Data(bytes[0..<cut])
    let chunk2 = Data(bytes[cut...])

    #expect(accumulator.ingest(chunk1).isEmpty)
    let events = accumulator.ingest(chunk2)
    #expect(events.count == 1)
    guard case let .warning(_, decodedPath) = events[0] else {
        Issue.record("expected .warning once the split character was reassembled")
        return
    }
    #expect(decodedPath == path)
    bytes.removeAll()
}

@Test func accumulatorEmitsCompleteLinesAsTheyArriveAndKeepsPartialTailsBuffered() {
    var accumulator = NDJSONEventAccumulator()
    let started = #"{"protocol_version":1,"generation":1,"event":"scan_started","roots":["/Users/test/home"],"incremental":false}"#

    // One chunk carries a complete line plus a partial tail of the next line.
    let firstChunk = Data("\(started)\n{\"protocol_versi".utf8)
    let events1 = accumulator.ingest(firstChunk)
    #expect(events1.count == 1)
    guard case .started = events1[0] else {
        Issue.record("expected .started from the first complete line")
        return
    }

    let secondChunk = Data("on\":1,\"generation\":1,\"event\":\"scan_completed\",\"reclaimable_bytes\":5,\"count\":1}\n".utf8)
    let events2 = accumulator.ingest(secondChunk)
    #expect(events2.count == 1)
    guard case let .completed(bytes, count) = events2[0] else {
        Issue.record("expected .completed once the tail line's newline finally arrives")
        return
    }
    #expect(bytes == 5)
    #expect(count == 1)

    // Nothing left over: finish() after a fully newline-terminated stream is a no-op.
    #expect(accumulator.finish() == nil)
}

private func item(id: String, bytes: Int64, selected: Bool) -> RecommendationItem {
    RecommendationItem(
        id: id,
        detectorId: "node-modules",
        category: "project-dependencies",
        label: "node_modules",
        path: "/Users/test/\(id)/node_modules",
        action: "delete_tree",
        allocatedBytes: bytes,
        reclaimableBytes: bytes,
        size: ByteFormatter.string(bytes),
        confidence: "strong",
        restoration: "redownload",
        selectedByDefault: selected,
        evidence: [],
        safetyRoot: "/Users/test/\(id)",
        reason: "Project has not changed in 143 days.",
        warning: "",
        generation: 1,
        lastActivityAt: nil
    )
}

@MainActor
@Test func appModelStreamsDeepScanEventsIntoState() async {
    let events: [DeepScanEvent] = [
        .started(roots: ["/Users/test/home"], incremental: false),
        .candidateFound(item(id: "a", bytes: 500, selected: true)),
        .candidateFound(item(id: "b", bytes: 200, selected: false)),
        .completed(reclaimableBytes: 700, count: 2),
    ]
    let model = AppModel(
        backend: EmptyCleanupBackend(),
        deepScanBackend: StubDeepScanBackend(events: events, applyResults: [])
    )

    await model.startDeepScan()

    #expect(model.deepScanState.items.count == 2)
    #expect(model.deepScanState.selectedIds == ["a"])
    #expect(!model.deepScanState.isRunning)
    #expect(model.deepScanState.selectedBytes == 500)
}

@MainActor
@Test func appModelReportsPartialApplyFailures() async {
    let events: [DeepScanEvent] = [
        .started(roots: ["/Users/test/home"], incremental: false),
        .candidateFound(item(id: "a", bytes: 500, selected: true)),
        .candidateFound(item(id: "b", bytes: 200, selected: true)),
        .completed(reclaimableBytes: 700, count: 2),
    ]
    let results = [
        ApplyResultItem(
            id: "a", label: "node_modules", path: "/Users/test/a/node_modules",
            reclaimableBytes: 500, size: "500 B", outcome: "removed", dryRun: false, error: ""
        ),
        ApplyResultItem(
            id: "b", label: "node_modules", path: "/Users/test/b/node_modules",
            reclaimableBytes: 200, size: "200 B", outcome: "changed_since_scan",
            dryRun: false, error: "the project was edited recently"
        ),
    ]
    let model = AppModel(
        backend: EmptyCleanupBackend(),
        deepScanBackend: StubDeepScanBackend(events: events, applyResults: results)
    )

    await model.startDeepScan()
    await model.applyDeepScanSelection()

    #expect(model.errorMessage == nil)
    #expect(model.warningMessage?.contains("edited recently") == true)
    #expect(model.warningMessage?.contains("1 item was skipped") == true)
}

@MainActor
@Test func appModelNeverAppliesAnEmptySelection() async {
    let backend = StubDeepScanBackend(events: [], applyResults: [])
    let model = AppModel(backend: EmptyCleanupBackend(), deepScanBackend: backend)

    await model.applyDeepScanSelection()

    #expect(await backend.appliedIds.isEmpty)
}

private actor CancellationRecorder {
    private(set) var hasStarted = false
    private(set) var wasCancelledFlag = false

    func markStarted() { hasStarted = true }
    func markCancelled() { wasCancelledFlag = true }
}

/// A backend whose `deepScan` never finishes on its own: it suspends inside
/// `withTaskCancellationHandler` (mirroring the real `DeepScanBackend`) until
/// the surrounding task is cancelled. This is what lets the test prove
/// `cancelDeepScan()` actually reaches the in-flight scan instead of just
/// asserting on `DeepScanState` in isolation.
private struct HangingDeepScanBackend: DeepScanBackendProtocol {
    let recorder: CancellationRecorder

    func deepScan(onEvent: @Sendable @escaping (DeepScanEvent) -> Void) async throws {
        onEvent(.started(roots: ["/Users/test/home"], incremental: false))
        await recorder.markStarted()
        try await withTaskCancellationHandler {
            // Long enough that the test's timeout (well under this) always
            // wins the race if cancellation never arrives.
            try await Task.sleep(nanoseconds: 60_000_000_000)
        } onCancel: {
            Task { await recorder.markCancelled() }
        }
    }

    func apply(ids: [String], dryRun: Bool) async throws -> [ApplyResultItem] { [] }
}

/// Proves defect 1 is fixed: with the pre-fix `Task.detached` bridge in
/// `AppModel.startDeepScan`, cancelling `deepScanTask` never reaches
/// `HangingDeepScanBackend.deepScan`'s cancellation handler, so `scanTask`
/// never settles and this test times out (RED). After propagating
/// cancellation into the inner task, `deepScan` observes cancellation,
/// `onCancel` fires, and the scan settles well within the timeout (GREEN).
@MainActor
@Test func cancelDeepScanStopsTheRunningScan() async throws {
    let recorder = CancellationRecorder()
    let backend = HangingDeepScanBackend(recorder: recorder)
    let model = AppModel(backend: EmptyCleanupBackend(), deepScanBackend: backend)

    let scanTask = Task { await model.startDeepScan() }

    for _ in 0..<500 where await !recorder.hasStarted {
        try await Task.sleep(nanoseconds: 10_000_000)
    }
    #expect(await recorder.hasStarted, "the fake backend never started scanning")

    model.cancelDeepScan()

    let settled = await withTaskGroup(of: Bool.self) { group in
        group.addTask {
            await scanTask.value
            return true
        }
        group.addTask {
            try? await Task.sleep(nanoseconds: 3_000_000_000)
            return false
        }
        let first = await group.next() ?? false
        group.cancelAll()
        return first
    }

    #expect(settled, "cancelDeepScan() did not stop the running scan within the timeout")
    #expect(!model.isBusy)
    #expect(await recorder.wasCancelledFlag, "deepScan's withTaskCancellationHandler never observed cancellation")
}

private struct EmptyCleanupBackend: CleanupBackendProtocol {
    func scan() async throws -> ScanReport {
        ScanReport(
            totalBytes: 0, total: "0 B", cleanableTotalBytes: 0, cleanableTotal: "0 B",
            reportOnlyTotalBytes: 0, reportOnlyTotal: "0 B", count: 0, items: []
        )
    }

    func clean(flags: [String]) async throws -> CleanReport {
        CleanReport(totalBytes: 0, total: "0 B", count: 0, items: [])
    }
}

private actor RecordedIds {
    var value: [String] = []
    func set(_ ids: [String]) { value = ids }
}

private struct StubDeepScanBackend: DeepScanBackendProtocol {
    let events: [DeepScanEvent]
    let applyResults: [ApplyResultItem]
    private let recorded = RecordedIds()

    var appliedIds: [String] {
        get async { await recorded.value }
    }

    func deepScan(onEvent: @Sendable @escaping (DeepScanEvent) -> Void) async throws {
        for event in events { onEvent(event) }
    }

    func apply(ids: [String], dryRun: Bool) async throws -> [ApplyResultItem] {
        await recorded.set(ids)
        return applyResults
    }
}
