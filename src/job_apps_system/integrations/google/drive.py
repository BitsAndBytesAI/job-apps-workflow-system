from __future__ import annotations

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from job_apps_system.integrations.google.oauth import get_google_credentials

GOOGLE_DRIVE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


def build_drive_client(session=None):
    credentials = get_google_credentials(session=session)
    if credentials is None:
        return None
    return build("drive", "v3", credentials=credentials)


def validate_drive_resource(resource_id: str, session=None) -> dict:
    client = build_drive_client(session=session)
    if client is None:
        raise ValueError("Google is not connected.")

    try:
        file = (
            client.files()
            .get(fileId=resource_id, fields="id,name,mimeType,webViewLink")
            .execute()
        )
        return {
            "ok": True,
            "resource_id": file["id"],
            "name": file.get("name"),
            "mime_type": file.get("mimeType"),
            "url": file.get("webViewLink"),
            "error": None,
        }
    except HttpError as error:
        return {
            "ok": False,
            "resource_id": resource_id,
            "name": None,
            "mime_type": None,
            "url": None,
            "error": str(error),
        }


def ensure_drive_folder(name: str, *, parent_id: str | None = None, session=None) -> dict:
    client = build_drive_client(session=session)
    if client is None:
        raise ValueError("Google is not connected.")

    existing = _find_folder(client, name=name, parent_id=parent_id)
    if existing is not None:
        return existing

    body = {
        "name": name,
        "mimeType": GOOGLE_DRIVE_FOLDER_MIME_TYPE,
    }
    if parent_id:
        body["parents"] = [parent_id]

    created = (
        client.files()
        .create(
            body=body,
            fields="id,name,mimeType,webViewLink",
        )
        .execute()
    )
    return _normalize_drive_file(created)


def _find_folder(client, *, name: str, parent_id: str | None = None) -> dict | None:
    query_parts = [
        f"mimeType = '{GOOGLE_DRIVE_FOLDER_MIME_TYPE}'",
        f"name = '{_escape_drive_query_value(name)}'",
        "trashed = false",
    ]
    if parent_id:
        query_parts.append(f"'{parent_id}' in parents")
    else:
        query_parts.append("'root' in parents")

    response = (
        client.files()
        .list(
            q=" and ".join(query_parts),
            spaces="drive",
            fields="files(id,name,mimeType,webViewLink)",
            pageSize=1,
        )
        .execute()
    )
    files = response.get("files", [])
    if not files:
        return None
    return _normalize_drive_file(files[0])


def _normalize_drive_file(file: dict) -> dict:
    return {
        "ok": True,
        "resource_id": file["id"],
        "name": file.get("name"),
        "mime_type": file.get("mimeType"),
        "url": file.get("webViewLink"),
        "error": None,
    }


def _escape_drive_query_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")
