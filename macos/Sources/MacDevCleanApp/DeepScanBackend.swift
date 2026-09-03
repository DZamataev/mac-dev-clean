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
    func deepScan(onEvent: @Sendable @escaping (DeepScanEvent) -> Void) async throws {
        let location = location
        try await withTaskCancellationHandler {
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

            try process.run()
            defer {
                if process.isRunning { process.terminate() }
            }

            var buffer = Data()
            let handle = stdoutPipe.fileHandleForReading
            while true {
                let chunk = handle.availableData
                if chunk.isEmpty { break }
                buffer.append(chunk)
                while let newline = buffer.firstIndex(of: UInt8(ascii: "\n")) {
                    let lineData = buffer[buffer.startIndex..<newline]
                    buffer.removeSubrange(buffer.startIndex...newline)
                    guard let line = String(data: lineData, encoding: .utf8),
                          let event = DeepScanEventParser.parse(line: line)
                    else { continue }
                    onEvent(event)
                }
            }

            process.waitUntilExit()
            let stderrData = stderrPipe.fileHandleForReading.readDataToEndOfFile()
            if process.terminationStatus != 0 && !Task.isCancelled {
                throw CleanupBackend.commandFailure(
                    operation: "deep scan",
                    result: CommandResult(
                        stdout: Data(),
                        stderr: String(data: stderrData, encoding: .utf8) ?? "",
                        terminationStatus: process.terminationStatus
                    )
                )
            }
        } onCancel: {
            // The child sees EOF on the next write and exits; its generation is
            // never completed, so nothing partial becomes cleanup-eligible.
        }
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
