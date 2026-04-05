from pydantic import BaseModel, Field


class JobSchema(BaseModel):
    id: str


class ScrapedJob(BaseModel):
    id: str
    tracking_id: str | None = None
    company_name: str = ""
    job_title: str = ""
    job_description: str = ""
    apply_url: str | None = None
    company_url: str | None = None
    search_url: str
    location: str | None = None
    listed_text: str | None = None


class JobIntakeRunRequest(BaseModel):
    search_urls: list[str] = Field(default_factory=list)
    max_jobs_per_search: int = 25


class JobIntakeRunSummary(BaseModel):
    ok: bool
    message: str
    search_urls: list[str] = Field(default_factory=list)
    scraped_count: int = 0
    accepted_count: int = 0
    processed_count: int = 0
    duplicate_count: int = 0
    filtered_count: int = 0
    accepted_jobs: list[ScrapedJob] = Field(default_factory=list)
