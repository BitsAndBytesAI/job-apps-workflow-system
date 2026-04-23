# Job Apply Agent — Revised Implementation Plan

## Overview

Build a new Apply Agent that submits job applications automatically for eligible rows from the Applications page. The first supported application type is Ashby, using the ButterflyMX example URL as the initial target:

`https://butterflymx.com/careers/?ashby_jid=fa4e4dcb-a5e7-432a-a600-858848a21165&utm_source=source%3Dlinkedin`

The implementation should use Playwright for browser control, an ATS-specific adapter for Ashby, and Claude/Anthropic for custom answer generation and fallback page interpretation. It should not rely on screenshot-only Claude Vision for normal Ashby form filling.

**Approach:** Playwright + ATS adapter first + Claude-assisted custom answers + screenshot/DOM fallback  
**First ATS:** Ashby  
**Submit mode:** Auto-submit  
**Resume source:** Download generated PDF from Google Drive to a temporary local file per application  
**Provider:** Anthropic only for apply-agent AI work; no fallback to another LLM provider  

---

## Why The Screenshot-Only Vision Loop Is Not The Primary Design

The alternate plan relies on Claude Vision screenshots and asks Claude to return CSS selectors. That is not reliable enough for automatic submission because:

- Screenshots do not contain CSS selectors.
- Claude may identify a field visually but invent or guess selectors.
- Coordinate or visual-only clicking is brittle inside iframes and dynamic ATS forms.
- Upload controls, hidden file inputs, radio groups, validation errors, and submit states are easier to handle through Playwright DOM access.
- Auto-submitting applications has real-world consequences; deterministic automation should do the mechanical work whenever possible.

Claude Vision is still useful, but it should be used with a DOM/action map or as a fallback, not as the core Ashby automation engine.

---

## Final Architecture

### `JobApplyAgent`

Owns orchestration:

1. Select eligible jobs from the local DB.
2. Download each generated resume PDF from Google Drive to a temp file.
3. Detect the application system type.
4. Launch Playwright.
5. Delegate to the matching ATS adapter.
6. Save result status, screenshot path, error detail, and submitted timestamp.
7. Mark the job as applied only after a verified successful submission.

### `ATSDetector`

Detects application type from URL and page structure.

Initial detection:

- `ashby_jid` query param on company careers page → Ashby.
- `jobs.ashbyhq.com` URL → Ashby.
- Embedded iframe with `jobs.ashbyhq.com` → Ashby.

Future detection:

- Greenhouse
- Lever
- Workday
- Generic unknown page

### `AshbyApplyAdapter`

Handles known Ashby behavior deterministically:

1. Navigate to `apply_url`.
2. If the page embeds an Ashby iframe, switch into that frame.
3. Click the `Application` tab/link.
4. Extract the form field map.
5. Upload the generated resume PDF.
6. Fill known profile fields from applicant config.
7. Ask Claude to draft answers for custom questions.
8. Fill generated custom answers.
9. Submit the application.
10. Verify success state.
11. Capture a success screenshot.

### `ApplicationAnswerService`

Uses Anthropic to answer custom free-text questions from:

- Applicant profile
- Base resume content
- Generated resume content, if available
- Job title
- Company name
- Job description
- Question label/help text

This service should be used for fields like:

- Programming languages and years of experience
- Favorite AI tool and daily usage
- Company values examples
- Why this company/role
- Cover-letter style prompts

### `VisionFallbackPlanner`

Later fallback for unknown ATS pages. This should receive both:

- A screenshot
- A numbered DOM/action map

Claude should return `element_id`, not raw CSS selectors.

This fallback should be added after the Ashby adapter works end-to-end.

---

## Eligibility Rules

The Apply Agent should only consider jobs that meet all of these criteria:

- Job belongs to the active project.
- Job appears on the Applications page.
- `applied = false`.
- `resume_url` is present.
- A matching `ResumeArtifact` exists with `pdf_drive_file_id` or `pdf_drive_url`.
- `apply_url` is present.
- `score >= score_threshold`.
- Job is not marked as duplicate/filtered.

Default run limit should be small during testing:

- Manual dashboard run default: `1`.
- API may accept `limit`.
- Specific `job_ids` can override normal selection for testing, but still require resume/apply URL unless explicitly forced by a developer-only test mode.

---

## Applicant Profile Data

Add applicant profile fields to both the initial setup wizard and the Setup page. The setup wizard must collect this before dashboard access is considered complete.

Initial fields:

```python
class ApplicantProfileConfig(BaseModel):
    legal_name: str = ""
    preferred_name: str = ""
    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    portfolio_url: str = ""
    github_url: str = ""
    current_company: str = ""
    current_title: str = ""
    years_of_experience: str = ""
    address_line_1: str = ""
    address_line_2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = "United States"
    work_authorized_us: bool = True
    requires_sponsorship: bool = False
    compensation_expectation: str = ""
    sms_consent: bool = False
    custom_answer_guidance: str = ""
```

Notes:

- `legal_name`, `email`, `phone`, `linkedin_url`, location/address, authorization, sponsorship, and compensation are required for reliable auto-apply.
- `sms_consent` should default to `False`.
- `custom_answer_guidance` lets the user specify tone/preferences for AI-generated answers.

---

## Resume Handling

Do not persist every generated resume on the local hard drive.

Runtime behavior per application:

1. Resolve the generated resume PDF using `ResumeArtifact.pdf_drive_file_id`.
2. Fallback to parsing `Job.resume_url` only if no artifact ID exists.
3. Download the PDF from Google Drive into a temporary file.
4. Upload that temp file to the application form.
5. Delete the temp file after the job attempt completes.

Drive download utility requirements:

- If the Drive file is a binary PDF, use `files().get_media`.
- If the Drive file is a Google Doc, export it as `application/pdf`.
- Support URLs and raw file IDs.
- Return bytes and metadata, including filename and MIME type.

---

## Browser Automation Model

Initial implementation:

- Use bundled Playwright Firefox.
- Use `headless=False` because the user expects a browser window to open during application submission.
- Use an isolated non-persistent browser context for external ATS application pages.
- Do not reuse the LinkedIn browser profile for application submission.
- Save screenshots to app runtime storage, not the repository.

Possible later setting:

- `Run Apply Agent visibly` toggle.
- Once stable, allow `headless=True` for background mode.

---

## Ashby Adapter Flow

For the ButterflyMX/Ashby flow:

1. Navigate to the company careers `apply_url`.
2. Wait for the Ashby iframe or direct Ashby page.
3. Resolve the active Ashby frame:
   - `iframe[src*="jobs.ashbyhq.com"]`, or
   - current page if already on `jobs.ashbyhq.com`.
4. Click `Application`.
5. Wait for form controls.
6. Upload resume:
   - Prefer the specific resume field (`#_systemfield_resume`) if present.
   - Ignore the optional "Autofill from resume" upload unless explicitly needed.
7. Fill known fields:
   - Legal Name
   - Email
   - Phone
   - Location/current address
   - LinkedIn profile URL
   - Work authorization
   - Sponsorship
   - Compensation expectation
   - SMS consent
8. Extract unknown/custom questions.
9. Generate answers using `ApplicationAnswerService`.
10. Fill custom answers.
11. Submit application.
12. Detect success confirmation.
13. Capture final screenshot.
14. Update DB state.

---

## DOM/Action Map For Vision Fallback

For generic or uncertain pages, Claude should never be asked to invent selectors from screenshots. Build a DOM/action map first:

```json
[
  {
    "element_id": "el_001",
    "frame_id": "main",
    "tag": "input",
    "type": "text",
    "label": "Legal Name",
    "placeholder": "Type here...",
    "required": true,
    "selector": "[data-apply-agent-id='el_001']"
  },
  {
    "element_id": "el_002",
    "frame_id": "ashby_0",
    "tag": "input",
    "type": "file",
    "label": "Resume",
    "required": true,
    "selector": "[data-apply-agent-id='el_002']"
  }
]
```

Claude should return actions by `element_id`:

```json
{
  "actions": [
    {
      "action": "fill",
      "element_id": "el_001",
      "value": "Kirk Rohani",
      "field_name": "Legal Name",
      "reasoning": "Required name field."
    },
    {
      "action": "upload",
      "element_id": "el_002",
      "field_name": "Resume",
      "reasoning": "Required resume upload field."
    }
  ],
  "done": false,
  "error": null
}
```

Allowed actions:

- `click`
- `fill`
- `select`
- `check`
- `uncheck`
- `upload`
- `scroll`
- `submit`
- `done`
- `error`

The Playwright executor resolves `element_id` to the known selector and frame. Claude does not directly control selectors.

---

## Files To Create

### `src/job_apps_system/agents/job_apply.py`

- `JobApplyAgent(session)`
- `run(*, limit, job_ids, step_reporter, cancel_checker) -> JobApplySummary`
- `_eligible_jobs(limit, job_ids)`
- `_apply_to_job(job, resume_artifact)`
- `_download_resume_pdf(resume_artifact, job)`
- `_record_success(job, result)`
- `_record_failure(job, error)`

### `src/job_apps_system/agents/apply/ats_detector.py`

- `detect_ats_type(url, page=None) -> str`
- Initial values: `ashby`, `unknown`

### `src/job_apps_system/agents/apply/ashby_adapter.py`

- `AshbyApplyAdapter`
- `apply(page, job, applicant, resume_path, answer_service, cancel_checker) -> ApplyJobResult`
- `_resolve_ashby_frame(page)`
- `_click_application_tab(frame)`
- `_extract_field_map(frame)`
- `_fill_known_fields(frame, field_map, applicant)`
- `_answer_custom_fields(frame, field_map, answer_service)`
- `_submit(frame)`
- `_verify_success(frame, page)`
- `_capture_success_screenshot(page, job)`

### `src/job_apps_system/agents/apply/action_map.py`

- DOM field extraction helpers.
- Stable temporary element IDs.
- Frame-aware action references.

### `src/job_apps_system/services/application_answer_service.py`

- `generate_custom_answer(question, applicant, job, resume_text, constraints) -> str`
- Uses Anthropic.
- No fallback to OpenAI.

### `src/job_apps_system/schemas/apply.py`

- `ApplyRunRequest`
- `ApplyJobResult`
- `JobApplySummary`
- `ApplyField`
- `ApplyAction`
- `ApplyActionPlan`

### `src/job_apps_system/web/routes/apply.py`

- `POST /apply/start`
- `POST /apply/run`
- Optional `GET /apply/status/{run_id}` if existing run tracking does not cover enough detail.

### `src/job_apps_system/services/job_apply_runner.py`

- Background runner matching the existing agent runner pattern.
- Must support cancel requests.
- Must report status to dashboard run history.

---

## Files To Modify

### `src/job_apps_system/config/models.py`

- Add `ApplicantProfileConfig`.
- Add `applicant: ApplicantProfileConfig` to setup config/update models.
- Add apply-agent settings:
  - `apply_default_limit: int = 1`
  - `apply_headless: bool = False`
  - `apply_auto_submit: bool = True`

### `src/job_apps_system/web/templates/onboarding.html`

- Add applicant profile step.
- Persist applicant data into setup config.
- Wizard should resume from this step if incomplete.

### `src/job_apps_system/web/static/onboarding.js`

- Add applicant profile form handling.
- Validate required auto-apply fields.
- Do not allow onboarding completion if required applicant profile fields are missing.

### `src/job_apps_system/web/templates/setup.html`

- Add editable Applicant Profile section.
- Add Apply Agent settings section.

### `src/job_apps_system/web/static/setup.js`

- Wire `config.applicant.*`.
- Wire apply-agent settings.

### `src/job_apps_system/integrations/llm/anthropic_client.py`

Add methods:

- `generate_json(...)`
- `generate_with_vision(...)`

Requirements:

- Use Anthropic Messages API.
- Accept image content blocks for fallback/validation use.
- Support 120s timeout.
- Return text parsed by schema validators outside the client.

### `src/job_apps_system/integrations/google/drive.py`

Add:

- `download_drive_file(file_ref, *, session=None) -> tuple[bytes, dict]`
- `resolve_drive_file_metadata(file_ref, *, session=None) -> dict`

Requirements:

- Extract/normalize Drive ID from URL or raw ID.
- Download binary PDFs with `get_media`.
- Export Google Docs as PDF if needed.

### `src/job_apps_system/db/models/jobs.py`

Confirm or add fields if missing:

- `applied`
- `applied_at`
- `application_status`
- `application_error`
- `application_screenshot_path`

If we do not want to expand `jobs`, create a separate `ApplicationSubmission` model.

### `src/job_apps_system/db/models/applications.py`

Preferred model if current jobs table should stay lean:

- `id`
- `project_id`
- `job_id`
- `ats_type`
- `status`
- `submitted_at`
- `screenshot_path`
- `confirmation_text`
- `error`
- `steps_json`
- `created_at`
- `updated_at`

### `src/job_apps_system/main.py`

- Register apply routes.

### `src/job_apps_system/web/templates/dashboard.html`

- Add Apply Agent card.
- Show apply run status and failures.

### `src/job_apps_system/web/static/dashboard.js`

- Add Apply Agent trigger.
- Require Anthropic API key before manual run.
- Disable button while Apply Agent is running.
- Include Apply Agent in current status ordering.

---

## Implementation Phases

### Phase 1 — Applicant Profile And Settings

1. Add applicant profile config model.
2. Add onboarding wizard applicant profile step.
3. Add Setup page applicant profile section.
4. Add Apply Agent settings.
5. Validate required fields for auto-apply.

### Phase 2 — Resume Download And Anthropic Support

1. Add Drive file metadata/download support.
2. Download generated resume PDFs to temp files.
3. Add Anthropic JSON generation helper.
4. Add Anthropic vision helper for later fallback/validation.
5. Add `ApplicationAnswerService`.

### Phase 3 — Apply Agent Data Model

1. Add apply schemas.
2. Add application submission DB model or add application fields to jobs.
3. Add migrations if this repo uses migrations for schema changes.
4. Add job eligibility query.

### Phase 4 — Ashby Adapter

1. Detect Ashby URL/page.
2. Resolve embedded iframe.
3. Click Application tab.
4. Extract Ashby fields.
5. Fill known applicant fields.
6. Generate and fill custom answers.
7. Upload resume PDF.
8. Submit.
9. Verify success.
10. Capture screenshot.

### Phase 5 — Runner, Routes, Dashboard

1. Add `JobApplyAgent`.
2. Add background runner.
3. Add `/apply/start` and `/apply/run`.
4. Register routes.
5. Add Dashboard Apply Agent card.
6. Wire cancel behavior and run status.

### Phase 6 — Vision/DOM Fallback

1. Build DOM/action map extractor.
2. Add screenshot capture and resize.
3. Add Claude action-plan schema using `element_id`.
4. Add fallback planner for unknown forms.
5. Keep unknown-form auto-submit disabled until separately tested.

---

## Error Handling

- If Anthropic API key is missing, do not start the Apply Agent.
- If Google is not connected, fail before browser launch.
- If resume download fails, skip that job.
- If ATS type is unknown, skip in v1.
- If CAPTCHA/login wall appears, mark job failed with `captcha_or_login_wall`.
- If a required applicant field is missing, fail before browser launch.
- If a required form field cannot be mapped, mark job failed with field details.
- If submit succeeds but success state cannot be verified, save screenshot and mark as `needs_review`, not `applied=True`.
- Retry once for transient Playwright failures.
- Retry once for malformed Anthropic JSON.
- Always close browser context and delete temp resume files.
- Respect `cancel_checker()` between jobs and between major form phases.

---

## Success Detection

Do not rely only on Claude returning `done`.

Accept success if any deterministic signal is found:

- URL changes to a known Ashby submitted/confirmation state.
- Page contains clear confirmation text like `Application submitted`, `Thank you`, or `received your application`.
- Submit button disappears and confirmation panel appears.

After success:

- Capture screenshot.
- Store screenshot path.
- Set `jobs.applied = True`.
- Set `applied_at`.
- Store `application_status = submitted`.

If the state is ambiguous:

- Capture screenshot.
- Store `application_status = needs_review`.
- Do not set `applied = True`.

---

## Verification Plan

### Unit Tests

- ATS detection for ButterflyMX Ashby URL.
- ATS detection for direct `jobs.ashbyhq.com` URL.
- Eligibility query excludes missing resume/apply URL and below-threshold jobs.
- Drive ID extraction from Drive URLs.
- Anthropic action-plan parser rejects raw selectors without element IDs.
- Applicant profile validation catches missing required fields.

### Integration Tests

- Ashby adapter extracts the Application form from the ButterflyMX page.
- Ashby field mapper identifies:
  - Legal Name
  - Email
  - Phone
  - Resume
  - LinkedIn URL
  - Work authorization
  - Compensation
  - Custom text questions
- Resume PDF temp download/upload path works.

### Manual Verification

1. Build macOS app: `bash scripts/build-macos-app.sh`.
2. Launch app.
3. Fill Applicant Profile in onboarding/setup.
4. Ensure one Applications row has:
   - Ashby `apply_url`
   - generated resume PDF URL/artifact
   - score above threshold
   - `applied = false`
5. Run Apply Agent with limit `1`.
6. Confirm browser opens visibly.
7. Confirm Ashby form is filled.
8. Confirm application submits automatically.
9. Confirm screenshot is stored.
10. Confirm job row shows `applied = true`.
11. Confirm run history/status records each major phase.

---

## Initial Product Constraints

- V1 supports Ashby only.
- V1 auto-submits only for known supported ATS adapters.
- V1 skips unknown ATS pages.
- V1 does not solve CAPTCHA.
- V1 does not use LinkedIn browser profile for application submission.
- V1 downloads resumes only to temp files and deletes them after use.
- V1 uses Anthropic only for Apply Agent AI work.

This design keeps the first implementation reliable for Ashby while preserving the path to broader Claude Vision/DOM fallback support later.
