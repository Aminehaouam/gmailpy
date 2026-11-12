"""Tests for the Ausbildung Job Finder.

Parser tests use real pages saved from the Jobsuche der BA (tests/pages).
The run logic is tested with a fake browser session.
"""

import csv
import io
import sys
import wave
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from alert_sound import attention_wav, done_wav  # noqa: E402
from ba_browser import RunEvents, RunSettings, StopRequested, run  # noqa: E402
from ba_parser import (  # noqa: E402
    KNOWN_SPECIALTIES,
    OTHER_SPECIALTY,
    Job,
    SearchPage,
    StateNotFound,
    apply_detail,
    build_search_url,
    extract_state,
    is_elektroniker,
    parse_contact_text,
    parse_search,
    specialty_of,
    specialty_selected,
)
from jobfinder_db import JobStore  # noqa: E402

PAGES = HERE / "pages"
ALL = set(KNOWN_SPECIALTIES) | {OTHER_SPECIALTY}


def page(name):
    return extract_state((PAGES / name).read_text(encoding="utf-8"))


# -- parser ---------------------------------------------------------------

@pytest.mark.parametrize("beruf, ok, specialty", [
    ("Elektroniker/in - Betriebstechnik", True, "Betriebstechnik"),
    ("Elektroniker/in - Energie- und Gebäudetechnik", True, "Energie- und Gebäudetechnik"),
    ("Elektroniker/in - Maschinen und Antriebstechnik (HwO)", True, "Maschinen und Antriebstechnik (HwO)"),
    ("Elektroniker/in", True, ""),
    ("IT-System-Elektroniker/in", False, ""),
    ("Informationselektroniker/in", False, ""),
    ("Industrieelektriker/in - Betriebstechnik", False, ""),
    ("Elektroanlagenmonteur/in", False, ""),
    ("Mechatroniker/in", False, ""),
    ("", False, ""),
])
def test_same_beruf_any_specialty(beruf, ok, specialty):
    assert is_elektroniker(beruf) is ok
    assert specialty_of(beruf) == specialty


def test_search_page_keeps_only_elektroniker():
    result = parse_search(page("search_koeln_25km.html"))
    assert result.all_count == 25 and len(result.jobs) == 22
    assert all(is_elektroniker(j.beruf) for j in result.jobs)
    assert result.max_results == 117 and result.last_page == 5
    assert set(result.specialties) == {"Betriebstechnik", "Automatisierungstechnik", "Energie- und Gebäudetechnik"}
    first = result.jobs[0]
    assert first.refnr and first.company and first.ort and first.url.endswith(first.refnr)


def test_broad_search_lists_all_specialties():
    result = parse_search(page("search_elektroniker_page2.html"))
    assert result.page == 2 and result.max_results > 5000
    assert {"Betriebstechnik", "Energie- und Gebäudetechnik", "Automatisierungstechnik",
            "Geräte und Systeme"} <= set(result.specialties)
    assert "IT-System-Elektroniker/in" not in result.specialties


def test_detail_page():
    job = Job("10000-1208628753-S", "", "Elektroniker/in - Betriebstechnik", "Betriebstechnik", "")
    apply_detail(job, page("detail.html"))
    assert job.company == "Deutsche Steinzeug Solar Ceramics GmbH"
    assert (job.street, job.plz, job.ort) == ("Servaisstr. 9", "53347", "Alfter")
    assert job.start == "2027-08-01" and job.education == "Mittlere Reife"
    assert job.homepage == "https://www.deutsche-steinzeug.de"
    assert "Elektroniker" in job.description


def test_missing_state_is_reported():
    with pytest.raises(StateNotFound):
        extract_state("<html><body>Wartungsarbeiten</body></html>")


def test_contact_text():
    contact = parse_contact_text(
        "Ansprechpartner\nFrau Maria Müller\nServaisstr. 9\n53347 Alfter\nTelefon: +49 228 391-1234\n"
        "Mobil 0171/1234567\nE-Mail: ausbildung@firma-gmbh.de\nReferenz-Nr.: 10000-1208628753-S\n"
        "Beginn 2027-08-01")
    assert contact["emails"] == ["ausbildung@firma-gmbh.de"]
    assert contact["phones"] == ["+49 228 391-1234", "0171/1234567"]
    assert contact["person"] == "Frau Maria Müller"


def test_search_url():
    url = build_search_url("Elektroniker/in", "Köln", 25, 3)
    assert url.startswith("https://www.arbeitsagentur.de/jobsuche/suche?suchbereich=ausbildung")
    assert "was=Elektroniker%2Fin" in url and "wo=K%C3%B6ln" in url and "umkreis=25" in url
    assert "page=3" in url
    assert "umkreis" not in build_search_url(wo="", umkreis=25)


def test_specialty_selection():
    assert specialty_selected("Betriebstechnik", {"Betriebstechnik"})
    assert not specialty_selected("Betriebstechnik", {"Automatisierungstechnik"})
    assert specialty_selected("Neue Fachrichtung", {OTHER_SPECIALTY})
    assert specialty_selected("", {OTHER_SPECIALTY})


# -- storage --------------------------------------------------------------

def test_store_and_csv(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    job = Job("R1", "Ausbildung Elektroniker", "Elektroniker/in - Betriebstechnik", "Betriebstechnik",
              "Müller & Söhne GmbH", ort="Köln", emails=["a@b.de"], contact_status="ok")
    store.save(job)
    open_job = Job("R2", "x", "Elektroniker/in", "", "Y", contact_status="captcha-skipped")
    store.save(open_job)
    assert store.is_complete("R1") and not store.is_complete("R2") and not store.is_complete("R3")
    assert [j.refnr for j, _ in store.all()] == ["R2", "R1"]
    target = tmp_path / "out.csv"
    assert store.export_csv(target) == 2
    raw = target.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                       # BOM for Excel
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
    assert rows[0][:3] == ["Gefunden am", "Firma", "Fachrichtung"]
    assert "Müller & Söhne GmbH" in rows[2] and "a@b.de" in rows[2]


# -- run logic with a fake browser --------------------------------------

class FakeSession:
    def __init__(self, pages, captcha_for=(), fail_for=(), stop_after=None):
        self.pages = pages
        self.captcha_for = set(captcha_for)
        self.fail_for = set(fail_for)
        self.stop_after = stop_after
        self.opened = []

    def search(self, was, wo, umkreis, page_number):
        return self.pages[page_number - 1]

    def search_url(self, url, page_number, only_elektroniker=True):
        pages = self.pages.get(url, []) if isinstance(self.pages, dict) else self.pages
        self.searched = getattr(self, "searched", []) + [(url, page_number)]
        return pages[page_number - 1]

    def fetch_detail(self, job, *, want_contact, on_captcha, on_captcha_done, captcha_timeout):
        if self.stop_after is not None and len(self.opened) >= self.stop_after:
            raise StopRequested()
        self.opened.append(job.refnr)
        if job.refnr in self.fail_for:
            raise RuntimeError("page broken")
        if want_contact and job.refnr in self.captcha_for:
            on_captcha(job)
            on_captcha_done(job, True)
        job.emails = [f"{job.refnr.lower()}@firma.de"] if want_contact else []
        job.contact_status = "ok" if want_contact else ""
        return job


def make_page(specialties, page_no=1, last=2):
    jobs = [Job(f"P{page_no}-{i}", "t", f"Elektroniker/in - {s}" if s else "Elektroniker/in", s, f"Firma {i}")
            for i, s in enumerate(specialties)]
    return SearchPage(jobs=jobs, all_count=len(jobs), max_results=last * 25, page=page_no, size=25,
                      specialties={"Betriebstechnik": 3})


def test_run_filters_specialties_and_respects_limits():
    pages = [make_page(["Betriebstechnik", "Automatisierungstechnik", "Betriebstechnik"], 1),
             make_page(["Betriebstechnik", "Neu"], 2)]
    session = FakeSession(pages)
    saved, seen = [], []
    events = RunEvents(job=lambda j: seen.append(j.refnr))
    summary = run(session, RunSettings(specialties={"Betriebstechnik"}, max_jobs=10),
                  known=lambda r: False, save=saved.append, events=events)
    assert session.opened == ["P1-0", "P1-2", "P2-0"]
    assert summary == {"opened": 3, "found": 3, "skipped": 0} and len(saved) == 3 == len(seen)
    session2 = FakeSession(pages)
    run(session2, RunSettings(specialties=ALL, max_jobs=2), known=lambda r: False, save=lambda j: None,
        events=RunEvents())
    assert session2.opened == ["P1-0", "P1-1"]


def test_run_skips_known_jobs():
    session = FakeSession([make_page(["Betriebstechnik", "Betriebstechnik"], 1, last=1)])
    summary = run(session, RunSettings(specialties=ALL), known=lambda r: r == "P1-0",
                  save=lambda j: None, events=RunEvents())
    assert session.opened == ["P1-1"] and summary["skipped"] == 1


def test_run_reports_captcha_and_survives_broken_pages():
    session = FakeSession([make_page(["Betriebstechnik"] * 3, 1, last=1)], captcha_for={"P1-1"},
                          fail_for={"P1-0"})
    captchas, saved = [], []
    events = RunEvents(captcha=lambda j: captchas.append(j.refnr))
    run(session, RunSettings(specialties=ALL), known=lambda r: False, save=saved.append, events=events)
    assert captchas == ["P1-1"]
    assert [j.contact_status for j in saved] == ["error", "ok", "ok"]


def test_stop_request_ends_the_run():
    session = FakeSession([make_page(["Betriebstechnik"] * 5, 1, last=1)], stop_after=2)
    with pytest.raises(StopRequested):
        run(session, RunSettings(specialties=ALL), known=lambda r: False, save=lambda j: None, events=RunEvents())
    assert len(session.opened) == 2


def test_sounds_are_valid_wav():
    for data in (attention_wav(), done_wav()):
        with wave.open(io.BytesIO(data)) as wav:
            assert wav.getnframes() > 1000


# -- window -----------------------------------------------------------------

def _display():
    try:
        import tkinter
        root = tkinter.Tk()
        root.destroy()
        return True
    except Exception:
        return False


@pytest.mark.skipif(not _display(), reason="no display")
def test_window_shows_results_and_captcha_alert(tmp_path, monkeypatch):
    import gc
    import time
    import jobfinder_gui

    class GuiFakeSession(FakeSession):
        def __init__(self, profile_dir, stop_event=None, skip_event=None, **kw):
            super().__init__([make_page(["Betriebstechnik", "Geräte und Systeme"], 1, last=1)])
            self.browser_name = "Fake"
            self.skip_event = skip_event

        def start(self):
            pass

        def close(self):
            pass

        def fetch_detail(self, job, *, want_contact, on_captcha, on_captcha_done, captcha_timeout):
            if job.refnr == "P1-1":
                on_captcha(job)
                assert self.skip_event.wait(10)      # the user clicks "skip"
                on_captcha_done(job, False)
                job.contact_status = "captcha-skipped"
                return job
            return super().fetch_detail(job, want_contact=want_contact, on_captcha=on_captcha,
                                        on_captcha_done=on_captcha_done, captcha_timeout=captcha_timeout)

    monkeypatch.setattr(jobfinder_gui, "BrowserSession", GuiFakeSession)
    sounds = []
    app = jobfinder_gui.JobFinderApp(tmp_path, alert=lambda fb: sounds.append("alert"),
                                     done_sound=lambda fb: sounds.append("done"))
    try:
        app.start()
        deadline = time.time() + 10
        while app.captcha_window is None and time.time() < deadline:
            app.update()
            time.sleep(0.02)
        assert app.captcha_window is not None and sounds == ["alert"]
        app.skip_captcha()
        while (app.worker.is_alive() or not app.events.empty()) and time.time() < deadline:
            app.update()
            time.sleep(0.02)
        app.update()
        values = [app.tree.item(i)["values"] for i in app.tree.get_children()]
        assert len(values) == 2
        statuses = {v[2]: v[6] for v in values}
        assert statuses == {"Firma 0": "Kontakt ✓", "Firma 1": "Sicherheitsabfrage offen"}
        assert app.last_summary["opened"] == 2 and sounds == ["alert", "done"]
        assert (tmp_path / "settings.json").exists()
    finally:
        app.on_close()
        del app
        gc.collect()


# -- complete listing data ----------------------------------------------------

from ba_parser import apply_kontaktdaten, find_kontaktdaten  # noqa: E402
from email_finder import extract_emails  # noqa: E402


def detail(name):
    st = page(name)
    return apply_detail(Job(st["jobdetail"]["referenznummer"], "", "", "", ""), st)


def test_extract_emails_plain_obfuscated_and_mailto():
    text = ("Bewerbung bitte an Bewerbung@Firma-GmbH.de oder info [at] firma-gmbh [dot] de, "
            "karriere(at)firma.de. Datenschutz: datenschutz@firma-gmbh.de, Vertrieb: zentralvertrieb@firma.de, "
            "logo@2x.png, noreply@firma.de, kontakt@arbeitsagentur.de")
    assert extract_emails(text) == ["bewerbung@firma-gmbh.de", "info@firma-gmbh.de", "karriere@firma.de"]
    assert extract_emails("", '<a href="mailto:ausbildung%40stadtwerke-bochum.de">x</a> jobs&#64;x-y.de') == \
        ["ausbildung@stadtwerke-bochum.de", "jobs@x-y.de"]


def test_email_in_ad_text_lanxess():
    job = detail("detail_lanxess_email_in_text.html")
    assert job.emails == ["ausbildung@lanxess.com"]
    assert job.email_source == "BA-Anzeige (Stellentext)"
    assert job.homepage == "http://karriere.lanxess.de/de/startseite/"


def test_email_in_employer_profile_deutsche_steinzeug():
    """Real ad whose application address the first version missed."""
    job = detail("detail.html")
    assert job.emails == ["bewerbung.witterschlick@deutsche-steinzeug.de"]
    assert job.email_source == "BA-Arbeitgeberprofil"
    assert job.homepage == "https://www.deutsche-steinzeug.de"
    assert job.founded == "1755" and job.company_size == "950" and job.benefits


def test_pay_and_employer_profile_ford():
    job = detail("detail_ford_pay.html")
    assert job.pay_per_year == [1261, 1328, 1419, 1485]
    assert job.pay_text() == "1. Jahr 1.261 €, 2. Jahr 1.328 €, 3. Jahr 1.419 €, 4. Jahr 1.485 €"
    assert job.homepage == "http://www.azubi-bei-ford.de"
    assert job.working_time == "Vollzeit" and job.education.startswith("Qualifizierter")
    assert job.locations == ["Henry-Ford-Str. 1, 50735 Köln"]


def test_employer_contact_block_deutsche_bahn():
    job = detail("detail_db_employer_contact.html")
    assert "Potsdamer Platz 2" in job.employer_contact
    assert job.phones == ["Telefon: +49 30 297 24707"]
    assert any(link.startswith("Facebook:") for link in job.social_links)
    assert job.homepage.startswith("https://")


def test_ansprechpartner_and_phone_in_text_krueger():
    job = detail("detail_krueger_person_in_text.html")
    assert job.contact_person == "Frau Invidia"
    assert job.phones == ["Telefon: 02202 105-5150"]
    assert job.external_url and job.partner_name == "On-apply GmbH"
    assert job.locations == ["Köln", "51469 Bergisch Gladbach"]


KONTAKTDATEN = {   # structure of the data the page loads after the Sicherheitsabfrage
    "bewerbungPerEMail": True, "bewerbungSchriftlich": True, "bewerbungUeberInternet": True,
    "urlBewerbungsformular": "https://firma.de/karriere/bewerben",
    "geforderteAnlagen": "Lebenslauf, letzte zwei Zeugnisse",
    "angebotskontakt": {
        "firma": "Muster Elektrotechnik GmbH", "anrede": "FRAU", "position": "Ausbildungsleitung",
        "name": {"titel": "Dr.", "vorname": "Maria", "nachname": "Müller"},
        "postadresse": {"strasse": "Hauptstr.", "hausnummer": "5", "plz": "50667", "ort": "Köln"},
        "telefonnummer": {"laendervorwahl": "+49", "vorwahl": "0221", "rufnummer": "123456"},
        "mobilnummer": {"laendervorwahl": "+49", "vorwahl": "0171", "rufnummer": "9876543"},
        "emailadresse": "Ausbildung@Muster-Elektro.de",
    },
}


def test_contact_data_after_sicherheitsabfrage():
    job = Job("R", "t", "Elektroniker/in", "", "Muster", emails=["info@muster-elektro.de"])
    apply_kontaktdaten(job, find_kontaktdaten({"wrapper": [{"x": 1}, KONTAKTDATEN]}))
    assert job.contact_person == "Frau Dr. Maria Müller"
    assert job.contact_position == "Ausbildungsleitung"
    assert job.emails == ["ausbildung@muster-elektro.de", "info@muster-elektro.de"]
    assert job.email_source == "BA-Kontaktdaten"
    assert job.phones == ["Telefon: +49 221 123456", "Mobil: +49 171 9876543"]
    assert job.contact_address == "Muster Elektrotechnik GmbH, Hauptstr. 5, 50667 Köln"
    assert job.application_methods == ["per E-Mail", "online (Website des Arbeitgebers)", "schriftlich (Post)"]
    assert job.application_url == "https://firma.de/karriere/bewerben"
    assert job.required_documents == "Lebenslauf, letzte zwei Zeugnisse"
    assert find_kontaktdaten({"nothing": "here"}) is None


def test_exports_contain_everything(tmp_path):
    import json as _json
    store = JobStore(tmp_path / "jobs.sqlite3")
    job = detail("detail_ford_pay.html")
    apply_kontaktdaten(job, KONTAKTDATEN)
    store.save(job)
    store.export_csv(tmp_path / "out.csv")
    rows = list(csv.reader(io.StringIO((tmp_path / "out.csv").read_text(encoding="utf-8-sig")), delimiter=";"))
    row = dict(zip(rows[0], rows[1]))
    assert row["Ansprechpartner"] == "Frau Dr. Maria Müller" and row["Position"] == "Ausbildungsleitung"
    assert row["E-Mail"].startswith("ausbildung@muster-elektro.de")
    assert row["Firmenwebsite"] == "http://www.azubi-bei-ford.de"
    assert row["Ausbildungsvergütung"].startswith("1. Jahr 1.261 €")
    assert "Stellenbeschreibung" in row and row["Stellenbeschreibung"]
    store.export_json(tmp_path / "out.json")
    data = _json.loads((tmp_path / "out.json").read_text(encoding="utf-8"))
    assert data[0]["contact_person"] == "Frau Dr. Maria Müller" and data[0]["url"].endswith(job.refnr)


def test_job_summary_lists_contact_first():
    from jobfinder_gui import job_summary
    job = detail("detail_ford_pay.html")
    apply_kontaktdaten(job, KONTAKTDATEN)
    text = job_summary(job)
    assert text.index("Ansprechpartner: Frau Dr. Maria Müller – Ausbildungsleitung") < text.index("STELLE")
    assert "Firmenwebsite: http://www.azubi-bei-ford.de" in text
    assert "Ausbildungsvergütung: 1. Jahr 1.261 €" in text


# -- the user's own links -----------------------------------------------------

from ba_parser import LinkError, classify_link, parse_links, with_page  # noqa: E402

GEB = ("https://www.arbeitsagentur.de/jobsuche/suche?suchbereich=ausbildung&"
       "was=Elektroniker%2Fin%20-%20Energie-%20und%20Geb%C3%A4udetechnik&wo=Bonn&umkreis=50&angebotsart=4")


def test_classify_links():
    kind, url = classify_link(GEB + "&page=3")
    assert kind == "search" and "page=" not in url and "umkreis=50" in url and "angebotsart=4" in url
    assert classify_link("www.arbeitsagentur.de/jobsuche/jobdetail/10000-1208539150-S?x=1") == \
        ("job", "10000-1208539150-S")
    assert classify_link("  10001-1003940968-S ") == ("job", "10001-1003940968-S")
    for bad in ("https://www.google.com/search?q=x", "https://www.arbeitsagentur.de/bildung", "hallo"):
        with pytest.raises(LinkError):
            classify_link(bad)


def test_parse_links_and_paging():
    searches, jobs, errors = parse_links(f"{GEB}\n\n{GEB}&page=2\n10001-1003940968-S\nkein link\n")
    assert len(searches) == 1 and jobs == ["10001-1003940968-S"] and len(errors) == 1
    paged = with_page(searches[0], 4)
    assert paged.endswith("&page=4") and "wo=Bonn" in paged
    assert "page=" not in with_page(paged, 1)


def test_filter_off_keeps_every_job_of_the_link():
    assert len(parse_search(page("search_koeln_25km.html"), only_elektroniker=False).jobs) == 25
    assert len(parse_search(page("search_koeln_25km.html")).jobs) == 22


def test_run_with_own_links():
    a = make_page(["Energie- und Gebäudetechnik"] * 2, 1, last=1)
    b = make_page(["Energie- und Gebäudetechnik", "Betriebstechnik"], 1, last=1)
    b.jobs[1].refnr = "B-1"
    b.jobs[0].refnr = a.jobs[1].refnr                     # same job in both searches
    session = FakeSession({"https://x/suche?a=1": [a], "https://x/suche?b=1": [b]})
    settings = RunSettings(specialties=ALL, search_urls=["https://x/suche?a=1", "https://x/suche?b=1"],
                           job_refs=["9999-1-S"])
    summary = run(session, settings, known=lambda r: False, save=lambda j: None, events=RunEvents())
    assert session.opened == ["9999-1-S", "P1-0", "P1-1", "B-1"]   # the duplicate is opened once
    assert summary["opened"] == 4
    assert [u for u, _ in session.searched] == ["https://x/suche?a=1", "https://x/suche?b=1"]


def test_run_specialty_filter_can_be_switched_off():
    pg = make_page(["Betriebstechnik", "Geräte und Systeme"], 1, last=1)
    pg.jobs[1].specialty = ""
    pg.jobs[1].beruf = "Mechatroniker/in"
    on = FakeSession({"u": [pg]})
    run(on, RunSettings(specialties={"Betriebstechnik"}, search_urls=["u"]), known=lambda r: False,
        save=lambda j: None, events=RunEvents())
    off = FakeSession({"u": [pg]})
    run(off, RunSettings(specialties={"Betriebstechnik"}, search_urls=["u"], filter_elektroniker=False),
        known=lambda r: False, save=lambda j: None, events=RunEvents())
    assert on.opened == ["P1-0"] and off.opened == ["P1-0", "P1-1"]
