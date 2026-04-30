from typing import Literal

from pydantic import BaseModel, Field, model_validator

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

JOB_SITE_OPTIONS = [
    "linkedin",
]


class GoogleResourcesConfig(BaseModel):
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
    browser_profile_path: str = "browser-profiles/linkedin-firefox"
    search_urls: list[str] = Field(default_factory=list)
    authenticated: bool = False


class ProviderModelsConfig(BaseModel):
    openai_model: str = OPENAI_MODEL_OPTIONS[0]
    anthropic_model: str = ANTHROPIC_MODEL_OPTIONS[0]


class OnboardingConfig(BaseModel):
    wizard_completed: bool = False
    wizard_current_step: str = "project"


class ProjectResumeConfig(BaseModel):
    source_type: str | None = None
    source_url: str | None = None
    original_file_name: str | None = None
    original_file_path: str | None = None
    extracted_text: str | None = None


class ApplicantProfileConfig(BaseModel):
    legal_name: str = ""
    preferred_name: str = ""
    email: str = ""
    phone: str = ""
    linkedin_url: str = ""
    portfolio_url: str = ""
    github_url: str = ""
    current_company: str = ""
    current_title: str = ""
    years_of_experience: str = ""
    address_line_1: str = ""
    address_line_2: str = ""
    city: str = ""
    state: str = ""
    postal_code: str = ""
    country: str = "United States"
    work_authorized_us: bool = True
    requires_sponsorship: bool = False
    compensation_expectation: str = ""
    programming_languages_years: str = ""
    favorite_ai_tool: str = ""
    favorite_ai_tool_usage: str = ""
    company_value_example: str = ""
    why_interested_guidance: str = ""
    additional_info_guidance: str = ""
    sms_consent: bool = False
    custom_answer_guidance: str = ""

    @property
    def full_address(self) -> str:
        return ", ".join(
            part
            for part in [
                self.address_line_1,
                self.address_line_2,
                self.city,
                self.state,
                self.postal_code,
                self.country,
            ]
            if part
        )

    @property
    def location_summary(self) -> str:
        return ", ".join(part for part in [self.city, self.state, self.country] if part)


class EmailTemplatesConfig(BaseModel):
    last_subject: str = ""
    last_body: str = ""
    bcc_self: bool = False


class AppBehaviorConfig(BaseModel):
    project_name: str = ""
    project_id: str = ""
    job_role: str = ""
    selected_job_sites: list[str] = Field(default_factory=list)
    schedule_minutes: int = 25
    max_jobs_per_run: int = 10
    auto_score_enabled: bool = False
    score_threshold: int = 820
    score_threshold_storage_version: int = 2
    hide_jobs_below_score_threshold: bool = True
    dry_run: bool = False
    send_enabled: bool = True
    send_bcc: str | None = None
    apply_default_limit: int = 1
    apply_headless: bool = False
    apply_auto_submit: bool = True
    apply_debug_retain_success_logs: bool = False
    apply_choice_behavior: Literal["always_manual", "always_ai", "always_ask"] = "always_ai"
    auto_score_enabled: bool = False
    intake_title_blocklist: list[str] = Field(
        default_factory=lambda: ["vice president", " vp", "head of"]
    )
    auto_find_contacts_enabled: bool = False
    auto_generate_resumes_enabled: bool = False

    @model_validator(mode="after")
    def normalize_score_threshold(self):
        if self.score_threshold_storage_version < 2 and 0 < self.score_threshold <= 100:
            self.score_threshold *= 10
        self.score_threshold_storage_version = 2
        return self


class SecretFieldStatus(BaseModel):
    configured: bool = False
    status_code: str = "missing_secret"
    status_message: str = "Not configured."
    last_validated_at: str | None = None


class SecretHelperStatus(BaseModel):
    backend: str = "python_native"
    available: bool = True
    healthy: bool = True
    helper_version: str | None = None
    protocol_version: int | None = None
    last_error_code: str | None = None
    status_message: str = "Using local secret storage."
    codesign_ok: bool | None = None
    entitlements_ok: bool | None = None
    access_group_ok: bool | None = None
    probe_round_trip_ok: bool | None = None


class SecretStatus(BaseModel):
    openai_api_key_configured: bool = False
    anthropic_api_key_configured: bool = False
    anymailfinder_api_key_configured: bool = False
    openai_api_key: SecretFieldStatus = Field(default_factory=SecretFieldStatus)
    anthropic_api_key: SecretFieldStatus = Field(default_factory=SecretFieldStatus)
    anymailfinder_api_key: SecretFieldStatus = Field(default_factory=SecretFieldStatus)
    google_oauth_token_json: SecretFieldStatus = Field(default_factory=SecretFieldStatus)
    helper: SecretHelperStatus = Field(default_factory=SecretHelperStatus)


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
    onboarding: OnboardingConfig = Field(default_factory=OnboardingConfig)
    project_resume: ProjectResumeConfig = Field(default_factory=ProjectResumeConfig)
    applicant: ApplicantProfileConfig = Field(default_factory=ApplicantProfileConfig)
    app: AppBehaviorConfig = Field(default_factory=AppBehaviorConfig)
    email_templates: EmailTemplatesConfig = Field(default_factory=EmailTemplatesConfig)
    secrets: SecretStatus = Field(default_factory=SecretStatus)
    field_validations: dict[str, PersistedFieldValidation] = Field(default_factory=dict)


class SetupConfigUpdate(BaseModel):
    google: GoogleConfig = Field(default_factory=GoogleConfig)
    linkedin: LinkedInConfig = Field(default_factory=LinkedInConfig)
    models: ProviderModelsConfig = Field(default_factory=ProviderModelsConfig)
    onboarding: OnboardingConfig = Field(default_factory=OnboardingConfig)
    project_resume: ProjectResumeConfig = Field(default_factory=ProjectResumeConfig)
    applicant: ApplicantProfileConfig = Field(default_factory=ApplicantProfileConfig)
    app: AppBehaviorConfig = Field(default_factory=AppBehaviorConfig)
    email_templates: EmailTemplatesConfig = Field(default_factory=EmailTemplatesConfig)
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


class LinkedInBrowserTerminateRequest(BaseModel):
    pid: int | None = None


class LinkedInBrowserTerminateResponse(BaseModel):
    ok: bool
    message: str
    pid: int | None = None


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
