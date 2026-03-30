# Job Applications Workflow System Requirements

## Purpose

Build a portable, local-first job application operations system that replaces the current `n8n` workflows with a single orchestrated application. The system must run on a user's machine, expose a local setup/control UI, persist state locally, and use Google services, LinkedIn browser automation, LLM APIs, and email delivery to execute the full application workflow.

## Product Goals

- Replace all current `n8n` workflows with one local application.
- Eliminate separate cron schedules for each stage.
- Use one orchestrator entrypoint and row/state-driven downstream execution.
- Preserve the current Google Sheets / Docs / Drive operational model.
- Make the system portable for other users without requiring custom cloud infrastructure.
- Centralize secrets, API keys, browser auth, and workflow settings in a local setup page.

## Core User Flows

### 1. Initial setup

The user must be able to:

- Open a local setup page on `localhost`.
- Connect Google via OAuth.
- Enter OpenAI, Anthropic, and Anymailfinder API keys.
- Enter either full Google resource URLs or raw Google IDs.
- Launch the LinkedIn automation browser profile and sign in manually once.
- Configure one or more LinkedIn job search URLs.
- Configure thresholds, send policies, and optional approval gates.

### 2. Daily orchestrator run

One scheduled orchestrator run must:

- scrape new LinkedIn jobs
- score jobs against the candidate background
- append accepted jobs to the EM jobs sheet
- append all seen jobs to the processed jobs sheet
- generate missing resumes for newly accepted jobs
- enrich contacts for applied jobs
- generate and send post-application outreach emails
- process new interview recordings and create transcript docs / interview rows
- generate and send thank-you emails when transcript editing is complete

### 3. Manual controls

The UI must also allow:

- `Run now` for the whole orchestrator
- `Run now` for a single agent on a single row
- `Retry failed step`
- `Regenerate resume`
- `Regenerate outreach email draft`
- `Regenerate thank-you email draft`
- `Open linked Google Doc / Drive file / Sheet row`

## Execution Model

### Single scheduler

The system must use one scheduler entrypoint only.

- Default cadence: configurable in the UI
- Recommended default: every `25` minutes
- The scheduler invokes the orchestrator
- Downstream agents must be triggered by state, not by independent cron schedules

### State-driven agents

The orchestrator must decide work from row state and local DB state.

Examples:

- A resume is generated when an EM jobs row has no `Resume URL`.
- Contact enrichment runs when `Applied == Y` and contact fields are missing.
- Outreach sends only when:
  - `Applied == Y`
  - contact email exists
  - corresponding `... Email Sent != Y`
- Thank-you send runs only when:
  - interview row exists
  - `Transcription Editted == Y`
  - `Email Sent != Y`

## Authentication Requirements

### Google

The system must support Google OAuth from the local setup page for:

- Google Sheets
- Google Docs
- Google Drive
- Gmail

The app must be able to:

- create missing Sheets / Docs / Drive folders when requested
- read and write existing resources when URLs or IDs are provided
- validate configured resources during setup

### LinkedIn

The system must not rely on LinkedIn OAuth.

Instead, it must:

- use a persistent local Playwright browser profile
- allow the user to open that profile from the setup page
- require manual LinkedIn login once in that profile
- verify whether the LinkedIn session is currently authenticated

The system must not require the Chrome cookie extension in the local-only architecture.

### API keys

The setup page must collect and manage:

- OpenAI API key
- Anthropic API key
- Anymailfinder API key

Secrets must not be stored in plain text workflow JSON.

Preferred storage:

- OS keychain for secret values
- local DB only for secret metadata / references

## AI Model Requirements

The system must support configurable model selection per task from the setup page.

Recommended defaults as of `2026-03-29`:

- OpenAI scoring / structured extraction: `gpt-5-mini`
- OpenAI complex reasoning / orchestration helpers: `gpt-5.2`
- OpenAI transcription: `gpt-4o-transcribe`
- Anthropic default generation: `claude-sonnet-4-20250514`
- Anthropic premium generation: `claude-opus-4-1-20250805`

The UI must allow overriding model IDs without code changes.

## Workflow Requirements

### A. Job intake and scoring

Must:

- use one or more LinkedIn jobs search URLs
- scrape jobs from LinkedIn with Playwright in the local authenticated browser profile
- deduplicate against the processed jobs sheet and local DB
- filter out excluded titles like director roles
- score fit against the candidate resume/background
- write accepted jobs to the EM jobs sheet
- write all processed jobs to the processed jobs sheet

### B. Resume generation

Must:

- read EM jobs rows missing `Resume URL`
- load the base resume from Google Docs
- generate a job-specific tailored resume
- create a Google Doc for the tailored resume
- preserve a user-editable Google Doc intermediate
- export that Google Doc to PDF
- store the final PDF in Google Drive
- write the final Drive URL back to the EM jobs sheet

### C. Contact enrichment

Must:

- run for EM jobs rows where `Applied == Y`
- enrich:
  - Job Poster by LinkedIn URL when available
  - CTO by company decision-maker lookup
  - HR by company decision-maker lookup
- update only fields that were actually found
- avoid blanking unrelated columns

### D. Post-application outreach

Must:

- detect applicable rows from EM jobs sheet state
- generate per-contact outreach content
- send via Gmail API
- update sent flags in the EM jobs sheet
- append audit rows to the Job Emails Sent sheet

### E. Interview transcript setup

Must:

- watch a Google Drive folder for new interview recordings
- download audio
- transcribe audio
- create a Google Doc transcript
- attach related EM job metadata
- resolve interviewer email where possible
- append a row to the Interview Emails sheet

### F. Thank-you emails

Must:

- detect interview rows where transcript editing is complete
- load the associated resume content
- generate a thank-you email using transcript discussion + resume + job description
- send via Gmail API
- update the Interview Emails row
- append an audit row to the Job Emails Sent sheet

## Setup Page Requirements

The local setup page must include these sections.

### 1. Google

- `Connect Google`
- `Disconnect Google`
- fields that accept either URL or raw ID for:
  - EM jobs sheet
  - processed jobs sheet
  - Job Emails Sent sheet
  - Interview Emails sheet
  - base resume Google Doc
  - resume docs folder
  - resume PDFs Drive folder
  - interview recordings folder
  - interview transcript docs folder

### 2. LinkedIn

- browser profile path
- launch automation browser
- verify login status
- add/edit/remove LinkedIn search URLs

### 3. AI Providers

- OpenAI API key
- OpenAI model IDs
- Anthropic API key
- Anthropic model IDs
- scoring threshold

### 4. Enrichment / Delivery

- Anymailfinder API key
- Gmail sender identity
- BCC address
- send enable/disable toggles
- dry-run mode
- per-stage approval toggles

### 5. Scheduling / Operations

- orchestrator enabled toggle
- cron / interval config
- run history
- failed work queue
- manual re-run controls

## Non-Functional Requirements

- Local-first: must run on macOS without cloud infrastructure.
- Portable: should be packageable for other users later.
- Idempotent: repeated runs must not duplicate rows or send duplicate emails.
- Observable: every stage must emit structured logs and audit records.
- Recoverable: failed stages must be retryable without rerunning the whole system.
- Configurable: all external IDs, URLs, provider choices, and model IDs must be editable from UI.

## Distribution Requirements

The system must support two operating modes.

### Phase 1: developer mode

- run as a local Python application
- suitable for rapid iteration and internal use
- may require local Python and development dependencies

### Phase 2: packaged end-user mode

The system must be distributable to nontechnical macOS users without requiring them to install Python manually.

The packaged distribution must:

- bundle the Python runtime and application dependencies
- bundle Playwright browser automation dependencies or install them during first-run setup
- launch the local web UI automatically
- start and manage the background orchestrator locally
- provide a standard macOS app experience
- persist user config, browser profile data, logs, and SQLite data in user-local application directories

The packaged distribution must not require the user to:

- install Python
- run `pip`
- clone a git repository
- install Playwright manually from the terminal

### Packaging expectations

The target packaging outcome is:

- a macOS app bundle and/or installer
- first-run setup wizard
- guided dependency/bootstrap checks
- upgrade-safe local data directory

### End-user setup expectations

For packaged users, the expected setup flow is:

- open the installed app
- complete Google OAuth
- launch the LinkedIn automation browser and sign in once
- paste API keys
- paste or select Google resource URLs / IDs
- enable the orchestrator

Users will still need:

- their own Google account access
- their own LinkedIn login
- their own API keys where applicable

## Security Requirements

- Rotate exposed Apify and Anymailfinder tokens from the original workflow exports.
- Never store raw secrets in source-controlled files.
- Store secrets in OS keychain where possible.
- Encrypt any local token cache that must exist on disk.
- Minimize Google OAuth scopes to the required services.

## Acceptance Criteria

The system is acceptable when:

- one local setup flow can connect Google and configure all required resources
- one local LinkedIn browser profile can be authenticated and reused
- one orchestrator run can process the full end-to-end system without separate cron workflows
- resumes are created as Google Docs, exported to PDF, stored in Drive, and linked back to the EM jobs sheet
- applied rows can be enriched with poster / CTO / HR contacts
- outreach emails can be sent and audited
- interview recordings can create transcript docs and interview rows
- thank-you emails can be generated and sent from edited transcript rows
- all API keys and model IDs are user-configurable from the setup page
- the application can be packaged for macOS users without requiring a preinstalled Python environment
