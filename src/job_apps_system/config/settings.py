from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    database_url: str = "sqlite:///./data/app.db"
    log_level: str = "INFO"
    google_oauth_client_config_json: str | None = None
    google_oauth_client_config_path: str | None = None
    google_oauth_redirect_uri: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def resolved_google_redirect_uri(self) -> str:
        if self.google_oauth_redirect_uri:
            return self.google_oauth_redirect_uri
        return f"http://{self.app_host}:{self.app_port}/setup/api/google/callback"


settings = Settings()
