"""SQLite processing history."""

import sqlite3

import pytest

from database import (
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_IN_PROGRESS,
    STATUS_SUCCESS,
    STATUS_UNVERIFIED,
    Database,
    DatabaseError,
)


def start(db, sha="a" * 64, name="Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", **kw):
    fields = dict(pdf_filename=name, pdf_path=f"C:/Users/x/Desktop/Bewerbungen/{name}", sha256=sha,
                  file_size=1234, file_mtime=1700000000.5, company_name="Trompetter", draft_id="r-1",
                  draft_message_id="m-1", draft_subject="Bewerbung", draft_recipients="To: a@trompetter.de")
    fields.update(kw)
    return db.start_operation(**fields)


def test_all_required_fields_are_stored(db):
    rid = start(db)
    db.finish_operation(rid, STATUS_SUCCESS, new_message_id="m-2")
    record = db.get(rid)
    assert record.pdf_filename == "Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf"
    assert record.pdf_path.endswith("Trompetter.pdf")
    assert record.sha256 == "a" * 64
    assert record.company_name == "Trompetter"
    assert record.draft_id == "r-1"
    assert record.status == STATUS_SUCCESS
    assert record.started_at and record.finished_at
    assert record.new_message_id == "m-2"


def test_write_ahead_record_starts_in_progress(db):
    rid = start(db)
    assert db.get(rid).status == STATUS_IN_PROGRESS
    assert db.get(rid).finished_at is None
    assert [r.id for r in db.list_incomplete()] == [rid]


def test_error_information_is_stored(db):
    rid = start(db)
    db.finish_operation(rid, STATUS_FAILED, error_message="HTTP 400 invalid")
    assert db.get(rid).error_message == "HTTP 400 invalid"


def test_blocking_lookup_by_hash(db):
    assert db.find_blocking_record("a" * 64) is None
    rid = start(db)
    db.finish_operation(rid, STATUS_FAILED)
    assert db.find_blocking_record("a" * 64) is None
    rid2 = start(db, status=STATUS_DRY_RUN, finished=True)
    assert db.find_blocking_record("a" * 64) is None
    rid3 = start(db)
    db.finish_operation(rid3, STATUS_SUCCESS)
    assert db.find_blocking_record("a" * 64).id == rid3
    assert db.latest_record_for_hash("a" * 64).id == rid3
    assert rid2


def test_invalid_status_is_rejected(db):
    with pytest.raises(ValueError):
        start(db, status="sent")
    rid = start(db)
    with pytest.raises(ValueError):
        db.finish_operation(rid, "sent")
    with pytest.raises(DatabaseError):
        db.finish_operation(9999, STATUS_SUCCESS)


def test_schema_enforces_status_values(db):
    conn = sqlite3.connect(db.path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO processing_history (pdf_filename, pdf_path, sha256, file_size, status, "
                     "started_at) VALUES ('a', 'b', 'c', 1, 'sent', 'now')")
    conn.close()


def test_history_listing_and_dry_run_filter(db):
    a = start(db, sha="1" * 64)
    db.finish_operation(a, STATUS_SUCCESS)
    start(db, sha="2" * 64, status=STATUS_DRY_RUN, finished=True)
    assert len(db.list_history()) == 2
    assert [r.id for r in db.list_history(include_dry_runs=False)] == [a]


def test_manual_resolution(db):
    rid = start(db)
    db.finish_operation(rid, STATUS_UNVERIFIED, error_message="timeout")
    db.resolve_manually(rid, STATUS_SUCCESS, "Checked by user.")
    record = db.get(rid)
    assert record.status == STATUS_SUCCESS
    assert "Checked by user." in record.error_message
    with pytest.raises(ValueError):
        db.resolve_manually(rid, STATUS_DRY_RUN, "x")


def test_database_survives_reopen(paths):
    db1 = Database(paths.database_file)
    rid = start(db1)
    db2 = Database(paths.database_file)
    assert db2.get(rid) is not None
