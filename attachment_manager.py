"""
attachment_manager.py - Safely add a PDF to an existing Gmail draft.

How a draft is modified
-----------------------
Gmail's drafts.update REPLACES the draft message with the message that is
uploaded.  To keep everything, the complete original message is downloaded
(drafts.get format=raw, i.e. the exact RFC 822 source including all existing
attachments), parsed with Python's email package, and ONE new MIME part
(the PDF) is appended:

    * root is multipart/mixed      -> the PDF part is appended to it
    * anything else (text/plain, text/html, multipart/alternative,
      multipart/related ...)       -> the original body is moved unchanged
                                      into a new multipart/mixed container
                                      and the PDF part is appended

All original headers (From, To, Cc, Bcc, Subject, In-Reply-To, References,
...) are written back verbatim, and every original MIME part keeps its exact
bytes - the HTML body, inline images and existing attachments are untouched.

Before anything is uploaded the new message is re-parsed and compared with
the original part by part (verify_preservation).  Any difference aborts the
operation.  After Gmail confirms the update the draft is read again and
checked (the new PDF must be present, existing attachments must still be
there).  Only then is the history record marked "success".

Sending (optional, execute(send=True)): only after the attachment was
confirmed by Gmail AND verified by reading the draft back.  A send is never
retried automatically (a retry after a timeout could send twice), and a PDF
that was already sent is never sent again.

Never: create a new draft, delete a draft, remove attachments, or modify the
local PDF file.
"""

from __future__ import annotations

import email
import email.errors
import email.policy
import hashlib
import io
import logging
from collections import Counter
from dataclasses import dataclass, field
from email.generator import BytesGenerator
from email.message import EmailMessage, MIMEPart
from pathlib import Path

from config import GMAIL_ATTACHMENT_LIMIT, GMAIL_RAW_UPLOAD_LIMIT
from database import (
    SEND_FAILED,
    SEND_SENT,
    SEND_UNKNOWN,
    Database,
    STATUS_ALREADY_ATTACHED,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_SUCCESS,
    STATUS_UNVERIFIED,
)
from file_scanner import PdfFile
from gmail_client import (
    AttachmentInfo,
    DraftInfo,
    DraftNotFoundError,
    ForbiddenOperationError,
    GmailApiError,
    GmailAuthError,
    GmailClient,
    GmailError,
    GmailNetworkError,
    RawDraft,
    human_size,
    parse_raw_message,
)

log = logging.getLogger(__name__)

# Policy used to parse and re-generate drafts:
#  * refold_source="none": original header lines are written back verbatim
#  * linesep CRLF: RFC 5322 line endings
#  * cte_type 8bit (default): original 8-bit bodies are written back unchanged
_POLICY = email.policy.default.clone(refold_source="none", linesep="\r\n")

# Structural parse problems that make it unsafe to rewrite a message.
_FATAL_DEFECTS = (
    email.errors.NoBoundaryInMultipartDefect,
    email.errors.StartBoundaryNotFoundDefect,
    email.errors.CloseBoundaryNotFoundDefect,
    email.errors.MultipartInvariantViolationDefect,
    email.errors.MissingHeaderBodySeparatorDefect,
    email.errors.FirstHeaderLineIsContinuationDefect,
    email.errors.MisplacedEnvelopeHeaderDefect,
)


# --------------------------------------------------------------------------
# Exceptions
# --------------------------------------------------------------------------

class AttachError(Exception):
    """The PDF cannot be attached; nothing was changed in Gmail."""


class DuplicateAttachmentError(AttachError):
    """The PDF (or a file with the same name) is already in the draft."""

    def __init__(self, message: str, exact_duplicate: bool):
        super().__init__(message)
        self.exact_duplicate = exact_duplicate


class DraftChangedError(AttachError):
    """The draft was edited in Gmail after the preview."""


class LocalFileChangedError(AttachError):
    """The PDF was changed, moved or deleted after the scan."""


class UnsafeDraftStructureError(AttachError):
    """The draft's MIME structure cannot be modified safely."""


class PreservationError(AttachError):
    """The rebuilt message would not preserve the original content."""


# --------------------------------------------------------------------------
# MIME construction and verification (pure functions, fully unit-tested)
# --------------------------------------------------------------------------

def _parse(raw: bytes) -> EmailMessage:
    return email.message_from_bytes(raw, policy=_POLICY)


def _check_structure(msg: EmailMessage) -> None:
    for part in msg.walk():
        for defect in part.defects:
            if isinstance(defect, _FATAL_DEFECTS):
                raise UnsafeDraftStructureError(
                    "The draft's internal structure is damaged or unusual "
                    f"({type(defect).__name__}). To be safe, nothing was changed - "
                    "please attach the PDF manually in Gmail."
                )


def _serialize(msg: EmailMessage) -> bytes:
    buffer = io.BytesIO()
    BytesGenerator(buffer, mangle_from_=False, policy=_POLICY).flatten(msg)
    return buffer.getvalue()


def build_message_with_attachment(original_raw: bytes, pdf_bytes: bytes, filename: str) -> bytes:
    """Return the original message with *pdf_bytes* added as an attachment.

    Nothing in the original message is removed or altered; one MIME part is
    added (see module docstring)."""
    msg = _parse(original_raw)
    _check_structure(msg)

    attachment = MIMEPart(policy=_POLICY)
    attachment.set_content(
        pdf_bytes,
        maintype="application",
        subtype="pdf",
        disposition="attachment",
        filename=filename,
        params={"name": filename},  # older clients read the name from Content-Type
    )

    if msg.get_content_type() != "multipart/mixed":
        # Moves all Content-* headers and the body (unchanged) into a new
        # first sub-part; all other headers stay on the top level.
        msg.make_mixed()
    if "MIME-Version" not in msg:
        msg["MIME-Version"] = "1.0"
    msg.attach(attachment)
    return _serialize(msg)


def _leaves(msg: EmailMessage) -> list[EmailMessage]:
    return [part for part in msg.walk() if not part.is_multipart()]


def _content_headers(part: EmailMessage) -> list[tuple[str, str]]:
    return [(k.lower(), str(v)) for k, v in part.items() if k.lower().startswith("content-")]


def _envelope_headers(msg: EmailMessage) -> list[tuple[str, str]]:
    return [
        (k.lower(), str(v)) for k, v in msg.items()
        if not k.lower().startswith("content-") and k.lower() != "mime-version"
    ]


def verify_preservation(original_raw: bytes, new_raw: bytes, pdf_bytes: bytes, filename: str) -> None:
    """Raise PreservationError unless *new_raw* is exactly *original_raw*
    plus one PDF attachment.  Checked: all top-level headers (recipients,
    subject, threading headers ...), the MIME tree shape, and for every
    original part its content headers and decoded bytes."""
    original = _parse(original_raw)
    new = _parse(new_raw)
    problems: list[str] = []

    if _envelope_headers(original) != _envelope_headers(new):
        problems.append("message headers (recipients/subject) differ")

    original_shape = [p.get_content_type() for p in original.walk()]
    new_shape = [p.get_content_type() for p in new.walk()]
    if original.get_content_type() == "multipart/mixed":
        expected_shape = original_shape + ["application/pdf"]
    else:
        expected_shape = ["multipart/mixed"] + original_shape + ["application/pdf"]
    if new_shape != expected_shape:
        problems.append(f"MIME structure differs ({new_shape} != {expected_shape})")

    original_leaves = _leaves(original)
    new_leaves = _leaves(new)
    if len(new_leaves) != len(original_leaves) + 1:
        problems.append("number of message parts differs")
    else:
        for index, (old_part, new_part) in enumerate(zip(original_leaves, new_leaves)):
            if _content_headers(old_part) != _content_headers(new_part):
                problems.append(f"headers of part {index + 1} differ")
            if (old_part.get_payload(decode=True) or b"") != (new_part.get_payload(decode=True) or b""):
                problems.append(f"content of part {index + 1} ({old_part.get_content_type()}) differs")
        added = new_leaves[-1]
        if added.get_content_type() != "application/pdf":
            problems.append("added part is not a PDF")
        if added.get_filename() != filename:
            problems.append("added part has the wrong file name")
        if added.get_content_disposition() != "attachment":
            problems.append("added part is not an attachment")
        if (added.get_payload(decode=True) or b"") != pdf_bytes:
            problems.append("added PDF bytes differ from the file")

    if problems:
        raise PreservationError(
            "Safety check failed - the updated draft would not preserve the original "
            "exactly: " + "; ".join(problems) + ". Nothing was changed."
        )


def find_duplicate(existing: list[AttachmentInfo], pdf_sha256: str, filename: str) -> DuplicateAttachmentError | None:
    """Detect the PDF (by content hash) or its file name among existing attachments."""
    for attachment in existing:
        if attachment.sha256 and attachment.sha256 == pdf_sha256:
            same = attachment.filename == filename
            return DuplicateAttachmentError(
                "This exact PDF is already attached to the draft"
                + ("" if same else f" (as '{attachment.filename}')") + ". Nothing was changed.",
                exact_duplicate=True,
            )
    for attachment in existing:
        if attachment.filename.casefold() == filename.casefold():
            return DuplicateAttachmentError(
                f"The draft already has a different attachment named '{attachment.filename}' "
                f"({human_size(attachment.size)}). Remove it in Gmail first if you want to "
                "attach the new version. Nothing was changed.",
                exact_duplicate=False,
            )
    return None


# --------------------------------------------------------------------------
# Plans and results
# --------------------------------------------------------------------------

@dataclass
class AttachPlan:
    """Everything needed for one confirmed attachment (built by prepare())."""

    pdf: PdfFile
    pdf_bytes: bytes
    company: str
    draft: DraftInfo                 # preview information shown to the user
    original: RawDraft               # exact original message
    existing_attachments: list[AttachmentInfo]
    new_raw: bytes
    warnings: list[str] = field(default_factory=list)

    @property
    def new_size(self) -> int:
        return len(self.new_raw)


@dataclass
class AttachResult:
    status: str
    message: str
    record_id: int | None = None
    new_message_id: str | None = None
    send_status: str | None = None   # None = sending not requested; else SEND_*

    @property
    def success(self) -> bool:
        return self.status == STATUS_SUCCESS

    @property
    def sent(self) -> bool:
        return self.send_status == SEND_SENT


# --------------------------------------------------------------------------
# Manager
# --------------------------------------------------------------------------

class AttachmentManager:
    def __init__(self, gmail: GmailClient, db: Database):
        self.gmail = gmail
        self.db = db

    # -- step 1: prepare (read-only) ------------------------------------
    def prepare(self, pdf: PdfFile, company: str, draft: DraftInfo) -> AttachPlan:
        """Read the current draft and build (but do not upload) the updated
        message.  Read-only: Gmail is not modified.  Raises AttachError."""
        pdf_bytes = self._read_pdf(pdf)

        try:
            original = self.gmail.get_draft_raw(draft.draft_id)
        except DraftNotFoundError as exc:
            raise AttachError(str(exc)) from exc
        if original.message_id != draft.message_id:
            raise DraftChangedError(
                "The draft was changed in Gmail after it was shown here. "
                "Please scan again to review the current version."
            )

        parsed = parse_raw_message(original.raw)
        duplicate = find_duplicate(parsed.attachments, pdf.sha256, pdf.name)
        if duplicate:
            raise duplicate

        total_attachments = sum(a.size for a in parsed.attachments) + len(pdf_bytes)
        if total_attachments > GMAIL_ATTACHMENT_LIMIT:
            raise AttachError(
                f"The attachments would total {human_size(total_attachments)}, more than Gmail's "
                f"limit of {human_size(GMAIL_ATTACHMENT_LIMIT)}. Nothing was changed."
            )

        new_raw = build_message_with_attachment(original.raw, pdf_bytes, pdf.name)
        verify_preservation(original.raw, new_raw, pdf_bytes, pdf.name)
        if len(new_raw) > GMAIL_RAW_UPLOAD_LIMIT:
            raise AttachError("The updated draft would be too large for Gmail. Nothing was changed.")

        warnings = []
        if not draft.to:
            warnings.append("The draft has no 'To' recipient yet.")
        log.info(
            "Prepared attachment of %s (%s) to draft %s; new message size %s",
            pdf.name, human_size(len(pdf_bytes)), draft.draft_id, human_size(len(new_raw)),
        )
        return AttachPlan(
            pdf=pdf, pdf_bytes=pdf_bytes, company=company, draft=draft, original=original,
            existing_attachments=parsed.attachments, new_raw=new_raw, warnings=warnings,
        )

    @staticmethod
    def _read_pdf(pdf: PdfFile) -> bytes:
        try:
            data = Path(pdf.path).read_bytes()
        except FileNotFoundError as exc:
            raise LocalFileChangedError(
                f"The PDF '{pdf.name}' no longer exists. Please scan again.") from exc
        except OSError as exc:
            raise LocalFileChangedError(f"The PDF '{pdf.name}' cannot be read: {exc}") from exc
        if hashlib.sha256(data).hexdigest() != pdf.sha256:
            raise LocalFileChangedError(
                f"The PDF '{pdf.name}' was modified after it was scanned. Please scan again.")
        return data

    def _history_fields(self, plan: AttachPlan) -> dict:
        return dict(
            pdf_filename=plan.pdf.name,
            pdf_path=str(plan.pdf.path),
            sha256=plan.pdf.sha256,
            file_size=plan.pdf.size,
            file_mtime=plan.pdf.mtime,
            company_name=plan.company,
            draft_id=plan.draft.draft_id,
            draft_message_id=plan.original.message_id,
            draft_subject=plan.draft.subject,
            draft_recipients=plan.draft.recipients_display(),
        )

    # -- step 2: execute (after confirmation / automatic approval) ------
    def execute(self, plan: AttachPlan, *, dry_run: bool, send: bool = False) -> AttachResult:
        """Upload the prepared message to the existing draft and, if *send*
        is True, send the draft - but only after the attachment was
        confirmed by Gmail and verified.

        With dry_run=True nothing is changed or sent in Gmail; the result of
        the full preparation (including the local preservation check) is
        recorded."""
        if dry_run:
            record_id = self.db.start_operation(
                **self._history_fields(plan), status=STATUS_DRY_RUN, finished=True,
                error_message="Dry run - Gmail was not modified.",
            )
            action = "attached and SENT" if send else "attached"
            return AttachResult(
                STATUS_DRY_RUN,
                f"Dry run complete: '{plan.pdf.name}' WOULD be {action} to the draft "
                f"'{plan.draft.subject or '(no subject)'}' ({plan.draft.recipients_display()}). "
                f"All safety checks passed (original body and {len(plan.existing_attachments)} "
                "existing attachment(s) preserved). Gmail was NOT modified and nothing was sent.",
                record_id,
            )
        result = self._attach(plan)
        if send and result.success:
            result = self._send(plan, result)
        return result

    def _send(self, plan: AttachPlan, attached: AttachResult) -> AttachResult:
        """Send the draft after a verified attachment.  Never retried."""
        record_id = attached.record_id
        recipients = plan.draft.recipients_display()

        def done(status: str, message: str, message_id: str | None = None, note: str | None = None) -> AttachResult:
            self.db.set_send_result(record_id, status, message_id=message_id, note=note or message)
            return AttachResult(STATUS_SUCCESS, message, record_id, attached.new_message_id, status)

        if not plan.draft.to:
            return done(SEND_FAILED, "The PDF was attached, but the draft has no recipient, so it was "
                                     "NOT sent. Add the recipient in Gmail and send it yourself.")
        if self.db.was_sent(plan.pdf.sha256):
            return done(SEND_FAILED, "The PDF was attached, but an e-mail with this exact PDF was already "
                                     "sent before, so it was NOT sent again.")
        try:
            response = self.gmail.send_draft(plan.draft.draft_id)
        except GmailNetworkError as exc:
            return done(SEND_UNKNOWN, f"The PDF was attached, but the connection broke while sending "
                                      f"({exc}). Check your Gmail 'Sent' folder - the app does NOT retry, "
                                      "to avoid sending the application twice.")
        except GmailApiError as exc:
            if exc.status is not None and exc.status >= 500:
                return done(SEND_UNKNOWN, f"The PDF was attached, but Gmail reported a server error while "
                                          f"sending ({exc}). Check your 'Sent' folder - not retried.")
            return done(SEND_FAILED, f"The PDF was attached, but Gmail refused to send it ({exc}). "
                                     "The draft with the PDF is still in Gmail - send it yourself.")
        except GmailError as exc:
            return done(SEND_FAILED, f"The PDF was attached, but sending failed ({exc}). "
                                     "The draft with the PDF is still in Gmail - send it yourself.")
        except Exception as exc:  # unexpected: we cannot know whether it was sent
            log.exception("Unexpected error while sending draft")
            return done(SEND_UNKNOWN, f"The PDF was attached, but an unexpected error occurred while "
                                      f"sending ({exc}). Check your 'Sent' folder - not retried.")
        message_id = response.get("id")
        return done(SEND_SENT, f"Attached '{plan.pdf.name}' and SENT the e-mail "
                               f"'{plan.draft.subject or '(no subject)'}' to {recipients}.",
                    message_id=message_id, note="E-mail sent.")

    def _attach(self, plan: AttachPlan) -> AttachResult:
        """Attach the PDF to the draft (no sending)."""

        # Pre-flight checks immediately before writing.
        try:
            self._read_pdf(plan.pdf)
            current_id = self.gmail.get_draft_message_id(plan.draft.draft_id)
            if current_id != plan.original.message_id:
                raise DraftChangedError(
                    "The draft was changed in Gmail after the preview. Nothing was changed - "
                    "please scan again.")
        except (AttachError, GmailError) as exc:
            record_id = self.db.start_operation(
                **self._history_fields(plan), status=STATUS_FAILED, finished=True,
                error_message=f"Not attempted: {exc}",
            )
            return AttachResult(STATUS_FAILED, str(exc), record_id)

        # Write-ahead record: if the app is interrupted from here on, the
        # record stays "in_progress" and is checked at the next start.
        record_id = self.db.start_operation(**self._history_fields(plan))

        outcome_known = True
        update_error: Exception | None = None
        new_message_id: str | None = None
        try:
            response = self.gmail.update_draft_raw(
                plan.draft.draft_id, plan.original.thread_id, plan.new_raw)
            new_message_id = (response.get("message") or {}).get("id")
        except (GmailAuthError, DraftNotFoundError, ForbiddenOperationError) as exc:
            update_error = exc  # request rejected/not sent: draft unchanged
        except GmailApiError as exc:
            update_error = exc
            # 4xx = rejected (unchanged).  5xx = server problem: unknown.
            outcome_known = exc.status is not None and 400 <= exc.status < 500
        except GmailNetworkError as exc:
            update_error = exc
            outcome_known = False
        except Exception as exc:  # unexpected - treat the outcome as unknown
            log.exception("Unexpected error while updating draft")
            update_error = exc
            outcome_known = False

        if update_error is not None and outcome_known:
            message = f"Gmail did not update the draft: {update_error}"
            self.db.finish_operation(record_id, STATUS_FAILED, error_message=message)
            return AttachResult(STATUS_FAILED, message + " Nothing was changed.", record_id)

        # Verify by reading the draft back from Gmail.
        verified, details = self.verify_draft(plan.draft.draft_id, plan)
        if update_error is None:
            if verified is True:
                self.db.finish_operation(record_id, STATUS_SUCCESS, new_message_id=new_message_id,
                                         error_message="Verified in Gmail.")
                return AttachResult(
                    STATUS_SUCCESS,
                    f"Attached '{plan.pdf.name}' to the draft "
                    f"'{plan.draft.subject or '(no subject)'}'. The e-mail was NOT sent - "
                    "it is still a draft in Gmail.",
                    record_id, new_message_id,
                )
            message = (f"Gmail confirmed the update, but the follow-up check was not conclusive "
                       f"({details}). Please open the draft in Gmail and check it.")
            self.db.finish_operation(record_id, STATUS_UNVERIFIED, error_message=message,
                                     new_message_id=new_message_id)
            return AttachResult(STATUS_UNVERIFIED, message, record_id, new_message_id)

        # The update outcome was unknown (timeout, server error).
        if verified is True:
            self.db.finish_operation(record_id, STATUS_SUCCESS,
                                     error_message=f"Verified in Gmail after: {update_error}")
            return AttachResult(
                STATUS_SUCCESS,
                f"Attached '{plan.pdf.name}' (confirmed by reading the draft back after a "
                "connection problem). The e-mail was NOT sent.",
                record_id,
            )
        if verified is False:
            message = f"The draft update did not go through ({update_error}). Nothing was changed."
            self.db.finish_operation(record_id, STATUS_FAILED, error_message=message)
            return AttachResult(STATUS_FAILED, message, record_id)
        message = (f"The outcome is unknown ({update_error}; {details}). Open the draft in Gmail "
                   "to check whether the PDF is attached, then resolve the entry in the History tab.")
        self.db.finish_operation(record_id, STATUS_UNVERIFIED, error_message=message)
        return AttachResult(STATUS_UNVERIFIED, message, record_id)

    def verify_draft(self, draft_id: str, plan: AttachPlan) -> tuple[bool | None, str]:
        """Read the draft back.  Returns (True, details) if the PDF is attached
        and the original attachments/subject/recipients are intact, (False,
        details) if the PDF is NOT attached, (None, details) if unknown."""
        try:
            info = self.gmail.get_draft_info(draft_id)
        except GmailError as exc:
            return None, f"could not read the draft back: {exc}"
        has_pdf = any(a.filename == plan.pdf.name and a.size == plan.pdf.size for a in info.attachments)
        if not has_pdf:
            return False, "the new PDF is not present in the draft"
        problems = []
        before = Counter((a.filename, a.size) for a in plan.existing_attachments)
        after = Counter((a.filename, a.size) for a in info.attachments)
        missing = before - after
        if missing:
            problems.append("existing attachment(s) missing: " + ", ".join(n for n, _ in missing))
        if info.subject != plan.draft.subject:
            problems.append("subject differs")
        if [a.lower() for _, a in info.to] != [a.lower() for _, a in plan.draft.to]:
            problems.append("recipients differ")
        if problems:
            return None, "; ".join(problems)
        return True, "PDF present, existing attachments, subject and recipients intact"

    # -- duplicates already in Gmail ----------------------------------------
    def mark_already_attached(self, pdf: PdfFile, company: str, draft: DraftInfo) -> int:
        """Record that the PDF was found already attached (no Gmail change)."""
        return self.db.start_operation(
            pdf_filename=pdf.name, pdf_path=str(pdf.path), sha256=pdf.sha256,
            file_size=pdf.size, file_mtime=pdf.mtime, company_name=company,
            draft_id=draft.draft_id, draft_message_id=draft.message_id,
            draft_subject=draft.subject, draft_recipients=draft.recipients_display(),
            status=STATUS_ALREADY_ATTACHED, finished=True,
            error_message="PDF was already attached in Gmail - no change made.",
        )

    # -- recovery of interrupted operations ---------------------------------
    def recover_interrupted(self) -> list[str]:
        """Check records left 'in_progress'/'unverified' by an interruption
        and resolve them by reading the draft.  Returns messages for the user."""
        messages = []
        for record in self.db.list_incomplete():
            label = f"'{record.pdf_filename}' (history #{record.id})"
            if not record.draft_id:
                messages.append(f"{label}: no draft recorded - please resolve manually.")
                continue
            try:
                info = self.gmail.get_draft_info(record.draft_id)
            except DraftNotFoundError:
                self.db.finish_operation(
                    record.id, STATUS_UNVERIFIED,
                    error_message="The draft no longer exists (sent or deleted?). Check your Sent "
                                  "folder, then resolve this entry manually.")
                messages.append(f"{label}: the draft no longer exists - please check manually.")
                continue
            except GmailError as exc:
                messages.append(f"{label}: could not be checked ({exc}).")
                break  # network/auth problem - try again next time
            present = any(
                a.filename == record.pdf_filename and a.size == record.file_size
                for a in info.attachments
            )
            if present:
                self.db.finish_operation(
                    record.id, STATUS_SUCCESS,
                    error_message="Verified after an interruption: the PDF is in the draft.")
                messages.append(f"{label}: verified - the PDF is attached.")
            else:
                self.db.finish_operation(
                    record.id, STATUS_FAILED,
                    error_message="Interrupted operation did not complete: the PDF is not in the draft.")
                messages.append(f"{label}: the PDF is not in the draft - it can be processed again.")
        return messages
