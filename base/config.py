"""配置管理。沿用矩阵既有模式（uni.config / indicators.config）。"""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

PG_HOST = os.environ.get("PG_HOST", "localhost")
PG_PORT = int(os.environ.get("PG_PORT", "5432"))
PG_USER = os.environ.get("PG_USER", "postgres")
PG_PASSWORD = os.environ.get("PG_PASSWORD", "")
PG_DATABASE = os.environ.get("PG_DATABASE", "nooshare_dev")
PG_SSLMODE = os.environ.get("PG_SSLMODE", "prefer")


def get_pg_dsn() -> str:
    """构建 PostgreSQL DSN。"""
    parts = [
        f"host={PG_HOST}",
        f"port={PG_PORT}",
        f"user={PG_USER}",
        f"dbname={PG_DATABASE}",
        f"sslmode={PG_SSLMODE}",
    ]
    if PG_PASSWORD:
        parts.insert(2, f"password={PG_PASSWORD}")
    return " ".join(parts)
