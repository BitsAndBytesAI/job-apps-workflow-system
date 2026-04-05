from pydantic import BaseModel, Field


class ResumeSchema(BaseModel):
    id: str


class ManagedGoogleFolderSchema(BaseModel):
    resource_id: str
    name: str
    url: str | None = None


class ResumeAgentSummary(BaseModel):
    ok: bool
    message: str
    pending_jobs: int = 0
    created_folders: list[ManagedGoogleFolderSchema] = Field(default_factory=list)
