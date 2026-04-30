from __future__ import annotations

from dataclasses import dataclass
import re

from job_apps_system.config.models import ApplicantProfileConfig


@dataclass(frozen=True)
class ApplicantNameParts:
    first_name: str
    last_name: str
    full_name: str
    preferred_name: str
    legal_first_name: str
    legal_last_name: str
    legal_name: str


def applicant_name_parts(applicant: ApplicantProfileConfig) -> ApplicantNameParts:
    legal_name = _clean_name(applicant.legal_name)
    preferred_name = _clean_name(applicant.preferred_name)
    legal_first_name, legal_last_name = _split_full_name(legal_name)
    first_name = _preferred_first_name(preferred_name, legal_last_name) or legal_first_name
    full_name = _join_name(first_name, legal_last_name) or preferred_name or legal_name
    return ApplicantNameParts(
        first_name=first_name,
        last_name=legal_last_name,
        full_name=full_name,
        preferred_name=preferred_name,
        legal_first_name=legal_first_name,
        legal_last_name=legal_last_name,
        legal_name=legal_name,
    )


def applicant_name_for_label(label: str, applicant: ApplicantProfileConfig) -> str:
    normalized = _normalize(label)
    names = applicant_name_parts(applicant)
    if not normalized:
        return names.full_name
    if _is_legal_name_label(normalized):
        if _is_first_name_label(normalized):
            return names.legal_first_name
        if _is_last_name_label(normalized):
            return names.legal_last_name
        return names.legal_name
    if "preferred" in normalized and "name" in normalized:
        return names.preferred_name or names.first_name
    if _is_first_name_label(normalized):
        return names.first_name
    if _is_last_name_label(normalized):
        return names.last_name
    return names.full_name


def _clean_name(value: str | None) -> str:
    return " ".join(str(value or "").split())


def _split_full_name(value: str) -> tuple[str, str]:
    parts = [part for part in value.split() if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _preferred_first_name(preferred_name: str, legal_last_name: str) -> str:
    if not preferred_name:
        return ""
    if legal_last_name:
        preferred_normalized = _normalize(preferred_name)
        last_normalized = _normalize(legal_last_name)
        if preferred_normalized.endswith(f" {last_normalized}"):
            return preferred_name[: -len(legal_last_name)].strip()
    return preferred_name


def _join_name(first_name: str, last_name: str) -> str:
    return " ".join(part for part in (first_name, last_name) if part)


def _normalize(value: str | None) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _is_legal_name_label(label: str) -> bool:
    return "legal" in label or "government name" in label or "name on government" in label


def _is_first_name_label(label: str) -> bool:
    return any(token in label for token in ("first name", "given name")) and "last" not in label


def _is_last_name_label(label: str) -> bool:
    return any(token in label for token in ("last name", "family name", "surname"))
