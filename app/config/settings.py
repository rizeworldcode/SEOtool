"""
LinkPilot Data Engine Configuration
Loaded via Pydantic Settings from environment variables or .env file.
"""
from pathlib import Path
from typing import Optional
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    # Master encryption key (AES-256-GCM)
    LINKPILOT_MASTER_KEY: str = Field(
        default="mock-master-key-must-be-changed-in-prod-32bytes!",
        description="Master key for credential encryption"
    )

    # Database
    DATABASE_URL: str = Field(
        default="sqlite:///./data/linkpilot.db",
        description="SQLAlchemy database connection string"
    )

    # Storage paths
    BASE_DIR: Path = Path(__file__).resolve().parent.parent.parent
    DATA_DIR: Path = Path("./data")
    RAW_DATA_DIR: Path = Path("./data/raw")
    PROCESSED_DATA_DIR: Path = Path("./data/processed")
    EXPORTS_DIR: Path = Path("./data/exports")
    MANIFESTS_DIR: Path = Path("./data/manifests")

    # Resource & Safety Limits
    MIN_DISK_FREE_MB: int = Field(default=5000, description="Minimum free disk space in MB")
    MAX_WARC_RECORD_COMPRESSED_BYTES: int = Field(default=10 * 1024 * 1024, description="10 MB compressed limit")
    MAX_WARC_PAYLOAD_DECOMPRESSED_BYTES: int = Field(default=24 * 1024 * 1024, description="24 MB decompressed limit")
    MAX_DECOMPRESSION_RATIO: float = Field(default=50.0, description="Decompression bomb ratio threshold")
    MAX_JOB_DOWNLOAD_BYTES: int = Field(default=500 * 1024 * 1024, description="500 MB per job limit")

    # Rate Limiting & Concurrency
    CDX_REQUESTS_PER_SECOND: float = Field(default=1.0, description="CDX index request rate")
    WARC_CONCURRENT_REQUESTS: int = Field(default=5, description="Max concurrent WARC byte-range fetches")
    WAYBACK_REQUESTS_PER_SECOND: float = Field(default=2.0, description="Wayback CDX request rate")

    # User Agent
    CRAWLER_USER_AGENT: str = Field(
        default="LinkPilotDataEngine/1.0 (+https://github.com/linkpilot/data-engine; bot@linkpilot.ai)",
        description="Descriptive User-Agent for all external requests"
    )


settings = Settings()
