"""Safe draft modification: MIME preservation, duplicates, update flow, recovery."""

import email
import email.policy
import hashlib
import os
import socket

import pytest

from attachment_manager import (
    AttachError,
    AttachmentManager,
    DraftChangedError,
    DuplicateAttachmentError,
    LocalFileChangedError,
    PreservationError,
    UnsafeDraftStructureError,
    build_message_with_attachment,
    find_duplicate,
    verify_preservation,
)
from database import (
    STATUS_ALREADY_ATTACHED,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_SUCCESS,
    STATUS_UNVERIFIED,
)
from fakes import http_error
from file_scanner import FileScanner
from gmail_client import parse_raw_message
import mime_samples as ms

PDF_NAME = "Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf"
TO = '"Personalabteilung Trompetter GmbH" <bewerbung@trompetter.de>'
SUBJECT = "=?UTF-8?Q?Bewerbung_um_einen_Ausbildungsplatz_als_Elektroniker_=E2=80=93_Trompetter?="


def parse(raw):
    return email.message_from_bytes(raw, policy=email.policy.default)


def leaves(msg):
    return [p for p in msg.walk() if not p.is_multipart()]


def assert_preserved(original_raw, new_raw, pdf_bytes, filename):
    """Independent re-implementation of the preservation guarantees."""
    old, new = parse(original_raw), parse(new_raw)
    for header in ("From", "To", "Cc", "Bcc", "Subject", "Message-ID", "In-Reply-To", "References", "Date"):
        assert str(old.get(header)) == str(new.get(header)), header
    old_leaves, new_leaves = leaves(old), leaves(new)
    assert len(new_leaves) == len(old_leaves) + 1
    for a, b in zip(old_leaves, new_leaves):
        assert a.get_content_type() == b.get_content_type()
        assert a.get_payload(decode=True) == b.get_payload(decode=True)
        assert a.get_filename() == b.get_filename()
        assert a.get("Content-ID") == b.get("Content-ID")
    added = new_leaves[-1]
    assert added.get_content_type() == "application/pdf"
    assert added.get_filename() == filename
    assert added.get_content_disposition() == "attachment"
    assert added.get_payload(decode=True) == pdf_bytes
    assert new.get_content_type() == "multipart/mixed"
    # CRLF line endings only
    assert b"\n" not in new_raw.replace(b"\r\n", b"")


# --------------------------------------------------------------------------
# Pure MIME construction
# --------------------------------------------------------------------------

SAMPLES = {
    "alternative": lambda: ms.alternative_draft(SUBJECT, TO, cc="cc@trompetter.de", bcc="me@example.com",
                                                body_plain="Sehr geehrte Damen und Herren der Trompetter GmbH,"),
    "mixed_with_attachment": lambda: ms.mixed_draft_with_attachment(SUBJECT, TO, ms.make_pdf_bytes("cv", 9000),
                                                                    "Lebenslauf.pdf"),
    "related_inline_image": lambda: ms.related_draft_with_inline_image(SUBJECT, TO, os.urandom(3000)),
    "plain_text": lambda: ms.plain_text_draft(SUBJECT, TO, "Sehr geehrte Damen und Herren,\nGrüße\nFrom here"),
    "eight_bit": lambda: ms.eight_bit_draft("Bewerbung Trompetter", TO, "Grüße aus Köln – äöüß €\nFrom me"),
    "html_only": lambda: ms.html_only_draft(SUBJECT, TO, "<p>Sehr geehrte <b>Damen</b> und Herren – ä</p>"),
    "reply": lambda: ms.alternative_draft("Re: Ihre Stellenanzeige", TO, extra_headers=[
        "In-Reply-To: <abc123@trompetter.de>", "References: <xyz@trompetter.de> <abc123@trompetter.de>"]),
}


@pytest.mark.parametrize("kind", sorted(SAMPLES))
def test_build_preserves_everything(kind):
    original = SAMPLES[kind]()
    pdf = ms.make_pdf_bytes("bewerbung", 20000)
    new = build_message_with_attachment(original, pdf, PDF_NAME)
    verify_preservation(original, new, pdf, PDF_NAME)       # module's own check
    assert_preserved(original, new, pdf, PDF_NAME)          # independent check


def test_original_body_bytes_appear_verbatim():
    original = SAMPLES["alternative"]()
    new = build_message_with_attachment(original, ms.make_pdf_bytes("p"), PDF_NAME)
    # The complete original multipart/alternative body (incl. HTML) is copied byte-for-byte.
    body = original.split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n")
    assert body in new
    # Original header lines are written back verbatim (no re-encoding).
    for line in original.split(b"\r\n\r\n", 1)[0].split(b"\r\n"):
        if not line.lower().startswith(b"content-type"):
            assert line in new, line


def test_existing_attachment_preserved_byte_for_byte():
    cv = ms.make_pdf_bytes("lebenslauf", 50000)
    original = ms.mixed_draft_with_attachment(SUBJECT, TO, cv, "Lebenslauf.pdf")
    pdf = ms.make_pdf_bytes("anschreiben")
    new = build_message_with_attachment(original, pdf, PDF_NAME)
    attachments = parse_raw_message(new).attachments
    assert [a.filename for a in attachments] == ["Lebenslauf.pdf", PDF_NAME]
    assert attachments[0].sha256 == hashlib.sha256(cv).hexdigest()
    assert attachments[1].sha256 == hashlib.sha256(pdf).hexdigest()


def test_inline_image_keeps_content_id():
    image = os.urandom(2048)
    original = ms.related_draft_with_inline_image(SUBJECT, TO, image)
    new = build_message_with_attachment(original, ms.make_pdf_bytes("x"), PDF_NAME)
    msg = parse(new)
    png = [p for p in msg.walk() if p.get_content_type() == "image/png"][0]
    assert png["Content-ID"] == "<ii_logo1>"
    assert png.get_payload(decode=True) == image
    html = [p for p in msg.walk() if p.get_content_type() == "text/html"][0]
    assert "cid:ii_logo1" in html.get_content()


@pytest.mark.parametrize("filename", [
    "Amine_Haouam_Ausbildung_Elektroniker_Müller_GmbH.pdf",
    "Amine_Haouam_Ausbildung_Elektroniker_Stadtwerke_Bochum_Netz_GmbH_und_Co_KG_Niederlassung_Ost.pdf",
])
def test_long_and_non_ascii_filenames_round_trip(filename):
    original = SAMPLES["alternative"]()
    pdf = ms.make_pdf_bytes("x")
    new = build_message_with_attachment(original, pdf, filename)
    verify_preservation(original, new, pdf, filename)
    assert parse_raw_message(new).attachments[-1].filename == filename


def test_verify_preservation_detects_tampering():
    original = SAMPLES["alternative"]()
    pdf = ms.make_pdf_bytes("x")
    new = build_message_with_attachment(original, pdf, PDF_NAME)
    with pytest.raises(PreservationError):
        verify_preservation(original, new.replace(b"Sehr geehrte", b"Sehr geehrtE"), pdf, PDF_NAME)
    with pytest.raises(PreservationError):
        verify_preservation(original, new.replace(b"bewerbung@trompetter.de", b"other@x.de"), pdf, PDF_NAME)
    with pytest.raises(PreservationError):
        verify_preservation(original, new, pdf + b"x", PDF_NAME)
    with pytest.raises(PreservationError):
        verify_preservation(original, original, pdf, PDF_NAME)  # nothing added


def test_broken_structure_is_refused():
    broken = (b"MIME-Version: 1.0\r\nTo: a@b.de\r\nSubject: x\r\n"
              b'Content-Type: multipart/mixed; boundary="abc"\r\n\r\nno boundaries here\r\n')
    with pytest.raises(UnsafeDraftStructureError):
        build_message_with_attachment(broken, ms.make_pdf_bytes("x"), PDF_NAME)


def test_find_duplicate():
    pdf = ms.make_pdf_bytes("x")
    digest = hashlib.sha256(pdf).hexdigest()
    existing = parse_raw_message(ms.mixed_draft_with_attachment(SUBJECT, TO, pdf, "renamed.pdf")).attachments
    dup = find_duplicate(existing, digest, PDF_NAME)
    assert dup is not None and dup.exact_duplicate and "renamed.pdf" in str(dup)
    other = parse_raw_message(ms.mixed_draft_with_attachment(SUBJECT, TO, b"%PDF other %%EOF", PDF_NAME)).attachments
    dup2 = find_duplicate(other, digest, PDF_NAME)
    assert dup2 is not None and not dup2.exact_duplicate
    assert find_duplicate([], digest, PDF_NAME) is None


# --------------------------------------------------------------------------
# Full attach flow with the fake Gmail backend
# --------------------------------------------------------------------------

@pytest.fixture
def pdf_file(make_pdf, db, config, folder):
    path = make_pdf(PDF_NAME, age=60)
    return FileScanner(db, config.scanner).check_single(path)


@pytest.fixture
def manager(gmail, db):
    return AttachmentManager(gmail, db)


def add_target_draft(backend, raw=None):
    raw = raw or ms.alternative_draft(SUBJECT, TO, bcc="me@example.com",
                                      body_plain="Sehr geehrte Damen und Herren der Trompetter GmbH,")
    return backend.add_draft(raw, thread_id="thread-42"), raw


def test_successful_attach_updates_existing_draft_only(backend, gmail, manager, db, pdf_file):
    other_raw = ms.alternative_draft("Bewerbung Siemens", "jobs@siemens.com")
    other_id = backend.add_draft(other_raw)
    draft_id, original = add_target_draft(backend)
    drafts_before = set(backend.drafts)
    info = gmail.get_draft_info(draft_id)
    old_message_id = info.message_id

    plan = manager.prepare(pdf_file, "Trompetter", info)
    result = manager.execute(plan, dry_run=False)

    assert result.success, result.message
    assert set(backend.drafts) == drafts_before                    # no draft created or deleted
    assert backend.raw_of(other_id) == other_raw                   # other drafts untouched
    new_raw = backend.raw_of(draft_id)
    assert_preserved(original, new_raw, pdf_file.path.read_bytes(), PDF_NAME)
    assert backend.drafts[draft_id]["message_id"] != old_message_id  # Gmail semantics
    update = backend.method_calls("drafts.update")[0]
    assert update["id"] == draft_id
    assert update["body"]["message"]["threadId"] == "thread-42"
    assert update["media_body"].mimetype() == "message/rfc822"
    assert backend.forbidden == []                                 # never send/create/delete
    record = db.get(result.record_id)
    assert record.status == STATUS_SUCCESS
    assert record.sha256 == pdf_file.sha256
    assert record.draft_id == draft_id
    assert record.company_name == "Trompetter"
    assert record.new_message_id == backend.drafts[draft_id]["message_id"]


def test_dry_run_never_modifies_gmail(backend, gmail, manager, db, pdf_file):
    draft_id, original = add_target_draft(backend)
    plan = manager.prepare(pdf_file, "Trompetter", gmail.get_draft_info(draft_id))
    result = manager.execute(plan, dry_run=True)
    assert result.status == STATUS_DRY_RUN
    assert backend.raw_of(draft_id) == original
    assert backend.method_calls("drafts.update") == []
    assert db.get(result.record_id).status == STATUS_DRY_RUN
    assert db.find_blocking_record(pdf_file.sha256) is None  # dry run does not count as processed


def test_attach_to_draft_with_existing_attachments(backend, gmail, manager, pdf_file):
    cv = ms.make_pdf_bytes("lebenslauf", 30000)
    raw = ms.mixed_draft_with_attachment(SUBJECT, TO, cv, "Lebenslauf.pdf")
    draft_id = backend.add_draft(raw)
    result = manager.execute(manager.prepare(pdf_file, "Trompetter", gmail.get_draft_info(draft_id)), dry_run=False)
    assert result.success
    names = [a.filename for a in gmail.get_draft_info(draft_id).attachments]
    assert names == ["Lebenslauf.pdf", PDF_NAME]
    assert_preserved(raw, backend.raw_of(draft_id), pdf_file.path.read_bytes(), PDF_NAME)


def test_exact_pdf_already_attached_is_detected(backend, gmail, manager, pdf_file):
    raw = ms.mixed_draft_with_attachment(SUBJECT, TO, pdf_file.path.read_bytes(), "something_else.pdf")
    draft_id = backend.add_draft(raw)
    with pytest.raises(DuplicateAttachmentError) as exc:
        manager.prepare(pdf_file, "Trompetter", gmail.get_draft_info(draft_id))
    assert exc.value.exact_duplicate
    assert backend.raw_of(draft_id) == raw


def test_same_filename_different_content_is_blocked(backend, gmail, manager, pdf_file):
    raw = ms.mixed_draft_with_attachment(SUBJECT, TO, ms.make_pdf_bytes("old version"), PDF_NAME)
    draft_id = backend.add_draft(raw)
    with pytest.raises(DuplicateAttachmentError) as exc:
        manager.prepare(pdf_file, "Trompetter", gmail.get_draft_info(draft_id))
    assert not exc.value.exact_duplicate
    assert backend.method_calls("drafts.update") == []


def test_mark_already_attached_blocks_future_scans(backend, gmail, manager, db, config, folder, pdf_file):
    draft_id, _ = add_target_draft(backend)
    manager.mark_already_attached(pdf_file, "Trompetter", gmail.get_draft_info(draft_id))
    assert db.find_blocking_record(pdf_file.sha256).status == STATUS_ALREADY_ATTACHED
    assert FileScanner(db, config.scanner).scan(folder).selected is None


def test_draft_changed_after_preview_is_refused(backend, gmail, manager, pdf_file):
    draft_id, original = add_target_draft(backend)
    info = gmail.get_draft_info(draft_id)
    edited = original.replace(b"Sehr geehrte", b"Liebe")
    backend.edit_draft_externally(draft_id, edited)
    with pytest.raises(DraftChangedError):
        manager.prepare(pdf_file, "Trompetter", info)
    assert backend.raw_of(draft_id) == edited


def test_draft_changed_between_confirmation_and_upload(backend, gmail, manager, db, pdf_file):
    draft_id, original = add_target_draft(backend)
    plan = manager.prepare(pdf_file, "Trompetter", gmail.get_draft_info(draft_id))
    edited = original.replace(b"Sehr geehrte", b"Liebe")
    backend.edit_draft_externally(draft_id, edited)
    result = manager.execute(plan, dry_run=False)
    assert result.status == STATUS_FAILED
    assert backend.raw_of(draft_id) == edited          # the user's edit was not overwritten
    assert backend.method_calls("drafts.update") == []
    assert db.find_blocking_record(pdf_file.sha256) is None


def test_deleted_draft(backend, gmail, manager, pdf_file):
    draft_id, _ = add_target_draft(backend)
    info = gmail.get_draft_info(draft_id)
    del backend.drafts[draft_id]
    backend.order.remove(draft_id)
    with pytest.raises(AttachError, match="no longer exists"):
        manager.prepare(pdf_file, "Trompetter", info)


def test_local_pdf_modified_or_missing(backend, gmail, manager, pdf_file):
    draft_id, _ = add_target_draft(backend)
    info = gmail.get_draft_info(draft_id)
    with open(pdf_file.path, "ab") as fh:
        fh.write(b"changed")
    with pytest.raises(LocalFileChangedError):
        manager.prepare(pdf_file, "Trompetter", info)
    pdf_file.path.unlink()
    with pytest.raises(LocalFileChangedError):
        manager.prepare(pdf_file, "Trompetter", info)


def test_update_rejected_by_gmail_marks_failed(backend, gmail, manager, db, pdf_file):
    draft_id, original = add_target_draft(backend)
    plan = manager.prepare(pdf_file, "Trompetter", gmail.get_draft_info(draft_id))
    backend.fail_before["drafts.update"] = [http_error(400, "invalidArgument", "Invalid draft")]
    result = manager.execute(plan, dry_run=False)
    assert result.status == STATUS_FAILED
    assert backend.raw_of(draft_id) == original
    assert db.get(result.record_id).status == STATUS_FAILED
    assert db.find_blocking_record(pdf_file.sha256) is None    # can be retried


def test_server_error_not_applied_is_verified_as_failed(backend, gmail, manager, db, pdf_file):
    draft_id, original = add_target_draft(backend)
    plan = manager.prepare(pdf_file, "Trompetter", gmail.get_draft_info(draft_id))
    backend.fail_before["drafts.update"] = [http_error(503, "backendError", "Backend Error")]
    result = manager.execute(plan, dry_run=False)
    assert result.status == STATUS_FAILED
    assert backend.raw_of(draft_id) == original


def test_timeout_after_update_applied_is_verified_as_success(backend, gmail, manager, db, pdf_file):
    draft_id, original = add_target_draft(backend)
    plan = manager.prepare(pdf_file, "Trompetter", gmail.get_draft_info(draft_id))
    backend.fail_after["drafts.update"] = [socket.timeout("timed out")]
    result = manager.execute(plan, dry_run=False)
    assert result.status == STATUS_SUCCESS
    assert_preserved(original, backend.raw_of(draft_id), pdf_file.path.read_bytes(), PDF_NAME)


def test_unknown_outcome_without_verification_is_unverified(backend, gmail, manager, db, config, folder, pdf_file):
    draft_id, _ = add_target_draft(backend)
    plan = manager.prepare(pdf_file, "Trompetter", gmail.get_draft_info(draft_id))
    backend.fail_after["drafts.update"] = [socket.timeout("timed out")]
    # The pre-flight check also uses drafts.get, so the network only "breaks"
    # after the update was applied - the read-back verification then fails.
    original_update = backend.update

    def update_then_break_network(*args, **kwargs):
        response = original_update(*args, **kwargs)
        backend.fail_before["drafts.get"] = [ConnectionResetError("reset")]
        return response

    backend.update = update_then_break_network
    result = manager.execute(plan, dry_run=False)
    assert result.status == STATUS_UNVERIFIED
    assert db.get(result.record_id).status == STATUS_UNVERIFIED
    # unverified PDFs are not picked again automatically
    assert FileScanner(db, config.scanner).scan(folder).selected is None


def test_attachment_size_limit(backend, gmail, manager, monkeypatch, pdf_file):
    import attachment_manager
    monkeypatch.setattr(attachment_manager, "GMAIL_ATTACHMENT_LIMIT", 1000)
    draft_id, _ = add_target_draft(backend)
    with pytest.raises(AttachError, match="limit"):
        manager.prepare(pdf_file, "Trompetter", gmail.get_draft_info(draft_id))


# --------------------------------------------------------------------------
# Interrupted operations
# --------------------------------------------------------------------------

def _in_progress_record(db, pdf_file, draft_id):
    return db.start_operation(
        pdf_filename=pdf_file.name, pdf_path=str(pdf_file.path), sha256=pdf_file.sha256,
        file_size=pdf_file.size, file_mtime=pdf_file.mtime, company_name="Trompetter",
        draft_id=draft_id, draft_message_id="m-old", draft_subject="s", draft_recipients="r")


def test_recovery_marks_completed_interrupted_operation_as_success(backend, gmail, manager, db, pdf_file):
    pdf = pdf_file.path.read_bytes()
    raw = build_message_with_attachment(ms.alternative_draft(SUBJECT, TO), pdf, PDF_NAME)
    draft_id = backend.add_draft(raw)
    rid = _in_progress_record(db, pdf_file, draft_id)
    messages = manager.recover_interrupted()
    assert db.get(rid).status == STATUS_SUCCESS
    assert "verified" in messages[0]


def test_recovery_marks_incomplete_interrupted_operation_as_failed(backend, manager, db, pdf_file):
    draft_id, _ = add_target_draft(backend)
    rid = _in_progress_record(db, pdf_file, draft_id)
    manager.recover_interrupted()
    assert db.get(rid).status == STATUS_FAILED
    assert db.find_blocking_record(pdf_file.sha256) is None


def test_recovery_with_missing_draft_stays_unverified(manager, db, pdf_file):
    rid = _in_progress_record(db, pdf_file, "r-gone")
    messages = manager.recover_interrupted()
    assert db.get(rid).status == STATUS_UNVERIFIED
    assert "no longer exists" in messages[0]


def test_recovery_network_problem_leaves_records_untouched(backend, manager, db, pdf_file):
    draft_id, _ = add_target_draft(backend)
    rid = _in_progress_record(db, pdf_file, draft_id)
    backend.fail_before["drafts.get"] = [ConnectionResetError("offline")]
    manager.recover_interrupted()
    assert db.get(rid).status == STATUS_IN_PROGRESS
