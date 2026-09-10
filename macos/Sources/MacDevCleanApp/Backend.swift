import Darwin
import Foundation

enum BackendError: LocalizedError {
    case sourceNotFound
    case pythonNotFound
    case commandFailed(operation: String, code: Int32, details: String)
    case commandTimedOut
    case outputLimitExceeded
    case processContainmentFailed
    case processSetupFailed
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
        case .processContainmentFailed:
            return "The cleanup engine could not verify that its process scope was contained."
        case .processSetupFailed:
            return "The cleanup engine could not start in a confined process scope."
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

    private struct ProcessSentinel {
        let url: URL
        let descriptor: Int32
        let device: UInt32
        let inode: UInt64
    }

    private static func createProcessSentinel() throws -> ProcessSentinel {
        // The inherited descriptor remains a stable ownership marker after fork,
        // reparenting, or process-group changes. A per-run random path prevents
        // accidental selection of an unrelated same-user process.
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("mac-dev-clean-process-\(UUID().uuidString).sentinel")
        let descriptor = url.path.withCString {
            open($0, O_RDONLY | O_CREAT | O_EXCL | O_CLOEXEC, S_IRUSR | S_IWUSR)
        }
        guard descriptor >= 0 else { throw BackendError.processSetupFailed }
        var fileStatus = stat()
        guard fstat(descriptor, &fileStatus) == 0 else {
            close(descriptor)
            try? FileManager.default.removeItem(at: url)
            throw BackendError.processSetupFailed
        }
        return ProcessSentinel(
            url: url,
            descriptor: descriptor,
            device: UInt32(bitPattern: fileStatus.st_dev),
            inode: fileStatus.st_ino
        )
    }

    private static func pathSentinelHolderPIDs(at url: URL) -> [pid_t]? {
        var capacity = 32
        while capacity <= 65_536 {
            var processIDs = [pid_t](repeating: 0, count: capacity)
            let returnedBytes = url.path.withCString { path in
                processIDs.withUnsafeMutableBytes {
                    proc_listpidspath(
                        UInt32(PROC_ALL_PIDS),
                        0,
                        path,
                        0,
                        $0.baseAddress,
                        Int32($0.count)
                    )
                }
            }
            guard returnedBytes >= 0 else { return nil }
            let initializedCount = min(
                Int(returnedBytes) / MemoryLayout<pid_t>.stride,
                capacity
            )
            if returnedBytes < processIDs.count * MemoryLayout<pid_t>.stride {
                return Array(processIDs.prefix(initializedCount)).filter { $0 > 1 }
            }
            if capacity == 65_536 { return nil }
            capacity *= 2
        }
        return nil
    }

    private static func processIDsForCurrentUser() -> [pid_t]? {
        var capacity = 256
        while capacity <= 65_536 {
            var processIDs = [pid_t](repeating: 0, count: capacity)
            let returnedBytes = processIDs.withUnsafeMutableBytes {
                proc_listpids(
                    UInt32(PROC_UID_ONLY),
                    UInt32(getuid()),
                    $0.baseAddress,
                    Int32($0.count)
                )
            }
            guard returnedBytes >= 0 else { return nil }
            let initializedCount = min(
                Int(returnedBytes) / MemoryLayout<pid_t>.stride,
                capacity
            )
            if returnedBytes < processIDs.count * MemoryLayout<pid_t>.stride {
                return Array(processIDs.prefix(initializedCount)).filter { $0 > 1 }
            }
            if capacity == 65_536 { return nil }
            capacity *= 2
        }
        return nil
    }

    private static func fileDescriptors(of pid: pid_t) -> [proc_fdinfo]? {
        let requiredBytes = proc_pidinfo(pid, PROC_PIDLISTFDS, 0, nil, 0)
        guard requiredBytes > 0 else { return nil }
        var capacity = max(
            Int(requiredBytes) / MemoryLayout<proc_fdinfo>.stride + 32,
            32
        )
        while capacity <= 65_536 {
            var descriptors = [proc_fdinfo](repeating: proc_fdinfo(), count: capacity)
            let returnedBytes = descriptors.withUnsafeMutableBytes {
                proc_pidinfo(
                    pid,
                    PROC_PIDLISTFDS,
                    0,
                    $0.baseAddress,
                    Int32($0.count)
                )
            }
            guard returnedBytes > 0 else { return nil }
            let initializedCount = min(
                Int(returnedBytes) / MemoryLayout<proc_fdinfo>.stride,
                capacity
            )
            let bufferBytes = descriptors.count * MemoryLayout<proc_fdinfo>.stride
            if Int(returnedBytes) + MemoryLayout<proc_fdinfo>.stride < bufferBytes {
                return Array(descriptors.prefix(initializedCount))
            }
            if capacity == 65_536 { return nil }
            capacity = min(capacity * 2, 65_536)
        }
        return nil
    }

    private static func vnodeSentinelHolderPIDs(_ sentinel: ProcessSentinel) -> [pid_t]? {
        guard let processIDs = processIDsForCurrentUser() else { return nil }
        var holders: [pid_t] = []
        for pid in processIDs {
            guard let descriptors = fileDescriptors(of: pid) else { continue }
            for descriptor in descriptors where descriptor.proc_fdtype == PROX_FDTYPE_VNODE {
                var vnode = vnode_fdinfo()
                let returnedBytes = proc_pidfdinfo(
                    pid,
                    descriptor.proc_fd,
                    PROC_PIDFDVNODEINFO,
                    &vnode,
                    Int32(MemoryLayout<vnode_fdinfo>.size)
                )
                if returnedBytes == MemoryLayout<vnode_fdinfo>.size,
                   vnode.pvi.vi_stat.vst_dev == sentinel.device,
                   vnode.pvi.vi_stat.vst_ino == sentinel.inode {
                    holders.append(pid)
                    break
                }
            }
        }
        return holders.contains(getpid()) ? holders : nil
    }

    private static func sentinelHolderPIDs(_ sentinel: ProcessSentinel) throws -> [pid_t] {
        // A descendant can unlink the pathname while every inherited descriptor
        // still references the vnode. Fall back to device/inode enumeration and
        // require the parent-held descriptor as a scanner health check.
        if let pathHolders = pathSentinelHolderPIDs(at: sentinel.url),
           pathHolders.contains(getpid()) {
            return pathHolders
        }
        guard let vnodeHolders = vnodeSentinelHolderPIDs(sentinel) else {
            throw BackendError.processContainmentFailed
        }
        return vnodeHolders
    }

    private static func withCStringArray<R>(
        _ strings: [String],
        body: (UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>) throws -> R
    ) throws -> R {
        var pointers = strings.map { strdup($0) }
        guard pointers.allSatisfy({ $0 != nil }) else {
            for pointer in pointers where pointer != nil { free(pointer) }
            throw BackendError.processSetupFailed
        }
        pointers.append(nil)
        defer {
            for pointer in pointers where pointer != nil { free(pointer) }
        }
        return try pointers.withUnsafeMutableBufferPointer {
            try body($0.baseAddress!)
        }
    }

    private struct SpawnedBackendProcess {
        let pid: pid_t
        let stdout: FileHandle
        let stderr: FileHandle
    }

    private static func spawnBackend(
        location: BackendLocation,
        arguments: [String],
        sentinelDescriptor: Int32
    ) throws -> SpawnedBackendProcess {
        var stdoutDescriptors = [Int32](repeating: 0, count: 2)
        guard pipe(&stdoutDescriptors) == 0 else { throw BackendError.processSetupFailed }
        var stderrDescriptors = [Int32](repeating: 0, count: 2)
        guard pipe(&stderrDescriptors) == 0 else {
            close(stdoutDescriptors[0])
            close(stdoutDescriptors[1])
            throw BackendError.processSetupFailed
        }
        var shouldCloseReadDescriptors = true
        defer {
            if shouldCloseReadDescriptors {
                close(stdoutDescriptors[0])
                close(stderrDescriptors[0])
            }
            close(stdoutDescriptors[1])
            close(stderrDescriptors[1])
        }

        var fileActions: posix_spawn_file_actions_t? = nil
        guard posix_spawn_file_actions_init(&fileActions) == 0 else {
            throw BackendError.processSetupFailed
        }
        defer { posix_spawn_file_actions_destroy(&fileActions) }
        // CLOEXEC_DEFAULT closes every unspecified descriptor. Preserve only the
        // sentinel plus the three standard streams wired below.
        guard posix_spawn_file_actions_addinherit_np(&fileActions, sentinelDescriptor) == 0,
              posix_spawn_file_actions_adddup2(
                  &fileActions,
                  sentinelDescriptor,
                  STDIN_FILENO
              ) == 0,
              posix_spawn_file_actions_adddup2(
                  &fileActions,
                  stdoutDescriptors[1],
                  STDOUT_FILENO
              ) == 0,
              posix_spawn_file_actions_adddup2(
                  &fileActions,
                  stderrDescriptors[1],
                  STDERR_FILENO
              ) == 0,
              location.workingDirectory.path.withCString({
                  posix_spawn_file_actions_addchdir_np(&fileActions, $0)
              }) == 0
        else {
            throw BackendError.processSetupFailed
        }

        var attributes: posix_spawnattr_t? = nil
        guard posix_spawnattr_init(&attributes) == 0 else {
            throw BackendError.processSetupFailed
        }
        defer { posix_spawnattr_destroy(&attributes) }
        let flags = Int16(POSIX_SPAWN_SETPGROUP | POSIX_SPAWN_CLOEXEC_DEFAULT)
        guard posix_spawnattr_setpgroup(&attributes, 0) == 0,
              posix_spawnattr_setflags(&attributes, flags) == 0
        else {
            throw BackendError.processSetupFailed
        }

        let argv = [location.pythonURL.path, "-m", "mac_dev_clean"] + arguments
        let environmentValues = pythonEnvironment(
            base: ProcessInfo.processInfo.environment,
            pythonPath: location.pythonPath
        )
        let environment = environmentValues.keys.sorted().map {
            "\($0)=\(environmentValues[$0]!)"
        }
        var pid: pid_t = 0
        let spawnResult = try location.pythonURL.path.withCString { executable in
            try withCStringArray(argv) { argvPointer in
                try withCStringArray(environment) { environmentPointer in
                    posix_spawn(
                        &pid,
                        executable,
                        &fileActions,
                        &attributes,
                        argvPointer,
                        environmentPointer
                    )
                }
            }
        }
        guard spawnResult == 0 else { throw BackendError.processSetupFailed }

        shouldCloseReadDescriptors = false
        return SpawnedBackendProcess(
            pid: pid,
            stdout: FileHandle(fileDescriptor: stdoutDescriptors[0], closeOnDealloc: true),
            stderr: FileHandle(fileDescriptor: stderrDescriptors[0], closeOnDealloc: true)
        )
    }


    private static func freezeSentinelHolders(
        _ sentinel: ProcessSentinel,
        visited: inout Set<pid_t>
    ) throws -> [pid_t] {
        // Repeat after freezing the current holders so a descendant created
        // during the first scan cannot escape the next one.
        var frozen: [pid_t] = []
        while true {
            var foundNewHolder = false
            for pid in try sentinelHolderPIDs(sentinel) where visited.insert(pid).inserted {
                if pid > 1, kill(pid, SIGSTOP) == 0 {
                    foundNewHolder = true
                    frozen.append(pid)
                }
            }
            if !foundNewHolder { return frozen }
        }
    }

    private static func signalSentinelHolders(
        _ candidates: [pid_t],
        sentinel: ProcessSentinel,
        signal: Int32
    ) throws {
        let currentHolders = Set(try sentinelHolderPIDs(sentinel))
        for pid in candidates where currentHolders.contains(pid) {
            _ = kill(pid, signal)
        }
    }

    private static func signalRoot(_ rootPID: pid_t, signal: Int32, ownsProcessGroup: Bool) {
        if ownsProcessGroup {
            _ = kill(-rootPID, signal)
        } else {
            _ = kill(rootPID, signal)
        }
    }

    private static func terminateProcessTree(rootPID: pid_t, sentinel: ProcessSentinel) async throws {
        let ownsProcessGroup = getpgid(rootPID) == rootPID
        signalRoot(rootPID, signal: SIGSTOP, ownsProcessGroup: ownsProcessGroup)

        do {
            var visited: Set<pid_t> = [rootPID, getpid()]
            let descendants = try freezeSentinelHolders(sentinel, visited: &visited)
            try signalSentinelHolders(descendants, sentinel: sentinel, signal: SIGTERM)
            signalRoot(rootPID, signal: SIGTERM, ownsProcessGroup: ownsProcessGroup)
            try signalSentinelHolders(descendants, sentinel: sentinel, signal: SIGCONT)
            signalRoot(rootPID, signal: SIGCONT, ownsProcessGroup: ownsProcessGroup)
            try signalSentinelHolders(descendants, sentinel: sentinel, signal: SIGKILL)
            signalRoot(rootPID, signal: SIGKILL, ownsProcessGroup: ownsProcessGroup)
        } catch {
            signalRoot(rootPID, signal: SIGKILL, ownsProcessGroup: ownsProcessGroup)
            throw error
        }
    }

    private static func terminationStatus(from waitStatus: Int32) -> Int32 {
        let signal = waitStatus & 0x7f
        if signal == 0 {
            return (waitStatus >> 8) & 0xff
        }
        return 128 + signal
    }

    private func run(arguments: [String], honorsCancellation: Bool = false) async throws -> CommandResult {
        let location = location
        let sentinel = try Self.createProcessSentinel()
        defer {
            close(sentinel.descriptor)
            try? FileManager.default.removeItem(at: sentinel.url)
        }
        let process = try Self.spawnBackend(
            location: location,
            arguments: arguments,
            sentinelDescriptor: sentinel.descriptor
        )
        async let stdoutCapture = Self.drainToEnd(
            process.stdout,
            limit: Self.stdoutLimit
        )
        async let stderrCapture = Self.drainToEnd(
            process.stderr,
            limit: Self.stderrLimit
        )

        let clock = ContinuousClock()
        let deadline = clock.now.advanced(by: commandTimeout)
        var waitStatus: Int32 = 0
        var processExited = false
        var wasCancelled = false
        var didTimeOut = false
        var containmentFailed = false
        while !processExited {
            let waitResult = waitpid(process.pid, &waitStatus, WNOHANG)
            if waitResult == process.pid {
                processExited = true
                break
            }
            if waitResult == -1, errno != EINTR {
                throw BackendError.processSetupFailed
            }
            if honorsCancellation, Task.isCancelled {
                wasCancelled = true
                do {
                    try await Self.terminateProcessTree(rootPID: process.pid, sentinel: sentinel)
                } catch {
                    containmentFailed = true
                }
                break
            }
            if clock.now >= deadline {
                didTimeOut = true
                do {
                    try await Self.terminateProcessTree(rootPID: process.pid, sentinel: sentinel)
                } catch {
                    containmentFailed = true
                }
                break
            }
            await Self.pollingDelay()
        }

        if wasCancelled || didTimeOut {
            while waitpid(process.pid, &waitStatus, 0) == -1, errno == EINTR {}
        } else {
            do {
                var visited: Set<pid_t> = [getpid(), process.pid]
                let escaped = try Self.freezeSentinelHolders(sentinel, visited: &visited)
                try Self.signalSentinelHolders(escaped, sentinel: sentinel, signal: SIGTERM)
                try Self.signalSentinelHolders(escaped, sentinel: sentinel, signal: SIGCONT)
                try Self.signalSentinelHolders(escaped, sentinel: sentinel, signal: SIGKILL)
            } catch {
                containmentFailed = true
            }
        }
        if containmentFailed {
            try? process.stdout.close()
            try? process.stderr.close()
        }

        let stdout = await stdoutCapture
        let stderr = await stderrCapture
        if containmentFailed {
            throw BackendError.processContainmentFailed
        }
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
            terminationStatus: Self.terminationStatus(from: waitStatus)
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
