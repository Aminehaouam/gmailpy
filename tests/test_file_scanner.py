"""Folder scanning: newest unprocessed PDF, temp/incomplete files, duplicates by hash."""

import os
import shutil
import time

import pytest

from database import STATUS_DRY_RUN, STATUS_FAILED, STATUS_SUCCESS, STATUS_UNVERIFIED
from file_scanner import FileScanner, ScanError, sha256_of_file
from mime_samples import make_pdf_bytes


@pytest.fixture
def scanner(db, config):
    return FileScanner(db, config.scanner)


def record(db, path, status, sha=None):
    rid = db.start_operation(
        pdf_filename=path.name, pdf_path=str(path), sha256=sha or sha256_of_file(path),
        file_size=path.stat().st_size, file_mtime=path.stat().st_mtime, company_name="X",
        draft_id="r-1", draft_message_id="m-1", draft_subject="S", draft_recipients="To: a@b.de")
    db.finish_operation(rid, status)
    return rid


def test_selects_newest_pdf(scanner, folder, make_pdf):
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Alt.pdf", age=600)
    newest = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Neu.pdf", age=60)
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Mittel.pdf", age=300)
    result = scanner.scan(folder)
    assert result.selected is not None
    assert result.selected.path == newest
    assert result.selected.sha256 == sha256_of_file(newest)
    assert result.total_pdfs == 3


def test_one_pdf_per_scan_newest_first(scanner, db, folder, make_pdf):
    """Each scan yields exactly one PDF; the next one only after it was processed."""
    files = [make_pdf(f"Amine_Haouam_Ausbildung_Elektroniker_Firma{i}.pdf", age=100 + i * 10)
             for i in range(4)]
    for expected in files:  # files[0] is the newest
        first = scanner.scan(folder)
        again = scanner.scan(folder)
        assert first.selected.path == expected
        assert again.selected.path == expected  # scanning alone never advances
        record(db, expected, STATUS_SUCCESS)
    assert scanner.scan(folder).selected is None


def test_ignores_non_pdf_files(scanner, folder, make_pdf):
    (folder / "notes.txt").write_text("hello")
    (folder / "Anschreiben.docx").write_bytes(b"PK\x03\x04")
    (folder / "picture.png").write_bytes(b"\x89PNG")
    pdf = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_A.pdf", age=1000)
    for name in ("notes.txt", "Anschreiben.docx", "picture.png"):
        os.utime(folder / name, None)  # newer than the PDF
    assert scanner.scan(folder).selected.path == pdf


def test_ignores_temporary_and_hidden_files(scanner, folder, make_pdf):
    good = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_A.pdf", age=900)
    make_pdf("~$temp.pdf", age=10)
    make_pdf(".~lock.Amine.pdf#.pdf", age=10)
    make_pdf(".hidden.pdf", age=10)
    make_pdf("~WRL0001.pdf", age=10)
    (folder / "download.pdf.crdownload").write_bytes(make_pdf_bytes("x"))
    (folder / "download.pdf.part").write_bytes(make_pdf_bytes("y"))
    result = scanner.scan(folder)
    assert result.selected.path == good
    skipped_names = {s.name for s in result.skipped}
    assert {"~$temp.pdf", ".hidden.pdf"} <= skipped_names


def test_ignores_empty_files(scanner, folder, make_pdf):
    good = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_A.pdf", age=900)
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Empty.pdf", age=10, content=b"")
    assert scanner.scan(folder).selected.path == good


def test_ignores_files_still_being_written(scanner, folder, make_pdf):
    older = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_A.pdf", age=900)
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_JustNow.pdf", age=0.5)
    result = scanner.scan(folder)
    assert result.selected.path == older
    assert any("may still be being written" in s.reason for s in result.skipped)


def test_ignores_incomplete_pdf_without_eof_marker(scanner, folder, make_pdf):
    older = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_A.pdf", age=900)
    truncated = make_pdf_bytes("truncated")[:-20]
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Truncated.pdf", age=60, content=truncated)
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_NotPdf.pdf", age=50, content=b"hello world" * 100)
    result = scanner.scan(folder)
    assert result.selected.path == older
    reasons = " ".join(s.reason for s in result.skipped)
    assert "end marker missing" in reasons
    assert "no PDF header" in reasons


def test_detects_file_changing_during_check(db, config, folder, make_pdf):
    path = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Growing.pdf", age=60)
    older = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_A.pdf", age=900)

    def sleep_and_append(_seconds):
        with open(path, "ab") as fh:  # another program keeps writing
            fh.write(b"more data")

    scanner = FileScanner(db, config.scanner, sleep=sleep_and_append)
    scanner.stability_delay = 0.01
    result = scanner.scan(folder)
    assert result.selected.path == older
    assert any("still being written" in s.reason for s in result.skipped)


def test_skips_processed_and_picks_next_newest(scanner, db, folder, make_pdf):
    older = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Older.pdf", age=900)
    newest = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Newest.pdf", age=60)
    record(db, newest, STATUS_SUCCESS)
    result = scanner.scan(folder)
    assert result.selected.path == older
    assert result.processed_count == 1


def test_renamed_or_moved_processed_pdf_is_recognised_by_hash(scanner, db, folder, make_pdf, tmp_path):
    original = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=600)
    record(db, original, STATUS_SUCCESS)
    # Move it elsewhere, then copy it back under a different name.
    archive = tmp_path / "archive"
    archive.mkdir()
    moved = archive / original.name
    shutil.move(str(original), moved)
    renamed = folder / "Trompetter_final_umbenannt.pdf"
    shutil.copy(moved, renamed)
    stamp = time.time() - 60
    os.utime(renamed, (stamp, stamp))
    result = scanner.scan(folder)
    assert result.selected is None
    assert result.processed_count == 1
    assert "already processed" in result.skipped[0].reason
    assert "Trompetter.pdf" in result.skipped[0].reason  # mentions the original name


def test_unverified_and_in_progress_records_block_reselection(scanner, db, folder, make_pdf):
    a = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_A.pdf", age=60)
    record(db, a, STATUS_UNVERIFIED)
    b = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_B.pdf", age=120)
    db.start_operation(pdf_filename=b.name, pdf_path=str(b), sha256=sha256_of_file(b), file_size=1,
                       file_mtime=0, company_name="B", draft_id="r", draft_message_id="m",
                       draft_subject="s", draft_recipients="r")  # stays in_progress
    assert scanner.scan(folder).selected is None


def test_failed_and_dry_run_records_do_not_block(scanner, db, folder, make_pdf):
    a = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_A.pdf", age=60)
    record(db, a, STATUS_DRY_RUN)
    record(db, a, STATUS_FAILED)
    result = scanner.scan(folder)
    assert result.selected.path == a
    assert result.previous_failure is not None


def test_all_processed_message(scanner, db, folder, make_pdf):
    a = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_A.pdf", age=60)
    record(db, a, STATUS_SUCCESS)
    result = scanner.scan(folder)
    assert result.selected is None
    assert "already been processed" in result.message


def test_missing_folder_raises(scanner, tmp_path):
    with pytest.raises(ScanError):
        scanner.scan(tmp_path / "does-not-exist")


def test_subfolders_are_not_scanned(scanner, folder, make_pdf):
    sub = folder / "Archiv"
    sub.mkdir()
    (sub / "Amine_Haouam_Ausbildung_Elektroniker_Archiv.pdf").write_bytes(make_pdf_bytes("a"))
    assert scanner.scan(folder).selected is None


def test_scanner_never_modifies_files(scanner, folder, make_pdf):
    path = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_A.pdf", age=60)
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    scanner.scan(folder)
    scanner.list_candidates(folder)
    scanner.check_single(path)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before


def test_check_single_rejects_processed_file(scanner, db, folder, make_pdf):
    path = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_A.pdf", age=60)
    record(db, path, STATUS_SUCCESS)
    with pytest.raises(ScanError, match="already processed"):
        scanner.check_single(path)
