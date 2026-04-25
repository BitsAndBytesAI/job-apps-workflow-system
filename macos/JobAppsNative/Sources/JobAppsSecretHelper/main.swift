import Foundation
import Security

private let protocolVersion = 1
private let helperVersionString = "0.1.0"
private let serviceName = "ai.bitsandbytes.jobapps.secret.v1"
private let accessGroupEnv = "JOB_APPS_KEYCHAIN_ACCESS_GROUP"
private let allowUnsignedHelperEnv = "JOB_APPS_ALLOW_UNSIGNED_HELPER"
private let expectedTeamInfoKey = "JobAppsExpectedTeamIdentifier"
private let knownSecrets: [String: (label: String, description: String)] = [
    "openai_api_key": ("Job Apps - OpenAI API Key", "OpenAI API key for AI Job Agents."),
    "anthropic_api_key": ("Job Apps - Anthropic API Key", "Anthropic API key for AI Job Agents."),
    "anymailfinder_api_key": ("Job Apps - Anymailfinder API Key", "Anymailfinder API key for AI Job Agents."),
    "google_oauth_token_json": ("Job Apps - Google OAuth Token", "Stored Google OAuth credentials for AI Job Agents."),
]

struct HelperRequest: Codable {
    let verb: String
    let secret_name: String?
    let secret_names: [String]?
    let secret_value: String?
    let label: String?
    let description: String?
}

struct ErrorPayload: Codable {
    let code: String
    let message: String
    let detail: String?
}

struct HelperResponse: Codable {
    let ok: Bool
    let protocol_version: Int
    let helper_version: String?
    let secret_name: String?
    let secret_value: String?
    let secrets: [String: String]?
    let status_message: String?
    let codesign_ok: Bool?
    let entitlements_ok: Bool?
    let access_group_ok: Bool?
    let probe_round_trip_ok: Bool?
    let error: ErrorPayload?

    init(
        ok: Bool,
        helperVersion: String? = helperVersionString,
        secretName: String? = nil,
        secretValue: String? = nil,
        secrets: [String: String]? = nil,
        statusMessage: String? = nil,
        codesignOK: Bool? = nil,
        entitlementsOK: Bool? = nil,
        accessGroupOK: Bool? = nil,
        probeRoundTripOK: Bool? = nil,
        error: ErrorPayload? = nil
    ) {
        self.ok = ok
        self.protocol_version = protocolVersion
        self.helper_version = helperVersion
        self.secret_name = secretName
        self.secret_value = secretValue
        self.secrets = secrets
        self.status_message = statusMessage
        self.codesign_ok = codesignOK
        self.entitlements_ok = entitlementsOK
        self.access_group_ok = accessGroupOK
        self.probe_round_trip_ok = probeRoundTripOK
        self.error = error
    }
}

struct HelperFailure: Error {
    let code: String
    let message: String
    let detail: String?
}

final class SecretStore {
    private let accessGroup: String?
    private let allowUnsignedHelper: Bool

    init() {
        let configuredAccessGroup = ProcessInfo.processInfo.environment[accessGroupEnv]?.trimmingCharacters(in: .whitespacesAndNewlines)
        self.accessGroup = configuredAccessGroup?.isEmpty == false ? configuredAccessGroup : nil
        self.allowUnsignedHelper = ProcessInfo.processInfo.environment[allowUnsignedHelperEnv] == "1"
    }

    func put(secretName: String, secretValue: String, label: String?, description: String?) throws {
        try ensureOperationalReadiness()
        let metadata = try metadataForSecret(secretName)
        let data = Data(secretValue.utf8)
        let updateAttributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrLabel as String: label ?? metadata.label,
            kSecAttrDescription as String: description ?? metadata.description,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly,
        ]

        let updateStatus = SecItemUpdate(baseQuery(secretName) as CFDictionary, updateAttributes as CFDictionary)
        switch updateStatus {
        case errSecSuccess:
            return
        case errSecItemNotFound:
            var addQuery = baseQuery(secretName)
            addQuery[kSecValueData as String] = data
            addQuery[kSecAttrLabel as String] = label ?? metadata.label
            addQuery[kSecAttrDescription as String] = description ?? metadata.description
            addQuery[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
            let addStatus = SecItemAdd(addQuery as CFDictionary, nil)
            guard addStatus == errSecSuccess else {
                throw failure(for: addStatus, fallbackMessage: "Unable to store secret in Keychain.")
            }
        default:
            throw failure(for: updateStatus, fallbackMessage: "Unable to store secret in Keychain.")
        }
    }

    func get(secretName: String) throws -> String {
        try ensureOperationalReadiness()
        _ = try metadataForSecret(secretName)
        var query = baseQuery(secretName)
        query[kSecReturnData as String] = kCFBooleanTrue
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        switch status {
        case errSecSuccess:
            guard let data = result as? Data, let stringValue = String(data: data, encoding: .utf8), !stringValue.isEmpty else {
                throw HelperFailure(code: "schema_invalid", message: "Stored secret is unreadable.", detail: nil)
            }
            return stringValue
        case errSecItemNotFound:
            throw HelperFailure(code: "missing_secret", message: "Secret not found.", detail: nil)
        default:
            throw failure(for: status, fallbackMessage: "Unable to read secret from Keychain.")
        }
    }

    func delete(secretName: String) throws {
        try ensureOperationalReadiness()
        _ = try metadataForSecret(secretName)
        let status = SecItemDelete(baseQuery(secretName) as CFDictionary)
        switch status {
        case errSecSuccess, errSecItemNotFound:
            return
        default:
            throw failure(for: status, fallbackMessage: "Unable to delete secret from Keychain.")
        }
    }

    func getBatch(secretNames: [String]) throws -> [String: String] {
        try ensureOperationalReadiness()
        var values: [String: String] = [:]
        for name in secretNames {
            do {
                values[name] = try get(secretName: name)
            } catch let error as HelperFailure {
                if error.code == "missing_secret" {
                    continue
                }
                throw error
            }
        }
        return values
    }

    func healthcheck() -> HelperResponse {
        let signatureValid = evaluateCodeSignature()
        let effectiveCodesignOK = signatureValid || allowUnsignedHelper
        let probeResult = performProbe()
        let entitlementsOK = probeResult.errorCode != "entitlement_missing"
        let accessGroupOK = accessGroup == nil || probeResult.errorCode != "access_group_misconfigured"
        let healthy = effectiveCodesignOK && probeResult.ok && entitlementsOK && accessGroupOK
        return HelperResponse(
            ok: healthy,
            statusMessage: healthy ? "Keychain helper is healthy." : probeResult.message ?? defaultHealthMessage(signatureValid: signatureValid),
            codesignOK: effectiveCodesignOK,
            entitlementsOK: entitlementsOK,
            accessGroupOK: accessGroupOK,
            probeRoundTripOK: probeResult.ok,
            error: healthy ? nil : ErrorPayload(
                code: !effectiveCodesignOK ? "codesign_invalid" : (probeResult.errorCode ?? "helper_runtime_failure"),
                message: probeResult.message ?? defaultHealthMessage(signatureValid: signatureValid),
                detail: probeResult.detail
            )
        )
    }

    private func metadataForSecret(_ secretName: String) throws -> (label: String, description: String) {
        guard let metadata = knownSecrets[secretName] else {
            throw HelperFailure(code: "unknown_secret_name", message: "Secret helper rejected an unknown secret.", detail: secretName)
        }
        return metadata
    }

    private func ensureOperationalReadiness() throws {
        guard evaluateCodeSignature() || allowUnsignedHelper else {
            throw HelperFailure(code: "codesign_invalid", message: "Secret helper signature is invalid. Reinstall the app.", detail: nil)
        }
    }

    private func baseQuery(_ secretName: String) -> [String: Any] {
        var query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: serviceName,
            kSecAttrAccount as String: secretName,
            kSecUseDataProtectionKeychain as String: kCFBooleanTrue as Any,
        ]
        if let accessGroup {
            query[kSecAttrAccessGroup as String] = accessGroup
        }
        return query
    }

    private func failure(for status: OSStatus, fallbackMessage: String) -> HelperFailure {
        switch status {
        case errSecItemNotFound:
            return HelperFailure(code: "missing_secret", message: "Secret not found.", detail: nil)
        case errSecMissingEntitlement:
            return HelperFailure(code: "entitlement_missing", message: "App secret access is misconfigured. Reinstall or contact support.", detail: nil)
        case errSecInteractionNotAllowed, errSecNotAvailable:
            return HelperFailure(code: "keychain_unavailable", message: "Keychain unavailable in current session. Log in again and retry.", detail: nil)
        case errSecParam:
            return HelperFailure(code: "schema_invalid", message: "Stored secret is unreadable. Re-enter it.", detail: nil)
        default:
            let text = SecCopyErrorMessageString(status, nil) as String? ?? fallbackMessage
            return HelperFailure(code: "helper_runtime_failure", message: text, detail: "OSStatus=\(status)")
        }
    }

    private func evaluateCodeSignature() -> Bool {
        let executablePath = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        let helperBundleURL = executablePath
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let expectedIdentifier = Bundle.main.bundleIdentifier
        let expectedTeamIdentifier = (Bundle.main.object(forInfoDictionaryKey: expectedTeamInfoKey) as? String)?
            .trimmingCharacters(in: .whitespacesAndNewlines)

        var staticCode: SecStaticCode?
        guard SecStaticCodeCreateWithPath(helperBundleURL as CFURL, SecCSFlags(), &staticCode) == errSecSuccess,
              let staticCode else {
            return false
        }

        var signingInfoRef: CFDictionary?
        guard SecCodeCopySigningInformation(staticCode, SecCSFlags(rawValue: kSecCSSigningInformation), &signingInfoRef) == errSecSuccess,
              let signingInfo = signingInfoRef as? [String: Any] else {
            return false
        }

        let actualIdentifier = signingInfo[kSecCodeInfoIdentifier as String] as? String
        let actualTeamIdentifier = signingInfo[kSecCodeInfoTeamIdentifier as String] as? String
        guard actualIdentifier == expectedIdentifier else {
            return false
        }
        guard let actualTeamIdentifier, !actualTeamIdentifier.isEmpty else {
            return false
        }
        if let expectedTeamIdentifier, !expectedTeamIdentifier.isEmpty, actualTeamIdentifier != expectedTeamIdentifier {
            return false
        }
        return true
    }

    private func performProbe() -> (ok: Bool, errorCode: String?, message: String?, detail: String?) {
        let probeName = "job_apps_helper_probe_\(UUID().uuidString)"
        let probeValue = "probe-\(UUID().uuidString)"
        do {
            try put(secretName: probeName, secretValue: probeValue, label: "Job Apps - Probe", description: "Temporary helper healthcheck probe.")
            let roundTrip = try get(secretName: probeName)
            try delete(secretName: probeName)
            return (roundTrip == probeValue, roundTrip == probeValue ? nil : "helper_runtime_failure", roundTrip == probeValue ? nil : "Keychain helper probe round trip failed.", nil)
        } catch let error as HelperFailure {
            if error.code == "unknown_secret_name" {
                return performLooseProbe()
            }
            return (false, error.code, error.message, error.detail)
        } catch {
            return (false, "helper_runtime_failure", "Keychain helper probe round trip failed.", String(describing: error))
        }
    }

    private func performLooseProbe() -> (ok: Bool, errorCode: String?, message: String?, detail: String?) {
        let probeName = "helper_probe_temp"
        let probeValue = "probe-\(UUID().uuidString)"
        var query = baseQuery(probeName)
        query[kSecValueData as String] = Data(probeValue.utf8)
        query[kSecAttrLabel as String] = "Job Apps - Probe"
        query[kSecAttrDescription as String] = "Temporary helper healthcheck probe."
        query[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly

        let addStatus = SecItemAdd(query as CFDictionary, nil)
        guard addStatus == errSecSuccess || addStatus == errSecDuplicateItem else {
            let failure = failure(for: addStatus, fallbackMessage: "Unable to store probe item.")
            return (false, failure.code, failure.message, failure.detail)
        }

        var readQuery = baseQuery(probeName)
        readQuery[kSecReturnData as String] = kCFBooleanTrue
        readQuery[kSecMatchLimit as String] = kSecMatchLimitOne

        var result: CFTypeRef?
        let readStatus = SecItemCopyMatching(readQuery as CFDictionary, &result)
        _ = SecItemDelete(baseQuery(probeName) as CFDictionary)
        guard readStatus == errSecSuccess, let data = result as? Data, let roundTrip = String(data: data, encoding: .utf8) else {
            let failure = failure(for: readStatus, fallbackMessage: "Unable to read probe item.")
            return (false, failure.code, failure.message, failure.detail)
        }
        return (roundTrip == probeValue, roundTrip == probeValue ? nil : "helper_runtime_failure", roundTrip == probeValue ? nil : "Keychain helper probe round trip failed.", nil)
    }

    private func defaultHealthMessage(signatureValid: Bool) -> String {
        if !signatureValid && !allowUnsignedHelper {
            return "Secret helper signature is invalid. Reinstall the app."
        }
        return "Secret helper failed unexpectedly. Check logs."
    }
}

private func main() -> Int32 {
    do {
        let request = try decodeRequest()
        let store = SecretStore()
        let response = try handle(request: request, store: store)
        try emit(response)
        return response.ok ? 0 : 1
    } catch let error as HelperFailure {
        return emitFailure(error)
    } catch {
        return emitFailure(
            HelperFailure(
                code: "helper_runtime_failure",
                message: "Secret helper failed unexpectedly. Check logs.",
                detail: String(describing: error)
            )
        )
    }
}

private func handle(request: HelperRequest, store: SecretStore) throws -> HelperResponse {
    switch request.verb {
    case "put":
        guard let secretName = request.secret_name, let secretValue = request.secret_value, !secretValue.isEmpty else {
            throw HelperFailure(code: "schema_invalid", message: "Secret payload is missing a secret name or value.", detail: nil)
        }
        try store.put(secretName: secretName, secretValue: secretValue, label: request.label, description: request.description)
        return HelperResponse(ok: true, secretName: secretName, statusMessage: "Key stored and ready.")
    case "get":
        guard let secretName = request.secret_name else {
            throw HelperFailure(code: "schema_invalid", message: "Secret payload is missing a secret name.", detail: nil)
        }
        let secretValue = try store.get(secretName: secretName)
        return HelperResponse(ok: true, secretName: secretName, secretValue: secretValue, statusMessage: "Key stored and ready.")
    case "delete":
        guard let secretName = request.secret_name else {
            throw HelperFailure(code: "schema_invalid", message: "Secret payload is missing a secret name.", detail: nil)
        }
        try store.delete(secretName: secretName)
        return HelperResponse(ok: true, secretName: secretName, statusMessage: "Secret deleted.")
    case "get-batch":
        guard let secretNames = request.secret_names else {
            throw HelperFailure(code: "schema_invalid", message: "Secret payload is missing secret names.", detail: nil)
        }
        let values = try store.getBatch(secretNames: secretNames)
        return HelperResponse(ok: true, secrets: values, statusMessage: "Secrets loaded.")
    case "healthcheck":
        return store.healthcheck()
    default:
        throw HelperFailure(code: "schema_invalid", message: "Secret helper received an unsupported verb.", detail: request.verb)
    }
}

private func decodeRequest() throws -> HelperRequest {
    let data = FileHandle.standardInput.readDataToEndOfFile()
    guard !data.isEmpty else {
        throw HelperFailure(code: "schema_invalid", message: "Secret helper did not receive a request payload.", detail: nil)
    }
    return try JSONDecoder().decode(HelperRequest.self, from: data)
}

private func emit(_ response: HelperResponse) throws {
    let encoder = JSONEncoder()
    let data = try encoder.encode(response)
    FileHandle.standardOutput.write(data)
    FileHandle.standardOutput.write(Data("\n".utf8))
}

@discardableResult
private func emitFailure(_ error: HelperFailure) -> Int32 {
    let response = HelperResponse(
        ok: false,
        statusMessage: error.message,
        error: ErrorPayload(code: error.code, message: error.message, detail: error.detail)
    )
    do {
        try emit(response)
    } catch {
        let fallback = """
        {"ok":false,"protocol_version":\(protocolVersion),"error":{"code":"helper_runtime_failure","message":"Secret helper failed unexpectedly. Check logs.","detail":"\(String(describing: error))"}}
        """
        FileHandle.standardOutput.write(Data(fallback.utf8))
        FileHandle.standardOutput.write(Data("\n".utf8))
    }
    return 1
}

exit(main())
