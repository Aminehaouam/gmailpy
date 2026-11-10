"""
logging_setup.py - Local operation log with OAuth secret redaction.

Every log record passes through SecretRedactingFilter before it is written,
so access tokens, refresh tokens, client secrets and authorization codes can
never end up in the log file or in the GUI activity log, even if a library
logs them by accident.
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

# Patterns for secrets that must never be written to a log.
_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    # Google OAuth access tokens.
    (re.compile(r"ya29\.[0-9A-Za-z\-_.]+"), "ya29.[REDACTED]"),
    # Google OAuth refresh tokens.
    (re.compile(r"1//[0-9A-Za-z\-_]+"), "1//[REDACTED]"),
    # Google OAuth client secrets.
    (re.compile(r"GOCSPX-[0-9A-Za-z\-_]+"), "GOCSPX-[REDACTED]"),
    # Authorization headers.
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9\-._~+/]+=*"), r"\1[REDACTED]"),
    # key=value / "key": "value" pairs for secret-bearing fields, as found in
    # JSON bodies and query strings.
    (
        re.compile(
            r"(?i)([\"']?(?:access_token|refresh_token|id_token|client_secret|"
            r"assertion|password)[\"']?\s*[:=]\s*[\"']?)"
            r"([^\"'&\s,}]+)"
        ),
        r"\1[REDACTED]",
    ),
    # OAuth authorization codes in redirect URLs (?code=... / &code=...).
    (re.compile(r"(?i)([?&]code=)[^&\s\"']+"), r"\1[REDACTED]"),
]


def redact(text: str) -> str:
    """Remove OAuth secrets from *text*."""
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class SecretRedactingFilter(logging.Filter):
    """Logging filter that rewrites each record's message with secrets removed."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # malformed format args - keep the raw template
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        if record.exc_info and not record.exc_text:
            # Pre-render the traceback so it can be redacted too.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


def setup_logging(log_dir: Path, level: int = logging.INFO) -> Path:
    """Configure the root logger with a rotating, redacted log file.

    Returns the path of the log file."""
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "bewerbung_attach_assistant.log"

    root = logging.getLogger()
    root.setLevel(level)

    # Avoid duplicate handlers if setup_logging is called twice.
    for handler in list(root.handlers):
        if getattr(handler, "_bewerbung_handler", False):
            root.removeHandler(handler)

    file_handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
    file_handler.addFilter(SecretRedactingFilter())
    file_handler._bewerbung_handler = True  # type: ignore[attr-defined]
    root.addHandler(file_handler)

    # Third-party libraries can log request URLs/bodies at DEBUG level; keep
    # them quiet.  (Redaction would still apply, this is defence in depth.)
    for noisy in (
        "googleapiclient",
        "googleapiclient.discovery",
        "googleapiclient.discovery_cache",
        "google_auth_oauthlib",
        "google.auth",
        "oauthlib",
        "requests_oauthlib",
        "urllib3",
        "httplib2",
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return log_file
