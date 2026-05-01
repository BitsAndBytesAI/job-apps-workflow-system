from pydantic import BaseModel, Field


class JobScoringRunRequest(BaseModel):
    limit: int | None = None
    job_ids: list[str] = Field(default_factory=list)
    trigger_source: str | None = Field(default=None, max_length=80)


class ScoredJobSchema(BaseModel):
    job_id: str
    company_name: str | None = None
    job_title: str | None = None
    score: int
    model: str
    provider: str = "anthropic"


class JobScoringSummary(BaseModel):
    ok: bool
    cancelled: bool = False
    message: str
    provider: str = "anthropic"
    model: str
    pending_jobs: int = 0
    attempted_count: int = 0
    scored_count: int = 0
    failed_count: int = 0
    scored_jobs: list[ScoredJobSchema] = Field(default_factory=list)
