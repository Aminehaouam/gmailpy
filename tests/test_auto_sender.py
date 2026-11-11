"""Fully automatic mode: latest PDF -> draft -> attach and send."""

import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import mime_samples as ms
from auto_sender import DRY_RUN, IDLE, PROBLEM, SENT, WAITING, AutoSender, file_time
from fakes import http_error

TROMPETTER = "Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf"


@pytest.fixture
def sender(gmail, db, config):
    return AutoSender(gmail, db, config.data, sleep=lambda _s: None)


def draft(backend, subject, to, body="Sehr geehrte Damen und Herren, anbei meine Bewerbung.", age_s=3600):
    raw = ms.alternative_draft(subject, to, body_plain=body)
    return backend.add_draft(raw, internal_date_ms=int((time.time() - age_s) * 1000))


def test_sends_latest_pdf_to_its_draft(sender, backend, folder, make_pdf, db):
    target = draft(backend, "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de")
    draft(backend, "Bewerbung Siemens", "jobs@siemens.com", age_s=60 * 90)
    make_pdf(TROMPETTER, age=30)
    result = sender.run_once(folder, dry_run=False)
    assert result.kind == SENT, result.message
    assert [s["draft_id"] for s in backend.sent] == [target]
    assert db.list_history()[0].send_status == "sent"
    # Next run: the latest PDF is already processed -> nothing happens.
    assert sender.run_once(folder, dry_run=False).kind == IDLE
    assert len(backend.sent) == 1


def test_only_the_latest_pdf_is_considered(sender, backend, folder, make_pdf):
    draft(backend, "Bewerbung Barth und Kerner", "info@barth-kerner.de")
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Barth_und_Kerner_GmbH.pdf", age=600)
    make_pdf(TROMPETTER, age=30)  # newest, but no draft yet
    result = sender.run_once(folder, dry_run=False)
    assert result.kind == WAITING and result.pdf_name == TROMPETTER
    assert backend.sent == []     # the older PDF is not touched


def test_latest_draft_is_used_when_company_only_in_text(sender, backend, folder, make_pdf):
    """Company not in subject/recipient, but the latest draft mentions it."""
    older = draft(backend, "Bewerbung", "jobs@siemens.com", age_s=7200)
    latest = draft(backend, "Bewerbung um einen Ausbildungsplatz", "info@tb-elektro.de",
                   body="Sehr geehrte Damen und Herren der Trompetter GmbH, hiermit bewerbe ich mich.")
    make_pdf(TROMPETTER, age=30)
    result = sender.run_once(folder, dry_run=False)
    assert result.kind == SENT
    assert [s["draft_id"] for s in backend.sent] == [latest]
    assert older in backend.drafts


def test_clear_match_wins_over_latest_draft(sender, backend, folder, make_pdf):
    """Latest draft is for another company (mentions Trompetter only in passing)."""
    target = draft(backend, "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de", age_s=7200)
    draft(backend, "Bewerbung Siemens", "jobs@siemens.com",
          body="Ich habe bereits ein Praktikum bei Trompetter gemacht.", age_s=300)
    make_pdf(TROMPETTER, age=30)
    assert sender.run_once(folder, dry_run=False).kind == SENT
    assert [s["draft_id"] for s in backend.sent] == [target]


def test_latest_draft_not_mentioning_company_is_not_sent(sender, backend, folder, make_pdf):
    draft(backend, "Bewerbung Siemens", "jobs@siemens.com")
    make_pdf(TROMPETTER, age=30)
    result = sender.run_once(folder, dry_run=False)
    assert result.kind == WAITING and "no recent draft mentions" in result.message
    assert backend.sent == [] and backend.method_calls("drafts.update") == []


def test_fresh_draft_is_sent_immediately(sender, backend, folder, make_pdf):
    """No waiting: a draft saved seconds ago is sent at once."""
    target = draft(backend, "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de", age_s=2)
    make_pdf(TROMPETTER, age=30)
    result = sender.run_once(folder, dry_run=False)
    assert result.kind == SENT, result.message
    assert [s["draft_id"] for s in backend.sent] == [target]


def test_optional_settle_time(gmail, db, config, backend, folder, make_pdf):
    config.data["draft_settle_seconds"] = 60
    sender = AutoSender(gmail, db, config.data, sleep=lambda _s: None)
    target = draft(backend, "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de", age_s=5)
    make_pdf(TROMPETTER, age=30)
    result = sender.run_once(folder, dry_run=False)
    assert result.kind == WAITING and "changed" in result.message
    backend.drafts[target]["internal_date"] -= 120_000
    sender._next_try.clear()
    sender.gmail._cache.clear()
    assert sender.run_once(folder, dry_run=False).kind == SENT


@pytest.mark.parametrize("subject, to, body, reason", [
    ("Bewerbung Trompetter", None, "Sehr geehrte Damen und Herren, anbei.", "no recipient"),
    ("", "bewerbung@trompetter.de", "Sehr geehrte Damen und Herren der Trompetter GmbH", "no subject"),
    ("Bewerbung Trompetter", "bewerbung@trompetter.de", "Hi", "no text"),
])
def test_unfinished_drafts_are_not_sent(sender, backend, folder, make_pdf, subject, to, body, reason):
    raw = ms.alternative_draft(subject or "x", to or "x@y.de", body_plain=body)
    if to is None:
        raw = raw.replace(b"To: x@y.de\r\n", b"")
    if not subject:
        raw = raw.replace(b"Subject: x\r\n", b"Subject: \r\n")
    if body == "Hi":
        raw = raw.replace(b"Mit freundlichen Gr=C3=BC=C3=9Fen", b"").replace(b"Amine Haouam<", b"<")
        raw = raw.replace(b"Amine Haouam\r\n", b"\r\n")
    backend.add_draft(raw)
    make_pdf(TROMPETTER, age=30)
    result = sender.run_once(folder, dry_run=False)
    assert result.kind == WAITING and reason in result.message, result.message
    assert backend.sent == []


def test_waits_and_retries_until_draft_exists(sender, backend, folder, make_pdf, monkeypatch):
    make_pdf(TROMPETTER, age=30)
    first = sender.run_once(folder, dry_run=False)
    assert first.kind == WAITING and first.retry_in == 3
    lists = len(backend.method_calls("drafts.list"))
    assert sender.run_once(folder, dry_run=False).kind == WAITING   # not due: no Gmail call
    assert len(backend.method_calls("drafts.list")) == lists
    draft(backend, "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de")
    sender._next_try.clear()                                          # retry time reached
    assert sender.run_once(folder, dry_run=False).kind == SENT


def test_dry_run_sends_nothing_and_does_not_repeat(sender, backend, folder, make_pdf, db):
    target = draft(backend, "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de")
    original = backend.raw_of(target)
    make_pdf(TROMPETTER, age=30)
    assert sender.run_once(folder, dry_run=True).kind == DRY_RUN
    assert sender.run_once(folder, dry_run=True).kind == WAITING
    assert backend.sent == [] and backend.raw_of(target) == original
    assert len(db.list_history()) == 1


def test_unreadable_filename_waits(sender, backend, folder, make_pdf):
    draft(backend, "Bewerbung bei Bosch", "karriere@bosch.de")
    make_pdf("Lebenslauf.pdf", age=30)
    result = sender.run_once(folder, dry_run=False)
    assert result.kind == WAITING and "file name" in result.message
    assert backend.sent == []


def test_file_still_being_saved_waits(sender, backend, folder, make_pdf):
    draft(backend, "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de")
    make_pdf(TROMPETTER, age=0.1)
    assert sender.run_once(folder, dry_run=False).kind == WAITING
    assert backend.sent == []


def test_send_failure_is_reported_once(sender, backend, folder, make_pdf, db):
    draft(backend, "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de")
    backend.fail_before["drafts.send"] = [http_error(400, "invalidArgument", "Invalid To header")]
    make_pdf(TROMPETTER, age=30)
    result = sender.run_once(folder, dry_run=False)
    assert result.kind == PROBLEM and ("NOT" in result.message or "refused" in result.message)
    assert sender.run_once(folder, dry_run=False).kind == IDLE  # not retried (could send twice)
    assert db.list_history()[0].send_status == "failed"


def test_hash_is_cached(sender, backend, folder, make_pdf, monkeypatch):
    import auto_sender
    make_pdf(TROMPETTER, age=30)
    calls = []
    real = auto_sender.sha256_of_file
    monkeypatch.setattr(auto_sender, "sha256_of_file", lambda p: calls.append(p) or real(p))
    for _ in range(5):
        sender._next_try.clear()
        sender.run_once(folder, dry_run=False)
    assert len(calls) == 1


def test_copied_file_counts_as_new():
    """Windows keeps the old 'modified' time on copies; the creation time is used too."""
    now = datetime.now(timezone.utc).timestamp()
    st = SimpleNamespace(st_mtime=now - 86400 * 30, st_birthtime=now - 5, st_ctime=now - 5)
    assert file_time(st) == now - 5


def test_empty_folder_is_idle(sender, folder):
    assert sender.run_once(folder, dry_run=False).kind == IDLE
    os.makedirs(folder / "Archiv")
    assert sender.run_once(folder, dry_run=False).kind == IDLE


def test_success_chime_is_valid_wav():
    import io
    import wave
    from sound import success_chime_wav
    with wave.open(io.BytesIO(success_chime_wav())) as wav:
        assert wav.getnchannels() == 1 and wav.getsampwidth() == 2
        assert 0.4 < wav.getnframes() / wav.getframerate() < 1.0


def test_old_config_gets_new_fast_timings(paths):
    import json
    from config import Config
    paths.config_file.write_text(json.dumps({
        "dry_run": False, "auto_send": True, "draft_settle_seconds": 60.0,
        "auto_retry_seconds": 15.0, "watch_interval_seconds": 3.0,
        "scanner": {"min_file_age_seconds": 3.0, "stability_check_seconds": 1.0},
    }), encoding="utf-8")
    cfg = Config.load(paths)
    assert cfg.data["draft_settle_seconds"] == 0.0
    assert cfg.data["auto_retry_seconds"] == 3.0
    assert cfg.watch_interval_ms == 2000
    assert cfg.scanner["min_file_age_seconds"] == 1.0
    assert cfg.auto_send is True and cfg.dry_run is False      # user choices are kept
    cfg.save()
    assert Config.load(paths).data["config_version"] == 2
