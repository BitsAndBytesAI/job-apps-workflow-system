import Foundation

private let knownSecrets = [
    "openai_api_key",
    "anthropic_api_key",
    "anymailfinder_api_key",
    "google_oauth_token_json",
]

struct HelperBatchRequest: Codable {
    let verb: String
    let secret_names: [String]
}

struct HelperBatchError: Codable {
    let code: String
    let message: String
    let detail: String?
}

struct HelperBatchResponse: Codable {
    let ok: Bool
    let secrets: [String: String]?
    let error: HelperBatchError?
}

private func main() -> Int32 {
    do {
        let executableURL = URL(fileURLWithPath: CommandLine.arguments[0]).standardizedFileURL
        let appBundleURL = try resolveAppBundle(from: executableURL)
        let resourcesURL = appBundleURL.appendingPathComponent("Contents/Resources", isDirectory: true)
        let pythonURL = resourcesURL.appendingPathComponent("python/bin/python", isDirectory: false)
        let backendRoot = resourcesURL.appendingPathComponent("backend", isDirectory: true)
        let playwrightURL = resourcesURL.appendingPathComponent("playwright-browsers", isDirectory: true)
        let helperURL = resolveHelperURL(appBundleURL: appBundleURL)
        let secretPayload = try loadSecretBatch(helperURL: helperURL)

        let process = Process()
        process.executableURL = pythonURL
        process.arguments = ["-m", "job_apps_system.cli.scheduler_tick"]
        process.currentDirectoryURL = backendRoot

        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONHOME"] = resourcesURL.appendingPathComponent("python", isDirectory: true).path
        environment["PYTHONPATH"] = backendRoot.appendingPathComponent("src", isDirectory: true).path
        environment["PLAYWRIGHT_BROWSERS_PATH"] = playwrightURL.path
        environment["APP_ENV"] = environment["APP_ENV"] ?? defaultAppEnvironment()
        environment["APP_PORT"] = environment["APP_PORT"] ?? "8000"
        environment["JOB_APPS_SECRET_BACKEND"] = "native_helper"
        environment["JOB_APPS_SECRET_HELPER"] = helperURL.path
        environment["JOB_APPS_SECRET_PAYLOAD_FD"] = "0"

        let googleOAuthURL = resourcesURL.appendingPathComponent("google-oauth-client.json", isDirectory: false)
        if FileManager.default.fileExists(atPath: googleOAuthURL.path) {
            environment["GOOGLE_OAUTH_CLIENT_CONFIG_PATH"] = googleOAuthURL.path
        }
        process.environment = environment

        let payloadPipe = Pipe()
        process.standardInput = payloadPipe.fileHandleForReading
        process.standardOutput = FileHandle.standardOutput
        process.standardError = FileHandle.standardError

        try process.run()
        payloadPipe.fileHandleForWriting.write(secretPayload)
        payloadPipe.fileHandleForWriting.closeFile()
        process.waitUntilExit()
        return process.terminationStatus
    } catch {
        let message = "JobAppsSchedulerAgent failed: \(error)"
        if let data = "\(message)\n".data(using: .utf8) {
            FileHandle.standardError.write(data)
        }
        return 1
    }
}

private func resolveAppBundle(from executableURL: URL) throws -> URL {
    var current = executableURL
    while current.path != "/" {
        if current.pathExtension == "app" {
            return current
        }
        current.deleteLastPathComponent()
    }
    throw NSError(domain: "JobAppsSchedulerAgent", code: 1, userInfo: [NSLocalizedDescriptionKey: "Unable to resolve containing app bundle."])
}

private func resolveHelperURL(appBundleURL: URL) -> URL {
    if let explicit = ProcessInfo.processInfo.environment["JOB_APPS_SECRET_HELPER"], !explicit.isEmpty {
        return URL(fileURLWithPath: explicit)
    }
    return appBundleURL.appendingPathComponent("Contents/Helpers/JobAppsSecretHelper.app/Contents/MacOS/JobAppsSecretHelper", isDirectory: false)
}

private func loadSecretBatch(helperURL: URL) throws -> Data {
    let process = Process()
    process.executableURL = helperURL
    process.arguments = []
    let outputPipe = Pipe()
    let errorPipe = Pipe()
    let inputPipe = Pipe()
    process.standardOutput = outputPipe
    process.standardError = errorPipe
    process.standardInput = inputPipe
    let input = try JSONEncoder().encode(HelperBatchRequest(verb: "get-batch", secret_names: knownSecrets))

    try process.run()
    inputPipe.fileHandleForWriting.write(input)
    inputPipe.fileHandleForWriting.closeFile()
    process.waitUntilExit()

    let outputData = outputPipe.fileHandleForReading.readDataToEndOfFile()
    let errorData = errorPipe.fileHandleForReading.readDataToEndOfFile()
    let decoded = try JSONDecoder().decode(HelperBatchResponse.self, from: outputData)
    if process.terminationStatus != 0 || !decoded.ok {
        let helperMessage = decoded.error?.message
            ?? String(data: errorData, encoding: .utf8)
            ?? "Unknown helper error."
        throw NSError(domain: "JobAppsSchedulerAgent", code: 2, userInfo: [NSLocalizedDescriptionKey: helperMessage])
    }
    return try JSONSerialization.data(withJSONObject: decoded.secrets ?? [:], options: [])
}

private func defaultAppEnvironment() -> String {
    #if DEBUG
    return "packaged_debug"
    #else
    return "packaged"
    #endif
}

exit(main())
