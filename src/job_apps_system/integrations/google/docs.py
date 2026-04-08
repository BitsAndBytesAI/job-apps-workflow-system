from __future__ import annotations

from googleapiclient.discovery import build

from job_apps_system.config.resource_ids import normalize_google_resource_id
from job_apps_system.integrations.google.oauth import get_google_credentials


class GoogleDocsClient:
    def __init__(self, session=None) -> None:
        credentials = get_google_credentials(session=session)
        if credentials is None:
            raise ValueError("Google is not connected.")
        self._client = build("docs", "v1", credentials=credentials)

    def get_document_text(self, document_ref: str) -> str:
        document_id = normalize_google_resource_id(document_ref)
        document = self._client.documents().get(documentId=document_id).execute()
        lines: list[str] = []
        for element in document.get("body", {}).get("content", []):
            paragraph = element.get("paragraph")
            if not paragraph:
                continue
            text_runs: list[str] = []
            for run in paragraph.get("elements", []):
                content = run.get("textRun", {}).get("content")
                if content:
                    text_runs.append(content)
            text = "".join(text_runs).strip()
            if text:
                lines.append(text)
        return "\n".join(lines).strip()

    def replace_document_text(self, document_ref: str, text: str) -> dict:
        document_id = normalize_google_resource_id(document_ref)
        document = self._client.documents().get(documentId=document_id).execute()
        end_index = document.get("body", {}).get("content", [{}])[-1].get("endIndex", 1)
        requests: list[dict] = []
        if end_index and end_index > 1:
            requests.append(
                {
                    "deleteContentRange": {
                        "range": {
                            "startIndex": 1,
                            "endIndex": end_index - 1,
                        }
                    }
                }
            )
        requests.append(
            {
                "insertText": {
                    "location": {"index": 1},
                    "text": text if text.endswith("\n") else f"{text}\n",
                }
            }
        )
        return self._client.documents().batchUpdate(documentId=document_id, body={"requests": requests}).execute()
