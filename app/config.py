from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


@dataclass(frozen=True)
class Settings:
    app_name: str = "ЮрНавигатор"
    debug: bool = os.getenv("APP_DEBUG", "1") == "1"
    secret_key: str = os.getenv("APP_SECRET_KEY", "") or secrets.token_urlsafe(32)
    db_url: str = os.getenv("DATABASE_URL", "") or f"sqlite:///{BASE_DIR / 'fastapi.db'}"
    field_encryption_key: str = os.getenv("FIELD_ENCRYPTION_KEY", "")


settings = Settings()
