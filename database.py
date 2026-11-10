"""
database.py - SQLite persistence for the processing history.

Every attachment attempt is recorded with the PDF's SHA-256 hash, so a PDF is
recognised as already processed even after it has been renamed or moved.

Status life cycle of a real (non dry-run) attachment:

    in_progress  -- written BEFORE Gmail is called (write-ahead record)
        |
        +--> success     Gmail confirmed the update and the draft was verified
        +--> failed      Gmail rejected the update / it was never sent
        +--> unverified  the outcome is unknown (e.g. timeout); must be checked

If the application is interrupted while a record is "in_progress", the record
stays in that state and is detected (and, when possible, automatically
resolved by inspecting the draft) the next time Gmail is connected.

A record is never set to "success" unless Gmail confirmed the draft update.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

log = logging.getLogger(__name__)

STATUS_IN_PROGRESS = "in_progress"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_UNVERIFIED = "unverified"
STATUS_DRY_RUN = "dry_run"
STATUS_ALREADY_ATTACHED = "already_attached"

ALL_STATUSES = (
    STATUS_IN_PROGRESS,
    STATUS_SUCCESS,
    STATUS_FAILED,
    STATUS_UNVERIFIED,
    STATUS_DRY_RUN,
    STATUS_ALREADY_ATTACHED,
)

# Records with these statuses mean "this PDF must not be picked automatically
# again": it was attached, is already in the draft, or its outcome is unclear.
BLOCKING_STATUSES = (
    STATUS_SUCCESS,
    STATUS_ALREADY_ATTACHED,
    STATUS_IN_PROGRESS,
    STATUS_UNVERIFIED,
)

# Records that need the user's (or the recovery check's) attention.
INCOMPLETE_STATUSES = (STATUS_IN_PROGRESS, STATUS_UNVERIFIED)

STATUS_LABELS = {
    STATUS_IN_PROGRESS: "Interrupted / in progress",
    STATUS_SUCCESS: "Success",
    STATUS_FAILED: "Failed",
    STATUS_UNVERIFIED: "Unverified - check draft",
    STATUS_DRY_RUN: "Dry run (no change)",
    STATUS_ALREADY_ATTACHED: "Already attached (no change)",
}

# Send outcome (column send_status; NULL = sending was not requested)
SEND_SENT = "sent"
SEND_FAILED = "failed"
SEND_UNKNOWN = "unknown"

SCHEMA_VERSION = 2

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS processing_history (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    pdf_filename      TEXT    NOT NULL,
    pdf_path          TEXT    NOT NULL,
    sha256            TEXT    NOT NULL,
    file_size         INTEGER NOT NULL,
    file_mtime        REAL,
    company_name      TEXT,
    draft_id          TEXT,
    draft_message_id  TEXT,
    new_message_id    TEXT,
    draft_subject     TEXT,
    draft_recipients  TEXT,
    status            TEXT    NOT NULL CHECK (status IN ({", ".join(repr(s) for s in ALL_STATUSES)})),
    error_message     TEXT,
    started_at        TEXT    NOT NULL,
    finished_at       TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_sha256 ON processing_history (sha256);
CREATE INDEX IF NOT EXISTS idx_history_status ON processing_history (status);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class HistoryRecord:
    id: int
    pdf_filename: str
    pdf_path: str
    sha256: str
    file_size: int
    file_mtime: float | None
    company_name: str | None
    draft_id: str | None
    draft_message_id: str | None
    new_message_id: str | None
    draft_subject: str | None
    draft_recipients: str | None
    status: str
    error_message: str | None
    started_at: str
    finished_at: str | None
    send_status: str | None = None
    sent_at: str | None = None
    sent_message_id: str | None = None

    @property
    def status_label(self) -> str:
        if self.status == STATUS_SUCCESS and self.send_status == SEND_SENT:
            return "Attached and SENT"
        if self.status == STATUS_SUCCESS and self.send_status == SEND_FAILED:
            return "Attached, NOT sent"
        if self.status == STATUS_SUCCESS and self.send_status == SEND_UNKNOWN:
            return "Attached, send unclear - check Sent"
        return STATUS_LABELS.get(self.status, self.status)

    @property
    def is_blocking(self) -> bool:
        return self.status in BLOCKING_STATUSES

    def local_time(self, which: str = "started_at", fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
        """Human readable local time of started_at / finished_at."""
        value = getattr(self, which)
        if not value:
            return ""
        try:
            return datetime.fromisoformat(value).astimezone().strftime(fmt)
        except ValueError:
            return value


class DatabaseError(Exception):
    """A history database operation failed."""


class Database:
    """Thin, thread-safe wrapper around the SQLite history database.

    A short-lived connection is opened for each operation, so the object can
    be shared between the GUI thread and the background worker thread."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._init_schema()

    # -- connection handling ---------------------------------------------
    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                conn = sqlite3.connect(self.path, timeout=15)
            except sqlite3.Error as exc:
                raise DatabaseError(f"Cannot open history database {self.path}: {exc}") from exc
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except sqlite3.Error as exc:
                conn.rollback()
                raise DatabaseError(f"History database error: {exc}") from exc
            finally:
                conn.close()

    def _init_schema(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            columns = {row[1] for row in conn.execute("PRAGMA table_info(processing_history)")}
            for column in ("send_status", "sent_at", "sent_message_id"):  # schema v2
                if column not in columns:
                    conn.execute(f"ALTER TABLE processing_history ADD COLUMN {column} TEXT")
            if version < SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version > SCHEMA_VERSION:
                log.warning(
                    "History database was created by a newer version (schema %s > %s)",
                    version, SCHEMA_VERSION,
                )

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> HistoryRecord:
        return HistoryRecord(**{key: row[key] for key in row.keys()})

    # -- writing ------------------------------------------------------------
    def start_operation(
        self,
        *,
        pdf_filename: str,
        pdf_path: str,
        sha256: str,
        file_size: int,
        file_mtime: float | None,
        company_name: str | None,
        draft_id: str | None,
        draft_message_id: str | None,
        draft_subject: str | None,
        draft_recipients: str | None,
        status: str = STATUS_IN_PROGRESS,
        error_message: str | None = None,
        finished: bool = False,
    ) -> int:
        """Insert a new history record and return its id.

        For real attachments this is called with status "in_progress" BEFORE
        Gmail is contacted (write-ahead), so an interruption is detectable."""
        if status not in ALL_STATUSES:
            raise ValueError(f"Unknown status {status!r}")
        now = utc_now_iso()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO processing_history (
                    pdf_filename, pdf_path, sha256, file_size, file_mtime,
                    company_name, draft_id, draft_message_id, draft_subject,
                    draft_recipients, status, error_message, started_at, finished_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pdf_filename, pdf_path, sha256, file_size, file_mtime,
                    company_name, draft_id, draft_message_id, draft_subject,
                    draft_recipients, status, error_message, now,
                    now if finished else None,
                ),
            )
            record_id = int(cursor.lastrowid)
        log.info("History record #%s created: %s (%s)", record_id, pdf_filename, status)
        return record_id

    def finish_operation(
        self,
        record_id: int,
        status: str,
        *,
        error_message: str | None = None,
        new_message_id: str | None = None,
    ) -> None:
        """Set the final status of a record."""
        if status not in ALL_STATUSES:
            raise ValueError(f"Unknown status {status!r}")
        with self._connect() as conn:
            updated = conn.execute(
                """
                UPDATE processing_history
                   SET status = ?, error_message = ?,
                       new_message_id = COALESCE(?, new_message_id),
                       finished_at = ?
                 WHERE id = ?
                """,
                (status, error_message, new_message_id, utc_now_iso(), record_id),
            ).rowcount
        if not updated:
            raise DatabaseError(f"History record #{record_id} does not exist")
        log.info("History record #%s -> %s%s", record_id, status,
                 f" ({error_message})" if error_message else "")

    def set_send_result(self, record_id: int, send_status: str, *, message_id: str | None = None,
                        note: str | None = None) -> None:
        """Store the outcome of sending the draft after a successful attachment."""
        if send_status not in (SEND_SENT, SEND_FAILED, SEND_UNKNOWN):
            raise ValueError(f"Unknown send status {send_status!r}")
        record = self.get(record_id)
        if record is None:
            raise DatabaseError(f"History record #{record_id} does not exist")
        details = " ".join(x for x in (record.error_message, note) if x) or None
        with self._connect() as conn:
            conn.execute(
                """UPDATE processing_history
                      SET send_status = ?, sent_at = ?, sent_message_id = ?, error_message = ?
                    WHERE id = ?""",
                (send_status, utc_now_iso() if send_status == SEND_SENT else None,
                 message_id, details, record_id),
            )
        log.info("History record #%s: send %s%s", record_id, send_status, f" ({note})" if note else "")

    def was_sent(self, sha256: str) -> bool:
        """True if an e-mail with this exact PDF was (or may have been) sent."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM processing_history WHERE sha256 = ? AND send_status IN (?, ?) LIMIT 1",
                (sha256, SEND_SENT, SEND_UNKNOWN),
            ).fetchone()
        return row is not None

    # -- reading ------------------------------------------------------------
    def get(self, record_id: int) -> HistoryRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM processing_history WHERE id = ?", (record_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def find_blocking_record(self, sha256: str) -> HistoryRecord | None:
        """Most recent record that marks this file content as processed (or
        with an unclear outcome).  None means the PDF may be processed."""
        placeholders = ", ".join("?" for _ in BLOCKING_STATUSES)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM processing_history
                 WHERE sha256 = ? AND status IN ({placeholders})
                 ORDER BY id DESC LIMIT 1
                """,
                (sha256, *BLOCKING_STATUSES),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def latest_record_for_hash(self, sha256: str) -> HistoryRecord | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM processing_history WHERE sha256 = ? ORDER BY id DESC LIMIT 1",
                (sha256,),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def find_blocking_by_path_stat(
        self, pdf_path: str, file_size: int, file_mtime: float
    ) -> HistoryRecord | None:
        """Fast pre-check without hashing: a blocking record for the same path,
        size and modification time almost certainly means the same file.
        (The scanner still uses the hash as the authoritative check.)"""
        placeholders = ", ".join("?" for _ in BLOCKING_STATUSES)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM processing_history
                 WHERE pdf_path = ? AND file_size = ? AND file_mtime = ?
                   AND status IN ({placeholders})
                 ORDER BY id DESC LIMIT 1
                """,
                (pdf_path, file_size, file_mtime, *BLOCKING_STATUSES),
            ).fetchone()
        return self._row_to_record(row) if row else None

    def list_history(self, limit: int = 1000, include_dry_runs: bool = True) -> list[HistoryRecord]:
        query = "SELECT * FROM processing_history"
        params: tuple = ()
        if not include_dry_runs:
            query += " WHERE status != ?"
            params = (STATUS_DRY_RUN,)
        query += " ORDER BY id DESC LIMIT ?"
        params = (*params, limit)
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_record(row) for row in rows]

    def list_incomplete(self) -> list[HistoryRecord]:
        """Records whose outcome is unknown (interrupted or unverified)."""
        placeholders = ", ".join("?" for _ in INCOMPLETE_STATUSES)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM processing_history WHERE status IN ({placeholders}) ORDER BY id",
                INCOMPLETE_STATUSES,
            ).fetchall()
        return [self._row_to_record(row) for row in rows]

    def resolve_manually(self, record_id: int, status: str, note: str) -> None:
        """User-confirmed resolution of an unverified/interrupted record."""
        if status not in (STATUS_SUCCESS, STATUS_FAILED):
            raise ValueError("Manual resolution must be 'success' or 'failed'")
        record = self.get(record_id)
        if record is None:
            raise DatabaseError(f"History record #{record_id} does not exist")
        previous = record.error_message or ""
        message = f"{note} {previous}".strip() if previous else note
        self.finish_operation(record_id, status, error_message=message)
