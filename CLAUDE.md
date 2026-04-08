# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Local-first job application workflow orchestrator replacing n8n flows. Python/FastAPI app that scrapes LinkedIn jobs, scores them via LLMs, generates tailored resumes, enriches contacts, sends outreach, and handles interview follow-ups — all coordinated through a web dashboard with Google Sheets/Docs/Drive as the operational layer.

## Development Commands

```bash
# Bootstrap (create venv, install in editable mode)
bash scripts/bootstrap.sh

# Run dev server with hot-reload (http://127.0.0.1:8000)
bash scripts/run-local.sh
# Or directly:
source .venv/bin/activate && uvicorn job_apps_system.main:app --reload
```

No test suite, linter, or formatter is configured yet. Tests directory exists at `tests/unit/` and `tests/integration/` but is empty.

## Architecture

### Entry Point & Web Layer

`src/job_apps_system/main.py` — FastAPI app factory (`create_app()`) with lifespan that calls `init_db()` on startup. Routes are organized as FastAPI `APIRouter` instances mounted at prefixes:

- `/` — Dashboard (HTML + vanilla JS polling for run status)
- `/setup/` — Config page + Google OAuth flow + LinkedIn browser launch
- `/runs/` — Workflow run history + manual agent triggers
- `/jobs/` — Job listing with per-row actions
- `/scoring/`, `/resumes/`, `/interviews/` — Feature-specific routes

Frontend is Jinja2 templates + vanilla JS (no framework). Static files served from `web/static/`.

### Agent System

Agents in `agents/` follow a consistent pattern: constructor takes a SQLAlchemy `Session`, loads config via `load_setup_config()`, and exposes a `run()` method that accepts an optional `step_reporter` callback for real-time UI updates. Each returns a Pydantic summary schema.

**Execution order (orchestrator sequence):**
1. `job_intake` — Scrape LinkedIn, dedupe, filter, append to sheets
2. `job_scoring` — Score jobs with Anthropic Claude
3. `resume_generation` — Tailor resumes via LLM, create Google Docs, export PDFs
4. `contact_enrichment` — (stub) Enrich contacts via Anymailfinder
5. `outreach_generation` — (stub) Draft per-recipient emails
6. `outreach_sending` — (stub) Send via Gmail
7. `interview_followup_setup` — (stub) Process recordings → transcripts
8. `interview_followup_sending` — (stub) Send thank-you emails

### Configuration System (Three Layers)

1. **Environment variables** (`config/settings.py`) — Pydantic Settings loaded from `.env`: app host/port, database URL, Google OAuth client config
2. **Database-persisted config** (`app_settings` table, JSON key-value) — Setup config, OAuth state, field validations. CRUD via `services/setup_config.py`
3. **OS keychain** (`config/secrets.py` via `keyring`) — API keys (OpenAI, Anthropic, Anymailfinder), Google OAuth tokens

### Database

SQLite at `data/app.db`. SQLAlchemy 2.0 with `mapped_column` annotations. Alembic is scaffolded but unused — schema changes are handled by programmatic migrations in `db/session.py:init_db()`.

Key models: `Job` (composite unique on `project_id + external_id`), `ResumeArtifact`, `WorkflowRun`, `AppSetting`. All agent-related tables use `project_id` for multi-project isolation.

### Integration Patterns

- **Google APIs** (`integrations/google/`) — OAuth 2.0 with PKCE, token refresh. Covers Sheets (read/write/append rows), Docs (create/edit/export), Drive (upload/share/permissions)
- **LinkedIn** (`integrations/linkedin/`) — Playwright with persistent Chrome profile at `data/browser-profiles/linkedin/`. Scrapes job cards by intercepting `/voyager/api/` network requests
- **LLM clients** (`integrations/llm/`) — Custom HTTP via `urllib` (no SDK). Anthropic for scoring/outreach, OpenAI for resume tailoring/transcription. Prompt templates in `integrations/llm/prompts/`

### Data Flow

Google Sheets is the source of truth for job tracking. `services/sheet_sync.py` handles bidirectional sync between the EM jobs sheet and the local `jobs` table. Agents read from/write to both the local DB and Sheets. Resume generation creates Google Docs and uploads PDFs to Drive, storing URLs back in both Sheets and local DB.

## Key Reference Files

- `build-spec.md` — Comprehensive system design blueprint (the authoritative spec)
- `requirements.md` — Feature requirements
- `config/models.py` — Pydantic schemas for all configuration (GoogleConfig, LinkedInConfig, ProviderModelsConfig, AppBehaviorConfig)
- `db/session.py` — Database init + programmatic migrations
