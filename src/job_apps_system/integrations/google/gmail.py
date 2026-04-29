from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from job_apps_system.integrations.google.oauth import get_google_credentials


def build_gmail_client(session=None):
    credentials = get_google_credentials(session=session)
    if credentials is None:
        return None
    return build("gmail", "v1", credentials=credentials)


def send_email(
    *,
    to: str,
    subject: str,
    body: str,
    bcc: str | None = None,
    session=None,
) -> dict[str, Any]:
    client = build_gmail_client(session=session)
    if client is None:
        raise ValueError("Google is not connected.")

    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    if bcc:
        message["Bcc"] = bcc
    message.set_content(body)

    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    try:
        sent = client.users().messages().send(userId="me", body={"raw": raw}).execute()
    except HttpError as error:
        raise ValueError(f"Gmail send failed: {error}") from error
    return {"id": sent.get("id"), "thread_id": sent.get("threadId")}
