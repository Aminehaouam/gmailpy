"""
auto_sender.py - Fully automatic mode ("watch the folder, send when it fits").

Twice a second AutoSender.run_once():

1. looks at the new PDFs in the Bewerbungen folder (newest of modified /
   created time, so copied files count as new): the LATEST PDF, and in batch
   mode every PDF saved since watching started - oldest first;
2. skips PDFs already processed (SHA-256, cached - no re-hashing);
3. reads the company from the file name;
4. reads only your most recent drafts (fast; read ahead of time while
   nothing is waiting) and picks the draft:
     * exactly one recent draft clearly matches (company in subject,
       recipient name or recipient domain, or the application sentence
       "... bei <company> bewerben")                -> that draft
     * for the latest PDF only: your LATEST draft, if it mentions the company
       anywhere (subject, recipient or text) and is not clearly the draft of
       another waiting PDF                           -> the latest draft
     * otherwise                                     -> wait and look again;
5. checks the draft has a recipient, a subject and some text (instant
   checks; optionally also `draft_settle_seconds` without changes, default 0
   = no waiting);
6. immediately attaches the PDF (same safe procedure as the manual mode,
   including the byte-by-byte preservation check and reading the draft back)
   and SENDS the e-mail.  One e-mail per call; when more PDFs are waiting the
   result says so and the GUI calls again at once.

A PDF that cannot be sent yet never holds up the others.  Nothing here ever
opens a dialog; run_once returns an AutoResult that the GUI shows in the
banner (and plays a sound for a sent e-mail).
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
            0.3, float(scanner_settings.get("stability_check_seconds", 0.3)))
        self.scanner = FileScanner(db, scanner_settings, clock=clock, sleep=sleep)
        self.parser = FilenameParser(settings["filename_parser"])
        self.engine = MatchingEngine(settings["matching"].get("generic_email_domains", []))
        self.manager = AttachmentManager(gmail, db)
        # Batch mode: PDFs that appeared after this moment count as new.
        lookback = max(0.0, float(settings.get("auto_batch_lookback_minutes", 5))) * 60
        self.since = clock() - lookback
        self._last_prefetch = 0.0
        self._hash_cache: dict[tuple[str, int, int], str] = {}
        self._stable: set[str] = set()          # sha256 of PDFs found completely saved
        self._next_try: dict[str, float] = {}   # sha256 -> time of next attempt
        self._last: dict[str, AutoResult] = {}  # sha256 -> last waiting result

    # -- settings -----------------------------------------------------------
    @property
    def retry_seconds(self) -> float:
        return max(1.0, float(self.settings.get("auto_retry_seconds", 3.0)))

    @property
    def settle_seconds(self) -> float:
        return max(0.0, float(self.settings.get("draft_settle_seconds", 0.0)))

    @property
    def recent_count(self) -> int:
        return max(1, int(self.settings.get("auto_recent_drafts", 15)))

    @property
    def batch(self) -> bool:
        return bool(self.settings.get("auto_batch", True))

    @property
    def prefetch_seconds(self) -> float:
        return float(self.settings.get("auto_prefetch_seconds", 3.0))

    # -- helpers -------------------------------------------------------------
    def _pdfs(self, folder: Path) -> list[tuple[float, str, Path, os.stat_result]]:
        """All PDFs of the folder (temporary files left out), oldest first."""
        found = []
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
                    found.append((file_time(st), entry.name, Path(entry.path), st))
        except OSError as exc:
            log.warning("Cannot read folder %s: %s", folder, exc)
            return []
        found.sort(key=lambda item: (item[0], item[1]))
        return found

    def latest_pdf(self, folder: Path) -> tuple[Path, os.stat_result] | None:
        """The single most recently saved/changed PDF in the folder."""
        pdfs = self._pdfs(folder)
        return (pdfs[-1][2], pdfs[-1][3]) if pdfs else None

    def candidate_pdfs(self, folder: Path) -> list[tuple[Path, os.stat_result]]:
        """The PDFs to look at, oldest first; the latest PDF is always last.
        Batch mode: every PDF saved since watching started; otherwise only
        the latest PDF."""
        pdfs = self._pdfs(folder)
        if not pdfs:
            return []
        chosen = [p for p in pdfs[:-1] if p[0] >= self.since] if self.batch else []
        return [(path, st) for _t, _n, path, st in chosen + [pdfs[-1]]]

    def _patience(self, st: os.stat_result) -> float:
        """Retry interval while waiting for a draft: quick at first, less
        often for PDFs that have been waiting for a while."""
        age = self.clock() - file_time(st)
        if age < 120:
            return self.retry_seconds
        return max(self.retry_seconds, 5.0 if age < 1800 else 30.0)

    def _maybe_prefetch(self) -> None:
        """While nothing is waiting: read the recent drafts ahead of time."""
        if self.prefetch_seconds <= 0 or self.clock() - self._last_prefetch < self.prefetch_seconds:
            return
        self._last_prefetch = self.clock()
        prefetch = getattr(self.gmail, "prefetch_recent", None)
        if prefetch is None:
            return
        try:
            prefetch(self.recent_count)
        except Exception as exc:   # only a speed-up - never an error
            log.debug("Reading drafts ahead of time failed: %s", exc)

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

    def choose_draft(self, company: str, drafts: list[DraftInfo], *, allow_latest: bool = True,
                     reserved: set[str] | frozenset[str] = frozenset()) -> tuple[DraftInfo | None, str]:
        """Pick the draft for *company* from the recent drafts (newest first).
        Returns (draft, reason) - draft is None when nothing fits yet.

        allow_latest: the latest draft may be used when it merely mentions
        the company (only for the latest PDF).  reserved: drafts that clearly
        belong to another waiting PDF - never used through that rule."""
        if not drafts:
            return None, "no drafts in Gmail yet"
        outcome = self.engine.match(company, drafts)
        by_id = {m.draft.draft_id: m for m in outcome.all_matches}
        latest = drafts[0]
        latest_match = by_id.get(latest.draft_id) if latest.draft_id not in reserved else None
        strong = [m for m in outcome.all_matches if m.strength == STRONG]
        if len(strong) == 1:
            return strong[0].draft, "company found in " + strong[0].evidence_summary()
        if len(strong) > 1:
            if allow_latest and latest_match is not None and latest_match.strength == STRONG:
                return latest, "latest draft, company found in " + latest_match.evidence_summary()
            return None, f"{len(strong)} recent drafts are for '{company}' - delete the extra one"
        if allow_latest and latest_match is not None and latest_match.strength == POSSIBLE:
            return latest, "latest draft, company found in " + latest_match.evidence_summary()
        possible = [m for m in outcome.all_matches if m.strength == POSSIBLE]
        if possible:
            return None, (f"'{company}' appears only somewhere in the text of {len(possible)} draft(s), "
                          f"not in the subject, the recipient or the sentence 'bei {company} bewerben'")
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
        """Look at the new PDFs and send at most one e-mail."""
        candidates = self.candidate_pdfs(folder)
        if not candidates:
            self._maybe_prefetch()
            return AutoResult(IDLE, "No PDF in the folder yet.")
        latest_path = candidates[-1][0]
        pending: list[tuple[Path, os.stat_result, str | None]] = []
        for path, st in candidates:
            try:
                sha = self._sha(path, st)
            except OSError:
                pending.append((path, st, None))      # still being saved
                continue
            if self.db.find_blocking_record(sha) is None:
                pending.append((path, st, sha))
        if not pending:
            self._maybe_prefetch()
            return AutoResult(IDLE, f"Latest PDF already processed: {latest_path.name}", latest_path.name)

        state: dict[str, Any] = {}     # draft list of this call (read once, when needed)
        waiting: list[AutoResult] = []
        for path, st, sha in pending:
            if sha is None:
                waiting.append(AutoResult(WAITING, "still being saved", path.name, retry_in=0.5))
                continue
            if self.clock() < self._next_try.get(sha, 0.0):
                waiting.append(self._last.get(sha) or AutoResult(WAITING, "waiting", path.name))
                continue
            result = self._handle(path, st, sha, is_latest=path == latest_path, pending=pending,
                                  state=state, dry_run=dry_run)
            if result.kind == WAITING:
                waiting.append(result)
                continue
            result.details["more"] = len(pending) > 1
            return result
        return self._summary(waiting, latest_path.name)

    def _summary(self, waiting: list[AutoResult], latest_name: str) -> AutoResult:
        """One result for the banner: the latest PDF's, and how many wait.
        details["soon"]: a PDF is just being saved - look again after that
        many seconds instead of at the next regular check."""
        main = next((r for r in waiting if r.pdf_name == latest_name), waiting[0])
        soon = [r.details["soon"] for r in waiting if "soon" in r.details]
        if len(waiting) == 1:
            return main
        result = AutoResult(main.kind, f"{main.message} (+{len(waiting) - 1} more PDF(s) waiting)",
                            main.pdf_name, main.draft_subject, min(r.retry_in for r in waiting))
        result.details["waiting"] = len(waiting)
        if soon:
            result.details["soon"] = min(soon)
        return result

    def _company(self, path: Path) -> str | None:
        parsed = self.parser.parse(path.name)
        return parsed.company if parsed.ok else None

    def _reserved(self, company: str, pending: list, drafts: list[DraftInfo]) -> set[str]:
        """Drafts that clearly belong to another waiting PDF's company."""
        reserved: set[str] = set()
        for path, _st, sha in pending:
            other = self._company(path) if sha else None
            if not other or other == company:
                continue
            for match in self.engine.match(other, drafts).all_matches:
                if match.strength == STRONG:
                    reserved.add(match.draft.draft_id)
        return reserved

    def _handle(self, path: Path, st: os.stat_result, sha: str, *, is_latest: bool, pending: list,
                state: dict[str, Any], dry_run: bool) -> AutoResult:
        """Try one PDF: wait, or attach and send it."""
        # The file must be completely written.
        age = self.clock() - file_time(st)
        if age < self.scanner.min_age:
            remaining = max(0.05, self.scanner.min_age - age)
            return AutoResult(WAITING, "still being saved", path.name, retry_in=remaining,
                              details={"soon": remaining})
        if sha not in self._stable:
            problem = self.scanner._stability_problem(path, st)
            if problem:
                return self._wait(sha, path.name, problem, retry_in=0.5)
            if len(self._stable) > 500:
                self._stable.clear()
            self._stable.add(sha)
        pdf = PdfFile(path=path, size=st.st_size, mtime=st.st_mtime, sha256=sha)

        parsed = self.parser.parse(path.name)
        if not parsed.ok:
            return self._wait(sha, path.name, f"company not readable from the file name: {parsed.error}",
                              retry_in=60)
        company = parsed.company

        try:
            if "drafts" not in state:
                # Gmail confirms the drafts' current versions as of now.
                state["listed_at"] = time.monotonic()
                count = max(self.recent_count, min(100, len(pending) + 10)) if self.batch else self.recent_count
                state["drafts"] = self.gmail.recent_drafts(count)
            drafts = state["drafts"]
            reserved = self._reserved(company, pending, drafts) if len(pending) > 1 else set()
            draft, reason = self.choose_draft(company, drafts, allow_latest=is_latest, reserved=reserved)
            if draft is None:
                return self._wait(sha, path.name, reason, retry_in=self._patience(st))
            not_ready = self.draft_not_ready(draft)
            if not_ready:
                return self._wait(sha, path.name, not_ready, retry_in=min(self._patience(st), 10),
                                  draft_subject=draft.subject)
            plan = self.manager.prepare(pdf, company, draft)
            plan.confirmed_at = state["listed_at"]
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
