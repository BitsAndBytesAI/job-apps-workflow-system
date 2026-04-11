# macOS Native App Plan

## Goal

Turn the existing local-first Python application into a native macOS app without rewriting the backend or the current HTML/CSS/JS UI.

The target architecture is:

- `Swift` native macOS wrapper
- `Python` backend runtime
- existing `FastAPI` server
- existing local web UI rendered inside a native app window

## Constraints

- Keep the current backend in Python
- Keep the current FastAPI-served UI
- Do not introduce a separate frontend framework or remote web app dependency
- Preserve Google OAuth, LinkedIn browser automation, SQLite state, and local agent execution
- Keep development mode working from the repository while adding packaged-app support

## Milestone 1 — Runtime Foundation

### Objective

Remove repo-relative runtime assumptions so the app can run from an installed macOS bundle.

### Deliverables

- Introduce a single app-data root abstraction
- Use `~/Library/Application Support/JobAppsWorkflowSystem/` for runtime data
- Resolve SQLite DB path from the app-data root
- Resolve LinkedIn browser profile path from the app-data root
- Create required runtime directories at startup
- Keep current development workflows functioning

### Exit Criteria

- Backend can start without assuming runtime files live beside the repo
- Relative runtime paths resolve through the app-data root abstraction
- Existing development usage still works

## Milestone 2 — First-Launch Bootstrap

### Objective

Ensure the app verifies and initializes all required runtime pieces before attempting to launch the backend.

### Deliverables

- Add a bootstrap module that runs before backend launch
- Check bundled Python runtime availability
- Check required Python dependencies are importable
- Initialize runtime directories under Application Support
- Initialize or migrate the SQLite database
- Check bundled Firefox availability for LinkedIn automation
- Check Google OAuth client config presence
- Separate blocking failures from non-blocking warnings
- Output a structured bootstrap result for launcher and wrapper use

### Exit Criteria

- The app can determine whether launch may proceed
- Missing critical runtime prerequisites fail early with a clear reason
- Missing optional/user-supplied requirements surface as warnings, not crashes

## Milestone 3 — Backend Launcher

### Objective

Create a stable backend runtime entrypoint that the native macOS wrapper can launch directly after bootstrap succeeds.

### Deliverables

- Add a dedicated Python launcher entrypoint
- Run bootstrap before backend startup
- Start FastAPI programmatically from that entrypoint
- Add startup healthcheck behavior
- Add clean shutdown behavior
- Add single-instance guard for the backend process
- Standardize runtime logging location under app-data

### Exit Criteria

- One launcher command can start the backend and expose a healthy local service
- The backend can be started and stopped without manual `uvicorn` shell usage

## Milestone 4 — Native Wrapper Skeleton

### Objective

Build the first Swift macOS wrapper around the local backend.

### Deliverables

- Create a Swift macOS app project
- Launch the Python backend on app start
- Wait for backend healthcheck
- Load the local UI in a native web view
- Quit backend cleanly when the app exits
- Display startup failure UI if backend fails to boot

### Exit Criteria

- Double-clicking the macOS app launches the local backend and shows the UI in a native window

## Milestone 5 — App Mode Integration

### Objective

Make the wrapped app behave like a real installed macOS application instead of a dev shell.

### Deliverables

- App-mode environment/config wiring
- Prefer app-bundled backend resources and bundled Python over repo discovery
- Keep repo + `.venv` fallback only for development
- Stable port ownership
- Better error presentation for missing Firefox, revoked Google auth, and missing API keys
- Native menu items for:
  - Open Dashboard
  - Open Logs Folder
  - Restart Backend

### Exit Criteria

- The app can be restarted and operated without terminal commands
- Common startup/runtime issues are visible from the app UI

## Milestone 6 — Packaging and Distribution

### Objective

Produce a distributable macOS application for nontechnical users.

### Deliverables

- Bundle Python runtime and dependencies
- Bundle static assets and backend code
- Preserve user-local runtime data across upgrades
- Create distributable `.app`
- Optionally add signed `.dmg` later

### Exit Criteria

- A clean macOS machine can run the app without installing Python manually

## Milestone 7 — Operational Hardening

### Objective

Make the macOS app maintainable and upgrade-safe.

### Deliverables

- App version metadata
- Upgrade-safe DB migrations
- Crash logging
- Better backend restart recovery
- Packaging build script
- Installation and support checklist

### Exit Criteria

- The macOS app can be updated without losing user data or browser/session state

## Implementation Order

1. Milestone 1 — Runtime Foundation
2. Milestone 2 — First-Launch Bootstrap
3. Milestone 3 — Backend Launcher
4. Milestone 4 — Native Wrapper Skeleton
5. Milestone 5 — App Mode Integration
6. Milestone 6 — Packaging and Distribution
7. Milestone 7 — Operational Hardening

## Current Step

Implement **Milestone 5 — App Mode Integration**:

1. app-bundle resource discovery
2. native menu actions
3. startup/runtime issue presentation cleanup
4. remove remaining dev-shell assumptions before packaging

## Current Status

- Milestone 1 — complete
- Milestone 2 — complete
- Milestone 3 — complete
- Milestone 4 — complete
- Milestone 5 — in progress
