"""
config.py - Application settings and file-system locations.

All user data (settings, OAuth token, processing history, logs) lives in a
per-user application data folder:

    Windows:  %LOCALAPPDATA%\\BewerbungAttachAssistant
    Linux:    ~/.local/share/BewerbungAttachAssistant   (development/testing)
    macOS:    ~/Library/Application Support/BewerbungAttachAssistant

Settings are stored as JSON (config.json) and merged with built-in defaults,
so a missing or partially invalid file never prevents the app from starting.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

APP_NAME = "Bewerbung Attach Assistant"
APP_ID = "BewerbungAttachAssistant"
APP_VERSION = "1.0.0"

# The only OAuth scope the application requests.
#
# gmail.compose = "Create, read, update, and delete drafts. Send messages and
# drafts."  It is the narrowest Google scope that allows reading AND updating
# drafts (gmail.readonly cannot update drafts, gmail.modify / mail.google.com
# are far broader).  Google offers no "drafts only" scope, so this scope would
# technically permit sending - the application itself never calls any send,
# create or delete endpoint, and gmail_client.SafeGmailService blocks those
# calls at runtime as a second line of defence.
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]

# Gmail limits (bytes).
GMAIL_ATTACHMENT_LIMIT = 25 * 1024 * 1024       # user-facing attachment limit
GMAIL_RAW_UPLOAD_LIMIT = 35 * 1024 * 1024       # drafts.update media upload limit


# --------------------------------------------------------------------------
# Locations
# --------------------------------------------------------------------------

def _windows_known_folder(folder_guid: str) -> Path | None:
    """Return a Windows Known Folder path (e.g. the real Desktop, which may be
    redirected to OneDrive) using SHGetKnownFolderPath. None on failure."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8),
            ]

        guid_bytes = uuid.UUID(folder_guid).bytes_le
        guid = GUID.from_buffer_copy(guid_bytes)
        path_ptr = ctypes.c_wchar_p()
        shell32 = ctypes.windll.shell32
        ole32 = ctypes.windll.ole32
        result = shell32.SHGetKnownFolderPath(
            ctypes.byref(guid), 0, None, ctypes.byref(path_ptr)
        )
        try:
            if result != 0 or not path_ptr.value:
                return None
            return Path(path_ptr.value)
        finally:
            ole32.CoTaskMemFree(path_ptr)
    except Exception:  # pragma: no cover - only reachable on Windows
        log.debug("SHGetKnownFolderPath failed", exc_info=True)
        return None


FOLDERID_DESKTOP = "{B4BFCC3A-DB2C-424C-B029-7FE99A87C641}"


def desktop_dir() -> Path:
    """The user's Desktop folder (handles OneDrive redirection on Windows)."""
    known = _windows_known_folder(FOLDERID_DESKTOP)
    if known is not None:
        return known
    return Path.home() / "Desktop"


def default_watch_folder() -> Path:
    """Default Bewerbungen folder: <Desktop>\\Bewerbungen."""
    return desktop_dir() / "Bewerbungen"


def app_data_dir() -> Path:
    """Per-user folder for settings, token, database and logs.

    The environment variable BEWERBUNG_ASSISTANT_HOME overrides the location
    (used by the automated tests and for portable setups)."""
    override = os.environ.get("BEWERBUNG_ASSISTANT_HOME")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / APP_ID
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / APP_ID
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / APP_ID
    base.mkdir(parents=True, exist_ok=True)
    return base


@dataclass(frozen=True)
class AppPaths:
    """All files the application reads or writes in its data folder."""

    root: Path

    @property
    def config_file(self) -> Path:
        return self.root / "config.json"

    @property
    def client_secret_file(self) -> Path:
        # Copy of the OAuth client JSON the user selected.
        return self.root / "client_secret.json"

    @property
    def token_file(self) -> Path:
        # OAuth token; encrypted with Windows DPAPI on Windows.
        return self.root / "token.bin"

    @property
    def database_file(self) -> Path:
        return self.root / "history.sqlite3"

    @property
    def log_dir(self) -> Path:
        path = self.root / "logs"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def lock_file(self) -> Path:
        return self.root / "app.lock"

    @classmethod
    def default(cls) -> "AppPaths":
        return cls(app_data_dir())


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

# Generic e-mail providers: a recipient at one of these domains says nothing
# about the company, so domains are never used as company evidence for them.
DEFAULT_GENERIC_EMAIL_DOMAINS = [
    "gmail.com", "googlemail.com", "gmx.de", "gmx.net", "gmx.at", "gmx.ch",
    "web.de", "t-online.de", "outlook.com", "outlook.de", "hotmail.com",
    "hotmail.de", "live.com", "live.de", "msn.com", "yahoo.com", "yahoo.de",
    "icloud.com", "me.com", "mac.com", "aol.com", "aol.de", "posteo.de",
    "posteo.net", "mailbox.org", "mail.de", "freenet.de", "arcor.de",
    "online.de", "email.de", "proton.me", "protonmail.com", "protonmail.ch",
    "zoho.com", "yandex.com", "mail.com", "kabelmail.de", "vodafone.de",
]

DEFAULTS: dict[str, Any] = {
    # Folder that is scanned for application PDFs ("" = <Desktop>\Bewerbungen).
    "watch_folder": "",
    # Dry-run mode: preview everything, never modify Gmail.  On by default so
    # the first run is always safe; switch it off in the main window.
    "dry_run": True,
    "scanner": {
        # Files modified more recently than this are treated as "still being
        # written" and skipped.
        "min_file_age_seconds": 3.0,
        # Size/mtime are sampled twice with this delay to detect ongoing writes.
        "stability_check_seconds": 1.0,
        # Require the PDF to start with %PDF- and end with %%EOF (detects
        # truncated/incomplete copies and downloads).
        "require_complete_pdf": True,
        # Additional file-name prefixes that mark temporary files.
        "ignored_prefixes": ["~$", "~", ".~lock", "."],
    },
    "filename_parser": {
        # Number of leading name components that form the applicant's name
        # (Amine_Haouam -> 2).
        "applicant_name_parts": 2,
        # If set, the leading components must match this name (case and
        # punctuation insensitive).  Leave empty to accept any applicant name.
        "expected_applicant_name": "Amine Haouam",
        # Words that describe the application type; one must follow the name.
        "application_types": ["Ausbildung"],
        "require_application_type": True,
        # Known job titles; one must follow the application type.  Multi-word
        # titles are allowed ("Elektroniker fuer Betriebstechnik").  The
        # longest matching title wins.
        "job_titles": ["Elektroniker"],
        "require_job_title": True,
        # Trailing tokens that are version markers, not part of the company.
        # Regular expressions, matched case-insensitively against a whole token.
        "ignored_trailing_tokens": [r"v\d+", r"final"],
    },
    "matching": {
        "generic_email_domains": DEFAULT_GENERIC_EMAIL_DOMAINS,
        # Safety cap on how many drafts are read per scan.
        "max_drafts": 500,
    },
    # OAuth browser sign-in timeout.
    "oauth_timeout_seconds": 300,
}


def _deep_merge(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Merge *overrides* into a copy of *defaults*.

    Only keys known in *defaults* are accepted and the value type must match
    the default's type (int is accepted where a float is expected).  Invalid
    values are ignored with a warning so the app can always start."""
    result = copy.deepcopy(defaults)
    for key, value in overrides.items():
        if key not in defaults:
            log.warning("Ignoring unknown setting %r", key)
            continue
        default_value = defaults[key]
        if isinstance(default_value, dict):
            if isinstance(value, dict):
                result[key] = _deep_merge(default_value, value)
            else:
                log.warning("Ignoring invalid setting %r (expected an object)", key)
        elif isinstance(default_value, bool):
            if isinstance(value, bool):
                result[key] = value
            else:
                log.warning("Ignoring invalid setting %r (expected true/false)", key)
        elif isinstance(default_value, (int, float)):
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                result[key] = type(default_value)(value) if isinstance(default_value, float) else value
            else:
                log.warning("Ignoring invalid setting %r (expected a number >= 0)", key)
        elif isinstance(default_value, list):
            if isinstance(value, list) and all(isinstance(v, str) for v in value):
                result[key] = list(value)
            else:
                log.warning("Ignoring invalid setting %r (expected a list of strings)", key)
        elif isinstance(default_value, str):
            if isinstance(value, str):
                result[key] = value
            else:
                log.warning("Ignoring invalid setting %r (expected text)", key)
        else:  # pragma: no cover - defensive
            result[key] = value
    return result


class Config:
    """Loads, validates and saves the application settings."""

    def __init__(self, paths: AppPaths, data: dict[str, Any] | None = None):
        self.paths = paths
        self.data: dict[str, Any] = data if data is not None else copy.deepcopy(DEFAULTS)

    # -- persistence -----------------------------------------------------
    @classmethod
    def load(cls, paths: AppPaths) -> "Config":
        overrides: dict[str, Any] = {}
        if paths.config_file.exists():
            try:
                with open(paths.config_file, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    overrides = loaded
                else:
                    log.warning("config.json does not contain an object - using defaults")
            except (OSError, ValueError) as exc:
                log.warning("Could not read config.json (%s) - using defaults", exc)
        return cls(paths, _deep_merge(DEFAULTS, overrides))

    def save(self) -> None:
        """Atomically write config.json (write temp file, then replace)."""
        tmp = self.paths.config_file.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self.paths.config_file)

    # -- convenience accessors -------------------------------------------
    @property
    def watch_folder(self) -> Path:
        folder = self.data.get("watch_folder") or ""
        return Path(folder) if folder else default_watch_folder()

    @watch_folder.setter
    def watch_folder(self, folder: Path | str) -> None:
        self.data["watch_folder"] = str(folder)

    @property
    def dry_run(self) -> bool:
        return bool(self.data.get("dry_run", True))

    @dry_run.setter
    def dry_run(self, value: bool) -> None:
        self.data["dry_run"] = bool(value)

    @property
    def scanner(self) -> dict[str, Any]:
        return self.data["scanner"]

    @property
    def filename_parser(self) -> dict[str, Any]:
        return self.data["filename_parser"]

    @property
    def matching(self) -> dict[str, Any]:
        return self.data["matching"]

    @property
    def oauth_timeout_seconds(self) -> int:
        return int(self.data.get("oauth_timeout_seconds", 300))
