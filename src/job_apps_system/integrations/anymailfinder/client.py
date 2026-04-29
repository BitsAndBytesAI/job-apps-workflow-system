from __future__ import annotations

import json
from dataclasses import dataclass
from urllib import error, request


ANYMAILFINDER_DECISION_MAKER_DOC_URL = "https://anymailfinder.com/email-finder-api/docs/find-decision-maker-email"
ANYMAILFINDER_AUTH_DOC_URL = "https://anymailfinder.com/email-finder-api/docs/authentication"
ANYMAILFINDER_DECISION_MAKER_ENDPOINT = "https://api.anymailfinder.com/v5.1/find-email/decision-maker"
ANYMAILFINDER_TIMEOUT_SECONDS = 180

DECISION_MAKER_LABELS = {
    "ceo": "CEO",
    "engineering": "Engineering",
    "finance": "Finance",
    "hr": "HR",
    "it": "IT",
    "logistics": "Logistics",
    "marketing": "Marketing",
    "operations": "Operations",
    "buyer": "Buyer",
    "sales": "Sales",
}

_DECISION_MAKER_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "engineering",
        (
            "engineer",
            "developer",
            "software",
            "frontend",
            "front-end",
            "backend",
            "back-end",
            "full stack",
            "fullstack",
            "devops",
            "platform",
            "data",
            "machine learning",
            "ml ",
            " ai",
            "qa",
            "sre",
            "security engineer",
        ),
    ),
    (
        "marketing",
        (
            "marketing",
            "growth",
            "seo",
            "content",
            "brand",
            "demand generation",
            "product marketing",
        ),
    ),
    (
        "sales",
        (
            "sales",
            "account executive",
            "account manager",
            "business development",
            "bdr",
            "sdr",
            "revenue",
            "customer success",
        ),
    ),
    (
        "finance",
        (
            "finance",
            "financial",
            "accounting",
            "controller",
            "fp&a",
            "treasury",
            "bookkeeper",
        ),
    ),
    (
        "hr",
        (
            "hr",
            "human resources",
            "recruiter",
            "recruiting",
            "talent",
            "people partner",
            "people ops",
            "people operations",
        ),
    ),
    (
        "it",
        (
            "it ",
            "information technology",
            "sysadmin",
            "system administrator",
            "help desk",
            "helpdesk",
            "desktop support",
            "network administrator",
        ),
    ),
    (
        "buyer",
        (
            "buyer",
            "procurement",
            "purchasing",
            "sourcing",
        ),
    ),
    (
        "logistics",
        (
            "logistics",
            "supply chain",
            "warehouse",
            "fulfillment",
            "transportation",
        ),
    ),
    (
        "operations",
        (
            "operations",
            "ops",
            "program manager",
            "project manager",
            "chief of staff",
            "administrator",
            "office manager",
            "business operations",
        ),
    ),
)


@dataclass(frozen=True)
class DecisionMakerResult:
    decision_maker_category: str | None
    email: str | None
    email_status: str | None
    person_full_name: str | None
    person_job_title: str | None
    person_linkedin_url: str | None
    valid_email: str | None

    @property
    def best_email(self) -> str | None:
        return self.valid_email or self.email


class AnymailfinderError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def pretty_decision_maker_category(category: str | None) -> str:
    normalized = (category or "").strip().lower()
    if not normalized:
        return "Decision Maker"
    return DECISION_MAKER_LABELS.get(normalized, normalized.replace("_", " ").title())


def infer_decision_maker_category(job_title: str | None, job_description: str | None = None) -> str | None:
    haystack = " ".join(part.strip().lower() for part in (job_title or "", job_description or "") if part and part.strip())
    if not haystack:
        return None

    padded = f" {haystack} "
    for category, hints in _DECISION_MAKER_HINTS:
        if any(hint in padded for hint in hints):
            return category
    return None


def find_decision_maker_email(
    api_key: str,
    *,
    domain: str | None = None,
    company_name: str | None = None,
    decision_maker_category: str,
) -> DecisionMakerResult:
    normalized_key = (api_key or "").strip()
    if not normalized_key:
        raise AnymailfinderError("Add an Anymailfinder API key in Setup before finding contacts.")

    normalized_domain = (domain or "").strip() or None
    normalized_company_name = (company_name or "").strip() or None
    if not normalized_domain and not normalized_company_name:
        raise AnymailfinderError("A company domain or company name is required to find contacts.")

    category = (decision_maker_category or "").strip().lower()
    if not category:
        raise AnymailfinderError("A decision-maker category is required to find contacts.")

    payload: dict[str, object] = {
        "decision_maker_category": [category],
    }
    if normalized_domain:
        payload["domain"] = normalized_domain
    if normalized_company_name:
        payload["company_name"] = normalized_company_name

    body = json.dumps(payload).encode("utf-8")
    api_request = request.Request(
        ANYMAILFINDER_DECISION_MAKER_ENDPOINT,
        data=body,
        method="POST",
        headers={
            "Authorization": normalized_key,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )

    try:
        with request.urlopen(api_request, timeout=ANYMAILFINDER_TIMEOUT_SECONDS) as response:
            raw = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = _decode_error_payload(exc)
        raise AnymailfinderError(_error_message_for_status(exc.code, detail), status_code=exc.code) from exc
    except error.URLError as exc:
        raise AnymailfinderError("Anymailfinder could not be reached right now. Please try again.") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AnymailfinderError("Anymailfinder returned an unreadable response.") from exc

    return DecisionMakerResult(
        decision_maker_category=_as_optional_string(payload.get("decision_maker_category")),
        email=_as_optional_string(payload.get("email")),
        email_status=_as_optional_string(payload.get("email_status")),
        person_full_name=_as_optional_string(payload.get("person_full_name")),
        person_job_title=_as_optional_string(payload.get("person_job_title")),
        person_linkedin_url=_as_optional_string(payload.get("person_linkedin_url")),
        valid_email=_as_optional_string(payload.get("valid_email")),
    )


def _decode_error_payload(exc: error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8")
    except Exception:
        return ""
    if not raw:
        return ""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return raw.strip()
    if isinstance(payload, dict):
        for key in ("message", "error", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return raw.strip()


def _error_message_for_status(status_code: int, detail: str) -> str:
    if status_code == 400:
        return detail or "Anymailfinder rejected the lookup request."
    if status_code == 401:
        return "Anymailfinder rejected the API key. Update it in Setup."
    if status_code == 402:
        return "Anymailfinder does not have enough credits to complete this lookup."
    if detail:
        return f"Anymailfinder request failed: {detail}"
    return f"Anymailfinder request failed with status {status_code}."


def _as_optional_string(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized or None
