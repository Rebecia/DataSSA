from __future__ import annotations

from functools import lru_cache
from typing import Any

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


class Settings(BaseSettings):
    """应用配置（环境变量 / .env）。步骤 2 仅读取，业务连接在后续步骤使用。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="mysql+pymysql://datasaa:datasaa@127.0.0.1:3306/datasaa",
        validation_alias="DATABASE_URL",
    )
    redis_url: str = Field(
        default="redis://127.0.0.1:6379/0",
        validation_alias="REDIS_URL",
    )

    jwt_secret: str = Field(default="change-me-in-production", validation_alias="JWT_SECRET")
    jwt_algorithm: str = Field(default="HS256", validation_alias="JWT_ALGORITHM")
    jwt_expire_minutes: int = Field(default=1440, validation_alias="JWT_EXPIRE_MINUTES")
    allow_register: bool = Field(default=False, validation_alias="ALLOW_REGISTER")

    datasaa_workspace: str = Field(default="./workspace", validation_alias="DATASSA_WORKSPACE")
    datasaa_test_mode: bool = Field(default=False, validation_alias="DATASSA_TEST_MODE")

    @field_validator("allow_register", "datasaa_test_mode", mode="before")
    @classmethod
    def _bool_fields(cls, value: Any) -> bool:
        return _parse_bool(value)


@lru_cache
def get_settings() -> Settings:
    return Settings()
