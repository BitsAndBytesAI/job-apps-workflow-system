# Apply Engine: AI-Orchestrated Architecture Plan

## Goal

Build a single job-application submission engine that completes forms across any ATS — known or unknown — by letting an LLM orchestrate browser actions through tool calls, with vision as a first-class observation channel and a small deterministic safety harness as the final guarantor of correctness.

This plan starts from the question: *if Anthropic's tool use, OpenAI's Responses API, and image input are all available, what's the smallest, most reliable apply engine you can build?*

The answer: **one engine, two LLMs, no per-ATS code.**

## Premise

Four observations drive every decision in this plan:

1. **Tool use eliminates the JSON-parsing failure mode.** When the LLM calls a structured tool with a validated input schema, "Claude returned weird JSON" stops being a real failure category. The engine validates inputs against the live page observation before executing.

2. **Vision eliminates the DOM-blindness failure mode.** DOM extraction misses custom widgets, dynamic reveals, visual validation states, and active-vs-passive CAPTCHA disambiguation. A screenshot fixes most of those in one feature.

3. **Stateful conversations make vision affordable on every turn.** When you don't re-send observation history each turn, vision-on-every-observation becomes routine instead of an expensive optimization.

4. **Some ATSs need a small executable escape hatch.** YAML hints can express patterns, known answers, and detection signatures, but they can't express *behavior* — iframe traversal sequences, post-upload autofill timing, cross-origin element handling, account-creation redirects. Pretending those don't exist would force them back into the orchestrator's prompt, which is the wrong place for code-shaped logic. The architecture allows optional Python hooks per ATS, scoped to specific lifecycle points.

Together those four change the cost-benefit calculus of "let the LLM drive vs. write deterministic adapters per ATS." The latter was correct when LLMs were unreliable JSON generators reading text-only DOM dumps. With tool use + vision + stateful conversations, the LLM-driven path is reliable enough that full per-ATS state machines are no longer justified.

The result: most ATS-specific knowledge becomes config (YAML hints loaded at runtime). The narrow set of behavioral quirks that genuinely need code lives as small lifecycle hooks (50–150 LOC per ATS, not 500+). New ATS support is usually a config edit; sometimes a config edit plus one hook function.

## High-Level Shape

```
┌─────────────────────────────────────────────────────────────┐
│  apply engine  (one orchestration loop, all ATSs)           │
│                                                              │
│   ┌────────────────────────────────────────────────────┐    │
│   │  observe → LLM tool_call → validate → dispatch → loop  │
│   └────────────────────────────────────────────────────┘    │
│                                                              │
│   ┌──── world tools ────┐        ┌── perception tools ──┐   │
│   │ fill_text           │        │ classify_captcha      │   │
│   │ select_option       │        │ read_custom_widget    │   │
│   │ select_radio        │        │ classify_page_state   │   │
│   │ set_checkbox        │        │ map_field_to_profile  │   │
│   │ upload_file         │        └───────────────────────┘   │
│   │ click               │                  │                  │
│   │ submit              │                  │ delegated to     │
│   │ pause_for_captcha   │                  │ Opus 4.7         │
│   │ request_manual      │                  ▼                  │
│   │ report_complete     │        ┌──────────────────────┐    │
│   └─────────────────────┘        │   Opus 4.7           │    │
│                  │                │ (vision specialist)  │    │
│                  ▼                └──────────────────────┘    │
│      ┌─────────────────┐                                      │
│      │ Playwright      │                                      │
│      │ + safety.py     │  ← validates each tool call          │
│      └─────────────────┘                                      │
│                                                                │
│   primary orchestrator: GPT-5.5 Responses API                 │
│   (stateful conversation_id per apply run)                    │
└────────────────────────────────────────────────────────────────┘
```

## Module Layout

```text
src/job_apps_system/agents/apply/
  engine.py              ← orchestration loop, model dispatch, safety wiring, hook invocation
  tools/
    world.py             ← Playwright tool implementations + their tool schemas
    perception.py        ← Opus delegation tools + their tool schemas
    auth.py              ← auth/profile tool schemas + dispatch
  primitives/
    playwright_actions.py  ← raw Playwright wrappers (fill, click, screenshot, etc.)
    observation.py         ← DOM extraction (fields, buttons, frames, validation msgs)
    vision.py              ← screenshot capture, base64 encode, viewport crop
  safety.py              ← deterministic guardrails: validator, captcha pauser, action validator, completion verifier, cost guard
  knowledge/
    _common.yaml         ← shared patterns: known compliance Q&A, generic submit text
    lever.yaml
    greenhouse.yaml
    ashby.yaml
    workday.yaml
    oracle_cloud.yaml
    icims.yaml
    dice.yaml
    linkedin.yaml
  hooks/                 ← optional ATS-specific lifecycle hooks (Python). One file per ATS that needs them.
    __init__.py          ← registry: ATS name → hook module
    base.py              ← Hook protocol + null-default implementations
    workday.py           ← e.g. iframe pre-traversal, multi-step autofill timing
    oracle_cloud.py      ← e.g. email-first login dance, terms checkbox sequence
    lever.py             ← e.g. consent re-check after validation error
    # other ATSs add a hook file only if YAML hints can't express the quirk
  llm/
    base.py              ← abstract: tool_call(...) -> ToolUseResult
    openai_responses.py  ← GPT-5.5 Responses API client (stateful conversations)
    anthropic.py         ← Opus 4.7 client (single-turn vision specialist)
    routing.py           ← maps tool name → which LLM provider handles it
  trace.py               ← per-run trace JSON: conversation, tool calls, screenshots, decisions, costs
  models.py              ← Pydantic: Observation, ToolCall, ToolResult, ApplyRunResult, HookContext
  legacy/                ← existing per-ATS adapters preserved here through the cutover
    lever_adapter.py
    greenhouse_adapter.py
    ...
```

The existing `ai_browser_loop.py` and per-ATS adapters are NOT removed in the initial migration. They move into `legacy/` and remain selectable as a fallback per-ATS until the new engine outperforms them on fixtures and live dry-runs. Removal is gated on metrics — see Phases 6–7.

## The Engine Loop

```python
def run_apply(job: Job, applicant: ApplicantProfile, ats: str) -> ApplyRunResult:
    page = open_page(job.apply_url)
    knowledge = load_knowledge(ats)              # YAML hints for the detected ATS
    hook = load_hook(ats)                         # optional Python hook module; null-default if none
    cost_guard = CostGuard(                       # hard per-run + per-day budget enforcement
        run_budget_usd=settings.apply_run_cost_budget_usd,    # default $1.00, configurable in Settings
        day_budget_usd=settings.apply_day_cost_budget_usd,    # default $20.00, configurable in Settings
        day_spent_usd=load_today_spend(user_id),
    )
    conversation = LLM.start_conversation(        # GPT-5.5 Responses API conversation_id
        system_prompt=ENGINE_PROMPT + knowledge.as_prompt() + hook.prompt_addendum(),
        tools=ALL_TOOL_SCHEMAS,
    )
    history: list[StateTransition] = []
    ctx = HookContext(page=page, knowledge=knowledge, applicant=applicant, job=job)

    for turn in range(MAX_TURNS):
        observation = observe(page)
        observation = hook.on_observe(observation, ctx) or observation   # hook may augment

        budget_reason = cost_guard.would_exceed_on_next_turn(estimate_input_tokens(observation))
        if budget_reason:
            return ApplyRunResult(status="manual", reason=f"{budget_reason}_exceeded", history=history)

        result = conversation.tool_call(
            new_input=[
                vision_block(observation.screenshot),
                text_block(observation.json_summary),
            ],
            tool_choice="any",                    # force a tool call, never free text
        )
        cost_guard.record(result.usage)

        for tool_use in result.tool_use_blocks:
            error = safety.validate(tool_use, observation)
            if error:
                conversation.append_tool_result(tool_use.id, ok=False, reason=error)
                continue

            # Optional pre-dispatch hook can substitute the tool's behavior
            override = hook.before_tool(tool_use, ctx)
            outcome = override if override else dispatch(tool_use, page, conversation, ctx)
            hook.after_tool(tool_use, outcome, ctx)

            conversation.append_tool_result(tool_use.id, ok=outcome.ok, payload=outcome.data)
            history.append(outcome.transition)

            if outcome.terminal:                  # report_complete / report_blocked / fatal
                trace.save(history, conversation, page, cost_guard)
                return outcome.result

    return ApplyRunResult(status="manual", reason="turn_limit_reached", history=history)
```

The loop has four jobs and only four:

1. **Observe** the page (DOM + screenshot), optionally augmented by an ATS hook.
2. **Enforce the cost budget** before the next LLM call.
3. **Ask the LLM** what to do next, structurally (via tool call).
4. **Validate and dispatch** the tool call, recording the outcome back into the conversation.

Everything else — when to pause for CAPTCHA, when to retry, when to give up — is encoded as a tool the LLM can call (`pause_for_captcha`, `request_manual`, `report_blocked`, `report_complete`). The engine never decides on its own to abort or retry except on hard invariant violations (cost overrun, turn budget, hook-fatal).

## Model Dispatch (Pattern B)

Two LLMs share the apply run, dispatched per task type. The split is invisible to the orchestrator's prompting because both speak through the same tool-call abstraction.

| Task | Model | Why |
|---|---|---|
| **Orchestration loop** | **GPT-5.5 Responses API** | Stateful `conversation_id` keeps observation history server-side. On a multi-page Workday application, the per-turn input cost stays bounded instead of growing linearly with page count. The orchestrator runs every turn, so the per-call savings compound. |
| **Vision-heavy CAPTCHA classification** | **Opus 4.7** | Stronger detail-level vision performance on adversarial visual UIs. CAPTCHA active-vs-passive, hCaptcha vs Arkose vs FunCaptcha disambiguation. Called sparingly (only when DOM heuristics flag *possible* challenge). |
| **Custom widget reading** | **Opus 4.7** | Date pickers, location autocompletes, signature pads, canvas-rendered fields, image-labeled options. Long-context (1M) helps when the widget's state requires a lot of surrounding visual context. |
| **Field classification** (label → profile) | Either (default GPT-5.5 inline within the orchestration call) | The orchestrator already has full conversation context. Folding classification into the same tool call avoids an extra round-trip. Use Opus only if the orchestrator explicitly delegates via `map_field_to_profile`. |
| **Page-state classification** (AUTH / FORM / PROFILE / etc.) | Either (default GPT-5.5 inline) | Same reasoning — the orchestrator usually knows the state from the observation it just received. |

### How dispatch works mechanically

The orchestrator calls perception tools by name. The engine's tool-routing layer (`llm/routing.py`) decides which provider implements each tool:

```python
TOOL_ROUTES = {
    # World tools — engine executes locally via Playwright
    "fill_text":           Local(world.fill_text),
    "click":               Local(world.click),
    "submit":              Local(world.submit),
    # ... all other world tools

    # Perception tools — engine routes to Opus
    "classify_captcha":    DelegateToAnthropic(perception.classify_captcha),
    "read_custom_widget":  DelegateToAnthropic(perception.read_custom_widget),

    # Either-provider tools — engine asks the orchestrator's own model unless explicitly delegated
    "classify_page_state": Local(perception.classify_page_state_inline),
    "map_field_to_profile": Local(perception.map_field_to_profile_inline),
}
```

When the GPT-5.5 orchestrator emits `tool_use(name="classify_captcha", input={screenshot_id: ...})`, the engine:

1. Reads the screenshot from the in-memory observation cache.
2. Calls Opus 4.7 with that image + the perception tool's specific schema.
3. Receives Opus's structured response.
4. Returns it as the `tool_result` for the GPT-5.5 conversation.

GPT-5.5 sees this as a normal tool call returning structured data. It has no awareness that a different provider produced the answer. The conversation state stays coherent.

### Why this pattern, not all-Opus or all-GPT

Going **all-Opus**: pays the input-history cost on every turn (no stateful conversation). For a 10-page Workday application this is meaningful. Pays for Opus on every routine action where Opus's vision strength isn't needed.

Going **all-GPT-5.5**: gets stateful conversations cheaply but cedes the strongest vision performance on the few moments that actually benefit from it (CAPTCHA, custom widgets).

Pattern B keeps the cheap-and-stateful path on the hot loop and uses the expensive-but-strong path only when needed. Empirically this should land within 5–10% of "best-case all-Opus" accuracy at 30–50% of the cost.

## Tool Catalog

### World tools (Playwright, executed locally)

Every browser action the engine can take is a tool. The LLM calls one per turn (or several in a single turn if the API allows parallel tool calls).

| Tool | Inputs | Behavior |
|---|---|---|
| `fill_text` | `target_id`, `value` | Fills a text/textarea/email/tel/url/number input. Clears existing value first. |
| `select_option` | `target_id`, `option_text` or `option_value` | Selects from a `<select>` or aria-listbox combobox. Fuzzy match on text. |
| `select_radio` | `group_id`, `option_text` | Selects a radio in a logical group. Engine resolves group → specific input. |
| `set_checkbox` | `target_id`, `checked` (bool) | Sets checkbox state with native click + JS fallback. |
| `upload_file` | `target_id`, `file_role` (`resume` \| `cover_letter`) | Attaches the file. **Dispatcher then waits for ATS extraction** (network idle + per-ATS configurable buffer, default 3s) and **captures a fresh observation** before returning. The tool_result includes the post-extraction observation summary so the LLM sees autofilled fields next turn. The LLM does not need a separate "wait for parsing" tool. |
| `click` | `target_id`, `expect` (`navigate` \| `reveal` \| `none`) | Clicks a non-submit element. `expect` declares the intended outcome. |
| `submit` | `target_id`, `is_final` (bool) | Submits a form. `is_final=true` triggers the pre-submit safety pass. Rejected if `pause_for_captcha` was called in the same turn. |
| `wait` | `reason`, `max_ms` | Waits for network idle / element appearance / animation completion. |
| `pause_for_captcha` | `screenshot_evidence_id`, `kind` | Engine surfaces the manual bar/modal. Apply run pauses; user resolves; user clicks Resume. |
| `request_manual` | `reason`, `proceed_url` | Hands off to user without retry. Persistent browser stays open. Run ends with status=manual. |
| `report_complete` | `confirmation_evidence_id`, `confirmation_kind` (`success_url` \| `success_banner` \| `email_confirmed` \| ...) | **Engine runs `CompletionVerifier`** before accepting. If verification fails, returns `tool_result(ok=false)` and the run continues. The LLM cannot end the run as successful by assertion alone. Only on verifier success: persist confirmation, run ends with status=success. |
| `report_blocked` | `reason`, `evidence_ids` | Apply cannot continue and isn't manually resolvable. Run ends with status=failed. |

### Auth tools (Playwright + credential store, executed locally)

Auth is a major source of real failures and gets first-class tools rather than being squeezed into the world catalog.

| Tool | Inputs | Behavior |
|---|---|---|
| `lookup_site_credential` | `site_key` (e.g. `lever.co`, `dice.com`, `oraclecloud.com`) | Returns `{has_credential: bool, identifier: str (masked), can_use: bool}`. Pulls from existing `apply_site_sessions` keychain. Never returns the password to the LLM — it stays in the engine. |
| `attempt_login` | `site_key`, `username_target_id`, `password_target_id`, `submit_target_id` | Engine fills credentials directly (LLM never sees the password) and clicks submit. Returns `{success: bool, next_state, evidence_id}`. Engine knows how to detect login failure from observation. |
| `register_account` | `site_key`, `email_target_id`, `password_target_id`, `confirm_password_target_id`, `submit_target_id`, `extra_fields[]` | Engine generates a strong password via the existing secret-store helper, fills it (and any extra fields the LLM provides), and submits. On success: persists the credential to keychain so future apply runs can use `lookup_site_credential`. |
| `pause_for_mfa` | `screenshot_evidence_id`, `kind` (`email_code` \| `sms_code` \| `totp` \| `magic_link` \| `oauth_redirect`) | Surfaces a manual bar specifically for MFA. User completes; clicks Resume. Same pause/resume mechanism as CAPTCHA but with MFA-specific UI copy. |
| `submit_mfa_code` | `code_target_id`, `submit_target_id` | After the user surfaces the code (often via Resume + manual paste into a designated field), the LLM calls this. Engine never reads the code from the conversation; the user pastes directly into the page. |
| `persist_site_credential` | `site_key`, `outcome` (`saved` \| `declined`) | Called after a successful registration or first-time login. Asks the user (via UI prompt) whether to save credentials for next time. On confirmation, engine writes to keychain. |

The `attempt_login` and `register_account` tools are the only places the engine handles passwords. The LLM never sees the password. This is enforced in the tool dispatcher — passwords are pulled from / written to the keychain inside the dispatcher, not passed through the LLM conversation.

### Perception tools (delegated to Opus)

| Tool | Inputs | Returns |
|---|---|---|
| `classify_captcha` | `screenshot_id` (current observation's image) | `{active: bool, kind: hcaptcha\|recaptcha\|arkose\|funcaptcha\|none, confidence}` |
| `read_custom_widget` | `screenshot_id`, `widget_bounds` (optional crop), `expected_kind` (date \| location \| signature \| etc.) | `{kind, current_value, options_visible[], hint_text}` |
| `classify_page_state` | `screenshot_id`, `dom_summary` | `{state: ENTRY\|AUTH\|PROFILE\|FORM\|VERIFICATION\|REVIEW\|SUCCESS\|UNKNOWN, confidence, evidence}` |
| `map_field_to_profile` | `field_label`, `question_text`, `option_texts[]`, `screenshot_id` | `{profile_path, value, confidence, reason}` or `{unmappable: true, suggested_action}` |

### Tool-call invariants

- **Every tool call must include a `reason` field**, free-text, ≤140 chars. Trace records it. Used for debugging, never for control flow.
- **`target_id` is always validated** against the latest observation before dispatch. Stale IDs return a tool_result error and the LLM gets a fresh observation next turn.
- **Forbidden actions**: `click` on social-login buttons, navigation to non-application domains, `submit` on non-final forms when `is_final=true` is set. Safety harness rejects these unconditionally.

## Safety Harness

The deterministic floor under everything. Lives in `safety.py`. ~350 lines of code. Same checks regardless of which LLM produced the tool call.

### Pre-submit validator

Runs whenever the LLM calls `submit(is_final=true)`. If it fails, the submit is blocked and a structured `tool_result` describes what's missing. The LLM gets to repair.

```python
class ValidationResult:
    ok: bool
    missing_required: list[FieldRef]
    invalid: list[FieldRef]
    visible_errors: list[str]
    captcha_active: bool
    file_inputs_empty: list[FieldRef]
    consents_unchecked: list[FieldRef]
    submit_button_disabled: bool
```

A single function `validate_pre_submit(observation) -> ValidationResult`. Used at exactly one point in the loop — before any final submit dispatches.

### Completion verifier

Runs whenever the LLM calls `report_complete`. The LLM cannot end the run as successful by assertion. Verifier checks:

```python
class CompletionVerifier:
    def verify(self, observation, claim, knowledge) -> CompletionResult:
        # Required signals (at least 2 must hold):
        # - URL matches knowledge.success.url_pattern
        # - DOM contains element matching knowledge.success.banner_pattern
        # - Visible text contains a known success phrase
        # - No active form submit buttons remain on the page
        # - No visible validation errors
        # If the claim's confirmation_kind is "email_confirmed", verifier
        # additionally requires a screenshot showing the confirmation UI;
        # the engine matches it against expected layouts via Opus
        # (perception.verify_success_screenshot).
```

If verification fails, `report_complete` returns `tool_result(ok=false, reason="completion_unverified", details=…)` and the run continues. After 3 consecutive unverified completion claims, the run aborts to `manual` so a user can resolve.

### Submit-blocker

Hard rule: if `pause_for_captcha` was called in the same turn as `submit`, `submit` is rejected. Prevents the LLM from "submitting through" an active challenge.

### CAPTCHA pauser

Pause if **either** signal is high-confidence positive (≥ 0.85), with the other treated as confirming evidence rather than required:

- DOM heuristic: visible iframe matching known captcha origins (hCaptcha, reCAPTCHA, Arkose, FunCaptcha) AND the iframe is in the viewport AND not in a passive-disclosure layout (e.g., Cloudflare's small lower-right "verifying you are human" badge).
- Vision (`classify_captcha`): returns `active=true` with confidence ≥ 0.85.

If both signals agree, pause with high confidence. If only one fires, still pause but note `single_signal=true` in the trace so a noisy heuristic shows up in metrics. The earlier "both required" rule was too restrictive — cross-origin iframes sometimes hide from naive DOM scans, and visually-rendered challenges sometimes appear before the DOM signal stabilizes.

### Action validator

For every tool call:
- `target_id` exists in the latest observation
- `option_text` matches at least one option for select/radio tools
- `file_role=resume` resolves to a file the job has
- Forbidden actions are rejected outright (social-login clicks, off-domain navigation, `submit(is_final=true)` without a prior pre-submit pass attempt within the same observation cycle)

### Cost guard

Hard dollar budget enforcement at two granularities — per apply run and per user per day. Both configurable in Settings → Workflow.

| Budget | Default | Behavior on hit |
|---|---|---|
| **Per-run** | **$1.00** | Engine refuses next LLM call. Run ends `status=manual, reason=cost_budget_exceeded`. Browser stays open so the user can finish manually. |
| **Per-day** | **$20.00 / user / day** | At 80% spent: UI shows "AI Apply degraded — daily budget low", future apply runs that day default to text-only orchestration (no vision per turn) to stretch the remaining budget. At 100%: new apply runs refuse to start with `status=manual, reason=daily_budget_exceeded`. Day rolls over at user's local midnight. |

```python
class CostGuard:
    def __init__(self, *, run_budget_usd: float, day_budget_usd: float, day_spent_usd: float):
        self.run_budget_usd = run_budget_usd
        self.day_budget_usd = day_budget_usd
        self.day_spent_usd = day_spent_usd     # rolling total for current calendar day
        self.run_spent_usd = 0.0

    def record(self, usage: TokenUsage) -> None:
        cost = price_for(usage)
        self.run_spent_usd += cost
        self.day_spent_usd += cost

    def would_exceed_on_next_turn(self, projected_input_tokens: int) -> str | None:
        projected_cost = price_for(estimated_usage(projected_input_tokens))
        if self.run_spent_usd + projected_cost > self.run_budget_usd:
            return "run_budget"
        if self.day_spent_usd + projected_cost > self.day_budget_usd:
            return "day_budget"
        return None

    def daily_degraded_mode(self) -> bool:
        return self.day_spent_usd >= 0.8 * self.day_budget_usd
```

Engine checks `would_exceed_on_next_turn` before each LLM call and routes the abort reason accordingly. Trace records final per-run spend; persistent ledger tracks rolling daily spend per user.

Telemetry: every apply run emits `apply_cost_total`, `apply_input_tokens`, `apply_output_tokens`, `apply_perception_calls`, `apply_turns` to whatever telemetry sink is configured. Day-one observability requirement, not a Phase 6 nicety.

### Settings-page controls

Both budgets are user-configurable, lives under **Settings → Workflow** (the same tab as the existing apply-behavior controls):

```
AI Apply Cost Limits
  Max cost per apply run        [ $1.00  ]   ← default; min $0.10, max $5.00
  Max cost per day              [ $20.00 ]   ← default; min $5.00, max $100.00
  Cost-aware degradation        [✓] Reduce vision usage when daily budget is 80% spent
```

The current `apply_choice_behavior` and `apply_default_limit` controls already live in Workflow — these new fields slot in next to them. Persistence path: `AppBehaviorConfig.apply_run_cost_budget_usd` and `apply_day_cost_budget_usd` (Pydantic floats with min/max validators).

### Safety harness is one of two places that can abort a run

The LLM has the right to give up via `report_blocked` or `request_manual`. The safety harness has the right to abort only on hard invariant violations:

- `target_id` stale 5+ times in a row (LLM confusion)
- Forbidden action attempted 2+ times (LLM misbehavior)
- Cost budget would be exceeded (`would_exceed_on_next_turn` true)
- Turn budget exhausted
- Completion verifier fails 3 times consecutively
- Hook returns `Hook.fatal()`

Otherwise: the LLM drives, the harness validates.

## Authentication & Profile

Account creation, login, and post-auth resumption have been the loudest source of real apply failures. They get an explicit treatment, not a sidebar.

### What the engine recognizes as auth-related state

The orchestrator detects auth state from the observation (login form fields, OAuth landing pages, MFA prompts, "create your account" copy). When the orchestrator decides the page is an auth gate, it follows this canonical sequence using the auth tools:

```
observe → detects auth gate
       │
       ├─ orchestrator calls lookup_site_credential(site_key)
       │       │
       │       ├─ has credential? → attempt_login(...)
       │       │       │
       │       │       ├─ success → continue (engine returns to bookmarked apply URL)
       │       │       └─ failure → orchestrator falls back to register_account or pause_for_manual
       │       │
       │       └─ no credential? → register_account(...)
       │
       └─ MFA detected mid-login? → pause_for_mfa(...) → user resolves → submit_mfa_code(...)
```

### Apply URL bookmarking

Before the engine ever interacts with an auth gate, it stores the original apply URL in the conversation context. After a successful login or registration, the engine navigates back to the bookmarked URL automatically (via a tool dispatcher rule, not via the LLM). The orchestrator sees the application page on the next observation as if the auth detour didn't happen.

### Credential store integration

Reuses the existing `apply_site_sessions` keychain helpers:

- Credentials are stored per `site_key` (the eTLD+1 of the auth host).
- Passwords are written to the OS keychain only — never to the database, conversation, screenshot, or trace.
- The dispatcher reads credentials at fill-time, never the LLM. Tool inputs reference the credential by `site_key`, not by value.
- On `register_account`, the engine generates a strong random password (existing helper), uses it once, and persists it on `persist_site_credential(saved)`.

### Resume from auth interrupt

If the user closes the app mid-auth (e.g. checks email for an MFA code, then comes back later), the engine recovers via:

1. The persistent browser profile keeps the session cookie.
2. On Resume, the engine re-observes the page.
3. If the page is past the auth gate, the orchestrator continues normally.
4. If the page is the original auth gate (session expired), the orchestrator restarts from `lookup_site_credential`.

### Failure containment

- `attempt_login` returns `{success: false, reason: "invalid_credentials"}` → orchestrator can choose to `register_account` (if site allows) or `request_manual`.
- `register_account` returns failure → orchestrator falls back to `request_manual`. Auto-registration loops are forbidden by the safety harness (max 1 attempt per run).
- MFA times out (>5 min) → engine surfaces a stronger prompt; if user doesn't engage in another 5 min, abort to `manual`.

### What the engine does NOT do

- Does not solve CAPTCHA programmatically. CAPTCHA inside an auth flow uses the same `pause_for_captcha` flow as anywhere else.
- Does not bypass terms-of-service checkboxes. Those are required consents and the LLM must check them via `set_checkbox`.
- Does not store credentials without explicit user opt-in via `persist_site_credential`.

## ATS Knowledge

Per-ATS hints live in YAML, loaded into the orchestrator's system prompt at run start.

```yaml
# knowledge/lever.yaml
ats: lever
detect:
  url_contains: ["jobs.lever.co"]
  dom_signature: 'form[data-qa="application-form"]'

submit:
  final_button_text: ["Submit application", "Submit"]
  final_button_disabled_until_complete: true
  required_consents:
    - pattern: "I have read and accept"
    - pattern: "consent to be contacted"

custom_widgets:
  - kind: location_autocomplete
    selector_hint: 'input[aria-label*="location"]'
    delegate_to_perception: true

known_questions:
  - matches: ["authorized to work in", "legally authorized"]
    profile_path: applicant.work_authorized_us
  - matches: ["require sponsorship", "need sponsorship"]
    profile_path: applicant.requires_sponsorship
  - matches: ["how did you hear", "how did you find"]
    answer: "Referral"

success:
  banner_pattern: ".application-success"
  url_contains: "/thanks"

known_blockers:
  - kind: hcaptcha
    pause_advice: "Verify in the open browser; click Resume AI when done."
```

When `detect_ats(job.apply_url)` returns `"lever"`, the engine loads `lever.yaml` and embeds it into the orchestrator's system prompt as authoritative hints. Unknown ATS → no YAML loaded; the engine relies on perception tools and the LLM's general knowledge.

YAML hints handle ~80% of ATS-specific knowledge. The rest needs hooks (next section).

## Per-ATS Hooks (Plugin Layer)

Some ATS quirks are behavioral and can't fit in YAML. Examples observed in the wild:

- **Workday**: candidate-home redirect after login; multi-iframe form rendering; resume parsing latency varies 2–15s; "Save and Continue" sometimes commits, sometimes opens a modal.
- **Oracle Cloud / iCertis / JPMC**: email-first login dance (enter email → page reloads → password field appears); terms checkbox must be checked before the page recognizes the email field as valid.
- **Lever**: both consent checkboxes get cleared when validation fails on submit; resume input clears after CAPTCHA verification errors.
- **Dice**: gateway flow that hands off to the employer's actual ATS via a redirect; both sides need different hint sets.

These are too procedural to YAML cleanly. Hooks let an ATS provide specific lifecycle code without owning the entire flow.

### Hook protocol

```python
class Hook(Protocol):
    """All methods optional. Default no-op implementations live in hooks/base.py."""

    def prompt_addendum(self) -> str:
        """Optional extra system-prompt text for this ATS, beyond YAML knowledge."""
        return ""

    def on_observe(self, observation: Observation, ctx: HookContext) -> Observation | None:
        """Augment or filter the observation before it reaches the orchestrator.
        Return a new Observation, or None to keep the original.
        Examples: add a hint pointing to a specific iframe; mark an iframe as
        the relevant one when DOM extraction surfaces several."""
        return None

    def before_tool(self, tool_use: ToolUse, ctx: HookContext) -> ToolOutcome | None:
        """Optionally short-circuit a tool call by handling it in the hook.
        Return None to let the engine dispatcher handle it normally.
        Example: Workday's upload_file pre-step that traverses to the
        correct frame before the engine attempts the actual upload."""
        return None

    def after_tool(self, tool_use: ToolUse, outcome: ToolOutcome, ctx: HookContext) -> None:
        """Side-effects after a tool ran. Examples: Lever re-checks consent
        boxes after a validation error tool_result."""
        pass

    def fatal(self) -> bool:
        """Allow the hook to abort the run on a condition the engine can't
        detect alone. Used very rarely. Default false."""
        return False
```

### Hook scope guardrails

- A hook can NEVER call an LLM directly. Hooks use Playwright primitives only.
- A hook can NEVER bypass safety. The safety harness still runs on every tool call regardless of hook origin.
- A hook can NEVER store state outside its `HookContext`. No globals, no module-level mutation. Each apply run gets a fresh hook instance.
- Hooks are 50–150 LOC each. If a hook needs more than that, the YAML hints are insufficient and the underlying ATS knowledge schema needs an upgrade — preferred over growing the hook.

### When to write a hook vs. extend YAML

| Pattern | Where it lives |
|---|---|
| Known field labels and answers | YAML `known_questions` |
| Submit button text variants | YAML `submit.final_button_text` |
| Success URL patterns | YAML `success.url_contains` |
| Captcha origin allowlist | YAML `known_blockers` |
| Iframe traversal sequence to reach the form | Hook `on_observe` (set the right active frame) |
| Post-upload extraction wait that varies by ATS | Hook `after_tool(upload_file, ...)` |
| Re-checking consents after validation failure | Hook `after_tool(submit, …)` |
| Multi-step wizard navigation that can't be expressed as "click Save and Continue" | Hook `before_tool(click, …)` |

### Initial hooks

Phase 4 ships with three hooks that real apply failures from the existing system have justified:

- `lever.py` (~70 LOC): consent re-check after validation, resume re-attach after verification error.
- `oracle_cloud.py` (~120 LOC): email-first login sequence, terms-checkbox-then-password ordering, multi-page application navigation.
- `workday.py` (~150 LOC): iframe traversal, post-upload extraction wait, candidate-home redirect handling.

Other ATSs (Greenhouse, Ashby, Dice, iCIMS, LinkedIn) start hook-free. Hooks added later only when failure metrics justify them.

## Conversation State

The orchestration LLM (GPT-5.5 Responses API) maintains state via `conversation_id`. The engine never re-sends previous observations or tool results.

```python
class OpenAIResponsesLLM:
    def start_conversation(self, *, system_prompt, tools) -> Conversation:
        conv = self.client.conversations.create(
            metadata={"apply_run_id": run_id},
        )
        # Initial response sets the system prompt + tool catalog
        self.client.responses.create(
            conversation=conv.id,
            model="gpt-5.5",
            input=[{"role": "developer", "content": system_prompt}],
            tools=tools,
            tool_choice="none",  # no action yet, just prime the conversation
        )
        return Conversation(id=conv.id, ...)

    def tool_call(self, conv, *, new_input, tool_choice):
        response = self.client.responses.create(
            conversation=conv.id,
            input=new_input,             # ONLY the new observation/screenshot
            tool_choice=tool_choice,
        )
        return ToolUseResult.from_response(response)

    def append_tool_result(self, conv, *, tool_use_id, ok, payload):
        self.client.responses.create(
            conversation=conv.id,
            input=[{"type": "function_call_output", "call_id": tool_use_id,
                    "output": json.dumps({"ok": ok, **payload})}],
        )
```

Per-turn input is ~one screenshot + one observation JSON, regardless of which turn you're on. On a 10-page Workday flow this is dramatic vs. the alternative.

When the apply run ends, the conversation is retained for ~24h (trace + debugging) then auto-deleted via lifecycle policy. No PII stays on the provider longer than necessary.

## Vision Policy

**Default-on at every observation.** Stateful conversations make the per-turn cost bounded, so vision is no longer a sometimes-feature.

- **Capture**: `page.screenshot(full_page=False)` after each settled mutation. Viewport-only by default; full-page only when explicitly requested by the LLM via a `screenshot(full_page=true)` tool.
- **Encode**: PNG, base64, max-edge 1568px (Anthropic-recommended; OpenAI doesn't penalize larger but doesn't gain accuracy above this either).
- **Storage**: in-memory cache keyed by `screenshot_id` (UUID). Engine references screenshots by ID in tool inputs; the LLM never sees the bytes directly outside the vision content blocks.
- **Trace**: hash-only in the persisted trace. Raw screenshots saved to a debug folder under the apply run, expires per retention policy.

Cropping for delegated perception calls: when the orchestrator calls `read_custom_widget` with `widget_bounds`, the engine crops the screenshot before sending to Opus. Saves Opus tokens and focuses attention.

## Failure Modes

| Mode | Detection | Response |
|---|---|---|
| LLM returns invalid `target_id` | safety.action_validator | `tool_result(ok=false, reason="stale_target_id")`. Counter increments; after 5 consecutive, abort run with `manual`. |
| Pre-submit validation fails | safety.pre_submit_validator | `tool_result` describes missing fields. LLM repairs. After 3 consecutive validation failures, abort with `manual`. |
| Active CAPTCHA detected | safety.captcha_pauser (DOM ≥0.85 OR perception ≥0.85) | `pause_for_captcha` triggered. UI surfaces manual bar. User completes; clicks Resume; engine re-observes and continues. |
| LLM calls `report_complete` without verifiable evidence | safety.completion_verifier | `tool_result(ok=false, reason="completion_unverified")`. LLM continues. After 3 consecutive unverified claims, abort with `manual`. |
| LLM calls forbidden action | safety.action_validator | Reject + tool_result. After 2 violations, abort with `failed` — the LLM is misbehaving. |
| Per-run cost budget would be exceeded ($1.00 default) | safety.cost_guard | Abort with `manual`, reason=`run_budget_exceeded`. **Modal C surfaces immediately.** Telemetry recorded. |
| Daily spend crosses 80% (first time today) | safety.cost_guard daily ledger | **Modal A surfaces immediately.** Subsequent applies that day default to cost-saving mode (text-only orchestration) unless the user raises the cap. |
| User clicks AI Apply while daily spend ≥80% but <100% | engine pre-flight check | **Modal B surfaces** offering Cancel / Run in Cost-Saving Mode / Raise Daily Budget. No silent degradation. |
| Per-day cost budget would be exceeded ($20.00 default) | safety.cost_guard | **Modal D surfaces.** New applies refuse to start until midnight rollover. Trace records reason=`day_budget_exceeded`. |
| Turn budget exhausted | engine main loop (MAX_TURNS) | Abort with `manual`. |
| Auth required, no credential, registration declined by user | engine auth tool dispatch | Abort with `manual`, reason=`auth_required`. Browser left open. |
| MFA timed out | engine pause_for_mfa watchdog | Stronger UI prompt at 5 min, abort with `manual` at 10 min. |
| Hook returns `fatal()` | engine after each hook call | Abort with `failed`, reason from hook. |
| Network error / Playwright crash | wrapping try/except in dispatch | Abort with `failed`. Save trace. |
| LLM API error | wrapping try/except around `tool_call` | Retry up to 3 times with backoff. Then abort with `failed`. |
| Submit succeeds but no confirmation visible | engine post-submit observation + completion_verifier | `report_complete` rejected; LLM's options are wait + re-observe, or `report_blocked`. |
| Resume upload but autofill never appears | upload_file dispatcher waits up to per-ATS timeout, returns whatever it sees | LLM gets the post-extraction observation (with or without autofilled values); decides whether to fill remaining fields manually. |
| Apply already submitted (duplicate) | DOM signature for "already applied" banners | Engine surfaces it as a special observation; LLM calls `report_complete` with `duplicate=true`. Verifier accepts duplicate-banner evidence as success. |

## Cost & Latency Budgets

These are projections, not guarantees. They will likely be wrong on the first 100 production runs. The point of starting with telemetry from day one is to replace these numbers with measured ones quickly.

| Metric | Projection (single-page form) | Projection (10-page Workday) |
|---|---|---|
| Total LLM calls per apply | 5–8 | 30–50 |
| Per-apply input tokens | 30–60k | 120–200k |
| Per-apply output tokens | 1–3k | 5–10k |
| Per-apply USD (rough) | $0.05–0.15 | $0.30–0.80 |
| Wall-clock | 20–45s | 90–180s |
| Vision calls (perception) | 0–2 | 2–6 |

### Hard enforcement, day one

- **Per-run budget**: default **$1.00**, configurable in Settings → Workflow (range $0.10–$5.00). Engine refuses to start a new turn if the projection puts the run over the cap. Runs end with `status=manual, reason=run_budget_exceeded`.
- **Per-day budget**: default **$20.00 / user / day**, configurable in Settings → Workflow (range $5.00–$100.00). Soft warning at 80% spent (UI banner + degraded text-only orchestration). Hard refusal of new applies at 100% (`status=manual, reason=day_budget_exceeded`). Day boundary is the user's local midnight.
- **Telemetry on every run**: input tokens, output tokens, perception calls, total spend, turn count, wall-clock. Persisted alongside the trace. Daily spend ledger persisted per user so the daily cap survives app restarts.

### What we do with the telemetry

- After 50 runs, recompute the projections and adjust the default budget.
- Identify outliers (runs that cost 3× median) and inspect their traces. Common causes: unbounded retry loops, vision-on-pages-that-don't-need-it, hooks that observe redundantly.
- Identify per-ATS cost variance. If Workday consistently costs 5× Lever, the Workday hook has room to optimize (skipping perception calls when DOM is sufficient, etc.).

### Budget notifications — no silent behavior changes

Any time the engine changes its behavior because of a budget threshold, the user is informed via a modal **at the moment of the change**. Nothing degrades, pauses, or aborts silently.

Four modal triggers, each fired exactly once per cause:

#### Modal A — Daily budget at 80% (first crossing today)

Fires the moment cumulative daily spend crosses 80% of the daily cap. After this, future apply runs in the same day will run in cost-saving mode (text-only orchestration, no vision per turn).

```
┌──────────────────────────────────────────────────────────────┐
│  AI Apply daily budget at 80%                                │
│                                                              │
│  You've used $16.00 of your $20.00 daily AI Apply budget.    │
│  Apply runs for the rest of today will use cost-saving mode  │
│  (no per-turn screenshots) so you don't run out before       │
│  midnight.                                                   │
│                                                              │
│  Want more headroom?                                          │
│                                                              │
│              [ Continue ]   [ Raise Daily Budget ]           │
└──────────────────────────────────────────────────────────────┘
```

The "Raise Daily Budget" button opens Settings → Workflow with the daily-budget field focused. The "Continue" button accepts the degraded mode for the rest of the day. Modal fires only on the FIRST crossing of 80% per calendar day.

#### Modal B — Cost-saving mode about to apply (per apply run)

Fires when the user clicks "AI Apply" and the daily ledger is between 80% and 100%. Lets the user opt out of degraded mode for *this specific apply* by temporarily raising the cap.

```
┌──────────────────────────────────────────────────────────────┐
│  This apply will run in cost-saving mode                     │
│                                                              │
│  Daily AI Apply budget is at 87%. To preserve the rest of    │
│  the day's budget, this apply will run without vision        │
│  (text-only). Visual elements like custom widgets and        │
│  CAPTCHA detection may be less reliable.                     │
│                                                              │
│      [ Cancel ]   [ Run in Cost-Saving Mode ]                │
│      [ Raise Daily Budget and Run Normally ]                 │
└──────────────────────────────────────────────────────────────┘
```

#### Modal C — Per-run budget exhausted mid-run

Fires the moment a running apply's spend crosses its per-run cap. The apply pauses, the browser stays open for manual completion.

```
┌──────────────────────────────────────────────────────────────┐
│  AI Apply hit the per-run cost limit                         │
│                                                              │
│  This application reached the $1.00 per-run cost cap before  │
│  finishing. The browser is open — you can finish manually.   │
│                                                              │
│  This is the 3rd apply today that hit the per-run cap.       │
│  Consider raising the per-run cost limit if this happens     │
│  often.                                                      │
│                                                              │
│      [ Got It ]   [ Raise Per-Run Budget ]                   │
└──────────────────────────────────────────────────────────────┘
```

The 3rd-time hint becomes visible only after the user has hit the per-run cap multiple times in a single day, so a one-off doesn't feel naggy.

#### Modal D — Daily budget exhausted

Fires the moment cumulative daily spend reaches 100%. Also fires when the user clicks "AI Apply" while already at 100%.

```
┌──────────────────────────────────────────────────────────────┐
│  Daily AI Apply budget exhausted                             │
│                                                              │
│  You've used $20.00 of your $20.00 daily AI Apply budget.    │
│  New AI Apply runs are paused until midnight (local time).   │
│                                                              │
│  Options:                                                    │
│  • Wait for the daily budget to refresh at midnight          │
│  • Raise the daily budget in Settings                         │
│  • Apply manually for the rest of today                      │
│                                                              │
│      [ Got It ]   [ Raise Daily Budget ]   [ Apply Manually ]│
└──────────────────────────────────────────────────────────────┘
```

### Modal-trigger rules

- Each modal fires **exactly once per cause per calendar day** (Modal A and D), or **once per apply run** (Modal B and C).
- Closing a modal never bypasses the underlying behavior. Modal A's "Continue" still leaves degraded mode active; user must explicitly raise the budget to opt out.
- Modals are blocking — the user must dismiss before the next action proceeds.
- All four modals link to Settings → Workflow with the relevant field focused. No need to hunt for it.
- Trace records `budget_modal_shown=A|B|C|D` per apply run so we can measure how often each fires.

### Apply trace and per-apply badge

Every apply trace records whether the apply ran in degraded mode and which budget modals were surfaced during the run. The dashboard apply detail view shows a small badge ("Cost-saving mode") so the user knows after the fact why a particular apply might have behaved differently.

## Test Strategy

Three layers, in order of value-per-test.

### 1. Fixture-based unit tests

Local HTML files served from disk via Playwright. Cover the safety harness, the perception tools (with stub LLM responses), and per-ATS YAML loading.

```text
tests/fixtures/apply/
  lever/
    aledade_application.html
    aledade_with_captcha.html
    aledade_post_verification.html
  greenhouse/
  ashby/
  oracle/
  workday/
  custom_widgets/
    date_picker_kendo.html
    location_autocomplete_algolia.html
```

For each fixture:
- safety harness validation runs
- perception tools mocked to return canned responses
- assertion: engine ends in expected status with expected tool-call sequence

### 2. Recorded-conversation replay tests

For each ATS, capture a real LLM conversation once (with care to scrub PII), then replay it deterministically. Detects regressions in the engine loop, safety harness, or tool-routing layer without making fresh LLM calls.

```python
@pytest.mark.replay("lever_aledade_2026_05.jsonl")
def test_lever_aledade_replay():
    result = engine.run_apply(...)
    assert result.status == "success"
```

### 3. Live integration tests (opt-in)

```bash
RUN_LIVE_APPLY_TESTS=1 pytest tests/integration/apply/
```

Real Playwright + real LLM calls. Never against real production ATS unless the test job is explicitly marked safe-to-submit. Used pre-release for confidence; not in CI.

## Trace / Debuggability

Per-run trace JSON, written to `~/Library/Application Support/JobAppsWorkflowSystem/apply_traces/<run_id>/`.

```jsonc
{
  "run_id": "...",
  "job_id": "...",
  "ats": "lever",
  "started_at": "...",
  "finished_at": "...",
  "status": "success",
  "model_orchestrator": "gpt-5.5",
  "model_perception": "claude-opus-4-7",
  "conversation_id": "conv_...",          // GPT-5.5 conversation handle
  "turns": [
    {
      "turn": 0,
      "observation": {"screenshot_hash": "sha256...", "field_count": 14, "page_url": "..."},
      "tool_calls": [
        {"name": "upload_file", "input": {"target_id": "frame_0:el_3", "file_role": "resume"},
         "reason": "Uploading resume to attached input.",
         "result": {"ok": true, "duration_ms": 412}}
      ]
    },
    // ... per-turn entries
  ],
  "perception_calls": [
    {"turn": 4, "tool": "classify_captcha", "input_screenshot_hash": "sha256...",
     "result": {"active": true, "kind": "hcaptcha", "confidence": 0.94}}
  ],
  "safety_events": [
    {"turn": 7, "kind": "pre_submit_validation_failed",
     "missing_required": ["frame_0:el_18"]}
  ],
  "final_evidence": {"confirmation_screenshot_hash": "sha256...", "url": ".../thanks"}
}
```

Sensitive values (passwords, SSN, etc.) masked at write-time. Screenshots stored as separate files keyed by hash; trace references them by hash so the JSON itself is small and shareable.

A trace viewer in the dashboard (Phase 5 — see Migration Path) renders this as a per-turn timeline with thumbnail screenshots, tool calls, and decisions inline.

## Migration Path

Six phases. Each is shippable on its own.

### Phase 1: Foundation

- `models.py`, `primitives/observation.py`, `primitives/playwright_actions.py`, `primitives/vision.py`, `safety.py`
- Pydantic types for `Observation`, `ToolCall`, `ToolResult`, `ApplyRunResult`
- Vision encoding helper, screenshot capture/cropping
- Safety harness with unit tests against fixture HTML

**Acceptance**: a Python script can call `observe(page)` on a fixture and get a structured `Observation`, and `safety.validate_pre_submit(obs)` returns the right `ValidationResult`. No LLM yet.

### Phase 2: LLM clients + tool routing

- `llm/base.py` (abstract `LLMClient.tool_call`)
- `llm/openai_responses.py` with `conversation_id` support
- `llm/anthropic.py` with single-turn vision calls
- `llm/routing.py` with the `TOOL_ROUTES` dispatch table
- `tools/world.py` and `tools/perception.py` with schemas

**Acceptance**: an integration test makes a single tool_call to GPT-5.5, gets back a parsed `tool_use` block, dispatches it, and a delegated `classify_captcha` call routes to Opus and returns parsed output.

### Phase 3: Engine loop

- `engine.py` with the main loop
- Run against the simplest fixture (single-page Lever) end-to-end
- Trace writer

**Acceptance**: `engine.run_apply(lever_aledade_fixture)` ends with `status=success` and a complete trace.

### Phase 4: ATS knowledge + hooks + first ATSs rollout

- `_common.yaml`, `lever.yaml`, `oracle_cloud.yaml`, `workday.yaml` (and one more from the failure-frequency leaderboard)
- Initial hooks: `lever.py`, `oracle_cloud.py`, `workday.py`
- ATS detection from URL/DOM with explicit per-ATS confidence threshold
- Auth tools (`tools/auth.py`) wired to existing `apply_site_sessions` keychain helpers
- Live integration tests against real Lever, Oracle, Workday jobs (dry-run mode — tests stop at `is_final=true`)

**Acceptance**: real apply runs in dry-run mode complete through the form and stop at the final submit gate on Lever, Oracle, and Workday. Trace shows all tool calls, vision decisions, hook invocations, safety checks, and cost.

### Phase 5: Soft cutover + trace viewer + remaining YAMLs

- New engine becomes the **default** for AI Apply runs. Existing legacy adapters remain available as opt-in fallback per-ATS via a config flag (`apply_engine_fallback_for: ["workday"]` etc.).
- Dashboard trace viewer route (`/apply-traces/<run_id>`)
- Per-apply badge in the UI: "engine: new" or "engine: legacy fallback" so user can see which ran.
- Remaining YAML hints: `greenhouse.yaml`, `ashby.yaml`, `dice.yaml`, `icims.yaml`, `linkedin.yaml` — written iteratively as failures accumulate.
- Cost telemetry surfaced in the apply detail view.

**Acceptance**: most apply runs use the new engine. Any ATS-specific failure can be diagnosed from the trace viewer. New YAML hint additions resolve >80% of recurring failures without code changes. The legacy adapters still work and can be selected manually for any ATS where the new engine is failing.

### Phase 6: Win-rate gate + soft removal of legacy paths

This phase ends only when **measured** numbers gate the deletion. No date-based commitments.

For each ATS, compare new-engine vs legacy-adapter on the same job feed:

| Metric | New engine threshold to retire legacy |
|---|---|
| Apply success rate | ≥ 110% of legacy (or matching with strong cost win) |
| Manual-handoff rate | ≤ legacy + 5pp |
| Cost per successful apply | ≤ 1.5× legacy |
| Trace diagnostic completeness | 100% (already a property of the new engine) |

For ATSs that meet the gate: the legacy adapter is removed from `legacy/`. Its YAML hints + hook (if any) are now the sole path.

For ATSs that do NOT meet the gate: legacy stays as the primary, new engine remains opt-in. The per-ATS hook/YAML keeps improving until the gate is met.

**Acceptance**: at least 4 ATSs (Lever, Greenhouse, Ashby, plus one more) have retired their legacy adapters. Workday and Oracle Cloud may or may not — their complexity may justify keeping the legacy adapter longer.

### Phase 7: Final cleanup (only when ready)

- Remove `legacy/` for any remaining ATSs that have met the gate.
- Remove `application_answer_service.py` and the legacy JSON-parse helpers in `anthropic_client.py` once no caller depends on them.
- Update CLAUDE.md and architecture docs to reflect the final shape.

**Acceptance**: any remaining `legacy/` files have a documented reason (typically: complexity gate not yet met). No dead code.

## Risks

| Risk | Mitigation |
|---|---|
| Two LLM providers, two SDKs, two billing surfaces | Routing layer hides this from the engine. SDK upgrades touch one file each. Single trace format. |
| Cross-provider tool-call mismatches | Tool schemas normalized in `tools/world.py` and `tools/perception.py`; provider clients translate to provider-native shapes. Type-checked. |
| GPT-5.5 conversation drift on long applications | Hard turn limit (`MAX_TURNS=80`); engine forces a manual handoff if exceeded. Per-turn observation always includes the apply URL so the LLM can re-orient. |
| Opus delegation latency (extra round-trip) | Only invoked on positive captcha-suspicion DOM signal or explicit widget detection; cached per `(screenshot_hash, tool_name)` within a run. |
| Vision tokens balloon | Viewport-only screenshots; max-edge 1568px; full-page only on explicit request. Cost guard enforces hard per-run budget; degrades to text-only orchestration when daily budget is low. |
| LLM calls forbidden actions | Safety action_validator with hard reject. After 2 violations: abort. Trace records the violations for prompt tuning. |
| ATS DOM changes break detection | YAML hints + optional hooks instead of state machines. Fixture replay tests catch regressions; YAML edits + small hook adjustments are the typical fix. |
| CAPTCHA pause false-negative (visible challenge but DOM and vision both miss it) | Either-source rule (≥0.85 from DOM or vision triggers pause) catches more cases than both-required. Beyond that: trace flags `single_signal=true` cases for prompt tuning. Fundamental limit: vision must see the page; if a CAPTCHA renders out-of-viewport before extraction, it gets missed until the LLM scrolls. |
| CAPTCHA pause false-positive (no challenge, but engine pauses) | Trace metric tracks `pause_resumed_immediately` (user clicks Resume within 5s of pause) as a false-positive proxy. Tunable confidence thresholds in `_common.yaml` per signal type. |
| `report_complete` claimed without true success | CompletionVerifier requires ≥2 success signals (URL + banner / banner + no-errors / etc.). Three consecutive unverified claims abort the run. LLM cannot mark success by assertion. |
| Auth credential mishandling | Passwords pulled at dispatch time inside the engine; never serialized into conversation, trace, or screenshots. DOM extraction masks `type=password`. `apply_site_sessions` keychain integration audited per migration. |
| Auto-registration loops on a misconfigured site | Safety harness limits `register_account` to 1 attempt per run. Beyond that → `request_manual`. |
| Adapter removal breaks an ATS we still need | Phase 6 win-rate gates legacy-adapter removal per ATS. Workday/Oracle may keep their legacy adapter as primary indefinitely; that's fine. |
| Conversation state retains PII | `conversation_id` lifecycle policy: 24h TTL, then auto-delete. No passwords ever included in observations. SSN-shaped values masked at observation time. |
| Cost overrun on a single bad apply | Hard per-run cost guard (default $1.00, configurable). Engine refuses next turn if projected cost exceeds budget. **Modal C surfaces** so the user sees what happened. Telemetry persists every run. |
| Cost overrun across many applies in one day | Daily ledger + per-day cap (default $20.00, configurable). **Modals A/B/D surface at 80%, on each subsequent apply, and at 100%** so behavior changes are never silent. Resets at user's local midnight. |
| User confused by sudden degradation in apply behavior | "No silent switches" rule: any budget-driven behavior change fires a modal. Apply trace records degraded-mode runs; UI shows a "Cost-saving mode" badge on the apply detail view. |
| Provider outage on either OpenAI or Anthropic | Engine continues for outage on Anthropic (perception falls back to "skip" and DOM heuristics drive). Engine cannot continue on outage of OpenAI (orchestrator) — falls back to legacy adapter for that ATS if available, else status=`failed`. Dashboard surfaces provider health. |

## Open Decisions

These need a call before Phase 2:

- **Conversation retention window**: 24h default; 7d for premium users? (Affects PII exposure window.)
- ~~**Cost budget per apply**~~: resolved — $1.00/run and $20.00/day, configurable in Settings → Workflow.
- **Manual handoff UX**: full pause-with-modal (current pattern) or non-blocking notification with "complete in browser" button? Affects whether the apply run is a foreground operation in the user's workflow.
- **Fallback when GPT-5.5 Responses API is unavailable**: silent failover to Anthropic-only orchestration (lose stateful conversations, cost goes up), or hard fail and surface as "service degraded"?
- **Replay testing budget**: how many recorded conversations to maintain, and how often to refresh them as ATSs evolve?
- **Privacy mode**: toggle to disable trace persistence entirely for users on shared machines?

## Success Metrics

- **Apply completion rate** ≥ 85% on known ATSs, ≥ 60% on unknown
- **Required-field correctness** at submit: 100% (deterministic check)
- **CAPTCHA pause precision**: <2% false-positive rate (pause when no challenge), <5% false-negative (continue through challenge)
- **Median apply duration**: <60s on single-page forms
- **Manual-handoff rate**: <15% of attempts
- **Trace diagnostic completeness**: 100% of failed runs answer "what failed and why" from the trace alone, no log diving
- **Per-ATS code surface**: 0 lines of ATS-specific Python; all hints in YAML

## What This Plan Does Not Include

- **No per-ATS state machines.** Procedural knowledge lives in YAML hints + optional hooks.
- **No premature deletion of legacy adapters.** They move to `legacy/` at the start of the migration and only retire per-ATS once the new engine beats them on measured metrics (Phase 6).
- **No ad-hoc JSON-parse paths.** All structured output goes through tool use.
- **No vision opt-in policy.** Vision is on by default; the question is just where to send it. Falls back to text-only when the daily cost budget crosses 80% — and that fallback is announced via a modal, never silent.
- **No silent budget-driven behavior changes.** Every threshold crossing (80% daily, 100% daily, per-run cap hit) surfaces a modal at the moment of change. The user can always raise the budget from the modal directly.
- **No browser-control LLM (Computer Use).** Both OpenAI and Anthropic offer it; both are slower and more expensive than scripted Playwright + tool use. Reserve Computer Use as a future fallback for sites where even the AI-orchestrated engine fails repeatedly.
- **No automatic ATS-detection heuristic improvements in v1.** YAML hints are loaded once at run start. If detection is wrong, the LLM falls back to general behavior. Better detection is a future optimization.
- **No password handling in the LLM conversation.** Auth tools pull credentials inside the engine dispatcher; the LLM never sees them.
- **No `report_complete` by LLM assertion.** CompletionVerifier gates every success claim against local evidence.

## First Implementation Slice

Aligned with Phase 1 + part of Phase 2:

1. `models.py` — Pydantic types for `Observation`, `ToolCall`, `ToolResult`, `HookContext`, `ApplyRunResult`, `CompletionResult`.
2. `primitives/observation.py` and `primitives/playwright_actions.py` — extract from existing code.
3. `primitives/vision.py` — new, ~50 lines.
4. `safety.py` — new, ~350 lines including pre-submit validator, completion verifier, captcha pauser, action validator, and cost guard. Unit-tested against fixture HTML.
5. `llm/base.py` and `llm/openai_responses.py` — new. Make a single round-trip with `conversation_id` work end-to-end.
6. `tools/world.py` and `tools/auth.py` schemas (no implementations yet — schemas only).
7. `hooks/base.py` — null-default Hook protocol.

Stop there. That's enough infrastructure to validate three high-risk pieces all at once:

- The conversation-state pattern actually saves tokens on a real multi-turn run.
- The safety harness rejects what it should (verified in unit tests against fixture pages).
- The completion verifier and cost guard are wired before any other code can call them — so they're never bypassed.

The full engine loop, hook invocation, and Opus delegation come next, once that foundation is proven.
