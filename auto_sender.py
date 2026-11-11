"""
auto_sender.py - Fully automatic mode ("watch the folder, send when it fits").

Every few seconds AutoSender.run_once():

1. looks at the LATEST PDF in the Bewerbungen folder (newest of modified /
   created time, so copied files count as new) - only that one file;
2. skips it if it was already processed (SHA-256, cached - no re-hashing);
3. reads the company from the file name;
4. reads only your most recent drafts (fast) and picks the draft:
     * exactly one recent draft clearly matches (company in subject,
       recipient name or recipient domain)          -> that draft
     * otherwise your LATEST draft, if it mentions the company anywhere
       (subject, recipient or text)                  -> the latest draft
     * otherwise                                     -> wait and look again;
5. checks the draft is finished: it has a recipient, a subject, some text,
   and was not changed for `draft_settle_seconds` (so a draft you are still
   typing - Gmail saves it every few seconds - is never sent half-written);
6. attaches the PDF (same safe procedure as the manual mode, including the
   byte-by-byte preservation check) and SENDS the e-mail.

Nothing here ever opens a dialog; run_once returns an AutoResult that the
GUI shows in the banner (and plays a sound for a sent e-mail).
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from attachment_manager import AttachError, AttachmentManager, DuplicateAttachmentError
from database import Database, STATUS_DRY_RUN, STATUS_FAILED, STATUS_SUCCESS
from file_scanner import FileScanner, PdfFile, sha256_of_file
from filename_parser import FilenameParser
from gmail_client import DraftInfo, GmailAuthError, GmailClient, GmailError
from matching_engine import POSSIBLE, STRONG, MatchingEngine

log = logging.getLogger(__name__)

# Result kinds
IDLE = "idle"          # nothing to do (no PDF / latest PDF already processed)
WAITING = "waiting"    # latest PDF cannot be sent yet; tried again automatically
SENT = "sent"          # attached and sent
DRY_RUN = "dry_run"    # would have been attached and sent
PROBLEM = "problem"    # attached but not sent / error - see message
AUTH = "auth"          # Gmail authorization expired - paused


def file_time(st: os.stat_result) -> float:
    """When the file appeared or changed.  Windows keeps the original
    'modified' time when a file is copied, but sets the creation time to
    now - so the newer of the two is used."""
    times = [st.st_mtime]
    birth = getattr(st, "st_birthtime", None)
    if birth is None and sys.platform == "win32":
        birth = st.st_ctime  # creation time on Windows
    if birth:
        times.append(birth)
    return max(times)


@dataclass
class AutoResult:
    kind: str
    message: str
    pdf_name: str | None = None
    draft_subject: str | None = None
    retry_in: float = 0.0          # seconds until this PDF is looked at again
    details: dict[str, Any] = field(default_factory=dict)


class AutoSender:
    def __init__(
        self,
        gmail: GmailClient,
        db: Database,
        settings: dict[str, Any],
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.gmail = gmail
        self.db = db
        self.settings = settings
        self.clock = clock
        scanner_settings = dict(settings["scanner"])
        scanner_settings["stability_check_seconds"] = min(
            0.5, float(scanner_settings.get("stability_check_seconds", 1.0)))
        self.scanner = FileScanner(db, scanner_settings, clock=clock, sleep=sleep)
        self.parser = FilenameParser(settings["filename_parser"])
        self.engine = MatchingEngine(settings["matching"].get("generic_email_domains", []))
        self.manager = AttachmentManager(gmail, db)
        self._hash_cache: dict[tuple[str, int, int], str] = {}
        self._next_try: dict[str, float] = {}   # sha256 -> time of next attempt
        self._last: dict[str, AutoResult] = {}  # sha256 -> last waiting result

    # -- settings -----------------------------------------------------------
    @property
    def retry_seconds(self) -> float:
        return max(5.0, float(self.settings.get("auto_retry_seconds", 15.0)))

    @property
    def settle_seconds(self) -> float:
        return max(0.0, float(self.settings.get("draft_settle_seconds", 60.0)))

    @property
    def recent_count(self) -> int:
        return max(1, int(self.settings.get("auto_recent_drafts", 15)))

    # -- helpers -------------------------------------------------------------
    def latest_pdf(self, folder: Path) -> tuple[Path, os.stat_result] | None:
        """The single most recently saved/changed PDF in the folder."""
        best: tuple[float, Path, os.stat_result] | None = None
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if not entry.is_file() or not entry.name.lower().endswith(".pdf"):
                            continue
                        st = entry.stat()
                    except OSError:
                        continue
                    if self.scanner._temp_reason(entry.name, st):
                        continue
                    key = file_time(st)
                    if best is None or (key, entry.name) > (best[0], best[1].name):
                        best = (key, Path(entry.path), st)
        except OSError as exc:
            log.warning("Cannot read folder %s: %s", folder, exc)
            return None
        return (best[1], best[2]) if best else None

    def _sha(self, path: Path, st: os.stat_result) -> str:
        key = (str(path), st.st_size, st.st_mtime_ns)
        digest = self._hash_cache.get(key)
        if digest is None:
            digest = sha256_of_file(path)
            if len(self._hash_cache) > 500:
                self._hash_cache.clear()
            self._hash_cache[key] = digest
        return digest

    def _wait(self, sha: str, pdf_name: str, message: str, retry_in: float | None = None,
              draft_subject: str | None = None) -> AutoResult:
        retry_in = self.retry_seconds if retry_in is None else retry_in
        self._next_try[sha] = self.clock() + retry_in
        result = AutoResult(WAITING, message, pdf_name, draft_subject, retry_in)
        previous = self._last.get(sha)
        if previous is None or previous.message != message:
            log.info("Waiting: %s - %s", pdf_name, message)
        self._last[sha] = result
        return result

    def choose_draft(self, company: str, drafts: list[DraftInfo]) -> tuple[DraftInfo | None, str]:
        """Pick the draft for *company* from the recent drafts (newest first).
        Returns (draft, reason) - draft is None when nothing fits yet."""
        if not drafts:
            return None, "no drafts in Gmail yet"
        outcome = self.engine.match(company, drafts)
        by_id = {m.draft.draft_id: m for m in outcome.all_matches}
        latest = drafts[0]
        latest_match = by_id.get(latest.draft_id)
        strong = [m for m in outcome.all_matches if m.strength == STRONG]
        if len(strong) == 1:
            return strong[0].draft, "company found in " + strong[0].evidence_summary()
        if len(strong) > 1:
            if latest_match is not None and latest_match.strength == STRONG:
                return latest, "latest draft, company found in " + latest_match.evidence_summary()
            return None, f"{len(strong)} recent drafts are for '{company}' - delete the extra one"
        if latest_match is not None and latest_match.strength == POSSIBLE:
            return latest, "latest draft, company found in " + latest_match.evidence_summary()
        return None, f"no recent draft mentions '{company}' yet"

    def draft_not_ready(self, draft: DraftInfo) -> str | None:
        if not draft.to:
            return "the draft has no recipient yet"
        if not draft.subject.strip():
            return "the draft has no subject yet"
        if len(" ".join(draft.body_text.split())) < 20:
            return "the draft has (almost) no text yet"
        if draft.internal_date is not None and self.settle_seconds:
            age = self.clock() - draft.internal_date.timestamp()
            if age < self.settle_seconds:
                return (f"the draft was changed {max(0, int(age))} s ago - sending when it was not "
                        f"changed for {int(self.settle_seconds)} s")
        return None

    # -- main step ---------------------------------------------------------
    def run_once(self, folder: Path, *, dry_run: bool) -> AutoResult:
        latest = self.latest_pdf(folder)
        if latest is None:
            return AutoResult(IDLE, "No PDF in the folder yet.")
        path, st = latest
        try:
            sha = self._sha(path, st)
        except OSError:
            return AutoResult(WAITING, "still being saved", path.name, retry_in=2)
        if self.db.find_blocking_record(sha) is not None:
            return AutoResult(IDLE, f"Latest PDF already processed: {path.name}", path.name)
        if self.clock() < self._next_try.get(sha, 0.0):
            return self._last.get(sha) or AutoResult(WAITING, "waiting", path.name)

        # The file must be completely written.
        if self.clock() - file_time(st) < self.scanner.min_age:
            return AutoResult(WAITING, "still being saved", path.name, retry_in=2)
        problem = self.scanner._stability_problem(path, st)
        if problem:
            return self._wait(sha, path.name, problem, retry_in=3)
        pdf = PdfFile(path=path, size=st.st_size, mtime=st.st_mtime, sha256=sha)

        parsed = self.parser.parse(path.name)
        if not parsed.ok:
            return self._wait(sha, path.name, f"company not readable from the file name: {parsed.error}",
                              retry_in=60)
        company = parsed.company

        try:
            drafts = self.gmail.recent_drafts(self.recent_count)
            draft, reason = self.choose_draft(company, drafts)
            if draft is None:
                return self._wait(sha, path.name, reason)
            not_ready = self.draft_not_ready(draft)
            if not_ready:
                return self._wait(sha, path.name, not_ready, retry_in=min(self.retry_seconds, 10),
                                  draft_subject=draft.subject)
            plan = self.manager.prepare(pdf, company, draft)
        except GmailAuthError as exc:
            return AutoResult(AUTH, str(exc), path.name, retry_in=30)
        except DuplicateAttachmentError as exc:
            if exc.exact_duplicate:
                self.manager.mark_already_attached(pdf, company, draft)
                return AutoResult(PROBLEM, f"{path.name}: {exc} Not sent automatically.", path.name,
                                  draft.subject)
            return self._wait(sha, path.name, str(exc), retry_in=60)
        except (AttachError, GmailError) as exc:
            return self._wait(sha, path.name, f"problem: {exc}", retry_in=30)

        log.info("Automatic: %s -> draft '%s' (%s)", path.name, draft.subject, reason)
        result = self.manager.execute(plan, dry_run=dry_run, send=True)
        self._next_try.pop(sha, None)
        self._last.pop(sha, None)
        if result.status == STATUS_DRY_RUN:
            self._next_try[sha] = float("inf")  # do not repeat the dry run every few seconds
            return AutoResult(DRY_RUN, result.message, path.name, draft.subject)
        if result.status == STATUS_SUCCESS and result.sent:
            return AutoResult(SENT, result.message, path.name, draft.subject)
        if result.status == STATUS_FAILED:
            return self._wait(sha, path.name, f"not attached: {result.message}", retry_in=30)
        return AutoResult(PROBLEM, result.message, path.name, draft.subject)


def describe_waiting(result: AutoResult) -> str:
    return f"{result.pdf_name}: {result.message}" if result.pdf_name else result.message


__all__ = ["AutoSender", "AutoResult", "IDLE", "WAITING", "SENT", "DRY_RUN", "PROBLEM", "AUTH",
           "file_time", "describe_waiting"]
