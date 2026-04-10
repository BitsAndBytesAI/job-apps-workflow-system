import AppKit
import Foundation

@MainActor
final class AppRuntime: ObservableObject {
    enum Phase {
        case idle
        case launching
        case ready
        case failed
    }

    static let shared = AppRuntime()

    @Published private(set) var phase: Phase = .idle
    @Published private(set) var statusMessage = "Starting AI Job Agents…"
    @Published private(set) var detailMessage = ""
    @Published private(set) var uiURL: URL?

    private let host = "127.0.0.1"
    private let port = 8000
    private let startupTimeout: TimeInterval = 45
    private let healthPollInterval: UInt64 = 500_000_000
    private let maxCapturedOutputLength = 8_000
    private let wrapperLogFileName = "native-wrapper.log"

    private var backendProcess: Process?
    private var ownsBackendProcess = false
    private var startupTask: Task<Void, Never>?
    private var shutdownRequested = false
    private var capturedOutput = ""

    private init() {}

    func startIfNeeded() {
        guard startupTask == nil else { return }
        startupTask = Task { [weak self] in
            await self?.start()
        }
    }

    func restart() {
        shutdown()
        phase = .idle
        statusMessage = "Restarting AI Job Agents…"
        detailMessage = ""
        uiURL = nil
        capturedOutput = ""
        shutdownRequested = false
        startupTask = nil
        startIfNeeded()
    }

    func shutdown() {
        shutdownRequested = true
        startupTask?.cancel()
        startupTask = nil

        guard ownsBackendProcess, let process = backendProcess else {
            backendProcess = nil
            ownsBackendProcess = false
            return
        }

        guard process.isRunning else {
            backendProcess = nil
            ownsBackendProcess = false
            return
        }

        process.terminate()
        let deadline = Date().addingTimeInterval(3)
        while process.isRunning && Date() < deadline {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.1))
        }

        if process.isRunning {
            kill(process.processIdentifier, SIGKILL)
        }

        backendProcess = nil
        ownsBackendProcess = false
    }

    func openLogsFolder() {
        let logsURL = appDataDirectory().appendingPathComponent("logs", isDirectory: true)
        FileManager.default.createFile(atPath: logsURL.appendingPathComponent(".keep").path, contents: nil)
        NSWorkspace.shared.open(logsURL)
    }

    private func start() async {
        phase = .launching
        statusMessage = "Launching backend…"
        detailMessage = ""
        uiURL = nil
        capturedOutput = ""
        log("Starting native wrapper launch sequence.")

        do {
            let launchConfiguration = try resolveLaunchConfiguration()
            log("Resolved repo root at \(launchConfiguration.repoRoot.path).")
            log("Using Python runtime at \(launchConfiguration.pythonURL.path).")
            try startBackendProcess(using: launchConfiguration)
            statusMessage = "Waiting for backend healthcheck…"
            log("Backend process launch requested. Waiting for healthcheck.")

            let ready = await waitForBackendReadiness()
            guard ready else {
                log("Backend healthcheck did not become ready before timeout.")
                failStartup(
                    title: "Backend failed to become healthy.",
                    details: failureDetails(defaultMessage: "Timed out waiting for /healthz to report ready.")
                )
                return
            }

            uiURL = baseURL()
            phase = .ready
            statusMessage = "Backend ready."
            detailMessage = ""
            log("Backend healthcheck succeeded. Loading \(uiURL!.absoluteString).")
        } catch {
            log("Native wrapper startup failed: \(error.localizedDescription)")
            failStartup(title: "Unable to launch backend.", details: error.localizedDescription)
        }
    }

    private func resolveLaunchConfiguration() throws -> LaunchConfiguration {
        guard let repoRoot = discoverRepoRoot() else {
            throw RuntimeError(
                message: "Unable to locate the repository root. Set JOB_APPS_REPO_ROOT or run the app from a bundle inside the repository."
            )
        }

        let pythonURL = repoRoot
            .appendingPathComponent(".venv", isDirectory: true)
            .appendingPathComponent("bin", isDirectory: true)
            .appendingPathComponent("python", isDirectory: false)

        guard FileManager.default.isExecutableFile(atPath: pythonURL.path) else {
            throw RuntimeError(
                message: "Python launcher not found at \(pythonURL.path). Build the virtualenv before launching the native app wrapper."
            )
        }

        return LaunchConfiguration(repoRoot: repoRoot, pythonURL: pythonURL)
    }

    private func startBackendProcess(using configuration: LaunchConfiguration) throws {
        let process = Process()
        process.executableURL = configuration.pythonURL
        process.arguments = [
            "-m",
            "job_apps_system.cli.launch_backend",
            "--host",
            host,
            "--port",
            String(port),
        ]
        process.currentDirectoryURL = configuration.repoRoot

        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = "src"
        environment["APP_ENV"] = "development"
        environment["APP_PORT"] = String(port)
        process.environment = environment

        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            let text = String(decoding: data, as: UTF8.self)
            Task { @MainActor [weak self] in
                self?.appendCapturedOutput(text)
            }
        }

        process.terminationHandler = { [weak self] terminatedProcess in
            Task { @MainActor [weak self] in
                self?.handleTermination(of: terminatedProcess)
            }
        }

        try process.run()
        backendProcess = process
        ownsBackendProcess = true
        statusMessage = "Backend process started."
        log("Spawned backend process pid=\(process.processIdentifier).")
    }

    private func handleTermination(of process: Process) {
        log("Backend process terminated. status=\(process.terminationStatus) reason=\(process.terminationReason.rawValue).")
        if process.terminationStatus == 2 {
            ownsBackendProcess = false
            if phase == .launching {
                statusMessage = "Using existing backend instance…"
            }
            return
        }

        if shutdownRequested {
            ownsBackendProcess = false
            backendProcess = nil
            return
        }

        if phase == .launching {
            failStartup(
                title: "Backend exited during startup.",
                details: failureDetails(defaultMessage: "Exit code \(process.terminationStatus).")
            )
        }
    }

    private func waitForBackendReadiness() async -> Bool {
        let deadline = Date().addingTimeInterval(startupTimeout)
        while Date() < deadline {
            if Task.isCancelled {
                log("Healthcheck wait cancelled.")
                return false
            }

            if await healthcheckOK() {
                log("Healthcheck returned success.")
                return true
            }

            try? await Task.sleep(nanoseconds: healthPollInterval)
        }
        return false
    }

    private func healthcheckOK() async -> Bool {
        var request = URLRequest(url: healthURL())
        request.timeoutInterval = 2
        request.cachePolicy = .reloadIgnoringLocalCacheData

        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            guard let httpResponse = response as? HTTPURLResponse else {
                return false
            }
            return httpResponse.statusCode == 200
        } catch {
            return false
        }
    }

    private func failStartup(title: String, details: String) {
        shutdown()
        phase = .failed
        statusMessage = title
        detailMessage = details
        log("Startup failed. title=\(title) details=\(details)")
    }

    private func failureDetails(defaultMessage: String) -> String {
        let trimmedOutput = capturedOutput.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmedOutput.isEmpty {
            return defaultMessage
        }
        return "\(defaultMessage)\n\nBackend output:\n\(trimmedOutput)"
    }

    private func appendCapturedOutput(_ newOutput: String) {
        capturedOutput.append(newOutput)
        if capturedOutput.count > maxCapturedOutputLength {
            capturedOutput = String(capturedOutput.suffix(maxCapturedOutputLength))
        }
    }

    private func discoverRepoRoot() -> URL? {
        let fileManager = FileManager.default
        let environment = ProcessInfo.processInfo.environment
        let explicitRoot = environment["JOB_APPS_REPO_ROOT"].map(URL.init(fileURLWithPath:))
        let bundleRepoRoot = (Bundle.main.object(forInfoDictionaryKey: "JobAppsRepoRoot") as? String)
            .map(URL.init(fileURLWithPath:))

        let executableURL = URL(fileURLWithPath: Bundle.main.executablePath ?? CommandLine.arguments[0]).standardizedFileURL
        let bundleURL = Bundle.main.bundleURL.standardizedFileURL
        let currentDirectoryURL = URL(fileURLWithPath: fileManager.currentDirectoryPath).standardizedFileURL

        let candidates = [
            explicitRoot,
            bundleRepoRoot,
            currentDirectoryURL,
            executableURL.deletingLastPathComponent(),
            bundleURL,
            bundleURL.deletingLastPathComponent(),
            bundleURL.deletingLastPathComponent().deletingLastPathComponent(),
        ].compactMap { $0 }

        for candidate in candidates {
            if let root = firstMatchingRepoRoot(startingAt: candidate) {
                return root
            }
        }

        return nil
    }

    private func firstMatchingRepoRoot(startingAt url: URL) -> URL? {
        var current = url.standardizedFileURL
        while true {
            if isRepoRoot(current) {
                return current
            }
            let next = current.deletingLastPathComponent()
            if next.path == current.path {
                return nil
            }
            current = next
        }
    }

    private func isRepoRoot(_ url: URL) -> Bool {
        let fileManager = FileManager.default
        let pyproject = url.appendingPathComponent("pyproject.toml").path
        let sourceRoot = url.appendingPathComponent("src/job_apps_system", isDirectory: true).path
        return fileManager.fileExists(atPath: pyproject) && fileManager.fileExists(atPath: sourceRoot)
    }

    private func appDataDirectory() -> URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library", isDirectory: true)
            .appendingPathComponent("Application Support", isDirectory: true)
            .appendingPathComponent("JobAppsWorkflowSystem", isDirectory: true)
    }

    private func wrapperLogURL() -> URL {
        appDataDirectory()
            .appendingPathComponent("logs", isDirectory: true)
            .appendingPathComponent(wrapperLogFileName, isDirectory: false)
    }

    private func log(_ message: String) {
        let logsDirectory = appDataDirectory().appendingPathComponent("logs", isDirectory: true)
        try? FileManager.default.createDirectory(at: logsDirectory, withIntermediateDirectories: true)
        let logURL = wrapperLogURL()
        let timestamp = ISO8601DateFormatter().string(from: Date())
        let line = "\(timestamp) \(message)\n"
        if let data = line.data(using: .utf8) {
            if FileManager.default.fileExists(atPath: logURL.path),
               let handle = try? FileHandle(forWritingTo: logURL) {
                defer { try? handle.close() }
                let _ = try? handle.seekToEnd()
                try? handle.write(contentsOf: data)
            } else {
                try? data.write(to: logURL, options: .atomic)
            }
        }
    }

    private func baseURL() -> URL {
        URL(string: "http://\(host):\(port)/")!
    }

    private func healthURL() -> URL {
        URL(string: "http://\(host):\(port)/healthz")!
    }
}

private struct LaunchConfiguration {
    let repoRoot: URL
    let pythonURL: URL
}

private struct RuntimeError: LocalizedError {
    let message: String

    var errorDescription: String? {
        message
    }
}
