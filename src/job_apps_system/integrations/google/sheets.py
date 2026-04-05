from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from googleapiclient.discovery import build

from job_apps_system.config.resource_ids import GoogleSheetReference, parse_google_sheet_reference
from job_apps_system.integrations.google.oauth import get_google_credentials


@dataclass(frozen=True)
class GoogleSheetTab:
    spreadsheet_id: str
    title: str
    gid: int


class GoogleSheetsClient:
    def __init__(self, session=None) -> None:
        credentials = get_google_credentials(session=session)
        if credentials is None:
            raise ValueError("Google is not connected.")
        self._client = build("sheets", "v4", credentials=credentials)
        self._metadata_cache: dict[str, dict[str, Any]] = {}

    def get_records(self, spreadsheet_ref: str) -> list[dict[str, str]]:
        tab = self.resolve_tab(spreadsheet_ref)
        values = self._get_values(spreadsheet_id=tab.spreadsheet_id, range_name=f"{_quote_tab(tab.title)}")
        if not values:
            return []

        header = [str(cell).strip() for cell in values[0]]
        if not any(header):
            return []

        records: list[dict[str, str]] = []
        for row in values[1:]:
            padded = list(row) + [""] * max(0, len(header) - len(row))
            record = {header[index]: str(padded[index]).strip() for index in range(len(header)) if header[index]}
            if any(value.strip() for value in record.values()):
                records.append(record)
        return records

    def get_header_row(self, spreadsheet_ref: str) -> list[str]:
        tab = self.resolve_tab(spreadsheet_ref)
        values = self._get_values(
            spreadsheet_id=tab.spreadsheet_id,
            range_name=f"{_quote_tab(tab.title)}!1:1",
        )
        if not values:
            return []
        return [str(cell).strip() for cell in values[0]]

    def ensure_headers(self, spreadsheet_ref: str, headers: Sequence[str]) -> dict[str, Any]:
        tab = self.resolve_tab(spreadsheet_ref)
        existing = self.get_header_row(spreadsheet_ref)
        normalized_headers = [str(header).strip() for header in headers]
        is_empty = not existing or not any(cell for cell in existing)
        matches = _trim_trailing_empty(existing) == _trim_trailing_empty(normalized_headers)

        if is_empty:
            self._client.spreadsheets().values().update(
                spreadsheetId=tab.spreadsheet_id,
                range=f"{_quote_tab(tab.title)}!1:1",
                valueInputOption="RAW",
                body={"values": [normalized_headers]},
            ).execute()
            return {
                "ok": True,
                "action": "written",
                "sheet_title": tab.title,
                "headers": normalized_headers,
                "message": f"Wrote header row to {tab.title}.",
            }

        return {
            "ok": True,
            "action": "noop",
            "sheet_title": tab.title,
            "headers": existing,
            "matches": matches,
            "message": f"Existing header row found in {tab.title}; left unchanged.",
        }

    def append_records(
        self,
        spreadsheet_ref: str,
        headers: Sequence[str],
        records: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        if not records:
            return {"ok": True, "count": 0}

        tab = self.resolve_tab(spreadsheet_ref)
        rows = [[record.get(header, "") for header in headers] for record in records]
        response = (
            self._client.spreadsheets()
            .values()
            .append(
                spreadsheetId=tab.spreadsheet_id,
                range=f"{_quote_tab(tab.title)}",
                valueInputOption="RAW",
                insertDataOption="INSERT_ROWS",
                body={"values": rows},
            )
            .execute()
        )
        return {"ok": True, "count": len(rows), "updates": response.get("updates", {})}

    def resolve_tab(self, spreadsheet_ref: str) -> GoogleSheetTab:
        reference = parse_google_sheet_reference(spreadsheet_ref)
        metadata = self._get_spreadsheet_metadata(reference.spreadsheet_id)
        sheets = metadata.get("sheets", [])
        if not sheets:
            raise ValueError(f"Spreadsheet {reference.spreadsheet_id} has no visible sheets.")

        if reference.gid is not None:
            for sheet in sheets:
                properties = sheet.get("properties", {})
                if properties.get("sheetId") == reference.gid:
                    return GoogleSheetTab(
                        spreadsheet_id=reference.spreadsheet_id,
                        title=properties["title"],
                        gid=properties["sheetId"],
                    )

        properties = sheets[0]["properties"]
        return GoogleSheetTab(
            spreadsheet_id=reference.spreadsheet_id,
            title=properties["title"],
            gid=properties["sheetId"],
        )

    def _get_spreadsheet_metadata(self, spreadsheet_id: str) -> dict[str, Any]:
        cached = self._metadata_cache.get(spreadsheet_id)
        if cached is not None:
            return cached

        metadata = (
            self._client.spreadsheets()
            .get(spreadsheetId=spreadsheet_id, fields="sheets(properties(sheetId,title))")
            .execute()
        )
        self._metadata_cache[spreadsheet_id] = metadata
        return metadata

    def _get_values(self, spreadsheet_id: str, range_name: str) -> list[list[str]]:
        response = (
            self._client.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range=range_name)
            .execute()
        )
        return response.get("values", [])


def _quote_tab(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


def _trim_trailing_empty(values: Sequence[str]) -> list[str]:
    trimmed = list(values)
    while trimmed and not trimmed[-1]:
        trimmed.pop()
    return trimmed
