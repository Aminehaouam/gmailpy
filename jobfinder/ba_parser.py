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
from urllib.parse import urlencode

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


def _address(location: dict | None) -> dict[str, str]:
    adresse = (location or {}).get("adresse") or {}
    street = " ".join(x for x in (adresse.get("strasse"), adresse.get("hausnummer")) if x)
    return {"street": street, "plz": adresse.get("plz") or "", "ort": adresse.get("ort") or "",
            "region": (adresse.get("region") or "").replace("_", " ").title()}


@dataclass
class Job:
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
    # filled from the detail page
    description: str = ""
    education: str = ""
    homepage: str = ""
    company_size: str = ""
    external_url: str = ""     # job from a partner portal: apply there
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    contact_person: str = ""
    contact_text: str = ""
    contact_status: str = ""   # "", "ok", "none", "external", "captcha-skipped", "error"

    @property
    def url(self) -> str:
        return detail_url(self.refnr)


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


def parse_search(state: dict[str, Any]) -> SearchPage:
    result = state.get("suchergebnis")
    if not isinstance(result, dict):
        raise StateNotFound("No search result data on the page.")
    items = result.get("ergebnisliste") or []
    jobs: list[Job] = []
    for item in items:
        berufe = [item.get("hauptberuf") or ""] + list(item.get("alleBerufe") or [])
        beruf = next((b for b in berufe if is_elektroniker(b)), "")
        if not beruf:
            continue
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
    "FACHHOCHSCHULREIFE": "Fachhochschulreife",
    "ABITUR_HOCHSCHULREIFE": "Abitur",
    "KEINE_ANGABE": "",
}


def apply_detail(job: Job, state: dict[str, Any]) -> Job:
    """Fill a Job with the data of its detail page."""
    detail = state.get("jobdetail")
    if not isinstance(detail, dict):
        raise StateNotFound("No job detail data on the page.")
    employer = state.get("arbeitgeberdarstellung") or {}
    job.description = detail.get("stellenangebotsBeschreibung") or job.description
    education = detail.get("geforderterBildungsabschluss") or ""
    job.education = _BILDUNG.get(education, education.replace("_", " ").title())
    job.start = ((detail.get("eintrittszeitraum") or {}).get("von") or job.start)
    loc = _address((detail.get("stellenlokationen") or [None])[0])
    for key, value in loc.items():
        if value:
            setattr(job, key, value)
    links = employer.get("links") or []
    homepage = next((l.get("url") for l in links if l.get("art") == "Homepage"), None)
    job.homepage = homepage or (links[0].get("url") if links else "") or ""
    job.company_size = str(employer.get("betriebsgroesse") or "")
    job.external_url = detail.get("externeURL") or ""
    job.company = detail.get("firma") or job.company
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
