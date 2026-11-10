"""
matching_engine.py - Decide which Gmail draft belongs to a company.

Safety principles
-----------------
* A match is based on the COMPLETE company name (ignoring capitalisation,
  punctuation, extra whitespace, legal forms such as "GmbH" and connectors
  such as "und"/"&").  Partial or similar-looking names are only ever "weak"
  evidence and are never proposed automatically.
* Evidence is graded by where the name was found:
    strong    - subject, recipient display name, or recipient e-mail domain
    possible  - only the body text, or only the local part of an address
    weak      - only part of the name, a similar spelling, or the name hidden
                inside a longer word
* The newest draft is NEVER preferred because it is the newest.  Candidates
  are ranked by evidence only, and whenever more than one draft is plausible
  the user must choose explicitly.

Outcomes (MatchOutcome.status)
------------------------------
    SINGLE_STRONG    exactly one draft with strong evidence -> confirm it
    SINGLE_POSSIBLE  no strong match, exactly one body-only match -> confirm
                     it with a warning
    MULTIPLE         several plausible drafts -> user must select one
    WEAK_ONLY        only weak/uncertain evidence -> stop, manual action
    NO_MATCH         nothing found -> stop, nothing is attached
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterable, Sequence

if TYPE_CHECKING:  # pragma: no cover
    from gmail_client import DraftInfo

# --------------------------------------------------------------------------
# Text normalisation
# --------------------------------------------------------------------------

# Legal forms and generic corporate suffixes.  They are ignored when matching,
# so "Barth und Kerner GmbH" matches a draft that only says "Barth & Kerner".
LEGAL_FORM_TOKENS = frozenset({
    "gmbh", "mbh", "ggmbh", "ag", "kg", "kgaa", "ohg", "gbr", "ug", "se", "co",
    "ek", "ev", "eg", "inc", "ltd", "llc", "plc", "corp", "bv", "nv", "sa",
    "sarl", "srl", "spa", "partg", "mbb", "haftungsbeschraenkt",
    "haftungsbeschrankt", "limited", "gruppe", "group", "holding",
    "unternehmensgruppe",
})

# Connector words; "Barth & Kerner" == "Barth und Kerner" == "Barth and Kerner".
CONNECTOR_TOKENS = frozenset({"und", "and", "u"})

_TRANSLIT = str.maketrans({
    "ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss", "æ": "ae", "ø": "oe", "œ": "oe",
})
_TOKEN_RE = re.compile(r"[0-9a-z]+")
_DOTTED_ABBREV_RE = re.compile(r"\b([a-z])\.\s?([a-z])\.")


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def fold_variants(text: str) -> tuple[str, str]:
    """Return two normalised spellings of *text*:

    1. umlauts transliterated  (Müller -> mueller)
    2. accents stripped        (Müller -> muller)

    Comparing variant 1 with variant 1 and variant 2 with variant 2 makes
    "Müller", "Mueller" and "MÜLLER" match each other."""
    base = unicodedata.normalize("NFKC", text or "").casefold()
    base = _DOTTED_ABBREV_RE.sub(r"\1\2", base)  # "e.K." -> "ek", "e. V." -> "ev"
    return _strip_accents(base.translate(_TRANSLIT)), _strip_accents(base)


def tokenize(folded: str) -> list[str]:
    """Split already-folded text into alphanumeric tokens."""
    return _TOKEN_RE.findall(folded)


def normalize_text(text: str) -> str:
    """Single canonical normalised form (used for display/equality checks)."""
    return " ".join(tokenize(fold_variants(text)[0]))


def without_connectors(tokens: Iterable[str]) -> list[str]:
    return [t for t in tokens if t not in CONNECTOR_TOKENS]


def significant_tokens(tokens: Iterable[str]) -> list[str]:
    """Company tokens that actually identify the company."""
    return [t for t in tokens if t not in CONNECTOR_TOKENS and t not in LEGAL_FORM_TOKENS]


def has_significant_company_tokens(company: str) -> bool:
    return any(significant_tokens(tokenize(v)) for v in fold_variants(company))


def _contains_sequence(haystack: Sequence[str], needle: Sequence[str]) -> bool:
    """True if *needle* occurs as a contiguous run of whole tokens."""
    n = len(needle)
    if n == 0 or n > len(haystack):
        return False
    first = needle[0]
    for i in range(len(haystack) - n + 1):
        if haystack[i] == first and list(haystack[i:i + n]) == list(needle):
            return True
    return False


def _levenshtein(a: str, b: str, limit: int) -> int:
    """Edit distance between a and b (returns limit+1 as soon as it exceeds limit)."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        row_min = i
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            value = min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost)
            current.append(value)
            row_min = min(row_min, value)
        if row_min > limit:
            return limit + 1
        previous = current
    return previous[-1]


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------

STRONG = "strong"
POSSIBLE = "possible"
WEAK = "weak"
NONE = "none"

_STRENGTH_RANK = {STRONG: 3, POSSIBLE: 2, WEAK: 1, NONE: 0}
_EVIDENCE_POINTS = {STRONG: 100, POSSIBLE: 30, WEAK: 5}

STRENGTH_LABELS = {
    STRONG: "Strong match",
    POSSIBLE: "Possible match",
    WEAK: "Weak / uncertain",
    NONE: "No match",
}


@dataclass(frozen=True)
class Evidence:
    """One reason why a draft might belong to the company."""

    field: str        # "subject", "recipient name", "recipient domain", "recipient address", "body"
    level: str        # STRONG / POSSIBLE / WEAK
    detail: str       # human readable explanation

    def __str__(self) -> str:
        return f"{self.field}: {self.detail}"


@dataclass
class DraftMatch:
    draft: "DraftInfo"
    strength: str = NONE
    score: int = 0
    evidence: list[Evidence] = field(default_factory=list)

    @property
    def strength_label(self) -> str:
        return STRENGTH_LABELS[self.strength]

    def evidence_summary(self) -> str:
        if not self.evidence:
            return "Company name not found in this draft."
        return "; ".join(str(e) for e in self.evidence)


SINGLE_STRONG = "SINGLE_STRONG"
SINGLE_POSSIBLE = "SINGLE_POSSIBLE"
MULTIPLE = "MULTIPLE"
WEAK_ONLY = "WEAK_ONLY"
NO_MATCH = "NO_MATCH"


@dataclass
class MatchOutcome:
    company: str
    status: str
    message: str
    proposed: DraftMatch | None          # only set for SINGLE_STRONG / SINGLE_POSSIBLE
    candidates: list[DraftMatch]         # plausible candidates (strong + possible)
    weak_candidates: list[DraftMatch]    # shown for reference only
    all_matches: list[DraftMatch]        # every draft, ranked by evidence

    @property
    def requires_selection(self) -> bool:
        return self.status == MULTIPLE

    @property
    def is_stop(self) -> bool:
        """True if nothing may be attached without manual intervention."""
        return self.status in (WEAK_ONLY, NO_MATCH)


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

class _CompanyPattern:
    """Pre-computed normalised forms of the company name (both spellings)."""

    def __init__(self, company: str):
        self.company = company
        self.variants: list[dict] = []
        for folded in fold_variants(company):
            tokens = tokenize(folded)
            core = significant_tokens(tokens)
            self.variants.append({
                "core": core,
                "core_compact": "".join(core),
                # compact form incl. connectors: "barthundkerner"
                "compact_with_connectors": "".join(
                    t for t in tokens if t not in LEGAL_FORM_TOKENS
                ),
                "full_tokens": without_connectors(tokens),
            })

    @property
    def valid(self) -> bool:
        return all(v["core"] for v in self.variants)


class MatchingEngine:
    """Scores drafts against a company name and applies the safety rules."""

    def __init__(self, generic_email_domains: Iterable[str] = ()):
        self.generic_domains = {d.strip().lower() for d in generic_email_domains if d.strip()}

    # -- public API -------------------------------------------------------
    def match(self, company: str, drafts: Sequence["DraftInfo"]) -> MatchOutcome:
        pattern = _CompanyPattern(company)
        if not pattern.valid:
            return MatchOutcome(
                company=company,
                status=NO_MATCH,
                message=(
                    f"The company name '{company}' contains no distinctive words "
                    "(only legal forms such as GmbH). Please enter the company name manually."
                ),
                proposed=None, candidates=[], weak_candidates=[],
                all_matches=[DraftMatch(d) for d in drafts],
            )

        matches = [self.score_draft(pattern, draft) for draft in drafts]
        # Rank by evidence only (stable sort keeps Gmail's order for ties).
        # Recency is deliberately NOT a ranking criterion.
        ranked = sorted(matches, key=lambda m: (_STRENGTH_RANK[m.strength], m.score), reverse=True)

        strong = [m for m in ranked if m.strength == STRONG]
        possible = [m for m in ranked if m.strength == POSSIBLE]
        weak = [m for m in ranked if m.strength == WEAK]

        if len(strong) == 1:
            others = len(possible)
            extra = (
                f" {others} other draft(s) mention the company only in the body; "
                "use 'Select Another Draft' to review them." if others else ""
            )
            return MatchOutcome(
                company, SINGLE_STRONG,
                f"One draft clearly matches '{company}'. Review it carefully and confirm.{extra}",
                strong[0], strong + possible, weak, ranked,
            )
        if len(strong) > 1:
            return MatchOutcome(
                company, MULTIPLE,
                f"{len(strong)} drafts match '{company}'. Select the correct draft - "
                "nothing is selected automatically.",
                None, strong + possible, weak, ranked,
            )
        if len(possible) == 1:
            return MatchOutcome(
                company, SINGLE_POSSIBLE,
                f"'{company}' was found only in the text of one draft (not in its subject "
                "or recipient). Check the recipient and subject very carefully before confirming.",
                possible[0], possible, weak, ranked,
            )
        if len(possible) > 1:
            return MatchOutcome(
                company, MULTIPLE,
                f"{len(possible)} drafts mention '{company}' in their text. Select the "
                "correct draft - nothing is selected automatically.",
                None, possible, weak, ranked,
            )
        if weak:
            return MatchOutcome(
                company, WEAK_ONLY,
                f"No reliable match for '{company}'. {len(weak)} draft(s) contain only part of "
                "the name or a similar spelling. Nothing will be attached automatically - "
                "edit the company name or select a draft manually.",
                None, [], weak, ranked,
            )
        return MatchOutcome(
            company, NO_MATCH,
            f"No draft mentions '{company}'. Nothing will be attached.",
            None, [], [], ranked,
        )

    # -- scoring ------------------------------------------------------------
    def score_draft(self, pattern: _CompanyPattern, draft: "DraftInfo") -> DraftMatch:
        result = DraftMatch(draft)
        evidence: list[Evidence] = []

        # Subject
        self._check_text_field(pattern, "subject", draft.subject, STRONG, evidence)

        # Recipients (To, Cc, Bcc)
        for _kind, name, address in draft.recipients:
            if name:
                self._check_text_field(pattern, "recipient name", name, STRONG, evidence,
                                       label=name)
            if address and "@" in address:
                local, _, domain = address.rpartition("@")
                self._check_domain(pattern, domain.lower(), address, evidence)
                self._check_text_field(pattern, "recipient address", local, POSSIBLE, evidence,
                                       label=address, allow_weak=False)

        # Body text (plain text + text extracted from HTML)
        self._check_text_field(pattern, "body", draft.body_text, POSSIBLE, evidence)

        evidence = _dedupe(evidence)
        result.evidence = evidence
        result.score = sum(_EVIDENCE_POINTS[e.level] for e in evidence)

        levels = [e.level for e in evidence]
        medium_fields = {e.field for e in evidence if e.level == POSSIBLE}
        if STRONG in levels or len(medium_fields) >= 2:
            result.strength = STRONG
        elif POSSIBLE in levels:
            result.strength = POSSIBLE
        elif WEAK in levels:
            result.strength = WEAK
        else:
            result.strength = NONE
        return result

    def _check_text_field(
        self,
        pattern: _CompanyPattern,
        field_name: str,
        text: str,
        level_if_full: str,
        evidence: list[Evidence],
        label: str | None = None,
        allow_weak: bool = True,
    ) -> None:
        if not text:
            return
        where = f" ('{_shorten(label)}')" if label else ""
        text_variants = [without_connectors(tokenize(v)) for v in fold_variants(text)]

        # 1. Full company name as whole words -> full evidence.
        for variant, tokens in zip(pattern.variants, text_variants):
            if _contains_sequence(tokens, variant["core"]):
                exact = _contains_sequence(tokens, variant["full_tokens"])
                detail = "exact company name" if exact else "company name (ignoring legal form)"
                evidence.append(Evidence(field_name, level_if_full, f"{detail} found{where}"))
                return

        if not allow_weak:
            return

        # 2. Weak evidence only - never sufficient on its own.
        for variant, tokens in zip(pattern.variants, text_variants):
            token_set = set(tokens)
            core = variant["core"]
            # 2a. Part of a multi-word name.
            if len(core) > 1:
                found = [t for t in core if len(t) >= 4 and t in token_set]
                if found:
                    missing = [t for t in core if t not in token_set]
                    evidence.append(Evidence(
                        field_name, WEAK,
                        f"only part of the name found ('{' '.join(found)}', missing "
                        f"'{' '.join(missing) or 'word order'}'){where}",
                    ))
                    return
            # 2b. Name hidden inside a longer word ("Bau" in "Baumann").
            compact = variant["core_compact"]
            if len(compact) >= 5:
                for t in token_set:
                    if compact in t and t != compact:
                        evidence.append(Evidence(
                            field_name, WEAK, f"name appears inside the word '{t}'{where}"))
                        return
            # 2c. Similar spelling (typo) of a distinctive word.
            for core_token in core:
                if len(core_token) < 5:
                    continue
                limit = 1 if len(core_token) < 9 else 2
                for t in token_set:
                    if t != core_token and abs(len(t) - len(core_token)) <= limit:
                        if _levenshtein(core_token, t, limit) <= limit:
                            evidence.append(Evidence(
                                field_name, WEAK,
                                f"similar spelling '{t}' (not '{core_token}'){where}"))
                            return

    def _check_domain(
        self, pattern: _CompanyPattern, domain: str, address: str, evidence: list[Evidence]
    ) -> None:
        """Compare the recipient's e-mail domain with the company name."""
        if not domain or domain in self.generic_domains:
            return
        labels = [lbl for lbl in domain.split(".") if lbl]
        if len(labels) < 2:
            return
        labels = labels[:-1]  # drop the top-level domain (.de, .com, ...)
        for variant in pattern.variants:
            core = variant["core"]
            compact = variant["core_compact"]
            compact_conn = variant["compact_with_connectors"]
            for lbl in labels:
                parts = [p for p in lbl.split("-") if p]
                lbl_compact = "".join(parts)
                if lbl_compact in (compact, compact_conn) or _contains_sequence(
                    without_connectors(parts), core
                ):
                    evidence.append(Evidence(
                        "recipient domain", STRONG, f"domain matches the company ('{address}')"))
                    return
            for lbl in labels:
                lbl_compact = lbl.replace("-", "")
                if len(compact) >= 5 and lbl_compact != compact and (
                    lbl_compact.startswith(compact) or lbl_compact.endswith(compact)
                ):
                    evidence.append(Evidence(
                        "recipient domain", POSSIBLE,
                        f"domain contains the company name ('{address}')"))
                    return


def _dedupe(evidence: list[Evidence]) -> list[Evidence]:
    """Keep the best evidence per field (and drop exact duplicates)."""
    best: dict[str, Evidence] = {}
    order: list[str] = []
    for item in evidence:
        current = best.get(item.field)
        if current is None:
            best[item.field] = item
            order.append(item.field)
        elif _STRENGTH_RANK[item.level] > _STRENGTH_RANK[current.level]:
            best[item.field] = item
    return [best[f] for f in order]


def _shorten(text: str | None, limit: int = 60) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
