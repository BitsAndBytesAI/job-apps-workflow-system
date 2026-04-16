from __future__ import annotations

from io import BytesIO

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from job_apps_system.config.resource_ids import normalize_google_resource_id
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


def copy_drive_file(file_ref: str, *, name: str, parent_id: str | None = None, session=None) -> dict:
    client = build_drive_client(session=session)
    if client is None:
        raise ValueError("Google is not connected.")

    body = {"name": name}
    if parent_id:
        body["parents"] = [parent_id]

    copied = (
        client.files()
        .copy(
            fileId=normalize_google_resource_id(file_ref),
            body=body,
            fields="id,name,mimeType,webViewLink",
        )
        .execute()
    )
    return _normalize_drive_file(copied)


def export_drive_file(file_ref: str, *, mime_type: str, session=None) -> bytes:
    client = build_drive_client(session=session)
    if client is None:
        raise ValueError("Google is not connected.")

    data = (
        client.files()
        .export(fileId=normalize_google_resource_id(file_ref), mimeType=mime_type)
        .execute()
    )
    return bytes(data)


def replace_drive_file_html(file_ref: str, *, html: str, session=None) -> dict:
    client = build_drive_client(session=session)
    if client is None:
        raise ValueError("Google is not connected.")

    media = MediaIoBaseUpload(BytesIO(html.encode("utf-8")), mimetype="text/html", resumable=False)
    updated = (
        client.files()
        .update(
            fileId=normalize_google_resource_id(file_ref),
            media_body=media,
            fields="id,name,mimeType,webViewLink",
        )
        .execute()
    )
    return _normalize_drive_file(updated)


def upload_drive_file(
    name: str,
    *,
    content: bytes,
    mime_type: str,
    parent_id: str | None = None,
    session=None,
) -> dict:
    client = build_drive_client(session=session)
    if client is None:
        raise ValueError("Google is not connected.")

    body = {"name": name}
    if parent_id:
        body["parents"] = [parent_id]

    media = MediaIoBaseUpload(BytesIO(content), mimetype=mime_type, resumable=False)
    created = (
        client.files()
        .create(
            body=body,
            media_body=media,
            fields="id,name,mimeType,webViewLink",
        )
        .execute()
    )
    return _normalize_drive_file(created)


def create_google_doc_from_html(
    name: str,
    *,
    html: str,
    parent_id: str | None = None,
    session=None,
) -> dict:
    client = build_drive_client(session=session)
    if client is None:
        raise ValueError("Google is not connected.")

    body = {
        "name": name,
        "mimeType": "application/vnd.google-apps.document",
    }
    if parent_id:
        body["parents"] = [parent_id]

    media = MediaIoBaseUpload(BytesIO(html.encode("utf-8")), mimetype="text/html", resumable=False)
    created = (
        client.files()
        .create(
            body=body,
            media_body=media,
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
