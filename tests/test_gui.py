"""GUI workflow tests: the real Tk application driven programmatically.

Runs with a display (on Linux CI: xvfb-run).  Modal dialogs are replaced by
scripted answers through the App's ask_*/show_* methods.
"""

import gc

import pytest

tk = pytest.importorskip("tkinter")

from database import STATUS_DRY_RUN, STATUS_SUCCESS  # noqa: E402
from fakes import FakeGmailService  # noqa: E402
from gmail_client import GmailClient  # noqa: E402
from matching_engine import MULTIPLE, NO_MATCH, SINGLE_STRONG  # noqa: E402
import mime_samples as ms  # noqa: E402


def _display_available():
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except tk.TclError:
        return False


pytestmark = pytest.mark.skipif(not _display_available(), reason="no display available")


class Script:
    """Records dialogs and returns scripted answers."""

    def __init__(self):
        self.infos, self.warnings, self.errors, self.questions, self.confirms = [], [], [], [], []
        self.answer_yes = True
        self.confirm = True
        self.company = None
        self.pick_draft = None


@pytest.fixture
def make_app(config, db, backend, folder):
    apps = []

    def _make(auto_check=False):
        from gui import App
        config.watch_folder = folder
        config.auto_check = auto_check
        config.auto_send = False
        script = Script()

        def factory(paths, token_store, **kwargs):
            return GmailClient(FakeGmailService(backend), account_email=backend.account, num_retries=0)

        app = App(config, db, gmail_factory=factory, auto_connect=False)
        app.show_info = lambda msg, title="": script.infos.append(msg)
        app.show_warning = lambda msg, title="": script.warnings.append(msg)
        app.show_error = lambda msg, title="": script.errors.append(msg)

        def ask_yes_no(msg, title="", warning=False):
            script.questions.append(msg)
            return script.answer_yes

        def ask_confirm_plan(plan, match, manual):
            script.confirms.append((plan, match, manual))
            return script.confirm

        app.ask_yes_no = ask_yes_no
        app.ask_confirm_plan = ask_confirm_plan
        app.ask_company = lambda filename, explanation, initial: script.company
        app.ask_pick_draft = lambda matches: script.pick_draft(matches) if script.pick_draft else None
        apps.append(app)
        return app, script

    yield _make
    for app in apps:
        try:
            app._on_close()
        except tk.TclError:
            pass
    apps.clear()
    gc.collect()  # free Tk objects on the main thread, never on a worker thread


def connect(app):
    app.connect_gmail(interactive=False)
    app.wait_until_idle()
    assert app.gmail is not None


def state(widget):
    return "disabled" if widget.instate(["disabled"]) else "normal"


def test_full_workflow_dry_run_then_live(make_app, backend, make_pdf, db):
    target = backend.add_draft(ms.alternative_draft("Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de"))
    other = backend.add_draft(ms.alternative_draft("Bewerbung Siemens", "jobs@siemens.com"))
    original = backend.raw_of(target)
    other_original = backend.raw_of(other)
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=60)
    app, script = make_app()

    assert state(app.confirm_button) == "disabled"
    connect(app)
    assert "amine.test@gmail.com" in app.gmail_pill.cget("text")

    app.scan_newest()
    app.wait_until_idle()
    assert app.company == "Trompetter"
    assert app.pdf_labels["company"].cget("text") == "Trompetter"
    assert app.outcome.status == SINGLE_STRONG
    assert app.selected_match.draft.draft_id == target
    assert "bewerbung@trompetter.de" in app.detail_labels["recipient"].cget("text")
    assert state(app.confirm_button) == "normal"
    assert app.confirm_button.cget("text") == "Confirm (Dry Run)"

    # Dry run (default): nothing changes in Gmail.
    app.confirm_and_attach()
    app.wait_until_idle()
    assert app.last_result.status == STATUS_DRY_RUN
    assert backend.raw_of(target) == original
    assert backend.method_calls("drafts.update") == []
    plan, match, manual = script.confirms[-1]
    assert plan.pdf.name.endswith("Trompetter.pdf") and not manual

    # Live mode.
    app.dry_run_var.set(False)
    app._on_dry_run_toggled()
    assert app.confirm_button.cget("text") == "Confirm and Attach"
    app.confirm_and_attach()
    app.wait_until_idle()
    assert app.last_result.status == STATUS_SUCCESS, app.last_result.message
    assert [a.filename for a in app.gmail.get_draft_info(target).attachments] == \
        ["Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf"]
    assert backend.raw_of(other) == other_original      # other drafts untouched
    assert backend.forbidden == []
    assert app.current_pdf is None                      # workflow reset after success
    assert len(app.history_tree.get_children()) == 2    # dry run + success
    assert db.list_history()[0].status == STATUS_SUCCESS


def test_declining_final_confirmation_changes_nothing(make_app, backend, make_pdf):
    target = backend.add_draft(ms.alternative_draft("Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de"))
    original = backend.raw_of(target)
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=60)
    app, script = make_app()
    app.dry_run_var.set(False)
    app._on_dry_run_toggled()
    connect(app)
    app.scan_newest()
    app.wait_until_idle()
    script.confirm = False
    app.confirm_and_attach()
    app.wait_until_idle()
    assert backend.raw_of(target) == original
    assert backend.method_calls("drafts.update") == []
    assert app.last_result is None


def test_multiple_matches_require_explicit_selection(make_app, backend, make_pdf):
    a = backend.add_draft(ms.alternative_draft("Bewerbung Trompetter", "jobs@trompetter.de"))
    backend.add_draft(ms.alternative_draft("Nachfrage Trompetter", "info@trompetter.de"))
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=60)
    app, _script = make_app()
    connect(app)
    app.scan_newest()
    app.wait_until_idle()
    assert app.outcome.status == MULTIPLE
    assert app.selected_match is None
    assert state(app.confirm_button) == "disabled"      # nothing chosen automatically
    assert len(app.tree.get_children()) == 2
    # user selects a row
    for item, match in app._tree_items.items():
        if match.draft.draft_id == a:
            app.tree.selection_set(item)
    app.update()
    assert app.selected_match.draft.draft_id == a
    assert state(app.confirm_button) == "normal"


def test_no_match_blocks_until_manual_selection(make_app, backend, make_pdf):
    unrelated = backend.add_draft(ms.alternative_draft("Bewerbung Siemens", "jobs@siemens.com"))
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=60)
    app, script = make_app()
    connect(app)
    app.scan_newest()
    app.wait_until_idle()
    assert app.outcome.status == NO_MATCH
    assert state(app.confirm_button) == "disabled"
    assert "Nothing will be attached" in app.banner.cget("text")

    # Manual intervention: user picks the unrelated draft and confirms the warning.
    script.pick_draft = lambda matches: next(m for m in matches if m.draft.draft_id == unrelated)
    script.answer_yes = False
    app.select_another_draft()
    assert app.selected_match is None                   # warning declined -> still blocked
    script.answer_yes = True
    app.select_another_draft()
    assert app.selected_match.draft.draft_id == unrelated
    assert state(app.confirm_button) == "normal"
    app.confirm_and_attach()
    app.wait_until_idle()
    _plan, _match, manual = script.confirms[-1]
    assert manual is True                               # final dialog shows the extra warning


def test_reject_match(make_app, backend, make_pdf):
    backend.add_draft(ms.alternative_draft("Bewerbung Trompetter", "jobs@trompetter.de"))
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=60)
    app, _script = make_app()
    connect(app)
    app.scan_newest()
    app.wait_until_idle()
    assert app.outcome.status == SINGLE_STRONG
    app.reject_match()
    assert app.outcome.status == NO_MATCH
    assert app.selected_match is None
    assert state(app.confirm_button) == "disabled"


def test_unparseable_filename_asks_for_company(make_app, backend, make_pdf):
    target = backend.add_draft(ms.alternative_draft("Bewerbung bei Bosch", "karriere@bosch.de"))
    make_pdf("Bewerbung_Bosch_final.pdf", age=60)
    app, script = make_app()
    connect(app)
    script.company = "Bosch"
    app.scan_newest()
    app.wait_until_idle()
    assert app.company == "Bosch"
    assert app.selected_match.draft.draft_id == target


def test_unparseable_filename_cancelled_stops(make_app, backend, make_pdf):
    backend.add_draft(ms.alternative_draft("Bewerbung bei Bosch", "karriere@bosch.de"))
    make_pdf("Bewerbung_Bosch_final.pdf", age=60)
    app, script = make_app()
    connect(app)
    script.company = None
    app.scan_newest()
    app.wait_until_idle()
    assert app.company is None
    assert app.drafts is None                            # Gmail was not even searched
    assert state(app.confirm_button) == "disabled"


def test_duplicate_attachment_offers_marking_processed(make_app, backend, make_pdf, config, db, folder):
    path = make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=60)
    backend.add_draft(ms.mixed_draft_with_attachment("Bewerbung Trompetter", "jobs@trompetter.de",
                                                     path.read_bytes(), path.name))
    app, script = make_app()
    connect(app)
    app.scan_newest()
    app.wait_until_idle()
    app.confirm_and_attach()
    app.wait_until_idle()
    assert any("already attached" in q for q in script.questions)
    assert backend.method_calls("drafts.update") == []
    from file_scanner import FileScanner
    assert FileScanner(db, config.scanner).scan(folder).selected is None


def test_forced_dry_run_cannot_be_disabled(config, db, backend):
    from gui import App
    app = App(config, db, force_dry_run=True, auto_connect=False,
              gmail_factory=lambda *a, **k: GmailClient(FakeGmailService(backend)))
    try:
        app.dry_run_var.set(False)
        app._on_dry_run_toggled()
        assert app.dry_run_active
        assert app.dry_run_check.instate(["disabled"])
    finally:
        app._on_close()
        del app
        gc.collect()


def test_dialogs_render_and_settings_save(make_app, backend, make_pdf, config):
    """Instantiate every dialog (non-modally) and exercise the settings logic."""
    from gui import CompanyDialog, DraftPickerDialog, PdfPickerDialog, SettingsDialog
    backend.add_draft(ms.alternative_draft("Bewerbung Trompetter", "jobs@trompetter.de"))
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=60)
    app, _script = make_app()
    connect(app)
    app.scan_newest()
    app.wait_until_idle()

    company = CompanyDialog(app, "x.pdf", "explanation", "Trompetter")
    company.var.set("  Barth   und Kerner  ")
    company._ok()
    assert company.result == "Barth und Kerner"

    picker = DraftPickerDialog(app, app.outcome.all_matches, set(), "Trompetter")
    first = picker.tree.get_children()[0]
    picker.tree.selection_set(first)
    picker._choose()
    assert picker.result.draft.subject == "Bewerbung Trompetter"

    candidates = app._make_scanner().list_candidates(app.current_pdf.path.parent)
    pdf_picker = PdfPickerDialog(app, candidates)
    pdf_picker.tree.selection_set(pdf_picker.tree.get_children()[0])
    pdf_picker._choose()
    assert pdf_picker.result == app.current_pdf.path

    settings = SettingsDialog(app, config)
    settings.vars["job_titles"].set("Elektroniker, Mechatroniker , Elektroniker für Betriebstechnik")
    settings.vars["min_file_age_seconds"].set("5,5")
    settings._save()
    assert settings.result is True
    assert config.filename_parser["job_titles"] == ["Elektroniker", "Mechatroniker", "Elektroniker für Betriebstechnik"]
    assert config.scanner["min_file_age_seconds"] == 5.5


def enable_auto(app, *, send, live):
    app.auto_check_var.set(True)
    app._on_auto_toggled()
    app.auto_send_var.set(send)
    app._on_auto_send_toggled()
    app.dry_run_var.set(not live)
    app._on_dry_run_toggled()


def auto_tick(app):
    app._auto_tick()
    app.wait_until_idle()
    app.wait_until_idle()  # the scan may chain the draft search and the attachment


def test_auto_check_finds_new_pdf_without_scan_click(make_app, backend, make_pdf):
    target = backend.add_draft(ms.alternative_draft("Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de"))
    app, _script = make_app()
    enable_auto(app, send=False, live=True)
    connect(app)
    auto_tick(app)
    assert app.current_pdf is None                      # empty folder: nothing happens
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=60)
    auto_tick(app)
    assert app.company == "Trompetter"
    assert app.selected_match.draft.draft_id == target
    assert backend.method_calls("drafts.update") == []  # without auto-send it waits for the click
    auto_tick(app)                                      # no new scan while a PDF is pending
    assert app.current_pdf is not None


def test_auto_send_single_strong_match(make_app, backend, make_pdf, db):
    backend.add_draft(ms.alternative_draft("Bewerbung Siemens", "jobs@siemens.com"))
    target = backend.add_draft(ms.alternative_draft("Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de"))
    app, script = make_app()
    script.answer_yes = True                            # confirms enabling auto-send
    enable_auto(app, send=True, live=True)
    connect(app)
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=60)
    auto_tick(app)
    assert app.last_result is not None and app.last_result.sent, app.last_result
    assert [s["draft_id"] for s in backend.sent] == [target]
    assert script.confirms == []                        # no confirmation dialog in auto mode
    assert app.current_pdf is None                      # ready for the next PDF
    assert db.list_history()[0].send_status == "sent"
    # The next tick does not send anything again.
    auto_tick(app)
    assert len(backend.sent) == 1


def test_auto_send_waits_when_ambiguous(make_app, backend, make_pdf):
    backend.add_draft(ms.alternative_draft("Bewerbung Trompetter", "jobs@trompetter.de"))
    backend.add_draft(ms.alternative_draft("Nachfrage Trompetter", "info@trompetter.de"))
    app, script = make_app()
    enable_auto(app, send=True, live=True)
    connect(app)
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=60)
    auto_tick(app)
    assert app.outcome.status == MULTIPLE
    assert backend.sent == [] and backend.method_calls("drafts.update") == []


def test_auto_send_waits_for_body_only_or_no_match(make_app, backend, make_pdf):
    backend.add_draft(ms.alternative_draft("Bewerbung als Elektroniker", "info@tb-elektro.de",
                                           body_plain="Sehr geehrte Damen und Herren der Trompetter GmbH,"))
    app, _script = make_app()
    enable_auto(app, send=True, live=True)
    connect(app)
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=60)
    auto_tick(app)
    assert app.outcome.status != SINGLE_STRONG
    assert backend.sent == []
    assert "Not sent automatically" in app.banner.cget("text")


def test_auto_send_requires_company_from_filename(make_app, backend, make_pdf):
    backend.add_draft(ms.alternative_draft("Bewerbung bei Bosch", "karriere@bosch.de"))
    make_pdf("Bewerbung_Bosch_final.pdf", age=60)
    app, script = make_app()
    enable_auto(app, send=True, live=True)
    connect(app)
    script.company = "Bosch"                            # typed by hand
    auto_tick(app)
    assert app.outcome.status == SINGLE_STRONG
    assert backend.sent == []


def test_auto_send_in_dry_run_sends_nothing(make_app, backend, make_pdf):
    target = backend.add_draft(ms.alternative_draft("Bewerbung - Trompetter GmbH", "bewerbung@trompetter.de"))
    original = backend.raw_of(target)
    app, _script = make_app()
    enable_auto(app, send=True, live=False)
    connect(app)
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=60)
    auto_tick(app)
    assert app.last_result.status == STATUS_DRY_RUN
    assert backend.sent == [] and backend.raw_of(target) == original
    auto_tick(app)                                      # same PDF is not dry-run again and again
    assert len([r for r in app.db.list_history() if r.status == STATUS_DRY_RUN]) == 1


def test_enabling_auto_send_needs_confirmation(make_app):
    app, script = make_app()
    script.answer_yes = False
    app.auto_send_var.set(True)
    app._on_auto_send_toggled()
    assert not app.send_enabled
    assert app.config_obj.auto_send is False


def test_manual_confirm_with_send_enabled_sends(make_app, backend, make_pdf):
    backend.add_draft(ms.alternative_draft("Bewerbung Trompetter", "jobs@trompetter.de"))
    backend.add_draft(ms.alternative_draft("Nachfrage Trompetter", "info@trompetter.de"))
    app, script = make_app()
    enable_auto(app, send=True, live=True)
    app.auto_check_var.set(False)
    connect(app)
    make_pdf("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf", age=60)
    app.scan_newest()
    app.wait_until_idle()
    item = next(iter(app._tree_items))
    app.tree.selection_set(item)
    app.update()
    assert app.confirm_button.cget("text") == "Confirm, Attach and Send"
    app.confirm_and_attach()
    app.wait_until_idle()
    assert len(backend.sent) == 1 and len(script.confirms) == 1
