# Secret Helper And Keychain Design

## Goal

Define a packaged-macOS secret architecture that:

- removes long-lived API keys and OAuth tokens from SQLite in packaged mode
- avoids the legacy macOS keychain trust prompt path
- supports scheduled background runs when the UI app is closed
- fits the current Swift wrapper + bundled Python backend architecture

This document focuses on:

- XPC helper vs subprocess helper
- keychain item schema
- how scheduled background runs fetch secrets without prompts

## Current-State Clarification

The repository is internally inconsistent today:

- `CLAUDE.md` says secrets should live in the OS keychain via `keyring`
- `pyproject.toml` includes the `keyring` dependency
- but the checked-out `origin/main` code in `src/job_apps_system/config/secrets.py` still persists secret payloads in `app_settings`

That means the migration plan cannot assume one legacy source.

This design treats the current checked-out code as the operational truth for `origin/main`, while also allowing for branches or local builds that already use `keyring`.

Migration source order must therefore be:

1. packaged helper-backed keychain store
2. legacy Python secret backend from `config/secrets.py`
3. optional direct `keyring` fallback if `config/secrets.py` is corrected before this design lands

## Scope

This design covers three runtime modes:

- default development mode
- packaged debug mode
- packaged release mode

Unless a section explicitly says otherwise, "packaged mode" below means both:

- `APP_ENV == "packaged_debug"`
- `APP_ENV == "packaged"`

## Modes

### Development Mode

Development mode is any run where:

- `APP_ENV` is unset, or any value other than `packaged_debug` and `packaged`

In development mode:

- do not require a signed native helper
- do not require helper entitlements
- keep using the existing Python secret backend through `src/job_apps_system/config/secrets.py`

This preserves current workflows like:

- `bash scripts/run-local.sh`

The design intentionally does not force local contributors to build a signed `.app` just to run the backend.

### Packaged Debug Mode

Packaged debug mode is any run where:

- `APP_ENV == "packaged_debug"`

In packaged debug mode:

- use the same helper-backed secret architecture as packaged release mode
- use a signed local app bundle with the real bundle layout
- exercise `SMAppService`, LaunchAgent registration, scheduler launching, and helper healthcheck
- allow developers to validate production behavior before release

This mode exists specifically so the production path can be tested during development without forcing that path into every default local run.

### Packaged Release Mode

Packaged release mode is any run where:

- `APP_ENV == "packaged"`

In packaged release mode:

- Python must not own secret persistence directly
- native signed components own all keychain access
- Python uses a native helper bridge

## Backend Selection

Add an explicit backend selector:

- `JOB_APPS_SECRET_BACKEND=python_native|native_helper`

Resolution rules:

1. If `JOB_APPS_SECRET_BACKEND` is set, honor it
2. Else if `APP_ENV == "packaged_debug"` or `APP_ENV == "packaged"`, use `native_helper`
3. Else use `python_native`

This gives us:

- a clear development-mode story
- a production-path test mode for local validation
- testability
- an emergency fallback during migration

## Recommendation

Recommend:

- V1 packaged mode: signed native subprocess helper
- require packaged-debug test coverage before release
- future upgrade path: XPC transport

Do not start with a pure XPC-only design.

## Why Subprocess Helper First

The subprocess helper is the best V1 fit because:

- Python can invoke it immediately
- scheduled runs can invoke it even when the app UI is closed
- we avoid teaching Python to use XPC in V1
- the transport can later be replaced without changing the logical secret API

## Option Comparison

## Option A: XPC Helper

### Advantages

- strongest native IPC model
- easy caller validation through `NSXPCConnection`
- no secrets on stdout
- good long-term security boundary

### Disadvantages

- Python integration is materially more complex
- bundled XPC services are tied to client lifetime unless we introduce a separate long-lived native host
- does not simplify the background scheduler case enough for V1

### Conclusion

Good future direction, not the right first move.

## Option B: Signed Native Subprocess Helper

### Advantages

- simplest Python bridge
- works for both foreground and background runs
- easy to package and test
- can be invoked by a native scheduler launcher and by Python

### Disadvantages

- weaker caller verification than XPC
- per-call process spawn overhead
- requires strict protocol design to avoid leaks

### Conclusion

This is the recommended V1 path.

## Final Packaged Architecture

Use these packaged-mode components:

1. `JobAppsNative.app`
2. `JobAppsSecretHelper.app`
3. `JobAppsSchedulerAgent`
4. bundled Python scheduler worker

## Component Details

### 1. Main App

Bundle:

- `JobAppsNative.app`

Responsibilities:

- collect secrets from the user
- call the secret helper for `put`, `get`, and `healthcheck`
- register the bundled LaunchAgent
- surface errors on Setup and Schedule pages

### 2. Secret Helper

Bundle:

- `JobAppsNative.app/Contents/Helpers/JobAppsSecretHelper.app`

Executable:

- `JobAppsNative.app/Contents/Helpers/JobAppsSecretHelper.app/Contents/MacOS/JobAppsSecretHelper`

This is a separate app-like helper bundle because it needs restricted entitlements for keychain access groups.

It must have its own:

- `Info.plist`
- entitlements
- embedded provisioning profile
- code signature

Responsibilities:

- all `SecItemAdd`, `SecItemCopyMatching`, `SecItemUpdate`, and `SecItemDelete` operations
- helper healthcheck
- legacy secret migration
- payload validation and schema enforcement

### 3. Scheduler Agent

Executable:

- `JobAppsNative.app/Contents/Resources/JobAppsSchedulerAgent`

LaunchAgent plist:

- `JobAppsNative.app/Contents/Library/LaunchAgents/ai.bitsandbytes.jobapps.scheduler.plist`

Registration:

- use `SMAppService.agent(plistName:)`

LaunchAgent plist should use:

- `BundleProgram`

and point to:

- `Contents/Resources/JobAppsSchedulerAgent`

The scheduler agent is a separate native executable, not part of the main app process.

Implementation form:

- separate Swift executable target

It does not need keychain entitlements.

It does need:

- a small CLI entrypoint
- awareness of the app bundle root
- the ability to invoke `JobAppsSecretHelper`
- the ability to launch the Python scheduler worker with an inherited pipe

It does not need its own app bundle or provisioning profile if it does not claim restricted entitlements.

### 4. Python Scheduler Worker

This remains the Python scheduler/orchestrator process.

In packaged mode it is launched by `JobAppsSchedulerAgent`, not directly by the LaunchAgent.

## Reconciliation With The Background Scheduler Spec

The earlier scheduler spec described:

- LaunchAgent -> wrapper shell script -> `python -m job_apps_system.cli.scheduler_tick`

This design amends that architecture as follows:

### Development Mode

Keep the earlier wrapper-based architecture:

- local shell wrapper -> Python

This is still the correct path for local development.

### Packaged Mode

Replace the direct shell-wrapper launch with:

- LaunchAgent -> `JobAppsSchedulerAgent` -> `JobAppsSecretHelper` -> Python scheduler worker

Reason:

- packaged mode needs a native secret bridge before Python starts
- packaged mode should use `SMAppService` and bundled `LaunchAgent` resources rather than ad hoc scripts where possible

So the two specs are reconciled by mode:

- dev mode: wrapper script path remains
- packaged debug mode: native scheduler launcher path is exercised in pre-release testing
- packaged mode: native scheduler launcher path supersedes it

## Keychain Model

Use:

- `SecItem`
- `kSecUseDataProtectionKeychain = true`
- a shared access group

Do not use:

- `SecKeychain`
- file-based keychain ACL APIs
- Touch ID / `userPresence` gating for scheduler-required secrets

Reason:

- scheduled runs must be unattended
- the data protection keychain is the correct modern macOS path

## Keychain Access Group

Use one shared access group:

- `<TeamID>.ai.bitsandbytes.jobapps.shared`

This access group must be enabled for:

- `JobAppsNative.app`
- `JobAppsSecretHelper.app`

The scheduler agent does not need this entitlement because it never calls `SecItem` directly.

## Keychain Item Schema

Use `kSecClassGenericPassword` for all app secrets.

Required attributes:

- `kSecClass = kSecClassGenericPassword`
- `kSecUseDataProtectionKeychain = true`
- `kSecAttrAccessGroup = <TeamID>.ai.bitsandbytes.jobapps.shared`
- `kSecAttrAccessible = kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly`
- `kSecAttrSynchronizable = false`

Required user-visible metadata:

- `kSecAttrLabel`
- `kSecAttrDescription`

These are mandatory, not optional.

Reason:

- users may inspect items in Keychain Access.app
- unlabeled generic-password entries are confusing and look suspicious

## Naming Schema

Use:

- `kSecAttrService = ai.bitsandbytes.jobapps.secret.v1`
- `kSecAttrAccount = <secret_name>`

Known secret names:

- `openai_api_key`
- `anthropic_api_key`
- `anymailfinder_api_key`
- `google_oauth_token_json`

Human-readable labels:

- `Job Apps - OpenAI API Key`
- `Job Apps - Anthropic API Key`
- `Job Apps - Anymailfinder API Key`
- `Job Apps - Google OAuth Token`

Descriptions:

- `Credential used by AI Job Agents for OpenAI API access`
- `Credential used by AI Job Agents for Anthropic API access`
- `Credential used by AI Job Agents for Anymailfinder API access`
- `Google OAuth refreshable token bundle used by AI Job Agents`

## Payload Schema

Every item value is UTF-8 JSON.

Shared envelope:

```json
{
  "schema_version": 1,
  "kind": "api_key",
  "provider": "openai",
  "value": "sk-...",
  "updated_at": "2026-04-23T00:00:00Z"
}
```

Google token envelope:

```json
{
  "schema_version": 1,
  "kind": "oauth_token",
  "provider": "google",
  "value": {
    "token": "...",
    "refresh_token": "...",
    "expiry": "...",
    "scopes": ["..."]
  },
  "updated_at": "2026-04-23T00:00:00Z"
}
```

## Versioning Rules

There are two versions to track:

1. helper protocol version
2. per-item payload `schema_version`

Rules:

- helper protocol starts at `1`
- item payload schema starts at `1`
- unknown newer `schema_version` must fail closed with `schema_too_new`
- unknown older but still supported versions may be migrated in-memory and rewritten
- downgrades are not supported

This avoids silent corruption when a user rolls back the app.

## Helper Allowlist

The helper must not accept arbitrary secret names.

Use a hardcoded registry in the helper source, for example:

- `KnownSecretRegistry`

Each registry entry defines:

- secret name
- provider
- label
- description
- expected payload kind
- supported schema versions

Adding a new secret type requires:

- updating the helper binary
- updating the Python adapter
- shipping a new app build

This is intentional.

## Helper Command Protocol

All helper operations use:

- verb on argv
- JSON payload on stdin
- JSON response on stdout

Do not pass secret values on argv.

## Supported Verbs

- `put`
- `get`
- `get-batch`
- `delete`
- `healthcheck`
- `migrate-legacy`

## Stdin Shapes

### `put`

```json
{
  "protocol_version": 1,
  "secret_name": "openai_api_key",
  "payload": {
    "schema_version": 1,
    "kind": "api_key",
    "provider": "openai",
    "value": "sk-...",
    "updated_at": "2026-04-23T00:00:00Z"
  }
}
```

### `get`

```json
{
  "protocol_version": 1,
  "secret_name": "openai_api_key"
}
```

### `get-batch`

```json
{
  "protocol_version": 1,
  "secret_names": [
    "anthropic_api_key",
    "google_oauth_token_json"
  ]
}
```

### `delete`

```json
{
  "protocol_version": 1,
  "secret_name": "openai_api_key"
}
```

### `healthcheck`

```json
{
  "protocol_version": 1
}
```

## Response Shape

Success:

```json
{
  "ok": true,
  "protocol_version": 1,
  "result": {}
}
```

Failure:

```json
{
  "ok": false,
  "protocol_version": 1,
  "error": {
    "code": "missing_secret",
    "message": "Secret not found.",
    "detail": null
  }
}
```

## Error Codes

Define stable helper error codes:

- `missing_secret`
- `schema_invalid`
- `schema_too_new`
- `unknown_secret_name`
- `helper_not_found`
- `keychain_unavailable`
- `entitlement_missing`
- `access_group_misconfigured`
- `codesign_invalid`
- `helper_protocol_unsupported`
- `helper_runtime_failure`
- `migration_incomplete`

These codes are the contract for UI and backend handling.

## Healthcheck Definition

`healthcheck` must validate the full chain:

1. helper binary launched successfully
2. helper verifies its own code signature state
3. helper verifies required entitlements are present
4. helper verifies the access group is usable
5. helper performs a temporary add-read-delete round trip on a probe keychain item

Suggested response fields:

- `helper_version`
- `protocol_version`
- `codesign_ok`
- `entitlements_ok`
- `access_group_ok`
- `probe_round_trip_ok`

This is the correct input for Setup page status:

- `Keychain healthy`
- or a targeted diagnostic state

## Foreground Secret Flow

In packaged mode:

1. user enters a secret in Setup
2. app calls `JobAppsSecretHelper put`
3. helper writes the secret to the data protection keychain
4. Python reads secrets through the helper when needed

In development mode:

- Python continues to use `config/secrets.py`

## Background Scheduler Secret Flow

Packaged scheduled-run flow:

1. `SMAppService` registers the bundled LaunchAgent
2. the LaunchAgent starts `JobAppsSchedulerAgent`
3. the scheduler agent determines which agent run is due
4. the scheduler agent requests the exact secret batch from `JobAppsSecretHelper get-batch`
5. the scheduler agent creates an anonymous pipe
6. the scheduler agent launches the Python worker with:
   - `JOB_APPS_SECRET_PAYLOAD_FD=<actual fd number>`
   - `JOB_APPS_SECRET_HELPER=<absolute helper executable path>`
   - `JOB_APPS_SECRET_BACKEND=native_helper`
7. Python reads the batch once from the provided FD and closes it
8. the run proceeds

No fixed FD number is assumed.

The parent creates the pipe and passes the actual FD number.

## Why This Avoids Prompts

This design avoids the classic keychain trust prompt because:

- access uses the data protection keychain
- access occurs in a user login context
- we are not relying on legacy file-based ACL approval flows
- we are not requiring `userPresence`, Touch ID, or passcode interaction

This does not eliminate all background-service UX.

`SMAppService` registration still participates in normal macOS background items approval flows. That is separate from keychain trust prompting.

## Google OAuth Refresh Write-Back

Python must be allowed to persist refreshed Google tokens in packaged mode.

Clarification:

- "Python does not access the keychain directly" means Python must not call `SecItem` or legacy keychain APIs itself
- Python is allowed to invoke the native helper subprocess

Write-back rule:

- after token refresh, Python calls `JobAppsSecretHelper put`

This works in both:

- foreground runs
- background scheduled runs

because the scheduler agent provides:

- `JOB_APPS_SECRET_HELPER=<path>`

So the background pipe is for initial batch read only.

Subsequent writes use a fresh helper subprocess call.

## Setup Page, Onboarding Wizard, And Error UX

The Setup page must switch from "boolean secret configured" to richer helper-backed status in packaged mode.

Status mapping:

- `missing_secret`
  - show `Not configured`
- `schema_invalid` or `schema_too_new`
  - show `Stored secret is unreadable. Re-enter it.`
- `codesign_invalid`
  - show `Secret helper signature is invalid. Reinstall the app.`
- `helper_not_found`
  - show `Secret helper is missing from the app bundle. Reinstall the app.`
- `entitlement_missing` or `access_group_misconfigured`
  - show `App secret access is misconfigured. Reinstall or contact support.`
- `keychain_unavailable`
  - show `Keychain unavailable in current session. Log in again and retry.`
- `helper_runtime_failure`
  - show `Secret helper failed unexpectedly. Check logs.`

Packaged-mode Setup must also display:

- helper healthcheck result
- secret-helper version
- last error code

## Onboarding Wizard Updates

The onboarding wizard currently owns first-run secret entry for:

- OpenAI API key
- Anthropic API key
- optional Anymailfinder API key

The packaged-mode plan must update the wizard, not just the standalone Setup page.

### Shared Status Model

`/onboarding/api/state` and `/setup/api/state` should expose the same packaged-mode secret metadata:

- `configured`
- `status_code`
- `status_message`
- `last_validated_at`

Add a packaged-mode helper summary block:

- `secret_helper.healthy`
- `secret_helper.helper_version`
- `secret_helper.protocol_version`
- `secret_helper.last_error_code`

Existing boolean flags can remain as compatibility fields, but UI decisions should move to the richer status fields.

### Models Step Changes

The `models` step currently accepts:

- model selections
- OpenAI API key
- Anthropic API key

In packaged mode:

1. persist the selected model names through the existing setup config path
2. persist any non-empty API key values through `JobAppsSecretHelper put`
3. immediately read back helper-backed status for both secrets
4. only advance the wizard if required keys are now in a healthy configured state

`/onboarding/api/models` should therefore return structured helper failures using the stable error codes above, not only generic `"API key is required"` strings.

The wizard UI should replace the current broad `"AI Keys Required or Subscribe"` fallback with the same targeted messages used on Setup.

### Anymailfinder Step Changes

The `anymailfinder` step should follow the same packaged-mode contract:

- non-empty key writes go through `JobAppsSecretHelper put`
- clear/delete uses helper `delete`
- success response returns refreshed helper-backed status
- failures surface the helper error code and targeted message

`send_enabled` should only auto-enable after the helper confirms the secret is stored successfully.

### Wizard Completion Rules

In packaged mode, `/onboarding/api/google/complete` should validate more than legacy boolean secret flags.

It should refuse completion if:

- the helper healthcheck is failing
- a required secret is in `missing_secret`
- a required secret is in `schema_invalid` or `schema_too_new`
- the helper is missing, unsigned, or mis-entitled

That final step should use the same helper-backed status source as Setup and the earlier wizard steps so users do not "finish" onboarding with a broken packaged install.

### Migration UX In The Wizard

If packaged mode detects legacy secrets during first launch:

- show a non-dismissed migration notice in onboarding before or during the `models` step
- run `migrate-legacy`
- on success, continue normally
- on failure, keep onboarding blocked on the affected step and surface `migration_incomplete`

The wizard should not silently mix legacy reads with helper-backed writes after packaged-mode migration starts.

### Copy And Diagnostics

The wizard should distinguish these states clearly:

- `Key stored and ready`
- `Key missing`
- `Stored key unreadable; re-enter it`
- `Secret helper misconfigured; reinstall the app`
- `Keychain unavailable in this login session`

Do not reduce packaged-mode failures back to a generic validation string once the helper layer exists.

## Setup Wizard Implementation Notes

Concrete backend/UI changes implied by this plan:

- update `/onboarding/api/models` to write required secrets through the packaged-mode helper path
- update `/onboarding/api/anymailfinder` to write and delete through the packaged-mode helper path
- extend `/onboarding/api/state` so masked fields are backed by helper status, not only `*_configured` booleans
- update onboarding JS error handling to display helper-derived messages and codes
- update final onboarding completion checks to require helper health in packaged mode
- keep development mode on the current Python-native behavior so local onboarding remains unchanged

## Migration Strategy

Migration must handle both possible legacy sources:

- current `app_settings`-backed secret storage
- possible `keyring`-backed storage on branches or local builds

## Migration Rules

1. Enumerate all known secret names
2. Read candidate values from the legacy Python secret backend
3. Stage all discovered values in memory
4. Write all staged values to the helper-backed keychain store
5. Read all of them back through the helper
6. Only if every write and read succeeds:
   - mark migration complete
   - delete legacy copies

If any step fails:

- do not delete any legacy copy
- return `migration_incomplete`
- surface the partial-failure diagnostic in Setup

This gives us an atomic-ish verify-then-delete process.

## Upgrade And Path Stability

The earlier feedback correctly flagged stale absolute helper paths.

Use these rules:

- LaunchAgent registration in packaged mode uses bundled `SMAppService` + `BundleProgram`
- do not hardcode an absolute path to the scheduler agent in the plist
- the scheduler agent resolves the helper path relative to its own app bundle

This avoids broken absolute paths after:

- app updates
- app moves
- reinstall

For the helper payload schema:

- unknown newer schema versions fail closed
- helper returns `schema_too_new`
- UI tells the user to update the app

## Concrete Recommendation

Implement packaged mode with:

- a signed subprocess helper bundle for all keychain access
- a native scheduler launcher executable
- a bundled LaunchAgent registered with `SMAppService`
- data protection keychain items in a shared access group
- required human-readable keychain labels
- initial secret injection through an anonymous pipe using the actual inherited FD number
- helper subprocess write-back for refreshed Google OAuth tokens

Keep development mode on the existing Python secret backend.

This resolves the secret-storage problem without breaking local development and reconciles the scheduler and secret-helper architectures into one consistent packaged-mode design.
