# Job Applications Workflow System Build Spec

## 1. Technical Baseline

### Runtime

- Python `3.12+`
- macOS primary target
- one local process manager for app + scheduler

### Main stack

- `FastAPI` for backend APIs and local web UI server
- `Jinja2` + HTMX or lightweight vanilla JS for the setup/control UI
- `Playwright` for LinkedIn browser automation
- `SQLAlchemy` + `Alembic` for SQLite persistence
- Google official Python clients for Sheets / Docs / Drive / Gmail
- OpenAI Python SDK
- Anthropic Python SDK
- structured logging with JSON output

## 2. Repository / Folder Structure

```text
job-apps-workflow-system/
  README.md
  requirements.md
  build-spec.md
  pyproject.toml
  .env.example
  .gitignore
  alembic.ini
  migrations/
    versions/
  data/
    app.db
    logs/
    browser-profiles/
      linkedin/
    cache/
  src/
    job_apps_system/
      __init__.py
      main.py
      config/
        __init__.py
        settings.py
        models.py
        secrets.py
        resource_ids.py
      db/
        __init__.py
        base.py
        session.py
        models/
          settings.py
          workflow_runs.py
          jobs.py
          resumes.py
          contacts.py
          emails.py
          interviews.py
          artifacts.py
      web/
        __init__.py
        routes/
          setup.py
          dashboard.py
          runs.py
          jobs.py
          interviews.py
        templates/
          layout.html
          setup.html
          dashboard.html
          runs.html
          jobs.html
          interviews.html
        static/
          app.css
          app.js
      integrations/
        google/
          oauth.py
          sheets.py
          docs.py
          drive.py
          gmail.py
        linkedin/
          browser.py
          auth.py
          scraper.py
          parsers.py
        llm/
          openai_client.py
          anthropic_client.py
          prompts/
            scoring.py
            resume.py
            outreach.py
            thank_you.py
        enrichment/
          anymailfinder.py
      services/
        orchestrator.py
        row_state.py
        dedupe.py
        audit.py
        resource_normalizer.py
        sheet_sync.py
      agents/
        job_intake.py
        resume_generation.py
        contact_enrichment.py
        outreach_generation.py
        outreach_sending.py
        interview_followup_setup.py
        interview_followup_sending.py
      schemas/
        jobs.py
        resumes.py
        contacts.py
        emails.py
        interviews.py
      scheduler/
        cron.py
        triggers.py
      cli/
        run_once.py
        sync_google.py
        launch_linkedin_browser.py
  scripts/
    bootstrap.sh
    run-local.sh
    migrate.sh
  tests/
    unit/
    integration/
```

## 3. Configuration Model

### Normalization rules

Every Google field in the UI must accept:

- raw ID
- full Docs URL
- full Sheets URL
- full Drive file URL
- full Drive folder URL

Normalize all of them through `resource_normalizer.py`.

### Key config objects

#### App settings

- scheduler enabled
- schedule expression or interval
- dry run enabled
- manual approval toggles
- default send BCC

#### Provider settings

- OpenAI API key ref
- Anthropic API key ref
- Anymailfinder API key ref
- model IDs per task

#### Google settings

- OAuth token metadata
- EM jobs sheet ID
- processed jobs sheet ID
- Job Emails Sent sheet ID
- Interview Emails sheet ID
- base resume doc ID
- resume docs folder ID
- resume PDFs folder ID
- interview recordings folder ID
- interview transcripts folder ID

#### LinkedIn settings

- browser profile path
- auth status
- search URLs list
- optional scrape caps

## 4. Database Schema

### `app_settings`

| column | type | notes |
|---|---|---|
| `key` | text pk | setting key |
| `value_json` | text | JSON-encoded value |
| `updated_at` | datetime | audit timestamp |

### `secret_refs`

| column | type | notes |
|---|---|---|
| `id` | text pk | internal key |
| `provider` | text | `openai`, `anthropic`, `anymailfinder`, `google` |
| `secret_name` | text | keychain handle |
| `created_at` | datetime | |
| `updated_at` | datetime | |

### `workflow_runs`

| column | type | notes |
|---|---|---|
| `id` | text pk | uuid |
| `trigger_type` | text | `schedule`, `manual`, `event` |
| `status` | text | `running`, `succeeded`, `failed`, `partial` |
| `started_at` | datetime | |
| `finished_at` | datetime nullable | |
| `summary_json` | text | counters + errors |

### `workflow_run_steps`

| column | type | notes |
|---|---|---|
| `id` | text pk | uuid |
| `run_id` | text fk | workflow run |
| `agent_name` | text | agent identifier |
| `entity_type` | text | `job`, `interview`, `email` |
| `entity_id` | text | source id |
| `status` | text | `queued`, `running`, `succeeded`, `failed`, `skipped` |
| `input_json` | text | |
| `output_json` | text | |
| `error_text` | text nullable | |
| `started_at` | datetime | |
| `finished_at` | datetime nullable | |

### `jobs`

Canonical local representation of the EM jobs sheet.

| column | type |
|---|---|
| `id` | text pk |
| `tracking_id` | text nullable |
| `company_name` | text |
| `job_title` | text |
| `job_description` | text |
| `apply_url` | text |
| `company_url` | text nullable |
| `score` | integer nullable |
| `applied` | boolean default false |
| `resume_url` | text nullable |
| `job_poster_name` | text nullable |
| `job_poster_title` | text nullable |
| `job_poster_linkedin` | text nullable |
| `job_poster_email` | text nullable |
| `job_poster_email_sent` | boolean default false |
| `cto_name` | text nullable |
| `cto_title` | text nullable |
| `cto_email` | text nullable |
| `cto_email_sent` | boolean default false |
| `hr_name` | text nullable |
| `hr_title` | text nullable |
| `hr_email` | text nullable |
| `hr_email_sent` | boolean default false |
| `created_time` | datetime nullable |
| `source_payload_json` | text |
| `last_sheet_sync_at` | datetime nullable |
| `created_at` | datetime |
| `updated_at` | datetime |

### `processed_jobs`

Tracks all seen jobs / dedupe decisions.

| column | type |
|---|---|
| `id` | text pk |
| `job_id` | text |
| `source` | text |
| `seen_at` | datetime |
| `decision` | text |
| `reason` | text nullable |

### `resumes`

| column | type |
|---|---|
| `id` | text pk |
| `job_id` | text fk |
| `base_resume_doc_id` | text |
| `tailored_doc_id` | text |
| `tailored_doc_url` | text |
| `pdf_drive_file_id` | text |
| `pdf_drive_url` | text |
| `status` | text |
| `prompt_version` | text |
| `generated_at` | datetime |
| `updated_at` | datetime |

### `contact_enrichments`

| column | type |
|---|---|
| `id` | text pk |
| `job_id` | text fk |
| `role` | text |
| `provider` | text |
| `lookup_input_json` | text |
| `result_json` | text |
| `status` | text |
| `attempt_count` | integer |
| `last_attempt_at` | datetime nullable |

### `email_deliveries`

| column | type |
|---|---|
| `id` | text pk |
| `job_id` | text nullable |
| `interview_row_id` | text nullable |
| `email_type` | text |
| `recipient_role` | text nullable |
| `recipient_name` | text |
| `recipient_email` | text |
| `subject` | text |
| `body` | text |
| `status` | text |
| `provider_message_id` | text nullable |
| `sent_at` | datetime nullable |
| `source_sheet` | text |
| `source_row_key` | text |

### `interview_rows`

Canonical local representation of the Interview Emails sheet.

| column | type |
|---|---|
| `id` | text pk |
| `job_id` | text nullable |
| `company_name` | text |
| `person_name` | text |
| `email` | text nullable |
| `backup_person` | text nullable |
| `backup_person_email` | text nullable |
| `linkedin` | text nullable |
| `position` | text |
| `resume_url` | text |
| `discussion` | text nullable |
| `job_description` | text |
| `transcription_doc_url` | text nullable |
| `transcription_edited` | boolean default false |
| `email_contents` | text nullable |
| `email_sent` | boolean default false |
| `created_date` | datetime nullable |
| `source_payload_json` | text |
| `updated_at` | datetime |

### `artifacts`

| column | type |
|---|---|
| `id` | text pk |
| `entity_type` | text |
| `entity_id` | text |
| `artifact_type` | text |
| `google_file_id` | text nullable |
| `url` | text nullable |
| `metadata_json` | text |
| `created_at` | datetime |

## 5. Google Sheet Mappings

### EM jobs sheet

Google sheet: `New EM Job Applications`

| Sheet Column | Local Field |
|---|---|
| `Applied` | `jobs.applied` |
| `Resume URL` | `jobs.resume_url` |
| `Created Time` | `jobs.created_time` |
| `Score` | `jobs.score` |
| `id` | `jobs.id` |
| `trackingid` | `jobs.tracking_id` |
| `Company Name` | `jobs.company_name` |
| `Job TItle` | `jobs.job_title` |
| `Job Description` | `jobs.job_description` |
| `Apply URL` | `jobs.apply_url` |
| `Company URL` | `jobs.company_url` |
| `Job Poster` | `jobs.job_poster_name` |
| `Job Poster Title` | `jobs.job_poster_title` |
| `Job Poster LinkedIn` | `jobs.job_poster_linkedin` |
| `Job Poster Email` | `jobs.job_poster_email` |
| `Job Poster Email Sent` | `jobs.job_poster_email_sent` |
| `CTO` | `jobs.cto_name` |
| `CTO Title` | `jobs.cto_title` |
| `CTO Email` | `jobs.cto_email` |
| `CTO Email Sent` | `jobs.cto_email_sent` |
| `HR` | `jobs.hr_name` |
| `HR Title ` | `jobs.hr_title` |
| `HR Email` | `jobs.hr_email` |
| `HR Email Sent` | `jobs.hr_email_sent` |

### Processed jobs sheet

Map the processed jobs workflow export into:

| Sheet Column | Local Field |
|---|---|
| `id` | `processed_jobs.job_id` |
| `trackingid` | `jobs.tracking_id` |
| `Company Name` | `jobs.company_name` |
| `Job Title` / equivalent | `jobs.job_title` |
| `Processed At` / equivalent | `processed_jobs.seen_at` |
| `Decision` / equivalent | `processed_jobs.decision` |

Final exact mapping should be confirmed from that sheet header during implementation.

### Job Emails Sent sheet

Google sheet: `Job Emails Sent`

| Sheet Column | Local Field |
|---|---|
| `Date Sent` | `email_deliveries.sent_at` |
| `Company` | `jobs.company_name` / `interview_rows.company_name` |
| `Email Type` | `email_deliveries.email_type` |
| `Email Address` | `email_deliveries.recipient_email` |
| `Name` | `email_deliveries.recipient_name` |
| `Title` | derived title at send time |
| `Email Content` | `email_deliveries.body` |

### Interview Emails sheet

Google sheet: `Interview Emails`

| Sheet Column | Local Field |
|---|---|
| `ID` | `interview_rows.id` |
| `Email Sent` | `interview_rows.email_sent` |
| `Created Date` | `interview_rows.created_date` |
| `Company Name` | `interview_rows.company_name` |
| `Person Name` | `interview_rows.person_name` |
| `Email` | `interview_rows.email` |
| `Backup Person` | `interview_rows.backup_person` |
| `Backup Person Email` | `interview_rows.backup_person_email` |
| `LinkedIn` | `interview_rows.linkedin` |
| `Position` | `interview_rows.position` |
| `Resume` | `interview_rows.resume_url` |
| `Discussion` | `interview_rows.discussion` |
| `Job Description` | `interview_rows.job_description` |
| `Transcription Doc` | `interview_rows.transcription_doc_url` |
| `Transcription Editted` | `interview_rows.transcription_edited` |
| `Email Contents` | `interview_rows.email_contents` |

## 6. Agent Responsibilities

### `job_intake`

Responsibilities:

- open local authenticated LinkedIn browser profile
- scrape all configured search URLs
- normalize jobs
- dedupe against local DB and processed sheet
- apply title filters
- score fit against candidate profile
- append accepted jobs to EM jobs sheet
- append processed rows to processed jobs sheet
- upsert local `jobs` / `processed_jobs`

### `resume_generation`

Responsibilities:

- find EM jobs rows with missing `Resume URL`
- load base resume doc
- tailor content with LLM
- create tailored Google Doc
- export to PDF
- upload/store in Drive
- update EM jobs sheet and local `resumes`

### `contact_enrichment`

Responsibilities:

- find applied jobs with missing contact fields
- poster lookup by `Job Poster LinkedIn`
- HR lookup by company + `decision_maker_category=hr`
- CTO lookup by company + `decision_maker_category=it`
- update only found values
- never blank unrelated fields

### `outreach_generation`

Responsibilities:

- create final per-recipient email subject/body
- support per-role prompting:
  - poster
  - cto
  - hr
- use resume URL, job description, company, and contact role context
- persist draft in `email_deliveries`

### `outreach_sending`

Responsibilities:

- send unsent outreach drafts via Gmail API
- update sent flags in EM jobs sheet
- append audit rows to Job Emails Sent sheet
- mark local `email_deliveries` sent

### `interview_followup_setup`

Responsibilities:

- watch interview recordings folder
- download audio file
- transcribe
- create transcript doc
- identify related job id and interviewer from filename
- resolve interviewer email if needed
- append Interview Emails row
- upsert local `interview_rows`

### `interview_followup_sending`

Responsibilities:

- detect interview rows ready for thank-you send
- load resume content
- merge with transcript discussion and job description
- generate structured thank-you email
- send via Gmail
- update interview row
- append audit row to Job Emails Sent

### `orchestrator`

Responsibilities:

- run agents in deterministic order
- enforce idempotency
- maintain per-run logs and step logs
- skip work already complete
- retry safe failures
- surface failures in UI

## 7. Orchestrator Order

One orchestrator cycle should run this order:

1. `job_intake`
2. `resume_generation`
3. `contact_enrichment`
4. `outreach_generation`
5. `outreach_sending`
6. `interview_followup_setup`
7. `interview_followup_sending`

Notes:

- `interview_followup_setup` can also be triggered by a Drive watcher event and then enqueue work immediately.
- `outreach_generation` and `outreach_sending` may be combined in phase 1 if approval is disabled.

## 8. Setup Page API Endpoints

### Config

- `GET /api/settings`
- `POST /api/settings`
- `POST /api/settings/validate`

### Google

- `GET /api/google/auth/status`
- `POST /api/google/auth/start`
- `GET /api/google/auth/callback`
- `POST /api/google/resources/resolve`
- `POST /api/google/resources/validate`

### LinkedIn

- `GET /api/linkedin/status`
- `POST /api/linkedin/browser/launch`
- `POST /api/linkedin/browser/check-session`

### Runs

- `POST /api/runs/orchestrator`
- `POST /api/runs/agent/{agent_name}`
- `GET /api/runs`
- `GET /api/runs/{run_id}`

### Rows / entities

- `GET /api/jobs`
- `POST /api/jobs/{job_id}/resume/regenerate`
- `POST /api/jobs/{job_id}/contacts/enrich`
- `POST /api/jobs/{job_id}/outreach/regenerate`
- `POST /api/interviews/{interview_id}/send-thank-you`

## 9. Recommended Model Assignments

As of `2026-03-29`, recommended defaults:

- job scoring: `gpt-5-mini`
- complex orchestration helpers: `gpt-5.2`
- resume tailoring: `claude-sonnet-4-20250514`
- premium resume rewrite option: `claude-opus-4-1-20250805`
- outreach generation: `claude-sonnet-4-20250514`
- thank-you generation: `claude-sonnet-4-20250514`
- transcription: `gpt-4o-transcribe`

These must remain user-editable in setup.

## 10. Implementation Phases

### Phase 1

- project bootstrap
- config + keychain + SQLite
- setup UI
- Google OAuth
- LinkedIn browser profile launcher

### Phase 2

- job intake + scoring
- EM jobs sheet / processed jobs sheet sync

### Phase 3

- resume generation via Google Docs + Drive

### Phase 4

- contact enrichment

### Phase 5

- outreach generation + sending + auditing

### Phase 6

- interview recording intake
- transcription doc creation
- interview sheet sync

### Phase 7

- thank-you email generation + sending

### Phase 8

- retries
- dashboards
- per-row controls
- packaging for other users

## 11. Packaging and Distribution Plan

### Goal

Ship a macOS distribution that does not require the user to install Python or manage dependencies manually.

### Recommended packaging strategy

Phase 2 packaging target:

- package the backend and local web app into a macOS app bundle
- embed the Python runtime
- embed application dependencies
- include first-run setup / bootstrap logic

Recommended first implementation:

- `PyInstaller` for Python runtime bundling
- app launcher script that:
  - starts the local FastAPI server
  - opens the local web UI in the default browser
  - starts the background scheduler/orchestrator

Optional later upgrade:

- wrap the local web UI in a desktop shell such as `Tauri` for a more polished app shell

### Packaged app responsibilities

The packaged app must:

- detect or create the local data directory
- initialize SQLite migrations on first run
- initialize browser profile directories
- validate Playwright browser availability
- guide the user through first-run setup
- allow restart of background services from the UI

### Suggested app-local directories

Use user-local application directories instead of storing runtime state beside the app bundle.

Example target layout:

```text
~/Library/Application Support/JobAppsWorkflowSystem/
  app.db
  logs/
  browser-profiles/
    linkedin/
  cache/
  exports/
```

### First-run bootstrap checks

On first run, the packaged app should verify:

- writable data directory exists
- database initialized and migrated
- Playwright browser binaries available
- Google OAuth config is ready
- required provider API keys are present or missing state is clearly shown

### Update strategy

Packaged updates must preserve:

- SQLite database
- browser profile data
- Google OAuth tokens
- UI configuration
- logs and audit history

### Operational constraints

The packaged app should assume:

- user keeps the Mac powered on during scheduled runs
- app may need login-item / background launch support later
- browser automation remains local and uses the dedicated LinkedIn profile

### Distribution artifacts

Initial supported artifact targets:

- `.app` bundle for direct distribution
- optionally a signed `.dmg` installer later

### Packaging work items

- create packaging build script
- define runtime entrypoint
- define app data directory policy
- add first-run bootstrap sequence
- add packaged-mode logging and crash capture
- test on a clean macOS machine with no Python installed

## 12. Known Cleanup From Existing n8n System

- Rotate exposed Anymailfinder API keys.
- Rotate exposed Apify token from the workflow export.
- Remove hardcoded workflow text from n8n into configurable prompt templates.
- Remove poster workflow bug referencing a missing `Google Sheets Trigger`.
- Stop writing blank CTO / HR values from the poster enrichment path.
