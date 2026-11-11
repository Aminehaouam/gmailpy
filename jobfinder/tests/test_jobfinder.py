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
    assert rows[0][:3] == ["Gefunden am", "Fachrichtung", "Firma"]
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
        statuses = {v[2]: v[5] for v in values}
        assert statuses == {"Firma 0": "Kontakt ✓", "Firma 1": "Sicherheitsabfrage offen"}
        assert app.last_summary["opened"] == 2 and sounds == ["alert", "done"]
        assert (tmp_path / "settings.json").exists()
    finally:
        app.on_close()
        del app
        gc.collect()


# -- e-mail search ----------------------------------------------------------

from ba_browser import fill_missing_emails  # noqa: E402
from email_finder import (  # noqa: E402
    candidate_links,
    company_tokens,
    decode_result_link,
    extract_emails,
    looks_like_company_site,
    rank_emails,
    urls_in_text,
)


def test_extract_emails_plain_obfuscated_and_mailto():
    text = ("Bewerbung bitte an Bewerbung@Firma-GmbH.de oder info [at] firma-gmbh [dot] de, "
            "karriere(at)firma.de. Datenschutz: datenschutz@firma-gmbh.de, Vertrieb: zentralvertrieb@firma.de, "
            "logo@2x.png, noreply@firma.de, kontakt@arbeitsagentur.de")
    assert extract_emails(text) == ["bewerbung@firma-gmbh.de", "info@firma-gmbh.de", "karriere@firma.de"]
    assert extract_emails("", '<a href="mailto:ausbildung%40stadtwerke-bochum.de">x</a> jobs&#64;x-y.de') == \
        ["ausbildung@stadtwerke-bochum.de", "jobs@x-y.de"]


def test_rank_prefers_application_addresses_on_company_domain():
    emails = ["info@firma.de", "max.mustermann@firma.de", "ausbildung@firma.de", "bewerbung@other.de"]
    assert rank_emails(emails, "https://www.firma.de")[:2] == ["ausbildung@firma.de", "info@firma.de"]


def test_company_site_check():
    assert company_tokens("Barth & Kerner GmbH") == ["barth", "kerner"]
    assert looks_like_company_site("https://deutsche-steinzeug.de/de", "Deutsche Steinzeug Solar Ceramics GmbH")
    assert not looks_like_company_site("https://www.db.com/", "Deutsche Steinzeug Solar Ceramics GmbH")
    assert not looks_like_company_site("https://de.indeed.com/cmp/barth", "Barth und Kerner GmbH")
    assert looks_like_company_site("https://www.barth-kerner.de", "Barth und Kerner GmbH")
    assert looks_like_company_site("https://www.example-energie.de", "Ostwind GmbH", "Ostwind GmbH – Startseite")


def test_search_result_links_are_decoded():
    assert decode_result_link("https://www.bing.com/ck/a?!&&p=x&u=a1aHR0cHM6Ly93d3cuZGIuY29tLw&ntb=1") == \
        "https://www.db.com/"
    assert decode_result_link("https://www.google.com/url?q=https://firma.de/&sa=U") == "https://firma.de/"
    assert decode_result_link("https://firma.de/x") == "https://firma.de/x"


def test_search_queries():
    from email_finder import search_queries
    assert search_queries("YNCORIS", "Hürth, Rheinland") == ["YNCORIS Hürth", "YNCORIS"]
    assert search_queries("Krüger GmbH & Co. KG", "") == ["Krüger GmbH & Co. KG"]


def test_website_pages_worth_visiting():
    links = [("Karriere", "/karriere"), ("Ausbildung", "/karriere/ausbildung"), ("Produkte", "/produkte"),
             ("Kontakt", "/kontakt"), ("Impressum", "/impressum"), ("Facebook", "https://facebook.com/x"),
             ("Datenblatt", "/x.pdf")]
    assert candidate_links(links, "https://www.firma.de/") == [
        "https://www.firma.de/karriere", "https://www.firma.de/karriere/ausbildung",
        "https://www.firma.de/kontakt", "https://www.firma.de/impressum"]
    assert urls_in_text("Mehr unter www.firma.de/karriere. Oder https://jobs.firma.de!") == \
        ["https://www.firma.de/karriere", "https://jobs.firma.de"]


def test_email_in_ad_text_is_found():
    """Real ad: the application address is written in the job text."""
    job = Job("10000-1208628753-S", "", "Elektroniker/in - Betriebstechnik", "Betriebstechnik", "")
    apply_detail(job, page("detail.html"))
    assert job.emails == ["bewerbung.witterschlick@deutsche-steinzeug.de"]
    assert job.email_source == "BA-Anzeige (Stellentext)"


def test_company_cache(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    assert store.company_get("Firma GmbH") is None
    job = Job("R1", "t", "Elektroniker/in", "", "Firma  GmbH", emails=["ausbildung@firma.de"],
              website="https://firma.de", email_source="Firmenwebsite (firma.de)")
    store.company_put(job)
    assert store.company_get("firma gmbh")["emails"] == ["ausbildung@firma.de"]
    store.company_put(Job("R2", "t", "Elektroniker/in", "", "Nichts AG"))
    assert store.company_get("Nichts AG") == {"website": "", "emails": [], "source": ""}


class WebFakeSession(FakeSession):
    def __init__(self, pages, websites):
        super().__init__(pages)
        self.websites = websites
        self.web_lookups = []

    def fetch_detail(self, job, *, want_contact, on_captcha, on_captcha_done, captcha_timeout):
        self.opened.append(job.refnr)
        job.contact_status = "none"
        return job

    def find_email_on_website(self, job, *, on_captcha, on_captcha_done, captcha_timeout):
        self.web_lookups.append(job.company)
        if job.company in self.websites:
            job.website, job.emails = self.websites[job.company]
            job.email_source, job.contact_status = "Firmenwebsite (x)", "ok"
        return job


def test_run_looks_up_email_on_website_once_per_company(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    pg = make_page(["Betriebstechnik", "Betriebstechnik", "Betriebstechnik"], 1, last=1)
    pg.jobs[1].company = pg.jobs[0].company          # two ads of the same company
    session = WebFakeSession([pg], {"Firma 0": ("https://firma0.de", ["ausbildung@firma0.de"])})
    run(session, RunSettings(specialties=ALL), known=lambda r: False, save=store.save, events=RunEvents(),
        company_get=store.company_get, company_put=store.company_put)
    assert session.web_lookups == ["Firma 0", "Firma 2"]   # cached for the second ad
    emails = {job.refnr: job.emails for job, _ in store.all()}
    assert emails == {"P1-0": ["ausbildung@firma0.de"], "P1-1": ["ausbildung@firma0.de"], "P1-2": []}


def test_fill_missing_emails_uses_ad_text_first(tmp_path):
    store = JobStore(tmp_path / "jobs.sqlite3")
    with_text = Job("A", "t", "Elektroniker/in", "", "Text GmbH", description="Mail: jobs@text-gmbh.de")
    on_web = Job("B", "t", "Elektroniker/in", "", "Web GmbH")
    done = Job("C", "t", "Elektroniker/in", "", "Fertig GmbH", emails=["x@fertig.de"])
    session = WebFakeSession([], {"Web GmbH": ("https://web.de", ["bewerbung@web.de"])})
    summary = fill_missing_emails(session, [with_text, on_web, done], RunSettings(), save=store.save,
                                  events=RunEvents(), company_get=store.company_get, company_put=store.company_put)
    assert summary["found"] == 2 and session.web_lookups == ["Web GmbH"]
    assert with_text.emails == ["jobs@text-gmbh.de"] and on_web.emails == ["bewerbung@web.de"]
