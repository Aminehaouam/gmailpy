"""End-to-end pipeline without GUI: folder -> parser -> Gmail drafts ->
matching -> confirmation -> draft update -> history, using the fake Gmail."""

import os
import shutil
import time

import pytest

from attachment_manager import AttachmentManager
from database import STATUS_SUCCESS
from file_scanner import FileScanner
from filename_parser import FilenameParser
from matching_engine import MULTIPLE, NO_MATCH, SINGLE_STRONG, MatchingEngine
import mime_samples as ms


def run_pipeline(folder, db, config, gmail):
    """One 'execution': scan -> parse -> fetch drafts -> match."""
    scan = FileScanner(db, config.scanner).scan(folder)
    assert scan.selected is not None, scan.message
    parsed = FilenameParser(config.filename_parser).parse(scan.selected.name)
    assert parsed.ok, parsed.error
    drafts, _ = gmail.fetch_all_drafts()
    outcome = MatchingEngine(config.matching["generic_email_domains"]).match(parsed.company, drafts)
    return scan.selected, parsed.company, outcome


@pytest.fixture
def mailbox(backend):
    """Several drafts; the NEWEST one belongs to a different company."""
    ids = {}
    ids["barth"] = backend.add_draft(ms.alternative_draft(
        "Bewerbung um einen Ausbildungsplatz als Elektroniker", "Barth & Kerner GmbH <info@barth-kerner.de>",
        body_plain="Sehr geehrte Damen und Herren der Barth und Kerner GmbH,"), internal_date_ms=1_000_000)
    ids["trompetter"] = backend.add_draft(ms.alternative_draft(
        "Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de",
        body_plain="Sehr geehrte Frau Schmidt,"), internal_date_ms=2_000_000)
    ids["siemens_newest"] = backend.add_draft(ms.alternative_draft(
        "Bewerbung als Elektroniker", "jobs@siemens.com",
        body_plain="Sehr geehrte Damen und Herren,"), internal_date_ms=9_000_000)
    return ids


def test_full_run_attaches_newest_pdf_to_correct_draft(folder, db, config, gmail, backend, mailbox, make_pdf):
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Barth_und_Kerner_GmbH.pdf", age=900)
    newest = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=60)
    originals = {k: backend.raw_of(v) for k, v in mailbox.items()}

    pdf, company, outcome = run_pipeline(folder, db, config, gmail)
    assert pdf.path == newest
    assert company == "Trompetter"
    assert outcome.status == SINGLE_STRONG
    assert outcome.proposed.draft.draft_id == mailbox["trompetter"]  # not the newest draft

    manager = AttachmentManager(gmail, db)
    result = manager.execute(manager.prepare(pdf, company, outcome.proposed.draft), dry_run=False)
    assert result.success

    names = [a.filename for a in gmail.get_draft_info(mailbox["trompetter"]).attachments]
    assert names == [newest.name]
    assert backend.raw_of(mailbox["barth"]) == originals["barth"]
    assert backend.raw_of(mailbox["siemens_newest"]) == originals["siemens_newest"]
    assert backend.forbidden == []

    # Second execution: the next PDF (multi-word company) goes to its own draft.
    pdf2, company2, outcome2 = run_pipeline(folder, db, config, gmail)
    assert company2 == "Barth und Kerner GmbH"
    assert outcome2.status == SINGLE_STRONG
    assert outcome2.proposed.draft.draft_id == mailbox["barth"]
    assert manager.execute(manager.prepare(pdf2, company2, outcome2.proposed.draft), dry_run=False).success

    # Third execution: nothing left; a renamed copy of a processed PDF is recognised.
    copy = folder / "Kopie_von_Trompetter.pdf"
    shutil.copy(newest, copy)
    stamp = time.time() - 30
    os.utime(copy, (stamp, stamp))
    scan = FileScanner(db, config.scanner).scan(folder)
    assert scan.selected is None
    assert len(db.list_history()) == 2 and all(r.status == STATUS_SUCCESS for r in db.list_history())


def test_no_matching_draft_stops(folder, db, config, gmail, backend, mailbox, make_pdf):
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Bosch_Rexroth_AG.pdf", age=60)
    _pdf, _company, outcome = run_pipeline(folder, db, config, gmail)
    assert outcome.status == NO_MATCH
    assert outcome.proposed is None
    assert backend.method_calls("drafts.update") == []


def test_multiple_matching_drafts_require_selection(folder, db, config, gmail, backend, mailbox, make_pdf):
    backend.add_draft(ms.alternative_draft("Zweite Bewerbung Trompetter (Elektroniker für Betriebstechnik)",
                                           "personal@trompetter.de"))
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=60)
    _pdf, _company, outcome = run_pipeline(folder, db, config, gmail)
    assert outcome.status == MULTIPLE
    assert outcome.proposed is None
    assert len(outcome.candidates) == 2


def test_dry_run_end_to_end_changes_nothing(folder, db, config, gmail, backend, mailbox, make_pdf):
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=60)
    before = {k: backend.raw_of(v) for k, v in mailbox.items()}
    pdf, company, outcome = run_pipeline(folder, db, config, gmail)
    manager = AttachmentManager(gmail, db)
    manager.execute(manager.prepare(pdf, company, outcome.proposed.draft), dry_run=True)
    assert {k: backend.raw_of(v) for k, v in mailbox.items()} == before
    assert backend.method_calls("drafts.update") == []
    # The PDF is still unprocessed after a dry run.
    assert FileScanner(db, config.scanner).scan(folder).selected.path == pdf.path
