# macOS Native Wrapper

This directory contains the native macOS wrapper for the local Python backend.

## Build

From the repository root:

```bash
./scripts/build-macos-app.sh
```

This creates a double-clickable app bundle at:

```text
macos/build/debug/JobAppsNative.app
```

## Launch

- Double-click `macos/build/debug/JobAppsNative.app`
- Or from Terminal:

```bash
open macos/build/debug/JobAppsNative.app
```

## Development notes

- The wrapper now prefers an app-bundled backend/runtime when present under:

```text
JobAppsNative.app/Contents/Resources/backend
JobAppsNative.app/Contents/Resources/python
```

- If no bundled runtime is present, it falls back to development discovery and expects to find the repository root by walking up from the built app bundle path.
- If needed, override discovery with:

```bash
JOB_APPS_REPO_ROOT=/absolute/path/to/job-apps-workflow-system open macos/build/debug/JobAppsNative.app
```

- For development fallback, the wrapper launches the backend via:

```text
.venv/bin/python -m job_apps_system.cli.launch_backend
```

- Backend runtime data still lives under:

```text
~/Library/Application Support/JobAppsWorkflowSystem/
```
