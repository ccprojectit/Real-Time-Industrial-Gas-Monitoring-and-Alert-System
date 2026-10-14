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

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

settings = Settings()
