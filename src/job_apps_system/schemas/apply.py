from __future__ import annotations

from pydantic import BaseModel, Field


class ApplyRunRequest(BaseModel):
    limit: int | None = 1
    job_ids: list[str] = Field(default_factory=list)
    mode: str = Field(default="ai", pattern="^(ai|manual)$")


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
    value: str | None = None
    field_name: str | None = None
    reasoning: str | None = None


class ApplyActionPlan(BaseModel):
    actions: list[ApplyAction] = Field(default_factory=list)
    done: bool = False
    error: str | None = None


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
    steps: list[str] = Field(default_factory=list)


class JobApplySummary(BaseModel):
    ok: bool
    cancelled: bool = False
    message: str
    pending_jobs: int = 0
    attempted_count: int = 0
    applied_count: int = 0
    failed_count: int = 0
    applied_jobs: list[ApplyJobResult] = Field(default_factory=list)
