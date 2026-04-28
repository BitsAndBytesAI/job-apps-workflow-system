from pydantic import BaseModel, Field


class JobSchema(BaseModel):
    id: str


class ScrapedJob(BaseModel):
    id: str
    tracking_id: str | None = None
    company_name: str = ""
    job_title: str = ""
    job_description: str = ""
    posted_date: str | None = None
    job_posting_url: str | None = None
    apply_url: str | None = None
    company_url: str | None = None
    search_url: str
    location: str | None = None
    listed_text: str | None = None


class JobIntakeRunRequest(BaseModel):
    search_urls: list[str] = Field(default_factory=list)
    max_jobs_per_search: int | None = None


class ScoreThresholdUpdateRequest(BaseModel):
    score_threshold: int = Field(ge=0, le=100)


class AutoScoreUpdateRequest(BaseModel):
    enabled: bool


class MoveToApplicationsRequest(BaseModel):
    source: str = Field(pattern="^(manual|ai)$")


class JobUpdateRequest(BaseModel):
    applied: bool | None = None
    resume_url: str | None = None
    company_name: str | None = None
    job_title: str | None = None
    job_description: str | None = None
    apply_url: str | None = None
    company_url: str | None = None
    job_posting_url: str | None = None


class JobIntakeRunSummary(BaseModel):
    ok: bool
    cancelled: bool = False
    message: str
    search_urls: list[str] = Field(default_factory=list)
    scraped_count: int = 0
    accepted_count: int = 0
    processed_count: int = 0
    duplicate_count: int = 0
    filtered_count: int = 0
    accepted_jobs: list[ScrapedJob] = Field(default_factory=list)
