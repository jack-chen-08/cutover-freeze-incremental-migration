"""Runtime configuration. Every value comes from environment variables only."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"missing required environment variable: {name}")
    return value  # type: ignore[return-value]


def _flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    source_dsn: str
    target_dsn: str
    freeze_enabled: bool
    freeze_message: str

    @classmethod
    def load(cls) -> "Settings":
        return cls(
            source_dsn=_env(
                "SOURCE_DATABASE_URL",
                "postgresql+psycopg://app:app_pw_change_me@127.0.0.1:55432/source",
            ),
            target_dsn=_env(
                "TARGET_DATABASE_URL",
                "postgresql+psycopg://app:app_pw_change_me@127.0.0.1:55435/target",
            ),
            freeze_enabled=_flag("FREEZE_ENABLED", False),
            freeze_message=_env(
                "FREEZE_MESSAGE",
                "System is in cutover freeze window. Write operations are temporarily "
                "disabled and will resume after the cutover is complete.",
            ),
        )


settings = Settings.load()
