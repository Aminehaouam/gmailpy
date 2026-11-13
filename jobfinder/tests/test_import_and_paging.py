"""Tests: long searches (all result pages, retries, continuing where the last
run stopped) and importing saved lists (CSV / Excel / JSON)."""

import gc
import json
import sys
import time
import zipfile
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from ba_browser import RunEvents, RunSettings, SearchPageError, StopRequested, run  # noqa: E402
from ba_parser import KNOWN_SPECIALTIES, OTHER_SPECIALTY, Job, SearchPage  # noqa: E402
from jobfinder_db import JobStore, merge_jobs  # noqa: E402
from jobfinder_import import ImportFileError, read_file, read_files  # noqa: E402

FILES = HERE / "files"
ALL = set(KNOWN_SPECIALTIES) | {OTHER_SPECIALTY}
URL = "https://www.arbeitsagentur.de/jobsuche/suche?suchbereich=ausbildung&was=Elektroniker%2Fin"


# --------------------------------------------------------------------------
# Long searches
# --------------------------------------------------------------------------

class PagedSession:
    """Fake browser: *pages* result pages with *per_page* jobs each."""

    def __init__(self, pages, per_page=2, failures=None, stop_after=None):
        self.pages, self.per_page = pages, per_page
        self.failures = dict(failures or {})     # page -> times it fails
        self.stop_after = stop_after
        self.searched, self.opened, self.waits = [], [], []

    def search_url(self, url, page_number, only_elektroniker=True):
        self.searched.append(page_number)
        if self.failures.get(page_number, 0) > 0:
            self.failures[page_number] -= 1
            raise TimeoutError("page did not load")
        jobs = [] if page_number > self.pages else [
            Job(f"{page_number}-{i}", "t", "Elektroniker/in - Betriebstechnik", "Betriebstechnik", f"F{i}")
            for i in range(self.per_page)]
        return SearchPage(jobs=jobs, all_count=len(jobs), max_results=self.pages * 25, page=page_number,
                          size=25, specialties={})

    def wait(self, seconds):
        self.waits.append(seconds)

    def fetch_detail(self, job, **_kwargs):
        if self.stop_after is not None and len(self.opened) >= self.stop_after:
            raise StopRequested()
        self.opened.append(job.refnr)
        job.contact_status = "ok"
        return job


def go(session, positions=None, **settings):
    events = RunEvents(position=lambda url, page: positions.append(page)) if positions is not None else RunEvents()
    settings.setdefault("specialties", ALL)
    settings.setdefault("search_urls", [URL])
    return run(session, RunSettings(**settings), known=lambda r: False, save=lambda j: None, events=events)


def test_run_reads_every_result_page():
    """No page limit: a search with 40 pages (1000 results) is read to the end."""
    session = PagedSession(pages=40, per_page=1)
    summary = go(session, max_jobs=1000)
    assert session.searched == list(range(1, 41))
    assert summary["opened"] == 40 and summary["pages"] == 40
    assert summary["complete"] and not summary["limit_reached"]


def test_job_limit_and_position_for_the_next_run():
    session, positions = PagedSession(pages=10), []
    summary = go(session, positions, max_jobs=5)
    assert session.opened == ["1-0", "1-1", "2-0", "2-1", "3-0"]
    assert summary["limit_reached"] and not summary["complete"]
    assert positions[-1] == 3               # page 3 was not finished


def test_next_run_continues_and_wraps_round():
    """Starts one page before the stop, reads to the end, then pages 1.. for
    new listings, and reports the search as finished."""
    session, positions = PagedSession(pages=6, per_page=1), []
    summary = go(session, positions, max_jobs=100, start_pages={URL: 4})
    assert session.searched == [3, 4, 5, 6, 1, 2]
    assert positions == [3, 4, 5, 6, 1, 2, 0]
    assert summary["complete"]


def test_continue_after_the_list_got_shorter():
    session = PagedSession(pages=3, per_page=1)
    summary = go(session, max_jobs=100, start_pages={URL: 9})
    assert session.searched == [8, 1, 2, 3]    # page 8 is empty -> back to page 1
    assert summary["opened"] == 3 and summary["complete"]


def test_resume_position_only_for_its_own_search():
    session = PagedSession(pages=2, per_page=1)
    go(session, max_jobs=100, start_pages={"https://other": 5})
    assert session.searched == [1, 2]


def test_search_page_is_tried_again():
    session = PagedSession(pages=2, per_page=1, failures={2: 2})
    summary = go(session, max_jobs=100, page_retries=(1.0, 2.0, 3.0))
    assert session.waits == [1.0, 2.0]
    assert session.searched == [1, 2, 2, 2] and summary["opened"] == 2


def test_search_page_gives_up_after_the_retries():
    session, positions = PagedSession(pages=3, per_page=1, failures={2: 9}), []
    with pytest.raises(SearchPageError):
        go(session, positions, max_jobs=100, page_retries=(1.0, 1.0))
    assert session.opened == ["1-0"] and positions[-1] == 2    # next run starts there


def test_stop_keeps_position():
    session, positions = PagedSession(pages=5, per_page=2, stop_after=3), []
    with pytest.raises(StopRequested):
        go(session, positions, max_jobs=100)
    assert positions[-1] == 2


# --------------------------------------------------------------------------
# Merging records
# --------------------------------------------------------------------------

def test_merge_prefers_contact_and_fills_gaps():
    good = Job("1-a-S", "t", "b", "s", "Firma", emails=["a@firma.de"], contact_person="Frau A",
               contact_status="ok")
    later = Job("1-a-S", "t", "b", "s", "Firma", description="Text", emails=["b@firma.de"],
                contact_status="captcha-skipped")
    merged = merge_jobs(later, good)
    assert merged.contact_status == "ok" and merged.contact_person == "Frau A"
    assert merged.description == "Text" and merged.emails == ["a@firma.de", "b@firma.de"]


def test_failed_second_visit_keeps_contact(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    store.save(Job("1-a-S", "t", "b", "s", "Firma", emails=["a@firma.de"], contact_status="ok"))
    store.save(Job("1-a-S", "t", "b", "s", "Firma", contact_status="error"))
    job = store.get("1-a-S")
    assert job.contact_status == "ok" and job.emails == ["a@firma.de"]
    assert store.is_complete("1-a-S")


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

def sample_store(path):
    store = JobStore(path)
    jobs = [
        (Job("10000-1208539150-S", "Ausbildung", "Elektroniker/in - Automatisierungstechnik",
             "Automatisierungstechnik", "Beispiel Chemie GmbH", plz="51373", ort="Leverkusen",
             description="Zeile 1\nZeile 2; mit Semikolon", pay_per_year=[1100, 1150, 1200],
             emails=["ausbildung@beispiel.de"], phones=["Telefon: 0214 1", "Fax: 0214 2"],
             locations=["Köln, 50667", "Bonn, 53111"], benefits=["Jobticket"], homepage="https://beispiel.de",
             contact_person="Frau A", contact_status="ok", published="2026-09-15"), "2026-11-10T09:15:02"),
        (Job("14338-k27156.1333-S", "Elektroniker", "Elektroniker/in - Energie- und Gebäudetechnik",
             "Energie- und Gebäudetechnik", "Elektro Sachsen", plz="01067", ort="Dresden",
             contact_status="captcha-skipped"), "2026-11-11T14:40:30"),
    ]
    store.import_jobs(jobs)
    return store, jobs


def test_csv_export_reads_back_completely(tmp_path):
    store, jobs = sample_store(tmp_path / "a.sqlite3")
    store.export_csv(tmp_path / "liste.csv")
    imported, unreadable = read_file(tmp_path / "liste.csv")
    assert unreadable == 0
    assert sorted(imported, key=lambda j: j[0].refnr) == sorted(jobs, key=lambda j: j[0].refnr)


def test_json_export_reads_back_completely(tmp_path):
    store, jobs = sample_store(tmp_path / "a.sqlite3")
    store.export_json(tmp_path / "liste.json")
    imported, _ = read_file(tmp_path / "liste.json")
    assert sorted(imported, key=lambda j: j[0].refnr) == sorted(jobs, key=lambda j: j[0].refnr)


def test_excel_workbook():
    """The export saved by Excel as .xlsx: dates are day numbers, the PLZ
    lost its leading zero, the description has line breaks."""
    imported, unreadable = read_file(FILES / "gespeichert_excel.xlsx")
    jobs = {job.refnr: (job, first_seen) for job, first_seen in imported}
    assert unreadable == 0 and len(jobs) == 3
    chemie, first_seen = jobs["10000-1208539150-S"]
    assert first_seen == "2026-11-10T09:15:02"
    assert chemie.published == "2026-09-15" and chemie.start == "2027-08-01"
    assert chemie.pay_per_year == [1100, 1150, 1200, 1250]
    assert chemie.emails == ["ausbildung@beispiel-chemie.de"] and chemie.contact_person == "Frau Anna Beispiel"
    assert chemie.description == "Deine Aufgaben:\nSchaltschränke verdrahten; Anlagen prüfen"
    assert chemie.homepage == "https://www.beispiel-chemie.de" and chemie.founded == "1863"
    assert chemie.benefits == ["Jobticket", "Kantine"] and chemie.contact_status == "ok"
    assert jobs["14338-k27156.1333-S"][0].plz == "01067"
    assert jobs["14338-k27156.1333-S"][0].phones == ["Telefon: 0351 123456", "Fax: 0351 123457"]
    assert jobs["10001-1003567813-S"][0].locations == ["Köln, 50667", "Bonn, 53111"]


def _xlsx_with_shared_strings(path, rows):
    """Minimal workbook as Excel writes it: shared strings (one with rich
    text runs), numbers, a date as day number."""
    strings, sheet_rows = [], []
    for r, row in enumerate(rows, 1):
        cells = []
        for c, value in enumerate(row):
            ref = f"{chr(65 + c)}{r}"
            if isinstance(value, (int, float)):
                cells.append(f'<c r="{ref}" s="1"><v>{value}</v></c>')
            elif value:
                strings.append(value)
                cells.append(f'<c r="{ref}" t="s"><v>{len(strings) - 1}</v></c>')
        sheet_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
    m = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    items = []
    for text in strings:
        if text.startswith("Elektro "):     # rich text: two runs
            items.append(f'<si><r><t>Elektro</t></r><r><rPr><b/></rPr><t xml:space="preserve"> '
                         f'{text[8:]}</t></r></si>')
        else:
            items.append(f"<si><t>{text}</t></si>")
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("xl/workbook.xml", f'<workbook {m} xmlns:r="http://schemas.openxmlformats.org/'
                    'officeDocument/2006/relationships"><sheets><sheet name="Liste" sheetId="1" r:id="rId1"/>'
                    '</sheets></workbook>')
        zf.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/'
                    'package/2006/relationships"><Relationship Id="rId1" Type="worksheet" '
                    'Target="worksheets/tabelle1.xml"/></Relationships>')
        zf.writestr("xl/sharedStrings.xml", f'<sst {m}>{"".join(items)}</sst>')
        zf.writestr("xl/worksheets/tabelle1.xml", f'<worksheet {m}><sheetData>{"".join(sheet_rows)}'
                    '</sheetData></worksheet>')


def test_excel_shared_strings(tmp_path):
    path = tmp_path / "liste.xlsx"
    _xlsx_with_shared_strings(path, [
        ["Gefunden am", "Firma", "PLZ", "E-Mail", "Kontaktstatus", "Link zur Stelle"],
        [46338.35, "Elektro Meier GmbH", 1067, "info@meier.de", "ok",
         "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1208539150-S"],
    ])
    [(job, first_seen)], _ = read_file(path)
    assert job.refnr == "10000-1208539150-S" and job.company == "Elektro Meier GmbH"
    assert job.plz == "01067" and job.emails == ["info@meier.de"] and job.contact_status == "ok"
    assert first_seen.startswith("2026-11-12T08:2")


def test_csv_saved_again_by_german_excel(tmp_path):
    """Excel 'CSV (Trennzeichen-getrennt)': ANSI, German dates, PLZ without 0."""
    text = ("Gefunden am;Firma;PLZ;Ort;E-Mail;Veröffentlicht;Kontaktstatus;Referenznummer\r\n"
            "12.11.2026 08:34;Müller & Söhne;1067;Dresden;a@m.de;15.09.2026;ok;10000-1208539150-S\r\n")
    path = tmp_path / "liste.csv"
    path.write_bytes(text.encode("cp1252"))
    [(job, first_seen)], _ = read_file(path)
    assert job.company == "Müller & Söhne" and job.plz == "01067"
    assert job.published == "2026-09-15" and first_seen == "2026-11-12T08:34:00"


def test_csv_of_the_first_versions(tmp_path):
    """First app versions: fewer columns, ', ' between values, no status."""
    text = ("Gefunden am;Fachrichtung;Firma;Straße;PLZ;Ort;Beginn;Ansprechpartner;E-Mail;Telefon;"
            "E-Mail gefunden in;Firmenwebsite;Homepage (BA);Bewerbung über Partnerseite;Stellentitel;"
            "Schulabschluss;Link zur Stelle;Referenznummer\n"
            "2026-11-11 10:00:00;Betriebstechnik;A GmbH;;50667;Köln;;Herr B;b@a.de, c@a.de;"
            "Telefon: 0221 1, Mobil: 0171 2;Kontaktdaten;https://web-found.de;https://a.de;;T;;"
            "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-1-S;10000-1-S\n"
            "2026-11-11 10:05:00;Betriebstechnik;C GmbH;;50667;Köln;;;;;;https://c-web.de;;;T;;"
            "https://www.arbeitsagentur.de/jobsuche/jobdetail/10000-2-S;\n")
    path = tmp_path / "alt.csv"
    path.write_text(text, encoding="utf-8-sig")
    jobs = {job.refnr: job for job, _ in read_file(path)[0]}
    a, c = jobs["10000-1-S"], jobs["10000-2-S"]
    assert a.emails == ["b@a.de", "c@a.de"] and a.phones == ["Telefon: 0221 1", "Mobil: 0171 2"]
    assert a.homepage == "https://a.de" and a.contact_status == "ok"
    assert c.homepage == "https://c-web.de" and c.contact_status == ""   # contact still to be read


def test_several_files_become_one_list(tmp_path):
    old = tmp_path / "1.csv"
    old.write_text("Gefunden am;Firma;Kontaktstatus;Referenznummer\n"
                   "2026-11-01 08:00:00;A;captcha-skipped;10000-1-S\n"
                   "2026-11-01 08:00:00;B;ok;10000-2-S\n", encoding="utf-8")
    new = tmp_path / "2.csv"
    new.write_text("Gefunden am;Firma;Ansprechpartner;E-Mail;Kontaktstatus;Referenznummer\n"
                   "2026-11-05 08:00:00;A;Frau A;a@a.de;ok;10000-1-S\n"
                   "2026-11-05 08:00:00;C;;;none;10000-3-S\n", encoding="utf-8")
    bad = tmp_path / "notizen.csv"
    bad.write_text("Name;Telefon\nx;1\n", encoding="utf-8")
    result = read_files([old, new, bad, tmp_path / "bild.png"])
    assert result.files == 2 and result.rows == 4 and len(result.jobs) == 3 and result.duplicates == 1
    assert len(result.errors) == 2
    a, first_seen = result.jobs["10000-1-S"]
    assert a.contact_person == "Frau A" and a.contact_status == "ok"
    assert first_seen == "2026-11-01T08:00:00"           # the earliest date
    assert result.complete == 3

    store = JobStore(tmp_path / "jobs.sqlite3")
    assert store.import_jobs(list(result.jobs.values())) == {"new": 3, "updated": 0, "unchanged": 0}
    assert store.import_jobs(list(result.jobs.values())) == {"new": 0, "updated": 0, "unchanged": 3}
    assert all(store.is_complete(r) for r in ("10000-1-S", "10000-2-S", "10000-3-S"))
    assert [fs for _, fs in store.all()][0] == "2026-11-05T08:00:00"


def test_rows_without_reference_are_counted(tmp_path):
    path = tmp_path / "x.csv"
    path.write_text("Firma;Ort;Referenznummer\nA;Köln;10000-1-S\nB;Bonn;keine Nummer\n;;\n", encoding="utf-8")
    jobs, unreadable = read_file(path)
    assert [j.refnr for j, _ in jobs] == ["10000-1-S"] and unreadable == 1


def test_not_a_job_list(tmp_path):
    path = tmp_path / "x.xlsx"
    path.write_bytes(b"not a zip")
    with pytest.raises(ImportFileError):
        read_file(path)


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------

def test_gui_import_and_live_rows(tmp_path, monkeypatch):
    jobfinder_gui = pytest.importorskip("jobfinder_gui")
    import tkinter
    try:
        app = jobfinder_gui.JobFinderApp(tmp_path / "home", alert=lambda fb: None, done_sound=lambda fb: None)
    except tkinter.TclError:
        pytest.skip("no display")
    try:
        monkeypatch.setattr(jobfinder_gui.filedialog, "askopenfilenames",
                            lambda **kw: [str(FILES / "gespeichert_excel.xlsx")])
        reports = []
        monkeypatch.setattr(jobfinder_gui.messagebox, "showinfo", lambda title, text, **kw: reports.append(text))
        app.import_files()
        assert len(app.tree.get_children()) == 3
        assert "3 verschiedene Stellen" in reports[0] and "Neu in der Liste: 3" in reports[0]
        assert "2 davon sind fertig gelesen" in reports[0]
        assert app.count_label.cget("text") == "3 Stellen gespeichert"

        # A job read during a search: its row is updated, a new one added on top.
        app.new_refs = set()
        job = app.store.get("14338-k27156.1333-S")
        job.emails, job.contact_status = ["info@sachsen.de"], "ok"
        app.store.save(job)
        app._on_job(job)
        new = Job("10000-9-S", "t", "Elektroniker/in", "", "Neue Firma", contact_status="none")
        app.store.save(new)
        app._on_job(new)
        rows = [app.tree.item(i)["values"] for i in app.tree.get_children()]
        assert len(rows) == 4 and rows[0][2] == "Neue Firma"
        assert any(r[5] == "info@sachsen.de" and r[6] == "Kontakt ✓" for r in rows)
        assert app.count_label.cget("text") == "4 Stellen gespeichert"

        # The position of a search is remembered for the next start.
        app._on_position(URL, 7)
        assert app.current_settings().start_pages == {URL: 7}
        assert json.loads((tmp_path / "home" / "settings.json").read_text(encoding="utf-8"))["resume_pages"] \
            == {URL: 7}
        app._on_position(URL, 0)
        assert app.current_settings().start_pages == {}
    finally:
        app.on_close()
        del app
        gc.collect()
        time.sleep(0)
