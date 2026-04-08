from pydantic import BaseModel, Field


class ResumeSchema(BaseModel):
    id: str


class ManagedGoogleFolderSchema(BaseModel):
    resource_id: str
    name: str
    url: str | None = None


class ResumeGenerationRunRequest(BaseModel):
    limit: int | None = None
    job_ids: list[str] = Field(default_factory=list)


class GeneratedResumeSchema(BaseModel):
    job_id: str
    company_name: str | None = None
    job_title: str | None = None
    tailored_doc_url: str | None = None
    pdf_url: str | None = None
    provider: str
    model: str


class ResumeAgentSummary(BaseModel):
    ok: bool
    cancelled: bool = False
    message: str
    provider: str
    model: str
    pending_jobs: int = 0
    attempted_count: int = 0
    generated_count: int = 0
    failed_count: int = 0
    created_folders: list[ManagedGoogleFolderSchema] = Field(default_factory=list)
    resumes: list[GeneratedResumeSchema] = Field(default_factory=list)
