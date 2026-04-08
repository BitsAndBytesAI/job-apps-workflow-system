from contextlib import contextmanager
import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from job_apps_system.config.settings import settings
from job_apps_system.db.base import Base
from job_apps_system.db import models  # noqa: F401


engine = create_engine(settings.database_url, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.begin() as connection:
        _migrate_jobs_table(connection)
        _ensure_jobs_columns(connection)
        _ensure_workflow_runs_project_id(connection)
        _ensure_resumes_columns(connection)
    Base.metadata.create_all(bind=engine)


@contextmanager
def get_db_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _migrate_jobs_table(connection) -> None:
    columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(jobs)").fetchall()}
    if not columns:
        return
    if "record_id" in columns and "project_id" in columns:
        connection.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_project_id_external_id ON jobs(project_id, id)"
        )
        return

    project_id = _default_project_id(connection)
    connection.exec_driver_sql(
        """
        CREATE TABLE IF NOT EXISTS jobs_migrated (
            record_id TEXT NOT NULL PRIMARY KEY,
            project_id TEXT NOT NULL,
            id TEXT NOT NULL,
            tracking_id TEXT,
            company_name TEXT,
            job_title TEXT,
            job_description TEXT,
            posted_date TEXT,
            apply_url TEXT,
            company_url TEXT,
            score INTEGER,
            applied BOOLEAN NOT NULL,
            resume_url TEXT,
            created_time DATETIME
        )
        """
    )
    connection.exec_driver_sql(
        """
        INSERT INTO jobs_migrated (
            record_id,
            project_id,
            id,
            tracking_id,
            company_name,
            job_title,
            job_description,
            posted_date,
            apply_url,
            company_url,
            score,
            applied,
            resume_url,
            created_time
        )
        SELECT
            ? || ':' || id,
            ?,
            id,
            tracking_id,
            company_name,
            job_title,
            job_description,
            NULL,
            apply_url,
            company_url,
            score,
            applied,
            resume_url,
            created_time
        FROM jobs
        """,
        (project_id, project_id),
    )
    connection.exec_driver_sql("DROP TABLE jobs")
    connection.exec_driver_sql("ALTER TABLE jobs_migrated RENAME TO jobs")
    connection.exec_driver_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_project_id_external_id ON jobs(project_id, id)"
    )
    connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_jobs_project_id ON jobs(project_id)")


def _ensure_workflow_runs_project_id(connection) -> None:
    columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(workflow_runs)").fetchall()}
    if not columns or "project_id" in columns:
        return

    connection.exec_driver_sql("ALTER TABLE workflow_runs ADD COLUMN project_id TEXT")
    project_id = _default_project_id(connection)
    connection.exec_driver_sql(
        "UPDATE workflow_runs SET project_id = ? WHERE project_id IS NULL",
        (project_id,),
    )


def _ensure_jobs_columns(connection) -> None:
    columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(jobs)").fetchall()}
    if not columns:
        return

    additions = [
        ("posted_date", "TEXT"),
        ("job_posting_url", "TEXT"),
    ]
    for column_name, column_type in additions:
        if column_name not in columns:
            connection.exec_driver_sql(f"ALTER TABLE jobs ADD COLUMN {column_name} {column_type}")


def _ensure_resumes_columns(connection) -> None:
    columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(resumes)").fetchall()}
    if not columns:
        return

    additions = [
        ("project_id", "TEXT"),
        ("base_resume_doc_id", "TEXT"),
        ("tailored_doc_url", "TEXT"),
        ("prompt_version", "TEXT"),
        ("updated_at", "DATETIME"),
    ]
    for column_name, column_type in additions:
        if column_name not in columns:
            connection.exec_driver_sql(f"ALTER TABLE resumes ADD COLUMN {column_name} {column_type}")

    if "project_id" in {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(resumes)").fetchall()}:
        project_id = _default_project_id(connection)
        connection.exec_driver_sql(
            "UPDATE resumes SET project_id = ? WHERE project_id IS NULL",
            (project_id,),
        )


def _default_project_id(connection) -> str:
    row = connection.exec_driver_sql(
        "SELECT value_json FROM app_settings WHERE key = 'setup_config'"
    ).fetchone()
    if not row or not row[0]:
        return "engineering-manager"

    try:
        payload = json.loads(row[0])
    except json.JSONDecodeError:
        return "engineering-manager"

    app_payload = payload.get("app", {}) if isinstance(payload, dict) else {}
    project_id = (app_payload.get("project_id") or "").strip().lower() if isinstance(app_payload, dict) else ""
    job_role = (app_payload.get("job_role") or "").strip().lower() if isinstance(app_payload, dict) else ""
    candidate = project_id or job_role or "engineering-manager"

    normalized = []
    last_separator = False
    for char in candidate:
        if char.isalnum():
            normalized.append(char)
            last_separator = False
        elif char in {"-", "_", " "} and not last_separator:
            normalized.append("-")
            last_separator = True
    return "".join(normalized).strip("-") or "engineering-manager"
