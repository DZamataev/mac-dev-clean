import Darwin
import Foundation

enum BackendError: LocalizedError {
    case sourceNotFound
    case pythonNotFound
    case commandFailed(operation: String, code: Int32, details: String)
    case commandTimedOut
    case outputLimitExceeded
    case invalidOutput(String)

    var errorDescription: String? {
        switch self {
        case .sourceNotFound:
            return "Could not find the bundled mac-dev-clean Python engine."
        case .pythonNotFound:
            return "Python 3 was not found. Install Xcode Command Line Tools and try again."
        case let .commandFailed(operation, code, details):
            let reason = details.isEmpty
                ? "The cleanup engine did not return a reason."
                : details
            return "The \(operation) could not finish. No additional files will be removed.\n\(reason)\n\nDiagnostic code: \(code)"
        case .commandTimedOut:
            return "The cleanup engine timed out. Its previous recommendations are no longer actionable."
        case .outputLimitExceeded:
            return "The cleanup engine exceeded the safe output limit. Its output was discarded."
        case let .invalidOutput(message):
            return "mac-dev-clean returned invalid data.\n\(message)"
        }
    }
}

struct BackendLocation: Sendable {
    let pythonURL: URL
    let pythonPath: URL
    let workingDirectory: URL
}

struct CommandResult: Sendable {
    let stdout: Data
    let stderr: String
    let terminationStatus: Int32
}

protocol CleanupBackendProtocol: Sendable {
    func scan() async throws -> ScanReport
    func clean(flags: [String]) async throws -> CleanReport
}

protocol ToolBackendProtocol: Sendable {
    func loadTools() async throws -> ToolReport
    func applyTool(id: String) async throws -> ToolApplyReport
}

struct CleanupBackend: CleanupBackendProtocol, ToolBackendProtocol, Sendable {
    let location: BackendLocation
    let commandTimeout: Duration

    private static let stdoutLimit = 8 * 1_024 * 1_024
    private static let stderrLimit = 64 * 1_024

    static let toolInventoryArguments = ["tools", "--json"]

    static func toolApplyArguments(id: String) -> [String] {
        ["tools-apply", "--id", id, "--json"]
    }

    init(location: BackendLocation? = nil, commandTimeout: Duration = .seconds(300)) throws {
        self.location = try location ?? BackendLocator.locate()
        self.commandTimeout = commandTimeout
    }

    func scan() async throws -> ScanReport {
        let result = try await run(arguments: [
            "scan",
            "--json",
            "--no-node-modules",
            "--no-project-derived-data",
        ], honorsCancellation: true)
        guard result.terminationStatus == 0 else {
            throw Self.commandFailure(operation: "scan", result: result)
        }
        return try Self.decode(ScanReport.self, from: result.stdout)
    }

    func clean(flags: [String]) async throws -> CleanReport {
        guard !flags.isEmpty else {
            return CleanReport(totalBytes: 0, total: "0 B", count: 0, items: [])
        }
        // Once a destructive command is handed off, incidental Task cancellation
        // must not hide its authoritative result. The process remains timeout-bounded.
        let result = try await run(arguments: ["clean"] + flags + ["--json"])
        return try Self.cleanReport(from: result)
    }

    func loadTools() async throws -> ToolReport {
        let result = try await run(arguments: Self.toolInventoryArguments, honorsCancellation: true)
        guard result.terminationStatus == 0 else {
            throw Self.commandFailure(operation: "tool inventory", result: result)
        }
        return try Self.decode(ToolReport.self, from: result.stdout)
    }

    func applyTool(id: String) async throws -> ToolApplyReport {
        guard !id.isEmpty else {
            throw BackendError.invalidOutput("Tool recommendation ID is empty.")
        }
        // As with path cleanup, finish the handed-off action unless the hard timeout
        // fires; killing it on view-task cancellation would make the result ambiguous.
        let result = try await run(arguments: Self.toolApplyArguments(id: id))
        return try Self.toolApplyReport(from: result, requestedID: id)
    }

    static func cleanReport(from result: CommandResult) throws -> CleanReport {
        if result.terminationStatus == 0 {
            return try Self.decode(CleanReport.self, from: result.stdout)
        }

        // The CLI intentionally returns 1 when cleanup completed but one or more
        // locations were skipped. Its JSON is still the authoritative result and
        // contains the per-location reasons the UI needs to show.
        if result.terminationStatus == 1,
           let report = try? JSONDecoder().decode(CleanReport.self, from: result.stdout)
        {
            return report
        }

        throw Self.commandFailure(operation: "cleanup", result: result)
    }

    static func toolApplyReport(from result: CommandResult, requestedID: String) throws -> ToolApplyReport {
        guard result.terminationStatus == 0 || result.terminationStatus == 1 else {
            throw Self.commandFailure(operation: "tool cleanup", result: result)
        }
        let report = try Self.decode(ToolApplyReport.self, from: result.stdout)
        guard report.results.count == 1,
              let item = report.results.first,
              item.id == requestedID,
              !item.dryRun
        else {
            throw BackendError.invalidOutput("Tool cleanup returned an invalid result contract.")
        }
        let statusMatchesOutcome = switch (result.terminationStatus, item.outcome) {
        case (0, "invoked"), (1, "failed"), (1, "skipped"):
            true
        default:
            false
        }
        guard statusMatchesOutcome else {
            throw BackendError.invalidOutput("Tool cleanup returned an inconsistent outcome.")
        }
        return report
    }

    static func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        do {
            return try JSONDecoder().decode(type, from: data)
        } catch {
            throw BackendError.invalidOutput("The cleanup engine returned malformed JSON.")
        }
    }

    static func commandFailure(operation: String, result: CommandResult) -> BackendError {
        let stderr = result.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
        let stdout = String(data: result.stdout, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let details = [stderr, stdout]
            .filter { !$0.isEmpty }
            .joined(separator: "\n")
        return .commandFailed(
            operation: operation,
            code: result.terminationStatus,
            details: String(details.prefix(4_000))
        )
    }

    static func pythonEnvironment(
        base: [String: String],
        pythonPath: URL
    ) -> [String: String] {
        var environment = base
        for key in environment.keys where key.hasPrefix("PYTHON") {
            environment.removeValue(forKey: key)
        }
        environment["PYTHONPATH"] = pythonPath.path
        environment["PYTHONNOUSERSITE"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONUNBUFFERED"] = "1"
        return environment
    }

    private struct CapturedStream: Sendable {
        let data: Data
        let exceededLimit: Bool
    }


    private static func drainToEnd(_ handle: FileHandle, limit: Int) async -> CapturedStream {
        var data = Data()
        var exceededLimit = false
        while true {
            let chunk = handle.availableData
            if chunk.isEmpty { break }
            let remaining = max(0, limit - data.count)
            if remaining > 0 {
                data.append(chunk.prefix(remaining))
            }
            if chunk.count > remaining {
                exceededLimit = true
            }
        }
        return CapturedStream(data: data, exceededLimit: exceededLimit)
    }

    private static func pollingDelay() async {
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            DispatchQueue.global().asyncAfter(deadline: .now() + .milliseconds(20)) {
                continuation.resume()
            }
        }
    }

    private static func directChildPIDs(of parentPID: pid_t) -> [pid_t] {
        var capacity = 32
        while capacity <= 65_536 {
            var children = [pid_t](repeating: 0, count: capacity)
            let count = proc_listchildpids(
                parentPID,
                &children,
                Int32(children.count * MemoryLayout<pid_t>.stride)
            )
            guard count >= 0 else { return [] }
            if count < capacity || capacity == 65_536 {
                let initializedCount = min(Int(count), capacity)
                return Array(children.prefix(initializedCount)).filter { $0 > 0 }
            }
            capacity *= 2
        }
        return []
    }

    private static func freezeDescendants(
        of parentPID: pid_t,
        visited: inout Set<pid_t>
    ) -> [pid_t] {
        var descendants: [pid_t] = []
        for childPID in directChildPIDs(of: parentPID) where visited.insert(childPID).inserted {
            _ = kill(childPID, SIGSTOP)
            descendants.append(contentsOf: freezeDescendants(of: childPID, visited: &visited))
            descendants.append(childPID)
        }
        return descendants
    }


    private static func signalRoot(_ rootPID: pid_t, signal: Int32, ownsProcessGroup: Bool) {
        if ownsProcessGroup {
            _ = kill(-rootPID, signal)
        } else {
            _ = kill(rootPID, signal)
        }
    }

    private static func terminateProcessTree(_ process: Process) async {
        let rootPID = process.processIdentifier
        let ownsProcessGroup = getpgid(rootPID) == rootPID
        signalRoot(rootPID, signal: SIGSTOP, ownsProcessGroup: ownsProcessGroup)

        var visited: Set<pid_t> = [rootPID]
        let descendants = freezeDescendants(of: rootPID, visited: &visited)
        for pid in descendants {
            _ = kill(pid, SIGTERM)
        }
        signalRoot(rootPID, signal: SIGTERM, ownsProcessGroup: ownsProcessGroup)
        for pid in descendants {
            _ = kill(pid, SIGCONT)
        }
        signalRoot(rootPID, signal: SIGCONT, ownsProcessGroup: ownsProcessGroup)

        for pid in descendants {
            _ = kill(pid, SIGKILL)
        }

        for _ in 0..<10 where process.isRunning {
            await pollingDelay()
        }
        signalRoot(rootPID, signal: SIGKILL, ownsProcessGroup: ownsProcessGroup)
    }

    private func run(arguments: [String], honorsCancellation: Bool = false) async throws -> CommandResult {
        let location = location
        let process = Process()
        let stdoutPipe = Pipe()
        let stderrPipe = Pipe()
        process.executableURL = location.pythonURL
        process.arguments = ["-m", "mac_dev_clean"] + arguments
        process.currentDirectoryURL = location.workingDirectory
        process.standardOutput = stdoutPipe
        process.standardError = stderrPipe
        process.environment = Self.pythonEnvironment(
            base: ProcessInfo.processInfo.environment,
            pythonPath: location.pythonPath
        )

        try process.run()
        async let stdoutCapture = Self.drainToEnd(
            stdoutPipe.fileHandleForReading,
            limit: Self.stdoutLimit
        )
        async let stderrCapture = Self.drainToEnd(
            stderrPipe.fileHandleForReading,
            limit: Self.stderrLimit
        )

        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: commandTimeout)
        var wasCancelled = false
        var didTimeOut = false
        while process.isRunning {
            if honorsCancellation, Task.isCancelled {
                wasCancelled = true
                await Self.terminateProcessTree(process)
                break
            }
            if clock.now >= deadline {
                didTimeOut = true
                await Self.terminateProcessTree(process)
                break
            }
            await Self.pollingDelay()
        }

        process.waitUntilExit()
        let stdout = await stdoutCapture
        let stderr = await stderrCapture
        if wasCancelled {
            throw CancellationError()
        }
        if didTimeOut {
            throw BackendError.commandTimedOut
        }
        if stdout.exceededLimit || stderr.exceededLimit {
            throw BackendError.outputLimitExceeded
        }

        return CommandResult(
            stdout: stdout.data,
            stderr: String(data: stderr.data, encoding: .utf8) ?? "",
            terminationStatus: process.terminationStatus
        )
    }
}

enum BackendLocator {
    static func locate(environment: [String: String] = ProcessInfo.processInfo.environment) throws -> BackendLocation {
        let fileManager = FileManager.default
        let pythonCandidates = [
            "/usr/bin/python3",
            "/opt/homebrew/bin/python3",
            "/usr/local/bin/python3",
        ]
        guard let pythonPath = pythonCandidates.first(where: { fileManager.isExecutableFile(atPath: $0) }) else {
            throw BackendError.pythonNotFound
        }

        if let resources = Bundle.main.resourceURL {
            let bundledPython = resources.appendingPathComponent("python", isDirectory: true)
            if fileManager.fileExists(atPath: bundledPython.appendingPathComponent("mac_dev_clean/__main__.py").path) {
                return BackendLocation(
                    pythonURL: URL(fileURLWithPath: pythonPath),
                    pythonPath: bundledPython,
                    workingDirectory: resources
                )
            }
        }

        var roots: [URL] = []
        if let configured = environment["MAC_DEV_CLEAN_REPO"], !configured.isEmpty {
            roots.append(URL(fileURLWithPath: configured, isDirectory: true))
        }
        roots.append(URL(fileURLWithPath: fileManager.currentDirectoryPath, isDirectory: true))

        for root in roots {
            let source = root.appendingPathComponent("src", isDirectory: true)
            if fileManager.fileExists(atPath: source.appendingPathComponent("mac_dev_clean/__main__.py").path) {
                return BackendLocation(
                    pythonURL: URL(fileURLWithPath: pythonPath),
                    pythonPath: source,
                    workingDirectory: root
                )
            }
        }

        throw BackendError.sourceNotFound
    }
}
