from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from job_apps_system.db.models.jobs import Job
from job_apps_system.integrations.google.sheets import GoogleSheetsClient
from job_apps_system.services.setup_config import load_setup_config


EM_JOBS_HEADERS: list[str] = [
    "Applied",
    "Resume URL",
    "Created Time",
    "Score",
    "id",
    "trackingid",
    "Company Name",
    "Job TItle",
    "Job Description",
    "Apply URL",
    "Company URL",
    "Job Poster",
    "Job Poster Title",
    "Job Poster LinkedIn",
    "Job Poster Email",
    "Job Poster Email Sent",
    "CTO",
    "CTO Title",
    "CTO Email",
    "CTO Email Sent",
    "HR",
    "HR Title ",
    "HR Email",
    "HR Email Sent",
]

PROCESSED_JOBS_HEADERS: list[str] = [
    "id",
    "trackingid",
    "Company Name",
    "Job Title",
    "Processed At",
    "Decision",
]


class SheetSyncService:
    def __init__(self, session: Session) -> None:
        self._session = session
        self._config = load_setup_config(session)
        self._project_id = self._config.app.project_id
        self._sheets = GoogleSheetsClient(session=session)

    def ensure_configured_headers(self) -> dict[str, dict[str, Any]]:
        results: dict[str, dict[str, Any]] = {}
        resources = self._config.google.resources

        if resources.em_jobs_sheet:
            results["em_jobs_sheet"] = self._sheets.ensure_headers(resources.em_jobs_sheet, EM_JOBS_HEADERS)

        if resources.processed_jobs_sheet:
            results["processed_jobs_sheet"] = self._sheets.ensure_headers(
                resources.processed_jobs_sheet,
                PROCESSED_JOBS_HEADERS,
            )

        return results

    def get_processed_job_ids(self) -> set[str]:
        processed_sheet = self._config.google.resources.processed_jobs_sheet
        if not processed_sheet:
            return set()
        records = self._sheets.get_records(processed_sheet)
        return {record["id"] for record in records if record.get("id")}

    def get_em_job_records(self) -> list[dict[str, str]]:
        em_jobs_sheet = self._config.google.resources.em_jobs_sheet
        if not em_jobs_sheet:
            return []
        return self._sheets.get_records(em_jobs_sheet)

    def append_records(self, sheet_ref: str, headers: Sequence[str], rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
        return self._sheets.append_records(sheet_ref, headers, rows)

    def sync_em_jobs_to_db(self) -> dict[str, Any]:
        records = self.get_em_job_records()
        created = 0
        updated = 0

        for record in records:
            job_id = _clean(record.get("id"))
            if not job_id:
                continue

            record_id = _project_record_id(self._project_id, job_id)
            row = self._session.get(Job, record_id)
            if row is None:
                row = Job(record_id=record_id, project_id=self._project_id, id=job_id, applied=False)
                self._session.add(row)
                created += 1
            else:
                updated += 1

            row.tracking_id = _clean(record.get("trackingid")) or None
            row.company_name = _clean(record.get("Company Name")) or None
            row.job_title = _clean(record.get("Job TItle")) or _clean(record.get("Job Title")) or None
            row.job_description = _clean(record.get("Job Description")) or None
            row.apply_url = _clean(record.get("Apply URL")) or None
            row.company_url = _clean(record.get("Company URL")) or None
            row.score = _parse_int(record.get("Score"))
            row.applied = _parse_bool(record.get("Applied"))
            row.resume_url = _clean(record.get("Resume URL")) or None
            row.created_time = _parse_datetime(record.get("Created Time"))

        self._session.flush()
        return {
            "ok": True,
            "row_count": len(records),
            "created": created,
            "updated": updated,
        }


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _parse_int(value: Any) -> int | None:
    raw = _clean(value)
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def _parse_bool(value: Any) -> bool:
    raw = _clean(value).lower()
    return raw in {"y", "yes", "true", "1"}


def _parse_datetime(value: Any) -> datetime | None:
    raw = _clean(value)
    if not raw:
        return None
    for candidate in (raw, raw.replace("Z", "+00:00")):
        try:
            return datetime.fromisoformat(candidate)
        except ValueError:
            continue
    return None


def _project_record_id(project_id: str, job_id: str) -> str:
    return f"{project_id}:{job_id}"
