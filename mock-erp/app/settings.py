from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./mock_erp.db"
    api_token: str = "assessment-token"
    faults_enabled: bool = True
    fault_sleep_seconds: float = 30.0


def get_settings() -> Settings:
    return Settings()
