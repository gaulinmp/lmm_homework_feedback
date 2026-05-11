from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    ENV: str = "dev"

    DATA_DIR: Path = PROJECT_ROOT / "data"
    DB_PATH: Path = PROJECT_ROOT / "data" / "tutor.db"
    LLM_CONFIG_PATH: Path = PROJECT_ROOT / "config" / "llm.toml"
    LLM_LOG_PATH: Path = PROJECT_ROOT / "data" / "logs" / "llm_messages.jsonl"

    HMAC_SECRET: str = "dev-hmac-secret-change-me"
    SESSION_SECRET: str = "dev-session-secret-change-me"

    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = False

    MAX_UPLOAD_EXCEL_BYTES: int = Field(default=5 * 1024 * 1024)
    MAX_UPLOAD_IMAGE_BYTES: int = Field(default=2 * 1024 * 1024)
    MAX_UPLOAD_PYTHON_BYTES: int = Field(default=100 * 1024)


settings = Settings()
