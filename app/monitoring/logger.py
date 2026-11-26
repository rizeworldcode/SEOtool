"""
Structured logging module for LinkPilot Data Engine using Loguru.
Ensures token redaction, formatted outputs, and log file rotation.
"""
import sys
import re
from loguru import logger
from app.config.settings import settings

# Redaction patterns for tokens, secrets, and auth headers
SECRET_PATTERNS = [
    re.compile(r"(Authorization:\s*Bearer\s+)[^\s'\"]+", re.IGNORECASE),
    re.compile(r"(apikey=)[^&\s'\"]+", re.IGNORECASE),
    re.compile(r"(api_key=)[^&\s'\"]+", re.IGNORECASE),
    re.compile(r"(client_secret=)[^&\s'\"]+", re.IGNORECASE),
    re.compile(r"(access_token=)[^&\s'\"]+", re.IGNORECASE),
    re.compile(r"(refresh_token=)[^&\s'\"]+", re.IGNORECASE),
]


def redact_secrets(message: str) -> str:
    """Redact sensitive tokens and credentials from log strings."""
    redacted = message
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(r"\g<1>[REDACTED]", redacted)
    return redacted


def setup_logger():
    """Configure loguru logger with formatting and secret filtering."""
    logger.remove()

    def sink_filter(record):
        record["message"] = redact_secrets(record["message"])
        return True

    # Console stdout sink
    logger.add(
        sys.stderr,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        level="INFO",
        filter=sink_filter,
    )

    # File log sink
    log_dir = settings.DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(log_dir / "dataengine_{time:YYYY-MM-DD}.log"),
        rotation="50 MB",
        retention="14 days",
        level="DEBUG",
        filter=sink_filter,
    )


setup_logger()
__all__ = ["logger", "redact_secrets"]
