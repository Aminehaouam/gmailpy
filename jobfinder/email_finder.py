"""
email_finder.py - Recognise e-mail addresses in text and HTML.

Finds plain addresses, mailto links and the usual obfuscations
('info [at] firma [dot] de', 'bewerbung(at)firma.de', '&#64;').  Addresses
that are never meant for applications (noreply@, datenschutz@, vertrieb@,
arbeitsagentur.de, image file names ...) are ignored.
"""

from __future__ import annotations

import re
from urllib.parse import unquote

_EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9][A-Za-z0-9._%+\-]*@[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?"
                       r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,24}(?![\w-])")
_AT = r"\s*(?:\[\s*at\s*\]|\(\s*at\s*\)|\{\s*at\s*\}|\[\s*ät\s*\]|\s+at\s+(?=[\w-]+\s*(?:\[|\()\s*dot))\s*"
_DOT = r"\s*(?:\[\s*(?:dot|punkt)\s*\]|\(\s*(?:dot|punkt)\s*\)|\{\s*(?:dot|punkt)\s*\})\s*"
_OBFUSCATED_RE = re.compile(rf"([A-Za-z0-9._%+\-]+){_AT}([A-Za-z0-9\-]+(?:(?:{_DOT}|\.)[A-Za-z0-9\-]+)+)", re.I)

# Never useful as an application address.
_BAD_LOCAL = re.compile(r"^(?:no-?reply|do-?not-?reply|dsb|dpo|gdpr|webmaster|postmaster|abuse|"
                        r"example|name|vorname|ihre?\.?email|email|e-mail|beispiel|test)\b", re.I)
# Departments that are not where applications go (anywhere in the local part).
_BAD_PART = re.compile(r"datenschutz|privacy|presse|press\b|newsletter|marketing|sales|vertrieb|"
                       r"rechnung|invoice|buchhaltung|einkauf|support|ombuds|compliance|whistle", re.I)
_BAD_DOMAINS = ("arbeitsagentur.de", "example.com", "example.de", "beispiel.de", "domain.de", "domain.com",
                "sentry.io", "wixpress.com", "w3.org", "schema.org", "googlemail.example")
_IMAGE_TLD = re.compile(r"\.(?:png|jpe?g|gif|svg|webp|bmp|ico|css|js)$", re.I)

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
