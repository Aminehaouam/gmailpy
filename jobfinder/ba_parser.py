"""
ba_parser.py - Read job data from Jobsuche der Bundesagentur fuer Arbeit pages.

The Jobsuche pages are rendered on the server and embed their data as JSON
(<script id="ng-state" type="application/json">).  Reading that JSON is far
more reliable than reading the visible page text:

    search page:  state["suchergebnis"]["ergebnisliste"] (25 jobs per page)
    detail page:  state["jobdetail"], state["arbeitgeberdarstellung"]

The employer's contact details are NOT in that JSON; they are loaded after a
"Sicherheitsabfrage" (captcha) that the user solves in the browser.  The
visible contact box text is parsed with parse_contact_text().
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse

BASE = "https://www.arbeitsagentur.de/jobsuche"

# Same Beruf ("Elektroniker/in"), any specialty.  Seen on the site:
KNOWN_SPECIALTIES = [
    "Betriebstechnik",
    "Energie- und Gebäudetechnik",
    "Automatisierungstechnik",
    "Automatisierungstechnik (Handwerk)",
    "Automatisierungs- und Systemtechnik",
    "Geräte und Systeme",
    "Informations- und Systemtechnik",
    "Maschinen und Antriebstechnik (BBiG)",
    "Maschinen und Antriebstechnik (HwO)",
    "Gebäudesystemintegration",
    "Gebäude- und Infrastruktursysteme",
    "Luftfahrttechnische Systeme",
    "Prüffeld",
]
OTHER_SPECIALTY = "(other / no specialty)"

_ELEKTRONIKER_RE = re.compile(r"^\s*elektroniker\s*/\s*in\b", re.I)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# German phone numbers start with +, 00 or 0 (reference numbers start with 1-9).
_PHONE_RE = re.compile(r"(?<![\w-])(?:\+|0)[\d \t/()\-]{6,}\d(?![\w-])")
_PERSON_RE = re.compile(
    r"\b(?:Herr|Frau)[ \t]+(?:Dr\.[ \t]+)?[A-ZÄÖÜ][\w\-äöüß]+(?:[ \t]+[A-ZÄÖÜ][\w\-äöüß]+)*")


class StateNotFound(Exception):
    """The page did not contain the embedded job data."""


def extract_state(html: str) -> dict[str, Any]:
    match = re.search(r'<script id="ng-state" type="application/json">(.*?)</script>', html, re.S)
    if not match:
        raise StateNotFound("No job data found on the page (page layout changed or not loaded).")
    return json.loads(match.group(1))


def is_elektroniker(beruf: str | None) -> bool:
    """True for 'Elektroniker/in' and 'Elektroniker/in - <specialty>' only
    (not IT-System-Elektroniker/in, Industrieelektriker/in, ...)."""
    return bool(beruf) and bool(_ELEKTRONIKER_RE.match(beruf))


def specialty_of(beruf: str | None) -> str:
    """'Elektroniker/in - Betriebstechnik' -> 'Betriebstechnik' ('' if none)."""
    if not beruf or not is_elektroniker(beruf):
        return ""
    parts = re.split(r"\s+-\s+", beruf, maxsplit=1)
    return parts[1].strip() if len(parts) == 2 else ""


def specialty_selected(specialty: str, selected: set[str]) -> bool:
    if specialty in selected:
        return True
    return specialty not in KNOWN_SPECIALTIES and OTHER_SPECIALTY in selected


def build_search_url(was: str = "Elektroniker/in", wo: str = "", umkreis: int | None = None,
                     page: int = 1) -> str:
    params: list[tuple[str, str]] = [("suchbereich", "ausbildung"), ("was", was)]
    if wo.strip():
        params.append(("wo", wo.strip()))
        if umkreis:
            params.append(("umkreis", str(int(umkreis))))
    if page > 1:
        params.append(("page", str(page)))
    return f"{BASE}/suche?{urlencode(params)}"


def detail_url(refnr: str) -> str:
    return f"{BASE}/jobdetail/{refnr}"


# --------------------------------------------------------------------------
# Links pasted by the user (search pages, single jobs, reference numbers)
# --------------------------------------------------------------------------

_REFNR_RE = re.compile(r"^\d{3,6}-[0-9A-Za-z.]+(?:-[0-9A-Za-z.]+)*-S$")


class LinkError(ValueError):
    """A pasted line is not a Jobsuche link."""


def classify_link(text: str) -> tuple[str, str]:
    """('search', url) for a Jobsuche search page, ('job', refnr) for a job
    page or a bare reference number.  Raises LinkError otherwise."""
    text = text.strip().strip("<>\"'")
    if _REFNR_RE.match(text):
        return "job", text
    url = text if "://" in text else "https://" + text
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not (host == "arbeitsagentur.de" or host.endswith(".arbeitsagentur.de")):
        raise LinkError(f"Kein Link der Jobsuche (arbeitsagentur.de): {text}")
    path = parsed.path.rstrip("/")
    if path.startswith("/jobsuche/jobdetail/"):
        refnr = path.split("/jobsuche/jobdetail/", 1)[1].split("/")[0]
        if refnr:
            return "job", refnr
    if path == "/jobsuche/suche":
        params = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False) if k != "page"]
        return "search", f"{BASE}/suche?{urlencode(params)}"
    raise LinkError(f"Weder Suchergebnis- noch Stellen-Link der Jobsuche: {text}")


def parse_links(text: str) -> tuple[list[str], list[str], list[str]]:
    """Split pasted text (one link per line) into search URLs, job reference
    numbers and error messages."""
    searches: list[str] = []
    jobs: list[str] = []
    errors: list[str] = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        try:
            kind, value = classify_link(line)
        except LinkError as exc:
            errors.append(str(exc))
            continue
        target = searches if kind == "search" else jobs
        if value not in target:
            target.append(value)
    return searches, jobs, errors


def with_page(url: str, page: int) -> str:
    """The same search URL for result page *page* (all filters kept)."""
    parsed = urlparse(url)
    params = [(k, v) for k, v in parse_qsl(parsed.query) if k != "page"]
    if page > 1:
        params.append(("page", str(page)))
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{urlencode(params)}"


def _address(location: dict | None) -> dict[str, str]:
    adresse = (location or {}).get("adresse") or {}
    street = " ".join(x for x in (adresse.get("strasse"), adresse.get("hausnummer")) if x)
    return {"street": street, "plz": adresse.get("plz") or "", "ort": adresse.get("ort") or "",
            "region": (adresse.get("region") or "").replace("_", " ").title()}


@dataclass
class Job:
    # --- from the search result list
    refnr: str
    title: str
    beruf: str
    specialty: str
    company: str
    street: str = ""
    plz: str = ""
    ort: str = ""
    region: str = ""
    start: str = ""
    published: str = ""
    # --- job ad (detail page)
    description: str = ""
    education: str = ""
    locations: list[str] = field(default_factory=list)    # all places of work
    pay_per_year: list[int] = field(default_factory=list)  # Ausbildungsvergütung 1./2./3./4. Jahr
    pay_note: str = ""
    working_time: str = ""
    contract: str = ""
    changed: str = ""
    chiffre: str = ""
    supported_by_ba: bool = False                        # "Stelle betreut durch die BA"
    external_url: str = ""                               # partner portal: apply there
    partner_name: str = ""
    # --- employer profile on the BA page
    homepage: str = ""                                   # company website (from the BA listing)
    other_links: list[str] = field(default_factory=list)
    social_links: list[str] = field(default_factory=list)
    employer_description: str = ""
    company_size: str = ""
    founded: str = ""
    headquarters: str = ""
    benefits: list[str] = field(default_factory=list)
    employer_contact: str = ""                           # contact block of the employer profile
    # --- contact data ("Informationen zur Bewerbung", behind the Sicherheitsabfrage)
    contact_person: str = ""                             # Ansprechpartner
    contact_position: str = ""
    contact_address: str = ""
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)      # "Telefon: ...", "Mobil: ...", "Fax: ..."
    email_source: str = ""                               # where the e-mail address was found
    application_methods: list[str] = field(default_factory=list)
    application_url: str = ""                            # online application form
    required_documents: str = ""
    contact_text: str = ""                               # visible text of the contact box
    contact_status: str = ""   # "", "ok", "none", "external", "captcha-skipped", "error"

    @property
    def url(self) -> str:
        return detail_url(self.refnr)

    def pay_text(self) -> str:
        return ", ".join(f"{i}. Jahr {euro:,} €".replace(",", ".") for i, euro in enumerate(self.pay_per_year, 1))


@dataclass
class SearchPage:
    jobs: list[Job]
    all_count: int        # jobs on this page before the Elektroniker filter
    max_results: int
    page: int
    size: int
    specialties: dict[str, int]  # specialty -> count (from the site's facets)

    @property
    def last_page(self) -> int:
        return max(1, -(-self.max_results // max(1, self.size)))


def parse_search(state: dict[str, Any], only_elektroniker: bool = True) -> SearchPage:
    """Jobs of a search result page.  With only_elektroniker=False every job
    of the page is kept (the user's own search link decides what is shown)."""
    result = state.get("suchergebnis")
    if not isinstance(result, dict):
        raise StateNotFound("No search result data on the page.")
    items = result.get("ergebnisliste") or []
    jobs: list[Job] = []
    for item in items:
        berufe = [item.get("hauptberuf") or ""] + list(item.get("alleBerufe") or [])
        beruf = next((b for b in berufe if is_elektroniker(b)), "")
        if not beruf and only_elektroniker:
            continue
        beruf = beruf or item.get("hauptberuf") or ""
        loc = _address((item.get("stellenlokationen") or [None])[0])
        jobs.append(Job(
            refnr=item.get("referenznummer") or "",
            title=item.get("stellenangebotsTitel") or "",
            beruf=beruf,
            specialty=specialty_of(beruf),
            company=item.get("firma") or "",
            start=((item.get("eintrittszeitraum") or {}).get("von") or ""),
            published=item.get("datumErsteVeroeffentlichung") or "",
            **loc,
        ))
    facets = ((result.get("facetten") or {}).get("topberuf") or {}).get("counts") or {}
    specialties: dict[str, int] = {}
    for beruf, count in facets.items():
        if is_elektroniker(beruf):
            name = specialty_of(beruf) or OTHER_SPECIALTY
            specialties[name] = specialties.get(name, 0) + int(count)
    return SearchPage(jobs=[j for j in jobs if j.refnr], all_count=len(items),
                      max_results=int(result.get("maxErgebnisse") or 0),
                      page=int(result.get("page") or 1), size=int(result.get("size") or 25),
                      specialties=specialties)


_BILDUNG = {
    "MITTLERE_REIFE_MITTLERER_BILDUNGSABSCHLUSS": "Mittlere Reife",
    "HAUPTSCHULABSCHLUSS": "Hauptschulabschluss",
    "QUALIFIZIERENDER_ERWEITERTER_HAUPTSCHULABSCHLUSS": "Qualifizierter / erweiterter Hauptschulabschluss",
    "QUALIFIZIERTER_ERWEITERTER_HAUPTSCHULABSCHLUSS": "Qualifizierter / erweiterter Hauptschulabschluss",
    "FACHHOCHSCHULREIFE": "Fachhochschulreife",
    "ABITUR_HOCHSCHULREIFE": "Abitur",
    "NICHT_RELEVANT": "nicht relevant",
    "KEINE_ANGABE": "",
}
_WORKING_TIME = [
    ("arbeitszeitVollzeit", "Vollzeit"), ("arbeitszeitTeilzeitVormittag", "Teilzeit vormittags"),
    ("arbeitszeitTeilzeitNachmittag", "Teilzeit nachmittags"), ("arbeitszeitTeilzeitAbend", "Teilzeit abends"),
    ("arbeitszeitTeilzeitFlexibel", "Teilzeit flexibel"),
    ("arbeitszeitSchichtNachtWochenende", "Schicht / Nacht / Wochenende"),
    ("homeofficemoeglich", "Homeoffice möglich"),
]
_ANREDE = {"HERR": "Herr", "FRAU": "Frau", "HERR_FRAU": ""}
_APPLICATION = [
    ("bewerbungPerEMail", "per E-Mail"), ("bewerbungUeberInternet", "online (Website des Arbeitgebers)"),
    ("bewerbungSchriftlich", "schriftlich (Post)"), ("bewerbungTelefonisch", "telefonisch"),
    ("bewerbungPersoenlich", "persönlich"), ("bewerbungUeberArbeitsagenturDe", "über das Portal der BA"),
]


def _label(value: str | None) -> str:
    if not value or value.startswith("KEINE_ANGABE"):
        return ""
    return value.replace("_", " ").capitalize()


def _format_address(adresse: dict | None) -> str:
    adresse = adresse or {}
    street = " ".join(x for x in (adresse.get("strasse"), adresse.get("hausnummer")) if x)
    city = " ".join(x for x in (adresse.get("plz"), adresse.get("ort")) if x)
    parts = [adresse.get("adresszusatz"), adresse.get("ortsteil"), street,
             f"Postfach {adresse['postfach']}" if adresse.get("postfach") else "", city]
    return ", ".join(p for p in parts if p)


def _strip_markdown(text: str) -> str:
    text = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", r"\1 (\2)", text or "")   # [label](url) -> label (url)
    return re.sub(r"[*_#`]+", "", text).strip()


def _find_person(text: str) -> str:
    """'Ihr Ansprechpartner: Herr Max Muster' / 'Frau Dr. Lisa Schmidt' in free text."""
    match = re.search(r"Ansprechpartner(?:in)?\s*(?:ist|:)?\s*[:\-–]?\s*((?:Herr|Frau)?[ \t]*(?:Dr\.[ \t]*)?"
                      r"[A-ZÄÖÜ][\w\-äöüß]+(?:[ \t]+[A-ZÄÖÜ][\w\-äöüß]+){0,3})", text or "")
    if match:
        return match.group(1).strip()
    person = _PERSON_RE.search(text or "")
    return person.group(0) if person else ""


def apply_detail(job: Job, state: dict[str, Any]) -> Job:
    """Fill a Job with everything on its detail page (job ad + employer profile)."""
    from email_finder import extract_emails

    detail = state.get("jobdetail")
    if not isinstance(detail, dict):
        raise StateNotFound("No job detail data on the page.")
    employer = state.get("arbeitgeberdarstellung") or {}

    job.title = detail.get("stellenangebotsTitel") or job.title
    job.company = detail.get("firma") or employer.get("firma") or job.company
    beruf = detail.get("hauptberuf") or ""
    if is_elektroniker(beruf):
        job.beruf, job.specialty = beruf, specialty_of(beruf)
    job.description = detail.get("stellenangebotsBeschreibung") or job.description
    education = detail.get("geforderterBildungsabschluss") or ""
    job.education = _BILDUNG.get(education, _label(education))
    job.start = (detail.get("eintrittszeitraum") or {}).get("von") or job.start
    job.published = detail.get("datumErsteVeroeffentlichung") or job.published
    job.changed = (detail.get("aenderungsdatum") or "")[:10]
    locations = detail.get("stellenlokationen") or []
    if locations:
        for key, value in _address(locations[0]).items():
            if value:
                setattr(job, key, value)
        job.locations = [a for a in (_format_address(loc.get("adresse")) for loc in locations) if a]
    job.pay_per_year = [int(detail[f"ausbildungsverguetungJahr{i}"]) for i in range(1, 5)
                        if isinstance(detail.get(f"ausbildungsverguetungJahr{i}"), (int, float))]
    job.pay_note = _label(detail.get("verguetungsangabe"))
    job.working_time = ", ".join(label for key, label in _WORKING_TIME if detail.get(key))
    job.contract = _label(detail.get("vertragsdauer"))
    job.chiffre = str(detail.get("chiffrenummer") or "")
    job.supported_by_ba = bool(detail.get("istBetreut"))
    job.external_url = detail.get("externeURL") or ""
    job.partner_name = detail.get("allianzpartnerName") or ""
    if job.partner_name == "arbeitsagentur.de":
        job.partner_name = ""

    # Employer profile
    links = employer.get("links") or []
    homepage = next((l.get("url") for l in links if (l.get("art") or "").lower() == "homepage"), None)
    job.homepage = homepage or (links[0].get("url") if links else "") or ""
    job.other_links = [f"{l.get('art') or 'Link'}: {l.get('url')}" for l in links
                       if l.get("url") and l.get("url") != job.homepage]
    job.social_links = [f"{l.get('art') or 'Link'}: {l.get('url')}" for l in employer.get("socialmedialinks") or []
                        if l.get("url")]
    job.employer_description = _strip_markdown(employer.get("beschreibung") or "")
    job.company_size = str(employer.get("betriebsgroesse") or "")
    job.founded = str(employer.get("gruendungsjahr") or "")
    job.headquarters = employer.get("hauptsitz") or ""
    job.benefits = list(employer.get("mitarbeitervorteile") or [])
    job.employer_contact = _strip_markdown(employer.get("kontaktinformationen") or "")

    # E-mail / phone / Ansprechpartner written in the ad or the employer profile.
    if not job.emails:
        for text, source in ((job.description, "BA-Anzeige (Stellentext)"),
                             (job.employer_contact, "BA-Arbeitgeberprofil"),
                             (job.employer_description, "BA-Arbeitgeberprofil")):
            found = extract_emails(text)
            if found:
                job.emails, job.email_source = found, source
                break
    if not job.phones:
        for text in (job.employer_contact, job.description):
            phones = parse_contact_text(text)["phones"]
            if phones:
                job.phones = [f"Telefon: {p}" for p in phones[:2]]
                break
    if not job.contact_person:
        job.contact_person = _find_person(job.description)
    return job


def _phone(number: dict | None) -> str:
    """{laendervorwahl:'+49', vorwahl:'0228', rufnummer:'3911234'} -> '+49 228 3911234'."""
    if not number or not number.get("rufnummer"):
        return ""
    country = (number.get("laendervorwahl") or "").strip()
    area = (number.get("vorwahl") or "").strip()
    if country and area.startswith("0"):
        area = area[1:]
    return " ".join(x for x in (country, area, number["rufnummer"].strip()) if x)


def find_kontaktdaten(data: Any) -> dict | None:
    """The contact data object (with 'angebotskontakt') somewhere inside a JSON response."""
    if isinstance(data, dict):
        if isinstance(data.get("angebotskontakt"), dict):
            return data
        for value in data.values():
            found = find_kontaktdaten(value)
            if found:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_kontaktdaten(value)
            if found:
                return found
    return None


def apply_kontaktdaten(job: Job, kontaktdaten: dict) -> Job:
    """Fill the contact fields from the contact data the page loads after the
    Sicherheitsabfrage (Ansprechpartner, e-mail, phone, address, how to apply)."""
    contact = kontaktdaten.get("angebotskontakt") or {}
    name = contact.get("name") or {}
    person = " ".join(x for x in (_ANREDE.get(contact.get("anrede") or "", ""), name.get("titel"),
                                  name.get("vorname"), name.get("vorsatzwort"), name.get("nachname")) if x)
    if person:
        job.contact_person = person
    job.contact_position = contact.get("position") or job.contact_position
    address = _format_address(contact.get("postadresse"))
    firma = contact.get("firma") or ""
    job.contact_address = ", ".join(x for x in (firma, address) if x) or job.contact_address
    email = (contact.get("emailadresse") or "").strip().lower()
    if email:
        job.emails = [email] + [e for e in job.emails if e != email]
        job.email_source = "BA-Kontaktdaten"
    phones = [f"{label}: {number}" for label, number in (
        ("Telefon", _phone(contact.get("telefonnummer"))), ("Mobil", _phone(contact.get("mobilnummer"))),
        ("Fax", _phone(contact.get("faxnummer")))) if number]
    if phones:
        job.phones = phones
    job.application_methods = [label for key, label in _APPLICATION if kontaktdaten.get(key)]
    job.application_url = kontaktdaten.get("urlBewerbungsformular") or ""
    job.required_documents = _strip_markdown(str(kontaktdaten.get("geforderteAnlagen") or ""))
    return job


def parse_contact_text(text: str) -> dict[str, Any]:
    """Pull e-mail addresses, phone numbers and a contact person out of the
    visible text of the contact box."""
    text = (text or "").replace("\xa0", " ")
    emails = list(dict.fromkeys(m.group(0).rstrip(".") for m in _EMAIL_RE.finditer(text)))
    phones = []
    for match in _PHONE_RE.finditer(text):
        candidate = " ".join(match.group(0).split())
        digits = re.sub(r"\D", "", candidate)
        # Skip postcodes, dates and reference numbers.
        if len(digits) < 7 or re.fullmatch(r"\d{5}", digits) or re.search(r"\d{4}-\d{2}-\d{2}", candidate):
            continue
        if candidate not in phones:
            phones.append(candidate)
    person = _PERSON_RE.search(text)
    return {"emails": emails, "phones": phones, "person": person.group(0) if person else "",
            "text": "\n".join(line.strip() for line in text.splitlines() if line.strip())}
