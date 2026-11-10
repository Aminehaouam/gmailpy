"""
filename_parser.py - Extract the company name from an application PDF name.

Expected structure (configurable in Settings):

    <Applicant name>_<Application type>_<Job title>_<Company ...>.pdf
    Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf
        -> applicant "Amine Haouam", type "Ausbildung",
           job title "Elektroniker", company "Trompetter"
    Amine_Haouam_Ausbildung_Elektroniker_Barth_und_Kerner_GmbH.pdf
        -> company "Barth und Kerner GmbH"

Rules
-----
* The extension must be .pdf; the original file name is never changed.
* Components are separated by underscores (spaces are accepted as well).
* The first N components are the applicant's name (N = number of words in
  the configured applicant name, default 2).  If an applicant name is
  configured, the components must match it.
* Next comes one of the configured application types (default "Ausbildung"),
  then one of the configured job titles (default "Elektroniker").  Multi-word
  types/titles are supported; the longest configured title wins.
* All remaining components form the company name; underscores become spaces.
  Company names may consist of any number of words.
* Copy markers added by Windows/browsers (" (1)", " - Kopie") and configured
  version markers at the end ("_v2", "_final") are removed.
* If the name does not follow this structure, parsing FAILS and the user is
  asked to type the company name.  The parser never guesses silently.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from matching_engine import has_significant_company_tokens, normalize_text

_SEPARATOR_RE = re.compile(r"[_\s]+")
_COPY_SUFFIX_RES = [
    re.compile(r"\s*\(\d{1,3}\)$"),                                   # "name (1)"
    re.compile(r"\s*-\s*(?:kopie|copy)(?:\s*\(\d{1,3}\))?$", re.I),   # "name - Kopie (2)"
]

# A company name never starts with these words; if it does, the job title in
# the file name is probably longer than the configured one
# ("Elektroniker_fuer_Betriebstechnik_...").  Parsing then fails safely.
_SUSPICIOUS_FIRST_WORDS = frozenset({
    "fuer", "fur", "for", "im", "in", "als", "zum", "zur", "mit", "bei", "und", "and", "of",
})


@dataclass
class ParseResult:
    filename: str
    ok: bool
    company: str | None = None
    applicant: str | None = None
    application_type: str | None = None
    job_title: str | None = None
    error: str | None = None
    # Best guess for the manual-input dialog when parsing fails.
    suggestion: str = ""
    notes: list[str] = field(default_factory=list)


def _norm(token: str) -> str:
    return normalize_text(token).replace(" ", "")


def _split(text: str) -> list[str]:
    return [t for t in _SEPARATOR_RE.split(text.strip()) if t]


class FilenameParser:
    """Configurable parser; see module docstring for the rules."""

    def __init__(self, settings: dict[str, Any]):
        self.expected_applicant = (settings.get("expected_applicant_name") or "").strip()
        expected_tokens = _split(self.expected_applicant.replace("_", " "))
        if expected_tokens:
            self.applicant_parts = len(expected_tokens)
        else:
            self.applicant_parts = max(0, int(settings.get("applicant_name_parts", 2)))
        self.expected_applicant_norm = [_norm(t) for t in expected_tokens]

        self.application_types = self._prepare_sequences(settings.get("application_types", []))
        self.require_application_type = bool(settings.get("require_application_type", True))
        self.job_titles = self._prepare_sequences(settings.get("job_titles", []))
        self.require_job_title = bool(settings.get("require_job_title", True))
        self.ignored_trailing = [
            re.compile(pattern, re.I) for pattern in settings.get("ignored_trailing_tokens", [])
        ]

    @staticmethod
    def _prepare_sequences(values: Sequence[str]) -> list[tuple[str, list[str]]]:
        """Configured words -> (display, normalised token list); longest first."""
        prepared = []
        for value in values:
            tokens = _split(value.replace("_", " "))
            norm = [_norm(t) for t in tokens if _norm(t)]
            if norm:
                prepared.append((value.strip(), norm))
        prepared.sort(key=lambda item: len(item[1]), reverse=True)
        return prepared

    @staticmethod
    def _match_sequence(
        tokens_norm: list[str], pos: int, options: list[tuple[str, list[str]]]
    ) -> tuple[str, int] | None:
        for display, seq in options:
            if tokens_norm[pos:pos + len(seq)] == seq:
                return display, len(seq)
        return None

    def parse(self, filename: str) -> ParseResult:
        name = os.path.basename(filename)
        stem, ext = os.path.splitext(name)
        result = ParseResult(filename=name, ok=False)

        if ext.lower() != ".pdf":
            result.error = "The file is not a PDF."
            return result

        # Remove copy markers such as " (1)" or " - Kopie".
        changed = True
        while changed:
            changed = False
            for regex in _COPY_SUFFIX_RES:
                new_stem = regex.sub("", stem)
                if new_stem != stem:
                    result.notes.append(f"Ignored copy marker '{stem[len(new_stem):].strip()}'")
                    stem, changed = new_stem, True

        tokens = _split(stem)
        tokens_norm = [_norm(t) for t in tokens]
        result.suggestion = self._suggest(tokens)

        expected_layout = "Name_Name_Ausbildung_Beruf_Firma.pdf"
        n = self.applicant_parts
        if len(tokens) <= n:
            result.error = (
                f"The file name has too few parts. Expected a name like {expected_layout}."
            )
            return result

        # 1. Applicant name
        applicant_tokens = tokens[:n]
        result.applicant = " ".join(applicant_tokens)
        if self.expected_applicant_norm and tokens_norm[:n] != self.expected_applicant_norm:
            result.error = (
                f"The file name does not start with the applicant name "
                f"'{self.expected_applicant}' (found '{result.applicant}')."
            )
            return result
        pos = n

        # 2. Application type ("Ausbildung")
        if self.application_types:
            matched = self._match_sequence(tokens_norm, pos, self.application_types)
            if matched:
                result.application_type = " ".join(tokens[pos:pos + matched[1]])
                pos += matched[1]
            elif self.require_application_type:
                expected = ", ".join(d for d, _ in self.application_types)
                found = tokens[pos] if pos < len(tokens) else "nothing"
                result.error = (
                    f"Expected the application type ({expected}) after the applicant name, "
                    f"found '{found}'."
                )
                return result

        # 3. Job title ("Elektroniker")
        if self.job_titles:
            matched = self._match_sequence(tokens_norm, pos, self.job_titles)
            if matched:
                result.job_title = " ".join(tokens[pos:pos + matched[1]])
                pos += matched[1]
            elif self.require_job_title:
                expected = ", ".join(d for d, _ in self.job_titles)
                found = tokens[pos] if pos < len(tokens) else "nothing"
                result.error = (
                    f"Expected a known job title ({expected}) after the application type, "
                    f"found '{found}'. Add the job title in Settings if it is correct."
                )
                return result

        # 4. Company = all remaining components
        company_tokens = tokens[pos:]
        while len(company_tokens) > 1 and any(
            regex.fullmatch(company_tokens[-1]) for regex in self.ignored_trailing
        ):
            result.notes.append(f"Ignored version marker '{company_tokens[-1]}'")
            company_tokens = company_tokens[:-1]

        if not company_tokens:
            result.error = "No company name found after the job title."
            return result
        if _norm(company_tokens[0]) in _SUSPICIOUS_FIRST_WORDS:
            result.error = (
                f"The company name would start with '{company_tokens[0]}', which suggests the "
                "job title is longer than the configured one. Please check the company name "
                "or add the full job title in Settings."
            )
            result.suggestion = " ".join(company_tokens)
            return result

        company = " ".join(company_tokens).strip()
        if not has_significant_company_tokens(company):
            result.error = (
                f"'{company}' is not a usable company name (it contains only a legal form "
                "or no letters)."
            )
            return result

        result.ok = True
        result.company = company
        result.suggestion = company
        return result

    def _suggest(self, tokens: list[str]) -> str:
        """Best-effort default text for the manual company input dialog."""
        n = self.applicant_parts
        rest = tokens[n:] if len(tokens) > n else tokens
        rest_norm = [_norm(t) for t in rest]
        for options in (self.application_types, self.job_titles):
            matched = self._match_sequence(rest_norm, 0, options) if options else None
            if matched:
                rest, rest_norm = rest[matched[1]:], rest_norm[matched[1]:]
        return " ".join(rest)
