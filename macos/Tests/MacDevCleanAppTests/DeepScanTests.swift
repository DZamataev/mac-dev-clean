import Foundation
import Testing
@testable import MacDevCleanApp

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
