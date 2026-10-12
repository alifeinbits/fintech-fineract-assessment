from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./integration.db"
    api_token: str = "assessment-token"
    erp_base_url: str = "http://mock-erp:8000"
    erp_api_token: str = "assessment-token"
    fineract_base_url: str = "https://fineract:8443/fineract-provider/api/v1"
    fineract_username: str = "mifos"
    fineract_password: str = "password"
    fineract_tenant: str = "default"
    fineract_verify_ssl: bool = False
    fineract_office_id: int = 1
    fineract_loan_product_id: int = 1
    fineract_payment_type_id: int = 1
    fineract_legal_form_id: int = 1
    fineract_strategy_code: str = "mifos-standard-strategy"
    bank_code: str = "1100"
    receivable_code: str = "1200"
    income_code: str = "4000"
    faults_enabled: bool = True
    retry_attempts: int = 3
    retry_backoff_seconds: float = 0.05


def get_settings() -> Settings:
    return Settings()
