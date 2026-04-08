from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

from job_apps_system.runtime.paths import default_app_data_dir, resolve_database_url


class Settings(BaseSettings):
    app_env: str = "development"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    app_data_dir: str | None = None
    database_url: str | None = None
    log_level: str = "INFO"
    google_oauth_client_config_json: str | None = None
    google_oauth_client_config_path: str | None = None
    google_oauth_redirect_uri: str | None = None

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    @property
    def resolved_app_data_dir(self) -> Path:
        return default_app_data_dir(app_env=self.app_env, configured=self.app_data_dir)

    @property
    def resolved_database_url(self) -> str:
        return resolve_database_url(self.database_url, app_data_dir=self.resolved_app_data_dir)

    @property
    def resolved_google_redirect_uri(self) -> str:
        if self.google_oauth_redirect_uri:
            return self.google_oauth_redirect_uri
        return f"http://{self.app_host}:{self.app_port}/setup/api/google/callback"


settings = Settings()
