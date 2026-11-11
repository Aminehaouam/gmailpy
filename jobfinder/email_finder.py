"""
email_finder.py - Find a company's contact e-mail address (pure helpers).

Sources, in this order (see ba_browser.BrowserSession.find_email):
  1. the BA job ad itself: job text, employer info, every e-mail / mailto
     link on the page (BA's own addresses are ignored);
  2. the contact box behind the BA "Sicherheitsabfrage";
  3. the company's website: the homepage linked in the BA ad, a web
     address in the job text, or a Google search (Bing as fallback).
     On the website: start page, then Karriere / Ausbildung / Bewerbung,
     Kontakt and Impressum pages (the Impressum must list an e-mail
     address by German law).

Application addresses (bewerbung@, ausbildung@, karriere@, personal@ ...)
are preferred over general ones (info@, kontakt@); datenschutz@, noreply@
and similar are never used.
"""

from __future__ import annotations

import base64
import re
import unicodedata
from urllib.parse import parse_qs, quote_plus, unquote, urljoin, urlparse

_EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
                       r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,24}(?![\w-])")
_AT = r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\[\s*ät\s*\]|\s+at\s+(?=[\w-]+\s*(?:\[|\()\s*dot))\s*"
_DOT = r"\s*(?:\[\s*(?:dot|punkt)\s*\]|\(\s*(?:dot|punkt)\s*\)|\{\s*(?:dot|punkt)\s*\})\s*"
_OBFUSCATED_RE = re.compile(rf"([A-Za-z0-9._%+\-]+){_AT}([A-Za-z0-9\-]+(?:(?:{_DOT}|\.)[A-Za-z0-9\-]+)+)", re.I)
_URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>()\"'\]\[,;]+", re.I)

# Never useful as an application address.
_BAD_LOCAL = re.compile(r"^(?:no-?reply|do-?not-?reply|dsb|dpo|gdpr|webmaster|postmaster|abuse|"
                        r"example|name|vorname|ihre?\.?email|email|e-mail|beispiel|test)\b", re.I)
# Departments that are not where applications go (anywhere in the local part).
_BAD_PART = re.compile(r"datenschutz|privacy|presse|press\b|newsletter|marketing|sales|vertrieb|"
                       r"rechnung|invoice|buchhaltung|einkauf|support|ombuds|compliance|whistle", re.I)
_BAD_DOMAINS = ("arbeitsagentur.de", "example.com", "example.de", "beispiel.de", "domain.de", "domain.com",
                "sentry.io", "wixpress.com", "w3.org", "schema.org", "googlemail.example")
_IMAGE_TLD = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|bmp|ico|css|js)$", re.I)

# Preferred local parts (lower index = better).
_PREFERRED = [
    r"bewerbung", r"ausbildung", r"azubi", r"karriere", r"career", r"jobs?", r"recruiting", r"personal",
    r"hr", r"talent", r"nachwuchs", r"lehrstelle",
    r"info", r"kontakt", r"contact", r"office", r"mail", r"zentrale", r"service", r"verwaltung",
]

# Job portals, social networks and directories: never the company website.
PORTAL_DOMAINS = (
    "arbeitsagentur.de", "indeed.", "stepstone.", "monster.", "xing.com", "linkedin.com", "kununu.com",
    "glassdoor.", "ausbildung.de", "azubiyo.de", "azubi.de", "aubi-plus.de", "ausbildungsstellen.de",
    "lehrstellen-radar.de", "meinestadt.de", "jobware.de", "stellenanzeigen.de", "jobs.de", "jobvector.de",
    "facebook.com", "instagram.com", "youtube.com", "tiktok.com", "twitter.com", "x.com", "wikipedia.org",
    "northdata.", "firmenwissen.de", "gelbeseiten.de", "dasoertliche.de", "11880.com", "cylex", "yelp.",
    "google.", "bing.com", "duckduckgo.com", "onapply.de", "studyflix.de", "jobboerse.", "talents.",
    "joblift.", "jooble.", "kimeta.de", "stellenonline.de", "yourfirm.de", "hokify.", "absolventa.de",
    "unternehmensregister.de", "handelsregister.de", "bundesanzeiger.de", "wlw.de", "kompass.com",
    "personio.", "softgarden.", "jobs.personio", "join.com", "recruitee.com", "smartrecruiters.com",
)

# Words that say little about which company it is.
_GENERIC_TOKENS = {
    "gmbh", "mbh", "ag", "kg", "co", "ohg", "gbr", "ug", "se", "ek", "ev", "eg", "und", "the", "der", "die",
    "das", "fur", "fuer", "von", "deutsche", "deutschland", "germany", "gruppe", "group", "holding",
    "elektro", "elektrotechnik", "technik", "service", "services", "systeme", "solutions", "industrie",
    "werk", "werke", "betrieb", "international", "haftungsbeschrankt", "ggmbh", "partner",
}


def _fold(text: str) -> str:
    text = (text or "").casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))


def company_tokens(company: str) -> list[str]:
    """Distinctive words of a company name ('Barth & Kerner GmbH' -> barth, kerner)."""
    tokens = re.findall(r"[a-z0-9]+", _fold(company))
    return [t for t in tokens if len(t) >= 3 and t not in _GENERIC_TOKENS]


def domain_of(url: str) -> str:
    host = urlparse(url if "://" in url else "http://" + url).hostname or ""
    return host.lower().removeprefix("www.")


def is_portal(url: str) -> bool:
    host = domain_of(url)
    return not host or any(p in host for p in PORTAL_DOMAINS)


def looks_like_company_site(url: str, company: str, title: str = "") -> bool:
    """Accept a website only if a distinctive word of the company name
    appears in its domain (or, failing that, in the page title)."""
    if is_portal(url):
        return False
    tokens = company_tokens(company)
    if not tokens:
        return False
    host = re.sub(r"[^a-z0-9]", "", _fold(domain_of(url)))
    if any(t in host for t in tokens):
        return True
    folded_title = _fold(title)
    return sum(1 for t in tokens if t in folded_title) >= min(2, len(tokens))


def _clean(email: str) -> str:
    return email.strip().strip(".").lower()


def _usable(email: str) -> bool:
    local, _, domain = email.partition("@")
    if not local or not domain or _IMAGE_TLD.search(email):
        return False
    if any(domain == d or domain.endswith("." + d) for d in _BAD_DOMAINS):
        return False
    if _BAD_LOCAL.match(local) or _BAD_PART.search(local):
        return False
    return not re.fullmatch(r"[0-9a-f]{16,}", local)  # tracking hashes


def extract_emails(text: str = "", html: str = "") -> list[str]:
    """All usable e-mail addresses in visible text and/or HTML (mailto links,
    obfuscated forms like 'info [at] firma [dot] de'), in order found."""
    found: list[str] = []
    sources = [text or ""]
    if html:
        sources.append(unquote(re.sub(r"&#64;|&#x40;", "@", html)).replace("%40", "@"))
    for source in sources:
        for match in _EMAIL_RE.finditer(source):
            found.append(_clean(match.group(0)))
        for match in _OBFUSCATED_RE.finditer(source):
            domain = re.sub(_DOT, ".", match.group(2), flags=re.I)
            found.append(_clean(f"{match.group(1)}@{domain}"))
    result = []
    for email in found:
        if email not in result and _usable(email):
            result.append(email)
    return result


def rank_emails(emails: list[str], site_domain: str = "") -> list[str]:
    """Best application address first."""
    site = domain_of(site_domain) if site_domain else ""

    def score(email: str) -> tuple[int, int, int]:
        local, _, domain = email.partition("@")
        preference = next((i for i, pattern in enumerate(_PREFERRED) if re.match(pattern, local)), len(_PREFERRED))
        same_domain = 0 if site and (domain == site or domain.endswith("." + site) or site.endswith("." + domain)) else 1
        personal = 1 if re.fullmatch(r"[a-z]+[.\-][a-z]+", local) else 0  # firstname.lastname: fine, but later
        return (same_domain if site else 0, preference, personal)

    return sorted(dict.fromkeys(emails), key=score)


def urls_in_text(text: str) -> list[str]:
    urls = []
    for match in _URL_RE.finditer(text or ""):
        url = match.group(0).rstrip(".:!?")
        if not url.lower().startswith("http"):
            url = "https://" + url
        if url not in urls:
            urls.append(url)
    return urls


_LINK_PRIORITY = [
    (re.compile(r"ausbildung|azubi|bewerb|karriere|career|jobs?\b|stellen|nachwuchs", re.I), 0),
    (re.compile(r"kontakt|contact|ansprech", re.I), 1),
    (re.compile(r"impressum|imprint|legal[- ]?notice", re.I), 2),
]


def candidate_links(links: list[tuple[str, str]], base_url: str, limit: int = 5) -> list[str]:
    """Pages of the same website worth visiting, best first:
    Karriere/Ausbildung/Bewerbung, then Kontakt, then Impressum."""
    base = domain_of(base_url)
    ranked: list[tuple[int, str]] = []
    seen = set()
    for text, href in links:
        if not href or href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = urljoin(base_url, href).split("#")[0]
        host = domain_of(url)
        if not (host == base or host.endswith("." + base) or base.endswith("." + host)):
            continue
        if re.search(r"\.(pdf|jpe?g|png|zip|docx?)$", url, re.I) or url in seen:
            continue
        label = f"{text} {urlparse(url).path}"
        for pattern, rank in _LINK_PRIORITY:
            if pattern.search(label):
                ranked.append((rank, url))
                seen.add(url)
                break
    career = [url for rank, url in ranked if rank == 0][:2]
    contact = [url for rank, url in ranked if rank == 1][:1]
    impressum = [url for rank, url in ranked if rank == 2][:1]
    return (career + contact + impressum)[:limit]


def google_url(query: str) -> str:
    return f"https://www.google.com/search?q={quote_plus(query)}&hl=de&num=10"


def bing_url(query: str) -> str:
    return f"https://www.bing.com/search?q={quote_plus(query)}&setlang=de&cc=DE"


def decode_result_link(href: str) -> str:
    """Real target of a search result link (Google /url?q=..., Bing /ck/a?...u=a1<base64>)."""
    if not href:
        return ""
    parsed = urlparse(href)
    query = parse_qs(parsed.query)
    if "google." in (parsed.hostname or "") and parsed.path == "/url":
        return (query.get("q") or query.get("url") or [""])[0]
    if "bing.com" in (parsed.hostname or "") and parsed.path.startswith("/ck/"):
        encoded = (query.get("u") or [""])[0]
        if encoded.startswith("a1"):
            encoded = encoded[2:]
            try:
                return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8", "replace")
            except Exception:
                return ""
    return href


def search_queries(company: str, ort: str) -> list[str]:
    """'YNCORIS', 'Hürth, Rheinland' -> ['YNCORIS Hürth', 'YNCORIS']."""
    place = re.split(r"[,(/]", ort or "")[0].strip()
    queries = [f"{company} {place}".strip(), company.strip()]
    return list(dict.fromkeys(q for q in queries if q))
