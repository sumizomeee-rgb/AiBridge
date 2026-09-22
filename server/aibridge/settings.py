from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = SERVER_DIR.parent
DATA_DIR = SERVER_DIR / "data"
STATIC_DIR = SERVER_DIR / "public" / "admin"


@dataclass(frozen=True)
class Settings:
    gateway_host: str = "0.0.0.0"
    gateway_port: int = 9600
    admin_host: str = "127.0.0.1"
    admin_port: int = 7009
    database_path: Path = DATA_DIR / "aibridge.db"
    secret_key_path: Path = DATA_DIR / "secret.key"
    log_path: Path = DATA_DIR / "logs" / "aibridge.log"
    log_max_bytes: int = 5 * 1024 * 1024
    log_backup_count: int = 5


settings = Settings()
