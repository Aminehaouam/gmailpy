"""Fully automatic mode: latest PDF -> draft -> attach and send."""

import os
import time
from pathlib import Path
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
    assert first.kind == WAITING and first.retry_in == 1.0
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
    """Configs of older versions get the current (faster) timings; the
    user's own choices stay."""
    import json
    from config import Config
    paths.config_file.write_text(json.dumps({
        "dry_run": False, "auto_send": True, "draft_settle_seconds": 60.0,
        "auto_retry_seconds": 15.0, "watch_interval_seconds": 3.0,
        "scanner": {"min_file_age_seconds": 3.0, "stability_check_seconds": 1.0},
    }), encoding="utf-8")
    cfg = Config.load(paths)
    assert cfg.data["draft_settle_seconds"] == 0.0
    assert cfg.data["auto_retry_seconds"] == 1.0
    assert cfg.watch_interval_ms == 500
    assert cfg.scanner["min_file_age_seconds"] == 0.2 and cfg.scanner["stability_check_seconds"] == 0.1
    assert cfg.data["auto_batch"] is True
    assert cfg.auto_send is True and cfg.dry_run is False      # user choices are kept
    cfg.save()
    from config import CONFIG_VERSION
    assert Config.load(paths).data["config_version"] == CONFIG_VERSION


def test_version_2_config_gets_version_3_timings(paths):
    import json
    from config import Config
    paths.config_file.write_text(json.dumps({
        "config_version": 2, "auto_send": True, "dry_run": False, "auto_retry_seconds": 3.0,
        "watch_interval_seconds": 2.0, "scanner": {"min_file_age_seconds": 1.0, "stability_check_seconds": 0.3},
    }), encoding="utf-8")
    cfg = Config.load(paths)
    assert cfg.watch_interval_ms == 500 and cfg.data["auto_retry_seconds"] == 1.0
    assert cfg.scanner["min_file_age_seconds"] == 0.2
    assert cfg.auto_send is True and cfg.dry_run is False


# --------------------------------------------------------------------------
# Speed: as few Gmail requests as possible, the same checks
# --------------------------------------------------------------------------

def gets(backend, fmt):
    return [kw for kw in backend.method_calls("drafts.get") if kw.get("format") == fmt]


def test_prepared_ahead_send_needs_four_gmail_requests(sender, backend, folder, make_pdf):
    """Draft read ahead of time while idle: list (current versions), update,
    read back (verification) and send - nothing else."""
    target = draft(backend, "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de")
    assert sender.run_once(folder, dry_run=False).kind == IDLE        # empty folder: reads ahead
    assert len(gets(backend, "raw")) == 1
    backend.calls.clear()
    make_pdf(TROMPETTER, age=30)
    result = sender.run_once(folder, dry_run=False)
    assert result.kind == SENT, result.message
    assert [s["draft_id"] for s in backend.sent] == [target]
    assert [m for m, _ in backend.calls] == ["drafts.list", "drafts.update", "drafts.get", "drafts.send"]
    assert gets(backend, "full") and not gets(backend, "raw") and not gets(backend, "minimal")


def test_without_reading_ahead_no_extra_version_check(sender, backend, folder, make_pdf):
    draft(backend, "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de")
    make_pdf(TROMPETTER, age=30)
    assert sender.run_once(folder, dry_run=False).kind == SENT
    assert not gets(backend, "minimal")       # the draft list was read a moment before
    assert len(gets(backend, "raw")) == 1


def test_edited_draft_is_read_again(sender, backend, folder, make_pdf):
    """A copy read ahead of time is only used for exactly that version."""
    target = draft(backend, "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de")
    sender.run_once(folder, dry_run=False)                             # reads ahead
    backend.edit_draft_externally(target, ms.alternative_draft(
        "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de",
        body_plain="Sehr geehrte Frau Neu, hiermit bewerbe ich mich bei Ihnen."))
    make_pdf(TROMPETTER, age=30)
    assert sender.run_once(folder, dry_run=False).kind == SENT
    assert b"Frau Neu" in backend.sent[0]["raw"]


def test_old_confirmation_is_checked_again(gmail, db, backend, folder, make_pdf):
    from attachment_manager import AttachmentManager
    from file_scanner import FileScanner
    target = draft(backend, "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de")
    pdf = FileScanner(db, {"min_file_age_seconds": 0, "stability_check_seconds": 0}).check_single(
        make_pdf(TROMPETTER, age=30))
    manager = AttachmentManager(gmail, db)
    plan = manager.prepare(pdf, "Trompetter", gmail.get_draft_info(target))
    plan.confirmed_at = time.monotonic() - 30
    backend.calls.clear()
    assert manager.execute(plan, dry_run=False).status == "success"
    assert gets(backend, "minimal")


def test_reading_ahead_waits_between_reads_and_ignores_errors(sender, backend, folder, monkeypatch):
    draft(backend, "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de")
    sender.run_once(folder, dry_run=False)
    sender.run_once(folder, dry_run=False)
    assert len(backend.method_calls("drafts.list")) == 1              # not again within 3 s

    def broken(*_a, **_k):
        raise OSError("offline")
    monkeypatch.setattr(sender.gmail, "prefetch_recent", broken)
    sender._last_prefetch = 0
    assert sender.run_once(folder, dry_run=False).kind == IDLE


def test_completely_saved_check_runs_once_per_pdf(sender, backend, folder, make_pdf, monkeypatch):
    calls = []
    original = sender.scanner._stability_problem
    monkeypatch.setattr(sender.scanner, "_stability_problem", lambda p, st: calls.append(p) or original(p, st))
    make_pdf(TROMPETTER, age=30)
    for _ in range(3):
        sender._next_try.clear()
        assert sender.run_once(folder, dry_run=False).kind == WAITING     # no draft yet
    assert len(calls) == 1


def test_long_waiting_pdf_is_looked_at_less_often(sender, folder, make_pdf):
    make_pdf(TROMPETTER, age=30)
    assert sender.run_once(folder, dry_run=False).retry_in == 1.0
    os.utime(folder / TROMPETTER, (time.time() - 600, time.time() - 600))
    sender._next_try.clear()
    assert sender.run_once(folder, dry_run=False).retry_in == 5.0


# --------------------------------------------------------------------------
# Batch mode: every new PDF into its own draft
# --------------------------------------------------------------------------

BARTH = "Amine_Haouam_Ausbildung_Elektroniker_Barth_und_Kerner_GmbH.pdf"
SCHMIDT = "Amine_Haouam_Ausbildung_Elektroniker_Elektro_Schmidt.pdf"


@pytest.fixture
def batch_sender(sender):
    sender.since = time.time() - 3600          # watching for an hour
    return sender


def test_batch_sends_every_new_pdf_oldest_first(batch_sender, backend, folder, make_pdf):
    d_barth = draft(backend, "Bewerbung Barth und Kerner", "info@barth-kerner.de")
    d_schmidt = draft(backend, "Bewerbung bei Elektro Schmidt", "info@elektro-schmidt.de")
    d_trompetter = draft(backend, "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de")
    make_pdf(BARTH, age=50)
    make_pdf(SCHMIDT, age=40)
    make_pdf(TROMPETTER, age=30)
    results = [batch_sender.run_once(folder, dry_run=False) for _ in range(3)]
    assert [r.kind for r in results] == [SENT, SENT, SENT]
    assert [r.details["more"] for r in results] == [True, True, False]
    assert [s["draft_id"] for s in backend.sent] == [d_barth, d_schmidt, d_trompetter]
    assert batch_sender.run_once(folder, dry_run=False).kind == IDLE


def test_batch_waiting_pdf_does_not_hold_up_the_others(batch_sender, backend, folder, make_pdf):
    target = draft(backend, "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de")
    make_pdf(BARTH, age=50)                                  # no draft yet
    make_pdf(TROMPETTER, age=30)
    first = batch_sender.run_once(folder, dry_run=False)
    assert first.kind == SENT and first.pdf_name == TROMPETTER
    assert [s["draft_id"] for s in backend.sent] == [target]
    second = batch_sender.run_once(folder, dry_run=False)
    assert second.kind == WAITING and second.pdf_name == BARTH


def test_batch_latest_draft_rule_only_for_the_latest_pdf(batch_sender, backend, folder, make_pdf):
    """An older PDF needs a clear match; 'mentioned in the latest draft' is
    only enough for the latest PDF."""
    draft(backend, "Bewerbung um einen Ausbildungsplatz", "info@tb-elektro.de",
          body="Sehr geehrte Damen und Herren der Barth und Kerner GmbH, hiermit bewerbe ich mich.")
    make_pdf(BARTH, age=50)
    make_pdf(TROMPETTER, age=30)
    result = batch_sender.run_once(folder, dry_run=False)
    assert result.kind == WAITING and backend.sent == []
    assert "1 more PDF" in result.message


def test_latest_draft_of_another_waiting_pdf_is_not_taken(batch_sender, backend):
    schmidt = draft(backend, "Bewerbung bei Elektro Schmidt", "info@elektro-schmidt.de",
                    body="Sehr geehrte Damen und Herren, ich habe auch an Trompetter geschrieben.")
    drafts = batch_sender.gmail.recent_drafts(15)
    assert batch_sender.choose_draft("Trompetter", drafts)[0].draft_id == schmidt
    pending = [(Path(SCHMIDT), None, "sha")]
    reserved = batch_sender._reserved("Trompetter", pending, drafts)
    assert reserved == {schmidt}
    assert batch_sender.choose_draft("Trompetter", drafts, reserved=reserved)[0] is None


def test_pdfs_from_before_watching_are_left_alone(sender, backend, folder, make_pdf):
    draft(backend, "Bewerbung Barth und Kerner", "info@barth-kerner.de")
    make_pdf(BARTH, age=3600)            # long before the app started
    make_pdf(TROMPETTER, age=30)
    assert sender.run_once(folder, dry_run=False).kind == WAITING
    assert backend.sent == []


def test_batch_off_only_the_latest_pdf(batch_sender, backend, folder, make_pdf, config):
    config.data["auto_batch"] = False
    draft(backend, "Bewerbung Barth und Kerner", "info@barth-kerner.de")
    make_pdf(BARTH, age=50)
    make_pdf(TROMPETTER, age=30)
    result = batch_sender.run_once(folder, dry_run=False)
    assert result.kind == WAITING and result.pdf_name == TROMPETTER and backend.sent == []


def test_pdf_being_saved_is_looked_at_again_soon(sender, folder, make_pdf):
    path = make_pdf(TROMPETTER, age=0)
    sender.clock = lambda: file_time(os.stat(path)) + 0.05      # saved 50 ms ago
    result = sender.run_once(folder, dry_run=False)
    assert result.kind == WAITING and result.message == "still being saved"
    assert abs(result.details["soon"] - 0.15) < 0.01             # min. age 0.2 s


# --------------------------------------------------------------------------
# Gastronomie applications (subject without company, as the skill writes it)
# --------------------------------------------------------------------------

GASTRO_SUBJECT = "Bewerbung um einen Ausbildungsplatz zum Fachmann für Restaurants und Veranstaltungsgastronomie (m/w/d)"


def gastro_body(word):
    return (f"Sehr geehrte Damen und Herren,\n\ngerne möchte ich mich um einen Ausbildungsplatz zum Fachmann "
            f"für Restaurants und Veranstaltungsgastronomie (m/w/d) bei {word} bewerben.\n"
            "Im Anhang finden Sie meine vollständigen Bewerbungsunterlagen als PDF-Datei.")


def test_gastronomy_application_is_sent(sender, backend, folder, make_pdf):
    target = draft(backend, GASTRO_SUBJECT, "info@gasthaus-krone.de", body=gastro_body("Krone"))
    draft(backend, "Bewerbung Siemens", "jobs@siemens.com", age_s=7200)
    make_pdf("Amine_Haouam_Ausbildung_Gastronomie_Krone.pdf", age=30)
    result = sender.run_once(folder, dry_run=False)
    assert result.kind == SENT, result.message
    assert [s["draft_id"] for s in backend.sent] == [target]


def test_gastronomy_batch_with_free_mail_addresses(batch_sender, backend, folder, make_pdf):
    """Small restaurants often use web.de/gmx addresses: the company in the
    address plus the company in the text is a clear match, so older PDFs
    of a batch are sent too."""
    linde = draft(backend, GASTRO_SUBJECT, "gasthof.linde@web.de", body=gastro_body("Linde"))
    adler = draft(backend, GASTRO_SUBJECT, "info@hotel-adler.de", body=gastro_body("Adler"))
    make_pdf("Amine_Haouam_Ausbildung_Gastronomie_Linde.pdf", age=50)
    make_pdf("Amine_Haouam_Ausbildung_Hotelfachmann_Adler.pdf", age=30)
    results = [batch_sender.run_once(folder, dry_run=False) for _ in range(2)]
    assert [r.kind for r in results] == [SENT, SENT]
    assert [s["draft_id"] for s in backend.sent] == [linde, adler]


def test_version_3_config_gets_gastronomy_titles_and_keeps_own_settings(paths):
    import json
    from config import Config
    paths.config_file.write_text(json.dumps({
        "config_version": 3, "watch_interval_seconds": 1.0, "auto_send": True,
        "filename_parser": {"job_titles": ["Elektroniker", "Mechatroniker"]},
    }), encoding="utf-8")
    cfg = Config.load(paths)
    titles = cfg.filename_parser["job_titles"]
    assert titles[:2] == ["Elektroniker", "Mechatroniker"]          # own titles first, kept
    assert "Gastronomie" in titles and "Hotelfachfrau" in titles and "Pflegefachmann" in titles
    assert titles.count("Elektroniker") == 1
    assert cfg.watch_interval_ms == 1000 and cfg.auto_send is True  # version 3 settings stay
    cfg.save()
    from config import CONFIG_VERSION
    assert Config.load(paths).data["config_version"] == CONFIG_VERSION


# --------------------------------------------------------------------------
# Real case: company only in the application sentence ("bei Knorr bewerben"),
# recipient address of the hotel without that word, other drafts newer.
# --------------------------------------------------------------------------

def skill_body(job, word):
    return ("Sehr geehrte Damen und Herren,\n\n"
            f"gerne möchte ich mich um einen Ausbildungsplatz zum {job} (m/w/d) bei {word} bewerben.\n\n"
            "Im Anhang finden Sie meine vollständigen Bewerbungsunterlagen als PDF-Datei.\n\n"
            "Ich freue mich auf die Möglichkeit, mich Ihnen in einem Online-Vorstellungsgespräch "
            "persönlich vorzustellen.\n\nMit freundlichen Grüßen\nAmine Haouam")


RVG = "Fachmann für Restaurants und Veranstaltungsgastronomie"


def test_application_sentence_is_a_clear_match(sender, backend, folder, make_pdf):
    knorr = draft(backend, f"Bewerbung um einen Ausbildungsplatz zum {RVG} (m/w/d)",
                  "info@alte-muehle-hotel.com", body=skill_body(RVG, "Knorr"), age_s=600)
    draft(backend, "Bewerbung um einen Ausbildungsplatz zum Hotelfachmann (m/w/d)",       # newer draft
          "info@seehotel-example.de", body=skill_body("Hotelfachmann", "Laichingen"), age_s=60)
    make_pdf("Amine_Haouam_Ausbildung_Gastronomie_Knorr.pdf", age=30)
    result = sender.run_once(folder, dry_run=False)
    assert result.kind == SENT, result.message
    assert [s["draft_id"] for s in backend.sent] == [knorr]


def test_bulk_skill_run_every_pdf_finds_its_draft(batch_sender, backend, folder, make_pdf):
    """A skill run made 20 drafts; the PDFs are saved afterwards in another
    order. Each goes to its own draft - also drafts older than the newest 15."""
    words = [f"Betrieb{chr(65 + i)}" for i in range(20)]
    ids = {w: draft(backend, f"Bewerbung um einen Ausbildungsplatz zum {RVG} (m/w/d)", f"info@hotel{i}.de",
                    body=skill_body(RVG, w), age_s=3000 - i * 60) for i, w in enumerate(words)}
    order = words[::-1][:5] + words[:3]                  # newest drafts first, then the oldest ones
    for n, w in enumerate(order):
        make_pdf(f"Amine_Haouam_Ausbildung_Gastronomie_{w}.pdf", age=100 - n)
    sent = [batch_sender.run_once(folder, dry_run=False) for _ in order]
    assert [r.kind for r in sent] == [SENT] * len(order), [r.message for r in sent]
    assert [s["draft_id"] for s in backend.sent] == [ids[w] for w in order]


def test_draft_older_than_the_newest_15_is_found(sender, backend, folder, make_pdf, config):
    config.data["auto_batch"] = False
    target = draft(backend, f"Bewerbung um einen Ausbildungsplatz zum {RVG} (m/w/d)", "info@altehotel.de",
                   body=skill_body(RVG, "Knorr"), age_s=9000)
    for i in range(39):                                         # 39 newer drafts of other companies
        draft(backend, "Bewerbung um einen Ausbildungsplatz zum Hotelfachmann (m/w/d)", f"info@h{i}.de",
              body=skill_body("Hotelfachmann", f"Betrieb{i}"), age_s=8000 - i * 60)
    make_pdf("Amine_Haouam_Ausbildung_Gastronomie_Knorr.pdf", age=30)
    result = sender.run_once(folder, dry_run=False)
    assert result.kind == SENT, result.message
    assert [s["draft_id"] for s in backend.sent] == [target]


def test_text_only_mention_explains_why_it_waits(sender, backend, folder, make_pdf):
    draft(backend, "Bewerbung", "info@altehotel.de", age_s=600,
          body="Sehr geehrte Damen und Herren, ich habe schon bei Knorr gearbeitet und bewerbe mich.")
    draft(backend, "Bewerbung Siemens", "jobs@siemens.com", age_s=60)
    make_pdf("Amine_Haouam_Ausbildung_Gastronomie_Knorr.pdf", age=30)
    result = sender.run_once(folder, dry_run=False)
    assert result.kind == WAITING and backend.sent == []
    assert "only somewhere in the text" in result.message


def test_version_4_config_checks_50_drafts(paths):
    import json
    from config import Config
    paths.config_file.write_text(json.dumps({"config_version": 4, "auto_recent_drafts": 15}), encoding="utf-8")
    assert Config.load(paths).data["auto_recent_drafts"] == 50
    paths.config_file.write_text(json.dumps({"config_version": 4, "auto_recent_drafts": 30}), encoding="utf-8")
    assert Config.load(paths).data["auto_recent_drafts"] == 30           # own value kept
