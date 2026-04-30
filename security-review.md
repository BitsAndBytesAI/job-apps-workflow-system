# Security Review Log

A running record of security audits performed on this repository. Each entry is a point-in-time snapshot — append new entries, do not rewrite history.

---

## 2026-04-30 — Malicious-intent scan + working-tree security review

**Reviewer:** Claude Code (Opus 4.7)
**Branch:** `main` (2 commits ahead of `origin/main`)
**Head commit:** `3528966db1e50817d37a497511c86d093963a853`
**Working tree:** 10 modified files + 1 untracked file (`src/job_apps_system/services/applicant_names.py`)

### Scope

Two reviews were run back-to-back:

1. **Malicious-intent scan** — Whole-repo grep for signs that the codebase is intentionally exfiltrating user data, harvesting credentials, or backdooring the host.
2. **`/security-review` skill** — Vulnerability review focused on changes introduced by the working-tree diff (53 KB of changes across 23 files including pending/uncommitted work).

### Review 1 — Malicious-intent scan

**Verdict: CLEAN (high confidence).** No signs of intentional malicious behavior.

#### Categories checked

| Category | Result |
|---|---|
| Suspicious outbound network calls (webhook.site, pastebin, discord, ngrok, hardcoded IPs) | None found |
| Credential exfiltration patterns (`~/.ssh`, `~/.aws`, browser profiles, `.netrc`, broad keychain enumeration) | None found |
| Code execution red flags (`eval`, `exec`, `pickle.loads`, `marshal.loads`, dynamic `__import__`, remote-URL imports) | None found |
| Obfuscation (long base64/hex blobs, ROT/XOR decoding, `codecs.decode`) | None found |
| Persistence / privilege escalation (launchd installs, crontab edits, shell-rc modifications, `sudo`, postinstall hooks) | None found |
| Supply-chain red flags (typosquats, `git+https` deps, `pip install` from URLs) | None found — all deps are official PyPI packages |
| Suspicious binary blobs | None — only legitimate Lottie JSON, PNG, SVG, and bundled JS libs under `web/static/uzhykova/` |
| Hidden network/FS IO at import time (`setup.py`, `conftest.py`, `__init__.py`) | None found |
| Third-party telemetry of prompts / user data | None found |

#### Verified-legitimate network endpoints

- `src/job_apps_system/integrations/llm/anthropic_client.py:37` → `api.anthropic.com/v1/messages`
- `src/job_apps_system/integrations/llm/openai_client.py:32` → `api.openai.com/v1/chat/completions`
- `src/job_apps_system/integrations/anymailfinder/client.py:10,218` → `api.anymailfinder.com/v5.1/find-email/decision-maker`
- Google APIs (Sheets / Docs / Drive / Gmail / Calendar) via OAuth 2.0
- LinkedIn (Playwright-driven, persistent profile at `data/browser-profiles/linkedin/`)
- ATS targets via Playwright: ashbyhq.com, greenhouse.io, icims.com, oraclecloud.com, dice.com

#### Credential handling

- API keys and OAuth tokens are stored in macOS Keychain via codesign-verified `JobAppsSecretHelper` binary (`macos/JobAppsNative/Sources/JobAppsSecretHelper/main.swift`) — entitlement-enforced, no outbound calls.
- `src/job_apps_system/config/secrets.py:333` — only `subprocess.run` invocation calls the trusted helper binary at a path resolved from the app bundle.
- `src/job_apps_system/config/secrets.py:462-472` — `keyring` library used only for legitimate Keychain access.
- No environment-variable dumps, no `~/.ssh` / `~/.aws` access, no browser-profile harvesting.

#### Working-tree files reviewed

- `src/job_apps_system/services/applicant_names.py` (untracked) — pure name-parsing utility, no I/O or network calls.
- ATS adapter modifications (`ai_browser_loop`, `ashby_adapter`, `greenhouse_adapter`, `icims_adapter`, `oracle_cloud_adapter`) — refactor of name-handling into the new shared helper; no suspicious changes.
- `src/job_apps_system/services/application_answer_service.py` — prompt-text tweaks for preferred names.

#### Coverage

LLM client integrations · secrets management · macOS Swift helper + scheduler agent · all modified ATS adapters · launcher / bootstrap code · `pyproject.toml` · git history · comprehensive grep for `eval`/`exec`, base64/hex encoding, webhook URLs, credential exfil patterns, `shell=True`, suspicious imports, env-var logging, hardcoded IPs, postinstall hooks.

---

### Review 2 — `/security-review` of working-tree diff

**Verdict: NO VULNERABILITIES FOUND.** No high-confidence (≥ 80%) findings. The PR introduces no new injection points, no authorization bypasses, and no unsafe deserialization.

#### Methodology

The review followed the `security-review` skill's three-phase methodology: (1) repository-context research, (2) comparative analysis against existing secure patterns, (3) per-file vulnerability assessment. A sub-task was launched to identify candidate vulnerabilities; with zero candidates produced, the parallel false-positive-filtering phase was skipped.

#### Threat-model assumptions (excluded from findings per scope)

- App is local-first, bound to `127.0.0.1` inside a bundled macOS app — no public exposure.
- No authentication layer is by-design and pre-existing — not flagged as a new vulnerability.
- Single-tenant, single-user SQLite-backed system — CSRF on localhost-only endpoints is out of scope.

#### PR surfaces examined

| Surface | File(s) | Verdict |
|---|---|---|
| New `POST /jobs/{job_id}/hide-card` endpoint | `web/routes/jobs.py` | Safe — Pydantic regex-bounded `page` field; SQLAlchemy ORM `session.get()`; only mutates two boolean columns |
| `HideJobCardRequest` schema | `schemas/jobs.py` | Safe — strict regex pattern `^(best_matches\|applications)$` |
| `web/templating.py` (`static_url`, `static_asset_version`) — new module | `web/templating.py` | Safe — all callers pass hardcoded literals; SHA-256 digest scoped to `STATIC_DIR` via `__file__` resolution |
| Cache-Control middleware | `main.py` | Safe — security-positive (`no-store` on HTML responses) |
| New `BOOLEAN` columns | `db/models/jobs.py`, `db/session.py` | Safe — hardcoded column names in ALTER TABLE migration loop |
| Remove-card UI | `web/static/jobs.js` | Safe — `escapeHtml(job.id)`, `escapeHtml(label)`, `encodeURIComponent` for path |
| Dice ATS handlers + JS-evaluate change | `agents/apply/ai_browser_loop.py` | Safe — static JS template, no user-controlled interpolation into `page.evaluate()` |
| Route template-import refactor (6 files) | `web/routes/{applications,communications,dashboard,interviews,onboarding,setup}.py` | Safe — behaviorally identical, import-only change |
| Template `static_url()` substitutions (8 files) | `web/templates/*.html` | Safe — hardcoded literals only |

#### Items considered and dismissed

- **Path traversal in `static_url`** — Only hardcoded literals are passed in this PR; the FastAPI `StaticFiles` mount confines serving to `STATIC_DIR` regardless.
- **XSS in new `#remove-card-modal`** — Only static text under Jinja autoescape; no untrusted variables interpolated.
- **SQL injection via `job_id` path param** — Used as an ORM primary-key lookup via `session.get()`; not string-interpolated.
- **Mass-assignment on `/hide-card`** — Endpoint hardcodes which column is set per `page` value; payload cannot mutate other columns.
- **Authorization on `/hide-card`** — By design (single-user, localhost-only); pre-existing pattern, not introduced by this PR.

---

### Out of scope (not covered by these reviews)

- Dependency CVE / vulnerable-package scan (no SCA tool was run).
- Runtime / distribution security of the macOS app bundle (codesigning, notarization, entitlement audit beyond what is referenced inline).
- Threat-model review of the "localhost-only + no auth" decision itself.
- Full-repo audit of pre-existing code outside the working-tree diff.

These should be separate exercises if/when the app moves beyond a single-user local deployment.
