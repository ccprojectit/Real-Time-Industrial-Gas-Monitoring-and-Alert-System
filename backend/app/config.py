from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "development"
    database_url: str = ""
    cors_origins: str = "http://localhost:5173"
    session_secret: str = "development-only-change-this-secret"
    admin_username: str = "operator"
    admin_password: str = ""
    auth_required: bool = False
    simulator_interval_seconds: float = Field(default=0.35, ge=0.25, le=10.0)
    simulator_max_runtime_seconds: int = Field(default=900, ge=60, le=3600)
    critical_simulation_cooldown_seconds: int = Field(default=5, ge=1, le=60)
    sms_mode: str = "mock"
    sms_enabled: bool = False
    exotel_account_sid: str = ""
    exotel_api_key: str = ""
    exotel_api_token: str = ""
    exotel_subdomain: str = "api"
    exotel_sender_id: str = ""
    exotel_dlt_entity_id: str = ""
    exotel_dlt_template_id: str = ""
    exotel_sms_type: str = ""
    sms_recipient_number: str = ""
    sms_provider: str = "exotel"
    textbee_base_url: str = "https://api.textbee.dev/api/v1"
    textbee_api_key: str = ""
    textbee_device_id: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def real_sms_ready(self) -> bool:
        if not self.sms_enabled or self.sms_mode.lower() != "real":
            return False

        if self.sms_provider.lower() == "textbee":
            return bool(
                self.textbee_api_key
                and self.textbee_device_id
                and self.sms_recipient_number
            )

        return bool(
            self.exotel_account_sid
            and self.exotel_api_key
            and self.exotel_api_token
            and self.exotel_sender_id
            and self.sms_recipient_number
        )


settings = Settings()
