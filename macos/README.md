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

- The wrapper now requires an app-bundled backend/runtime under:

```text
JobAppsNative.app/Contents/Resources/backend
JobAppsNative.app/Contents/Resources/python
JobAppsNative.app/Contents/Resources/playwright-browsers
```

- The build script now bundles those resources directly from your local development environment at build time. The app does not fall back to the repository at launch.

- The wrapper launches the backend with the bundled Python runtime and bundled Playwright Firefox.

- The local build currently sources those bundled resources from:

```text
src/job_apps_system
.venv/lib/python3.13/site-packages
Homebrew Python 3.13 framework
~/Library/Caches/ms-playwright/firefox-*
```

- Backend runtime data still lives under:

```text
~/Library/Application Support/JobAppsWorkflowSystem/
```
