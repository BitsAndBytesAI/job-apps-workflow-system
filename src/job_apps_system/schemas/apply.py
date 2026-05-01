from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ApplyRunRequest(BaseModel):
    limit: int | None = 1
    job_ids: list[str] = Field(default_factory=list)
    mode: str = Field(default="ai", pattern="^(ai|manual)$")
    trigger_source: str | None = Field(default=None, max_length=80)


class ApplyField(BaseModel):
    element_id: str
    frame_id: str = "main"
    tag: str
    type: str | None = None
    label: str = ""
    placeholder: str = ""
    required: bool = False
    selector: str


class ApplyAction(BaseModel):
    action: str
    element_id: str | None = None
    value: str | bool | None = None
    field_name: str | None = None
    reasoning: str | None = None
    confidence: float | None = None


class ApplyActionPlan(BaseModel):
    actions: list[ApplyAction] = Field(default_factory=list)
    done: bool = False
    terminal: bool = False
    needs_manual: bool = False
    needs_review: bool = False
    error: str | None = None
    confidence: float = 0.0
    summary: str | None = None


class ApplyJobResult(BaseModel):
    job_id: str
    company_name: str | None = None
    job_title: str | None = None
    ats_type: str = "unknown"
    status: str
    success: bool = False
    error: str | None = None
    screenshot_path: str | None = None
    confirmation_text: str | None = None
    discovered_apply_url: str | None = None
    discovered_company_name: str | None = None
    steps: list[str] = Field(default_factory=list)
    action_log: list[dict[str, Any]] = Field(default_factory=list)


class JobApplySummary(BaseModel):
    ok: bool
    cancelled: bool = False
    message: str
    pending_jobs: int = 0
    attempted_count: int = 0
    applied_count: int = 0
    failed_count: int = 0
    applied_jobs: list[ApplyJobResult] = Field(default_factory=list)
