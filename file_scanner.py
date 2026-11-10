"""
file_scanner.py - Find the newest unprocessed application PDF.

Rules
-----
* Only the selected folder itself is scanned (no sub-folders).
* Only *.pdf files are considered; everything else is ignored.
* Temporary/incomplete files are ignored: Office lock files (~$...),
  LibreOffice locks (.~lock...), hidden/system/temporary files, empty files,
  and PDFs without a PDF header or without the %%EOF end marker.
* Files that are still being copied or written are ignored: files modified
  in the last few seconds, files whose size/modification time changes during
  a short re-check, and (on Windows) files another program holds open for
  writing.
* Files are ordered by modification time, newest first.  The first file that
  has not been processed yet (checked by SHA-256 hash, so renamed or moved
  copies are recognised) is selected.
* Exactly ONE file is returned per scan.  The scanner never processes files,
  it only selects a candidate; local files are only ever read, never changed.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from database import Database, HistoryRecord, STATUS_FAILED

log = logging.getLogger(__name__)

_HASH_CHUNK = 1024 * 1024
_WINDOWS_SKIP_ATTRIBUTES = (
    getattr(stat, "FILE_ATTRIBUTE_HIDDEN", 0x2)
    | getattr(stat, "FILE_ATTRIBUTE_SYSTEM", 0x4)
    | getattr(stat, "FILE_ATTRIBUTE_TEMPORARY", 0x100)
)


class ScanError(Exception):
    """The folder cannot be scanned (missing, not a folder, no access)."""


@dataclass
class PdfFile:
    path: Path
    size: int
    mtime: float
    sha256: str

    @property
    def name(self) -> str:
        return self.path.name


@dataclass
class SkippedFile:
    name: str
    reason: str


@dataclass
class ScanResult:
    folder: Path
    selected: PdfFile | None
    message: str
    skipped: list[SkippedFile] = field(default_factory=list)
    total_pdfs: int = 0
    processed_count: int = 0
    previous_failure: HistoryRecord | None = None


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_HASH_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_open_for_writing_elsewhere(path: Path) -> bool:
    """Windows only: True if another process has the file open for writing
    (or exclusively).  Opening with FILE_SHARE_READ fails with a sharing
    violation in that case.  PDF viewers (read-only handles) do not block."""
    if sys.platform != "win32":
        return False
    try:  # pragma: no cover - Windows only
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        GENERIC_READ = 0x80000000
        FILE_SHARE_READ = 0x00000001
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x80
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
        handle = kernel32.CreateFileW(
            str(path), GENERIC_READ, FILE_SHARE_READ, None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None
        )
        if handle in (None, 0, INVALID_HANDLE_VALUE):
            error = ctypes.get_last_error()
            return error in (32, 33)  # ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION
        kernel32.CloseHandle(handle)
        return False
    except Exception:  # pragma: no cover
        log.debug("Write-lock check failed for %s", path, exc_info=True)
        return False


def pdf_completeness_problem(path: Path) -> str | None:
    """None if the file looks like a complete PDF, else a description."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(1024)
            if b"%PDF-" not in head:
                return "not a valid PDF (no PDF header)"
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            tail = fh.read()
    except OSError as exc:
        return f"cannot be read ({exc.strerror or exc})"
    if b"%%EOF" not in tail:
        return "PDF end marker missing - the file is probably still being written or incomplete"
    return None


class FileScanner:
    """Selects the newest unprocessed, complete PDF in a folder."""

    def __init__(
        self,
        db: Database,
        settings: dict[str, Any],
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.db = db
        self.min_age = float(settings.get("min_file_age_seconds", 3.0))
        self.stability_delay = float(settings.get("stability_check_seconds", 1.0))
        self.require_complete = bool(settings.get("require_complete_pdf", True))
        self.ignored_prefixes = tuple(settings.get("ignored_prefixes", ["~$", "~", ".~lock", "."]))
        self._clock = clock
        self._sleep = sleep

    # -- helpers ------------------------------------------------------------
    def _temp_reason(self, name: str, st: os.stat_result) -> str | None:
        if name.startswith(self.ignored_prefixes):
            return "temporary or hidden file"
        attributes = getattr(st, "st_file_attributes", 0)
        if attributes & _WINDOWS_SKIP_ATTRIBUTES:
            return "hidden, system or temporary file"
        if st.st_size == 0:
            return "empty file (incomplete)"
        return None

    def _list_pdfs(self, folder: Path) -> tuple[list[tuple[Path, os.stat_result]], list[SkippedFile]]:
        if not folder.exists():
            raise ScanError(f"The folder does not exist: {folder}")
        if not folder.is_dir():
            raise ScanError(f"This is not a folder: {folder}")
        entries: list[tuple[Path, os.stat_result]] = []
        skipped: list[SkippedFile] = []
        try:
            with os.scandir(folder) as iterator:
                for entry in iterator:
                    try:
                        if not entry.is_file():
                            continue
                        if not entry.name.lower().endswith(".pdf"):
                            continue  # non-PDF files are ignored silently
                        st = entry.stat()
                    except OSError:
                        continue
                    reason = self._temp_reason(entry.name, st)
                    if reason:
                        skipped.append(SkippedFile(entry.name, reason))
                        continue
                    entries.append((Path(entry.path), st))
        except PermissionError as exc:
            raise ScanError(f"No permission to read the folder: {folder}") from exc
        except OSError as exc:
            raise ScanError(f"Cannot read the folder {folder}: {exc}") from exc
        # Newest first; the file name is a deterministic tie-breaker.
        entries.sort(key=lambda item: (item[1].st_mtime, item[0].name), reverse=True)
        return entries, skipped

    def _stability_problem(self, path: Path, st: os.stat_result) -> str | None:
        """None if the file is stable and complete, else the reason it is not."""
        if self.stability_delay > 0:
            self._sleep(self.stability_delay)
        try:
            st2 = path.stat()
        except OSError:
            return "disappeared during the check"
        if st2.st_size != st.st_size or st2.st_mtime_ns != st.st_mtime_ns:
            return "still being written (size or time changed during the check)"
        if _is_open_for_writing_elsewhere(path):
            return "still open for writing in another program"
        if self.require_complete:
            problem = pdf_completeness_problem(path)
            if problem:
                return problem
        return None

    def _processed_reason(self, path: Path, st: os.stat_result, sha256: str | None) -> str | None:
        record = None
        if sha256 is None:
            record = self.db.find_blocking_by_path_stat(str(path), st.st_size, st.st_mtime)
        else:
            record = self.db.find_blocking_record(sha256)
        if record is None:
            return None
        same_name = record.pdf_filename == path.name
        as_name = "" if same_name else f" as '{record.pdf_filename}'"
        return f"already processed{as_name} on {record.local_time()} ({record.status_label})"

    # -- public API -------------------------------------------------------
    def scan(self, folder: Path, exclude_hashes: Iterable[str] = (),
             newer_than: float | None = None) -> ScanResult:
        """Select the newest unprocessed PDF (exactly one, or none).

        newer_than: only consider files modified after this Unix time (used by
        the automatic mode so that old files are never processed by surprise)."""
        excluded = set(exclude_hashes)
        entries, skipped = self._list_pdfs(folder)
        if newer_than:
            entries = [(p, st) for p, st in entries if st.st_mtime >= newer_than]
        result = ScanResult(folder=folder, selected=None, message="", total_pdfs=len(entries))
        newer_skipped: list[SkippedFile] = []
        now = self._clock()

        for path, st in entries:
            age = now - st.st_mtime
            if age < self.min_age:
                newer_skipped.append(SkippedFile(
                    path.name, f"modified {max(0, int(age))}s ago - may still be being written"))
                continue
            quick = self._processed_reason(path, st, None)
            if quick:
                result.processed_count += 1
                newer_skipped.append(SkippedFile(path.name, quick))
                continue
            try:
                digest = sha256_of_file(path)
            except OSError as exc:
                newer_skipped.append(SkippedFile(path.name, f"cannot be read ({exc.strerror or exc})"))
                continue
            if digest in excluded:
                newer_skipped.append(SkippedFile(path.name, "skipped for this session"))
                continue
            processed = self._processed_reason(path, st, digest)
            if processed:
                result.processed_count += 1
                newer_skipped.append(SkippedFile(path.name, processed))
                continue
            problem = self._stability_problem(path, st)
            if problem:
                newer_skipped.append(SkippedFile(path.name, problem))
                continue

            result.selected = PdfFile(path=path, size=st.st_size, mtime=st.st_mtime, sha256=digest)
            last = self.db.latest_record_for_hash(digest)
            if last is not None and last.status == STATUS_FAILED:
                result.previous_failure = last
            break

        # Only report skipped files that are newer than the selection (they
        # explain why the very newest file was not chosen) plus temp files.
        result.skipped = (newer_skipped + skipped)[:25]
        if result.selected:
            result.message = f"Selected the newest unprocessed PDF: {result.selected.name}"
        elif not entries:
            result.message = "No PDF files found in the folder."
        elif result.processed_count == len(entries):
            result.message = f"All {len(entries)} PDF file(s) in the folder have already been processed."
        else:
            result.message = "No unprocessed PDF is ready yet (see skipped files)."
        log.info("Scan of %s: %s", folder, result.message)
        return result

    def list_candidates(self, folder: Path) -> list[tuple[Path, os.stat_result, str]]:
        """All PDFs with a short status, newest first (for manual selection).
        Status is 'ready' for selectable files or the reason why not."""
        entries, _skipped = self._list_pdfs(folder)
        now = self._clock()
        out = []
        for path, st in entries:
            if now - st.st_mtime < self.min_age:
                out.append((path, st, "modified just now - may still be being written"))
                continue
            try:
                digest = sha256_of_file(path)
            except OSError as exc:
                out.append((path, st, f"cannot be read ({exc.strerror or exc})"))
                continue
            processed = self._processed_reason(path, st, digest)
            out.append((path, st, processed or "ready"))
        return out

    def check_single(self, path: Path) -> PdfFile:
        """Validate one manually chosen PDF with the same rules as scan()."""
        try:
            st = path.stat()
        except FileNotFoundError as exc:
            raise ScanError(f"The file no longer exists: {path.name}") from exc
        except OSError as exc:
            raise ScanError(f"Cannot access {path.name}: {exc}") from exc
        if not path.name.lower().endswith(".pdf"):
            raise ScanError(f"{path.name} is not a PDF file.")
        reason = self._temp_reason(path.name, st)
        if reason:
            raise ScanError(f"{path.name} cannot be used: {reason}.")
        if self._clock() - st.st_mtime < self.min_age:
            raise ScanError(f"{path.name} was modified just now and may still be being written. "
                            "Try again in a few seconds.")
        try:
            digest = sha256_of_file(path)
        except OSError as exc:
            raise ScanError(f"{path.name} cannot be read: {exc}") from exc
        processed = self._processed_reason(path, st, digest)
        if processed:
            raise ScanError(f"{path.name} was {processed}.")
        problem = self._stability_problem(path, st)
        if problem:
            raise ScanError(f"{path.name} cannot be used: {problem}.")
        return PdfFile(path=path, size=st.st_size, mtime=st.st_mtime, sha256=digest)
