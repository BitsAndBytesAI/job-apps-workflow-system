from pydantic import BaseModel, Field

OPENAI_MODEL_OPTIONS = [
    "gpt-5.4",
    "gpt-5.2",
    "gpt-5.1",
    "gpt-5-mini",
    "gpt-5-nano",
    "gpt-4.1",
    "gpt-4.1-mini",
]

ANTHROPIC_MODEL_OPTIONS = [
    "claude-sonnet-4-5-20250929",
    "claude-opus-4-1-20250805",
    "claude-sonnet-4-20250514",
    "claude-3-7-sonnet-20250219",
    "claude-3-5-sonnet-20241022",
    "claude-3-5-haiku-20241022",
]


class GoogleResourcesConfig(BaseModel):
    em_jobs_sheet: str | None = None
    processed_jobs_sheet: str | None = None
    job_emails_sent_sheet: str | None = None
    interview_emails_sheet: str | None = None
    base_resume_doc: str | None = None


class GoogleManagedFolderConfig(BaseModel):
    resource_id: str
    name: str
    url: str | None = None


class GoogleManagedResourcesConfig(BaseModel):
    root_folder: GoogleManagedFolderConfig | None = None
    resume_docs_folder: GoogleManagedFolderConfig | None = None
    resume_pdfs_folder: GoogleManagedFolderConfig | None = None
    interview_recordings_folder: GoogleManagedFolderConfig | None = None
    interview_transcripts_folder: GoogleManagedFolderConfig | None = None


class GoogleConfig(BaseModel):
    connected: bool = False
    resources: GoogleResourcesConfig = Field(default_factory=GoogleResourcesConfig)
    managed_resources: GoogleManagedResourcesConfig = Field(default_factory=GoogleManagedResourcesConfig)


class LinkedInConfig(BaseModel):
    browser_profile_path: str = "browser-profiles/linkedin"
    search_urls: list[str] = Field(default_factory=list)
    authenticated: bool = False


class ProviderModelsConfig(BaseModel):
    openai_model: str = OPENAI_MODEL_OPTIONS[0]
    anthropic_model: str = ANTHROPIC_MODEL_OPTIONS[0]


class AppBehaviorConfig(BaseModel):
    project_id: str = "engineering-manager"
    job_role: str = "Engineering Manager"
    schedule_minutes: int = 25
    score_threshold: int = 82
    dry_run: bool = False
    send_enabled: bool = True
    send_bcc: str | None = None


class SecretStatus(BaseModel):
    openai_api_key_configured: bool = False
    anthropic_api_key_configured: bool = False
    anymailfinder_api_key_configured: bool = False


class SecretInputs(BaseModel):
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    anymailfinder_api_key: str | None = None


class PersistedFieldValidation(BaseModel):
    ok: bool
    message: str
    level: str = "info"
    normalized_value: str | int | bool | None = None
    updated_at: str | None = None


class SetupConfig(BaseModel):
    google: GoogleConfig = Field(default_factory=GoogleConfig)
    linkedin: LinkedInConfig = Field(default_factory=LinkedInConfig)
    models: ProviderModelsConfig = Field(default_factory=ProviderModelsConfig)
    app: AppBehaviorConfig = Field(default_factory=AppBehaviorConfig)
    secrets: SecretStatus = Field(default_factory=SecretStatus)
    field_validations: dict[str, PersistedFieldValidation] = Field(default_factory=dict)


class SetupConfigUpdate(BaseModel):
    google: GoogleConfig = Field(default_factory=GoogleConfig)
    linkedin: LinkedInConfig = Field(default_factory=LinkedInConfig)
    models: ProviderModelsConfig = Field(default_factory=ProviderModelsConfig)
    app: AppBehaviorConfig = Field(default_factory=AppBehaviorConfig)
    secrets: SecretInputs = Field(default_factory=SecretInputs)


class SetupValidationResponse(BaseModel):
    normalized: SetupConfig
    errors: list[str] = Field(default_factory=list)


class FieldValidationRequest(BaseModel):
    field_name: str
    payload: SetupConfigUpdate


class FieldValidationResponse(BaseModel):
    field_name: str
    ok: bool
    message: str
    level: str = "info"
    normalized_value: str | int | bool | None = None
    updated_at: str | None = None


class LinkedInBrowserLaunchResponse(BaseModel):
    ok: bool
    message: str
    profile_path: str
    pid: int | None = None


class LinkedInAuthStatus(BaseModel):
    ok: bool
    authenticated: bool
    message: str
    profile_path: str
    cookie_count: int = 0


class GoogleAuthStatus(BaseModel):
    connected: bool
    client_configured: bool
    redirect_uri: str
    scopes: list[str]


class GoogleResourceValidationItem(BaseModel):
    field_name: str
    resource_id: str
    ok: bool
    mime_type: str | None = None
    name: str | None = None
    url: str | None = None
    error: str | None = None


class GoogleResourceValidationResponse(BaseModel):
    connected: bool
    results: list[GoogleResourceValidationItem] = Field(default_factory=list)
