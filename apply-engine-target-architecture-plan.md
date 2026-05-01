# Apply Engine Target Architecture Plan

## Purpose

This plan defines the next architecture for the job application submission engine. The goal is to move from a mostly generic AI browser-control loop toward a reliable ATS-aware system:

- Playwright remains the browser automation layer.
- Known ATSs are handled by deterministic adapters.
- A shared form engine owns browser primitives and validation.
- The LLM is used for ambiguous field classification and answer generation, not for routine page control on known ATSs.
- The generic AI browser loop remains available only as fallback for unknown or unsupported sites.

The main reliability issue to solve is the current pattern where the LLM fills fields, submits, then page mutations, CAPTCHA challenges, validation errors, or file-upload clearing create reactive patches. Known ATS flows should instead be modeled as explicit state machines with pre-submit validation and post-verification recovery.

## Current State

The current apply system already has useful pieces:

- `JobApplyAgent` resolves job records, resume artifacts, browser profiles, credentials, and ATS type.
- Playwright controls the browser and supports persistent apply profiles.
- Specific adapters exist for some ATSs.
- `AiBrowserApplyLoop` can observe DOM fields/buttons, ask the LLM for actions, execute actions, detect some blockers, and hand off for manual completion.
- CAPTCHA/manual verification can pause the run and resume after user action.
- The latest Lever work introduced a dedicated adapter, pre-submit resume refresh, submit consent handling, and some compliance correction.

The current weaknesses are:

- The generic AI loop still carries too much cross-ATS behavior.
- ATS-specific patches can leak into generic logic.
- Known ATSs do not all have explicit flow states.
- Form primitives are scattered across adapters and the AI loop.
- Required-field validation is mostly reactive after submit rather than deterministic before submit.
- CAPTCHA/manual-resume behavior is inconsistent across ATSs.
- We do not have enough local fixture tests for real ATS page behavior.

## Target Principles

1. Known ATSs should be deterministic by default.
2. AI should classify and infer, but code should execute.
3. Every adapter should own its flow state.
4. Shared browser/form primitives should live in one reusable engine.
5. Pre-submit validation should happen before every final submit attempt.
6. Manual verification should pause without losing context, and resume should continue from the current page when it is still an application page.
7. Unknown sites should still be supported by the generic AI fallback.
8. The system should preserve user automation priority: do not add avoidable blockers or hard stops.

## Proposed Module Layout

Create a shared form engine package:

```text
src/job_apps_system/agents/apply/
  form_engine/
    __init__.py
    actions.py
    classifier.py
    fields.py
    flow.py
    observation.py
    validation.py
    verification.py
    tracing.py
```

Keep adapters under:

```text
src/job_apps_system/agents/apply/
  lever_adapter.py
  greenhouse_adapter.py
  ashby_adapter.py
  workday_adapter.py
  oracle_cloud_adapter.py
  icims_adapter.py
  dice_adapter.py
  linkedin_adapter.py
  ai_browser_loop.py
```

The generic `ai_browser_loop.py` should remain, but it should stop being the main execution engine for known ATSs.

## Core Data Structures

Define shared structured models in `form_engine/fields.py` and `form_engine/actions.py`:

```python
class ObservedField:
    id: str
    frame_id: str
    selector: str
    kind: str
    tag: str
    type: str
    label: str
    question_text: str
    option_text: str
    group_id: str | None
    required: bool
    disabled: bool
    checked: bool | None
    current_value: str
    invalid: bool
    visible: bool

class ObservedButton:
    id: str
    frame_id: str
    selector: str
    text: str
    kind: str
    disabled: bool
    busy: bool
    visible: bool

class FormAction:
    action: Literal[
        "fill_text",
        "select_option",
        "select_radio",
        "set_checkbox",
        "upload_file",
        "click",
        "submit",
        "wait",
    ]
    target_id: str | None
    value: str | bool | None
    reason: str
    source: Literal["adapter", "form_engine", "llm"]
```

The key improvement is adding `question_text`, `option_text`, and `group_id`. The Lever failure happened because the model saw only the option label `Yes`, not the full question. The observation layer should extract both the group question and the option text for radio/checkbox groups.

## Adapter Contract

Each ATS adapter should implement the same contract:

```python
class ApplyAdapter(Protocol):
    ats_type: str

    def apply(
        self,
        *,
        page: Page,
        job: Job,
        applicant: ApplicantProfileConfig,
        resume_path: Path,
        answer_service: ApplicationAnswerService,
        screenshot_path: Path,
        auto_submit: bool,
        cancel_checker: Callable[[], bool] | None,
        site_credential: ApplySiteCredential | None,
    ) -> ApplyJobResult:
        ...
```

Adapters should use a shared state-machine runner:

```python
class AdapterFlow:
    def run(self) -> ApplyJobResult:
        while True:
            state = self.detect_state()
            match state:
                case FlowState.ENTRY:
                    self.handle_entry()
                case FlowState.AUTH:
                    self.handle_auth()
                case FlowState.PROFILE:
                    self.handle_profile()
                case FlowState.FORM:
                    self.fill_form()
                case FlowState.MANUAL_VERIFICATION:
                    self.await_manual_verification()
                case FlowState.POST_VERIFICATION:
                    self.resume_after_verification()
                case FlowState.PRE_SUBMIT:
                    self.pre_submit_validate()
                case FlowState.SUBMIT:
                    self.submit()
                case FlowState.SUCCESS:
                    return self.success()
                case FlowState.MANUAL:
                    return self.manual()
                case FlowState.FAILED:
                    return self.failed()
```

## Flow States

Every known ATS adapter should map the browser into these states:

| State | Meaning | Adapter Responsibility |
| --- | --- | --- |
| `ENTRY` | Job detail or external apply landing page | Click the correct apply/start button or navigate to known apply URL. |
| `AUTH` | Login/register/account gate | Use saved site credentials, generate account if needed, or pause for unavailable login/MFA. |
| `PROFILE` | ATS profile setup before application | Fill required profile basics or return to application URL after profile completion. |
| `FORM` | Application fields visible | Use shared form engine to fill known fields and classify ambiguous fields. |
| `MANUAL_VERIFICATION` | Active CAPTCHA, MFA, or verification puzzle | Show manual bar/modal and pause; do not continue filling behind active challenge. |
| `POST_VERIFICATION` | User completed verification and clicked Resume AI | Continue from current application page if possible; otherwise navigate to saved application URL. |
| `PRE_SUBMIT` | Form appears complete | Run deterministic validation and repair pass. |
| `SUBMIT` | Final submit action | Click final submit and wait for result. |
| `SUCCESS` | Confirmation page/banner detected | Mark applied and persist screenshot/confirmation. |
| `MANUAL` | User chose to finish manually or blocker remains | Preserve browser state and record manual status. |
| `FAILED` | Automation cannot recover | Save screenshot, trace, and concise error. |

## Shared Form Engine Responsibilities

### Observation

`form_engine/observation.py` should produce a rich page snapshot:

- Visible frames only, with frame visibility metadata.
- Fields with labels, parent question text, option text, group IDs, current value, required/invalid state.
- Buttons with text, disabled/busy state, href/navigation risk.
- Validation messages.
- Success messages.
- CAPTCHA/manual verification signals.
- Auth/register/profile signals.

Radio and checkbox extraction is especially important:

- Group related radio inputs by `name`, fieldset, nearby question text, or DOM container.
- Store the full question text once per group.
- Store the specific option text separately.
- Never force the LLM to infer from option text alone.

### Actions

`form_engine/actions.py` should provide reusable Playwright action primitives:

- Fill text safely.
- Select option by label/value/fuzzy candidate.
- Select radio by group question plus desired answer.
- Set checkbox with native and JS fallback.
- Upload resume/file.
- Click button/link with visibility and navigation checks.
- Submit final button.
- Wait for network/load/page mutation.

### Validation

`form_engine/validation.py` should validate before submit:

- Required text fields have values.
- Required selects/comboboxes have selected values.
- Required radio groups have selected options.
- Required checkboxes are checked.
- Required resume/CV upload is attached or refreshed.
- Known compliance questions match profile values.
- Visible validation errors are absent.
- Active CAPTCHA/manual verification is not blocking.
- Final submit button is visible and actionable.

The validator should return a structured result:

```python
class ValidationResult:
    ok: bool
    missing_fields: list[ObservedField]
    invalid_fields: list[ObservedField]
    repairs: list[FormAction]
    blockers: list[str]
```

### Verification

`form_engine/verification.py` should centralize detection of:

- Active hCaptcha/reCAPTCHA/Arkose/FunCaptcha challenge.
- Passive CAPTCHA disclosure.
- MFA/code verification.
- Login-required pages.
- Post-submit verification retry banners.

Adapters should pause on active verification, not continue filling fields behind it.

### Tracing

`form_engine/tracing.py` should save:

- Action log.
- State transitions.
- Screenshots at state changes.
- Sanitized DOM snapshot on failure.
- Validation report before submit.
- Final confirmation or blocker reason.

This is what makes future failures diagnosable without guessing from screenshots.

## LLM Role

The LLM should not directly control known ATS pages except as fallback.

For known ATSs, the LLM should only be called for:

- Ambiguous custom text answers.
- Mapping unusual field labels to known profile fields.
- Choosing among unclear select options when deterministic matching fails.
- Summarizing why a page is not recognized.

The LLM should return structured JSON such as:

```json
{
  "field_mappings": [
    {
      "target_id": "frame_0:el_019",
      "answer": "Negotiable",
      "confidence": 0.94,
      "reason": "Desired salary text field with no explicit setup value."
    }
  ],
  "unanswered": [],
  "needs_manual": false
}
```

Execution remains deterministic. The engine should reject LLM actions that target unknown IDs, final submit too early, social-login buttons, unrelated navigation, or fields blocked by active verification.

## Adapter Behavior by ATS

### Lever

Highest priority because it is currently failing in test runs.

Lever adapter should own:

- Application form detection.
- hCaptcha/verification pause and resume.
- Resume/CV upload and refresh after verification errors.
- Work authorization and sponsorship radio groups.
- Age confirmation.
- Source/referral fields.
- Desired salary defaulting.
- State/location dropdown.
- Voluntary EEO decline choices.
- Required submit consent checkboxes.
- Final submit and success/error detection.

Lever-specific pre-submit pass:

1. Re-observe page.
2. Correct known yes/no groups.
3. Refresh resume upload if required.
4. Check all required submit consent boxes.
5. Validate all required fields.
6. Submit only when validation passes.

### Greenhouse

Greenhouse already has a stronger deterministic adapter. Refactor it to use shared primitives:

- Resume upload.
- Core fields.
- Known custom questions.
- Consent and demographic fields.
- Manual fallback.

Avoid changing behavior while extracting shared logic.

### Ashby

Ashby should use shared primitives for:

- Resume upload.
- Core fields.
- Custom question answers.
- Numeric field sanitization.
- Known structured choices.

### Dice

Dice should remain a gateway adapter plus external handoff:

- Canonical job URL resolution.
- Login/register/profile creation.
- Persistent Dice browser profile reuse.
- Return to canonical job/application URL after auth/profile.
- Detect external handoff to KForce or other employer ATS and update job metadata.

Dice should not try to own the external employer application once it leaves Dice; it should hand to the detected downstream ATS or generic fallback.

### Oracle Cloud / JPMC

Oracle should become a full state machine:

- Email-first login/register.
- Terms checkbox handling.
- Code verification pause/resume.
- Multi-page application navigation.
- Page-level required question handling.
- Pre-submit validation per page and final submit.

### Workday

Workday should be implemented after Lever/Oracle because it is complex:

- Account/login.
- Candidate home/profile reuse.
- Multi-step wizard.
- Resume parsing and upload.
- Repeated required profile sections.
- Long-running page transitions.

### iCIMS

iCIMS should reuse shared primitives:

- Candidate info.
- Resume upload.
- Knockout questions.
- Location and work authorization.
- Success detection.

### LinkedIn

LinkedIn should remain limited:

- Easy Apply if present and supported.
- External URL resolution.
- Do not try to bypass LinkedIn auth challenges.

## Implementation Phases

### Phase 1: Form Engine Skeleton

Deliverables:

- Create `form_engine` package.
- Move observation primitives from `AiBrowserApplyLoop` into `form_engine/observation.py`.
- Move action primitives into `form_engine/actions.py`.
- Move blocker detection into `form_engine/verification.py`.
- Add data models for observed fields/buttons/actions/validation.

Acceptance criteria:

- Existing tests pass.
- Generic AI loop still works using the new observation/action wrappers.
- No behavior change required in this phase.

### Phase 2: Validation Engine

Deliverables:

- Implement `ValidationResult`.
- Detect missing required text/select/radio/checkbox/file fields.
- Detect visible validation errors.
- Detect active verification blockers.
- Add repair action generation for common missing items.

Acceptance criteria:

- Unit tests cover required radio groups, required checkboxes, file upload, validation errors, and active CAPTCHA.
- Validation can run without submitting.

### Phase 3: Lever Adapter State Machine

Deliverables:

- Convert Lever adapter from delegated AI loop subclass into explicit state machine.
- Use shared observation/actions/validation.
- Keep LLM only for custom ambiguous questions.
- Add Lever HTML fixtures based on saved screenshots/DOM examples.

Acceptance criteria:

- Lever can fill Aledade-style forms deterministically.
- Resume is present immediately before every submit.
- Both submit consent checkboxes are checked only in pre-submit.
- Sponsorship future/current questions match setup profile.
- Active CAPTCHA pauses before further field filling.
- Resume AI after CAPTCHA resumes on current application page if valid.
- Verification retry does not loop indefinitely.

### Phase 4: Generic AI Loop Shrink

Deliverables:

- Remove known-ATS special cases from generic AI loop.
- Make generic loop consume shared form engine primitives.
- Route known ATSs to adapters first.
- Keep unknown ATS fallback behavior.

Acceptance criteria:

- No Lever/Oracle/Dice-specific code remains in generic loop except shared fallback-compatible utilities.
- Unknown ATS still attempts AI browser completion.

### Phase 5: Adapter Migration

Migration order:

1. Lever
2. Greenhouse
3. Ashby
4. Dice gateway/downstream handoff
5. Oracle Cloud
6. iCIMS
7. Workday
8. LinkedIn Easy Apply/external resolver

Each migration should follow:

- Add fixture tests.
- Move duplicated helper logic into shared engine.
- Preserve existing adapter behavior.
- Add pre-submit validation.
- Add state-transition trace.

### Phase 6: Trace Viewer and Debugging

Deliverables:

- Persist sanitized run trace JSON under debug folder.
- Add trace link in application result details.
- Include state transitions, validation reports, action logs, and screenshots.

Acceptance criteria:

- A failed run can be diagnosed without manually reading backend logs.
- Sensitive fields and passwords remain masked.

## Test Strategy

### Unit Tests

Cover:

- Observation extraction.
- Radio group question/option extraction.
- Required-field validation.
- Checkbox fallback.
- Resume upload targeting.
- CAPTCHA active/passive distinction.
- Known compliance answer inference.
- Adapter state transitions.

### Fixture Tests

Add local HTML fixtures:

```text
tests/fixtures/apply/
  lever/
    aledade_application.html
    aledade_application_with_captcha.html
    aledade_verification_error_resume_cleared.html
  greenhouse/
  ashby/
  oracle/
  dice/
```

Use Playwright against local fixture pages so failures are reproducible without hitting live ATS pages.

### Integration Tests

Add opt-in integration tests that run against real pages only when explicitly enabled:

```bash
RUN_LIVE_APPLY_TESTS=1 pytest tests/integration/apply/
```

These should never submit real applications unless a special test flag is present.

## Operational Behavior

### Manual Verification

When active CAPTCHA/MFA/verification is visible:

- Pause immediately.
- Keep the browser open.
- Show manual bar/modal.
- Do not continue filling fields behind the challenge.
- On Resume AI, inspect the current page first.
- If current page is still an application page, continue in place.
- If current page is a generic profile/home page, navigate to the saved application URL.

### Submit Retry

For post-submit verification errors:

- Do not blindly retry more than the configured submit attempt limit.
- Re-run pre-submit validation before each retry.
- Reattach resume if file input is empty or required.
- Re-check required consents.
- If active verification appears, pause.

### Auto Submit

`apply_auto_submit` remains the user-level control:

- If enabled, adapters may submit after validation passes.
- If disabled, adapters stop at review/needs_review when ready.

## Data and Secrets

- Site credentials stay in the OS keychain/local secret store.
- Passwords must not be stored in DB, logs, screenshots, or run traces.
- Browser profiles may persist cookies/session state per site key.
- Job metadata updates remain in SQLite.
- Debug traces must mask password fields and sensitive credential values.

## Risks

| Risk | Mitigation |
| --- | --- |
| ATS DOM changes frequently | Fixture tests plus adapter-specific state detection. |
| LLM misclassifies compliance fields | Deterministic known-question inference and pre-submit correction. |
| CAPTCHA interrupts flow | Central verification detection and pause/resume state. |
| File inputs clear after validation failures | Pre-submit resume refresh for ATSs that need it. |
| Generic fallback breaks known ATSs | Route known ATSs to adapters first and remove known-specific generic patches. |
| Too many one-off adapters | Shared form engine primitives reduce adapter surface area. |

## Success Metrics

- Known ATS completion rate improves without increasing manual loops.
- CAPTCHA pause/resume does not lose application progress.
- Resume upload is present before submit for all known ATSs.
- Required consent checkboxes are handled deterministically.
- Compliance answers match setup profile.
- New ATS fixes are implemented in adapters or shared primitives, not ad hoc generic patches.
- A failed run includes enough trace data to identify the blocker in one pass.

## First Implementation Slice

The first slice should be deliberately narrow:

1. Create `form_engine` package and data models.
2. Extract observation and action primitives without behavior changes.
3. Add Lever fixture pages.
4. Convert Lever to explicit state machine.
5. Add Lever pre-submit validation report.
6. Keep the generic AI loop fallback unchanged until Lever is stable.

This gives us the highest reliability gain with the least architectural churn.
