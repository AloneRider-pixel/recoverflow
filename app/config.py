from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    app_name: str = "RecoverFlow"
    database_url: str = "sqlite:///./recoverflow.db"
    openai_api_key: str = ""
    openai_model: str = "gpt-5-mini"
    session_secret: str = "dev-only-change-me"
    cron_secret: str = ""
    razorpay_platform_key_id: str = ""
    razorpay_platform_key_secret: str = ""
    razorpay_platform_webhook_secret: str = ""
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    whatsapp_graph_version: str = ""
    environment: str = "production"
    public_base_url: str = ""
    sales_email: str = ""
    legal_entity_name: str = ""
    support_email: str = ""
    business_address: str = ""
    jurisdiction: str = ""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

settings = Settings()

if settings.database_url.startswith("postgresql://"):
    settings.database_url = settings.database_url.replace("postgresql://", "postgresql+psycopg://", 1)
