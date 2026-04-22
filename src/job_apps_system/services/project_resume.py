from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
import re
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
import zipfile

from sqlalchemy.orm import Session

from job_apps_system.config.models import ProjectResumeConfig
from job_apps_system.config.resource_ids import normalize_google_resource_id
from job_apps_system.config.settings import settings
from job_apps_system.integrations.google.docs import GoogleDocsClient


WORD_NAMESPACE = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


@dataclass
class StoredResumeFile:
    source_type: str
    source_url: str | None
    original_file_name: str | None
    original_file_path: str | None
    extracted_text: str


def store_uploaded_docx(*, project_id: str, filename: str, content: bytes) -> StoredResumeFile:
    extracted_text = extract_docx_text(content)
    storage_dir = _project_resume_dir(project_id)
    storage_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _sanitize_filename(filename or "base-resume.docx")
    file_path = storage_dir / safe_name
    file_path.write_bytes(content)
    return StoredResumeFile(
        source_type="upload",
        source_url=None,
        original_file_name=safe_name,
        original_file_path=str(file_path),
        extracted_text=extracted_text,
    )


def resolve_resume_from_url(*, session: Session, project_id: str, source_url: str) -> StoredResumeFile:
    normalized_url = source_url.strip()
    if not normalized_url:
        raise ValueError("Resume link is empty.")

    if _looks_like_google_doc(normalized_url):
        document_id = normalize_google_resource_id(normalized_url)
        try:
            extracted_text = read_public_google_doc_text(document_id)
        except Exception as public_error:
            try:
                extracted_text = GoogleDocsClient(session=session).get_document_text(document_id)
            except Exception as oauth_error:
                raise ValueError(
                    "This Google Doc is not publicly readable and Google is not connected."
                ) from oauth_error
        return StoredResumeFile(
            source_type="google_docs_link",
            source_url=normalized_url,
            original_file_name=None,
            original_file_path=None,
            extracted_text=extracted_text,
        )

    remote_file = _download_docx_from_url(normalized_url)
    stored = store_uploaded_docx(
        project_id=project_id,
        filename=remote_file.filename,
        content=remote_file.content,
    )
    stored.source_type = "docx_url"
    stored.source_url = normalized_url
    return stored


def read_public_google_doc_text(document_ref: str) -> str:
    document_id = normalize_google_resource_id(document_ref)
    export_url = f"https://docs.google.com/document/d/{document_id}/export?format=txt"
    request = Request(export_url, headers={"User-Agent": "AIJobAgents/1.0"})
    with urlopen(request) as response:
        content_type = response.headers.get("Content-Type", "")
        content = response.read()
        final_url = response.geturl()

    if "ServiceLogin" in final_url or "accounts.google.com" in final_url:
        raise ValueError("Google Doc requires sign-in.")
    if "text/plain" not in content_type and not content:
        raise ValueError("Google Doc did not return readable text.")

    text = content.decode("utf-8", errors="replace").strip()
    if not text or "<html" in text.lower():
        raise ValueError("Google Doc did not return readable text.")
    return text


def project_resume_config_from_file(stored: StoredResumeFile) -> ProjectResumeConfig:
    return ProjectResumeConfig(
        source_type=stored.source_type,
        source_url=stored.source_url,
        original_file_name=stored.original_file_name,
        original_file_path=stored.original_file_path,
        extracted_text=stored.extracted_text,
    )


def extract_docx_text(content: bytes) -> str:
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            xml_bytes = archive.read("word/document.xml")
    except KeyError as exc:
        raise ValueError("The uploaded Word document is missing word/document.xml.") from exc
    except zipfile.BadZipFile as exc:
        raise ValueError("The uploaded file is not a valid .docx Word document.") from exc

    root = ET.fromstring(xml_bytes)
    paragraphs: list[str] = []
    for paragraph in root.findall(".//w:p", WORD_NAMESPACE):
        parts = [node.text or "" for node in paragraph.findall(".//w:t", WORD_NAMESPACE)]
        text = "".join(parts).strip()
        if text:
            paragraphs.append(text)

    extracted = "\n".join(paragraphs).strip()
    if not extracted:
        raise ValueError("No readable text was found in the uploaded Word document.")
    return extracted


def extract_html_text(html: str) -> str:
    parser = _HTMLTextExtractor()
    parser.feed(html)
    text = parser.text()
    if not text:
        raise ValueError("No readable text was found in the provided resume page.")
    return text


@dataclass
class DownloadedFile:
    filename: str
    content: bytes


def _download_docx_from_url(source_url: str) -> DownloadedFile:
    request = Request(source_url, headers={"User-Agent": "AIJobAgents/1.0"})
    with urlopen(request) as response:
        content_type = response.headers.get("Content-Type", "")
        content = response.read()
        final_url = response.geturl()

    if "application/vnd.openxmlformats-officedocument.wordprocessingml.document" in content_type or final_url.lower().endswith(".docx"):
        filename = Path(urlparse(final_url).path).name or "linked-resume.docx"
        return DownloadedFile(filename=filename, content=content)

    if "text/html" in content_type:
        raise ValueError("The provided URL is an HTML page, not a directly downloadable Word document. Upload the .docx file or use a Google Docs link.")

    raise ValueError("Unable to extract a Word document from the provided URL.")


def _project_resume_dir(project_id: str) -> Path:
    return settings.resolved_app_data_dir / "project-resumes" / project_id


def _sanitize_filename(filename: str) -> str:
    candidate = filename.strip() or "base-resume.docx"
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", candidate)
    if not candidate.lower().endswith(".docx"):
        candidate = f"{candidate}.docx"
    return candidate


def _looks_like_google_doc(source_url: str) -> bool:
    return "docs.google.com/document" in source_url or "/document/d/" in source_url


class _HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._chunks: list[str] = []

    def handle_data(self, data: str) -> None:
        chunk = data.strip()
        if chunk:
            self._chunks.append(chunk)

    def text(self) -> str:
        return "\n".join(self._chunks).strip()
