import Foundation

protocol DeepScanBackendProtocol: Sendable {
    func deepScan(onEvent: @Sendable @escaping (DeepScanEvent) -> Void) async throws
    func apply(ids: [String], dryRun: Bool) async throws -> [ApplyResultItem]
}

struct DeepScanBackend: DeepScanBackendProtocol, Sendable {
    let location: BackendLocation

    init(location: BackendLocation? = nil) throws {
        self.location = try location ?? BackendLocator.locate()
    }

    /// Reads stdout incrementally so the UI can render findings while the scan
    /// is still walking the disk. Cancelling the enclosing Task terminates the
    /// Python child, which leaves its generation incomplete and therefore
    /// ineligible for cleanup.
    ///
    /// `process` is created before `withTaskCancellationHandler` (rather than
    /// inside its `operation` closure) specifically so `onCancel` can reach it:
    /// `operation` is a synchronous blocking read loop that never checks
    /// `Task.isCancelled`, so the *only* thing that unblocks it on cancellation
    /// is `onCancel` terminating the child, which closes the pipes and hands
    /// the blocked `availableData` calls an EOF.
    func deepScan(onEvent: @Sendable @escaping (DeepScanEvent) -> Void) async throws {
        let location = location
        let process = Process()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.executableURL = location.pythonURL
        process.arguments = ["-m", "mac_dev_clean", "deep-scan"]
        process.currentDirectoryURL = location.workingDirectory
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe
        process.environment = CleanupBackend.pythonEnvironment(
            base: ProcessInfo.processInfo.environment,
            pythonPath: location.pythonPath
        )

        try await withTaskCancellationHandler {
            guard !Task.isCancelled else { throw CancellationError() }
            try process.run()
            defer {
                if process.isRunning { process.terminate() }
            }

            // Drained concurrently with stdout (via `async let`, a structured
            // child task) so a large write to stderr -- a traceback, warnings,
            // anything non-NDJSON -- cannot fill the ~64KB pipe buffer and
            // deadlock the child against the incremental stdout read below.
            async let stderrData = Self.drainToEnd(stderrPipe.fileHandleForReading)

            var accumulator = NDJSONEventAccumulator()
            let handle = stdoutPipe.fileHandleForReading
            while true {
                let chunk = handle.availableData
                if chunk.isEmpty { break }
                for event in accumulator.ingest(chunk) {
                    onEvent(event)
                }
            }
            // The read loop above only emits complete newline-terminated
            // lines. If the engine's last write (e.g. `scan_completed`) ever
            // lands without a trailing newline, `finish()` recovers it from
            // the accumulator's residual buffer instead of silently dropping
            // it, which would otherwise leave the UI stuck showing a running
            // scan forever.
            if let event = accumulator.finish() {
                onEvent(event)
            }

            process.waitUntilExit()
            let stderrBytes = await stderrData
            if process.terminationStatus != 0 && !Task.isCancelled {
                throw CleanupBackend.commandFailure(
                    operation: "deep scan",
                    result: CommandResult(
                        stdout: Data(),
                        stderr: String(data: stderrBytes, encoding: .utf8) ?? "",
                        terminationStatus: process.terminationStatus
                    )
                )
            }
        } onCancel: {
            // Terminating the child is what actually stops the scan: it closes
            // the pipes, which is what unblocks the blocking `availableData`
            // reads above with EOF instead of leaving them (and this function)
            // suspended until the child produces more output on its own.
            if process.isRunning { process.terminate() }
        }
    }

    private static func drainToEnd(_ handle: FileHandle) async -> Data {
        var data = Data()
        while true {
            let chunk = handle.availableData
            if chunk.isEmpty { break }
            data.append(chunk)
        }
        return data
    }

    func apply(ids: [String], dryRun: Bool) async throws -> [ApplyResultItem] {
        guard !ids.isEmpty else { return [] }
        var arguments = ["-m", "mac_dev_clean", "apply", "--json"]
        for id in ids {
            arguments.append("--id")
            arguments.append(id)
        }
        if dryRun { arguments.append("--dry-run") }

        let location = location
        let result = try await Task.detached(priority: .userInitiated) {
            let process = Process()
            let stdoutPipe = Pipe()
            let stderrPipe = Pipe()
            process.executableURL = location.pythonURL
            process.arguments = arguments
            process.currentDirectoryURL = location.workingDirectory
            process.standardOutput = stdoutPipe
            process.standardError = stderrPipe
            process.environment = CleanupBackend.pythonEnvironment(
                base: ProcessInfo.processInfo.environment,
                pythonPath: location.pythonPath
            )
            try process.run()
            process.waitUntilExit()
            let stdout = stdoutPipe.fileHandleForReading.readDataToEndOfFile()
            let stderrData = stderrPipe.fileHandleForReading.readDataToEndOfFile()
            return CommandResult(
                stdout: stdout,
                stderr: String(data: stderrData, encoding: .utf8) ?? "",
                terminationStatus: process.terminationStatus
            )
        }.value

        // Exit code 1 means "finished, but some items were refused". The JSON
        // body is still the authoritative per-item result the UI must show.
        if let report = try? JSONDecoder().decode(ApplyReport.self, from: result.stdout) {
            return report.results
        }
        throw CleanupBackend.commandFailure(operation: "apply", result: result)
    }
}
