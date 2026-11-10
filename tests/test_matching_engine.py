"""Draft matching and the safety rules (strong / possible / weak / none)."""

from datetime import datetime, timedelta, timezone

import pytest

from config import DEFAULT_GENERIC_EMAIL_DOMAINS
from gmail_client import DraftInfo
from matching_engine import (
    MULTIPLE,
    NO_MATCH,
    POSSIBLE,
    SINGLE_POSSIBLE,
    SINGLE_STRONG,
    STRONG,
    WEAK,
    WEAK_ONLY,
    MatchingEngine,
    normalize_text,
)

_counter = iter(range(1, 10_000))


def draft(subject="", to=(), body="", cc=(), age_days=0):
    n = next(_counter)
    return DraftInfo(
        draft_id=f"r-{n}", message_id=f"m-{n}", thread_id=f"t-{n}", subject=subject,
        to=[(name, addr) for name, addr in to], cc=list(cc), body_text=body,
        internal_date=datetime.now(timezone.utc) - timedelta(days=age_days),
    )


@pytest.fixture
def engine():
    return MatchingEngine(DEFAULT_GENERIC_EMAIL_DOMAINS)


def test_single_strong_match_by_subject(engine):
    target = draft("Bewerbung um einen Ausbildungsplatz - Trompetter GmbH", [("", "jobs@tb-elektro.de")])
    other = draft("Bewerbung bei Siemens", [("", "jobs@siemens.com")])
    outcome = engine.match("Trompetter", [other, target])
    assert outcome.status == SINGLE_STRONG
    assert outcome.proposed.draft is target
    assert outcome.proposed.strength == STRONG


def test_strong_match_by_recipient_domain(engine):
    target = draft("Bewerbung als Elektroniker", [("", "bewerbung@trompetter.de")])
    outcome = engine.match("Trompetter", [target, draft("Bewerbung", [("", "info@bosch.de")])])
    assert outcome.status == SINGLE_STRONG
    assert outcome.proposed.draft is target
    assert any(e.field == "recipient domain" for e in outcome.proposed.evidence)


def test_strong_match_by_recipient_display_name(engine):
    target = draft("Bewerbung", [("Personalabteilung Barth & Kerner GmbH", "hr@bk-elektro.de")])
    outcome = engine.match("Barth und Kerner GmbH", [target])
    assert outcome.status == SINGLE_STRONG
    assert outcome.proposed.draft is target


def test_multiword_domain_match(engine):
    target = draft("Bewerbung", [("", "info@barth-kerner.de")])
    target2 = draft("Bewerbung", [("", "info@barthundkerner.de")])
    assert engine.match("Barth und Kerner GmbH", [target]).status == SINGLE_STRONG
    assert engine.match("Barth und Kerner GmbH", [target2]).status == SINGLE_STRONG


def test_legal_form_and_spacing_are_ignored(engine):
    target = draft("Bewerbung bei der Trompetter   GmbH & Co. KG", [("", "jobs@example-elektro.de")])
    assert engine.match("Trompetter", [target]).status == SINGLE_STRONG
    target2 = draft("Bewerbung bei TROMPETTER", [("", "x@y.de")])
    assert engine.match("Trompetter GmbH", [target2]).status == SINGLE_STRONG


def test_potential_match_trompetter_gmbh_in_body(engine):
    """Company 'Trompetter' and a draft mentioning 'Trompetter GmbH' only in the body."""
    target = draft("Bewerbung um einen Ausbildungsplatz als Elektroniker", [("", "info@tb-elektro.de")],
                   body="Sehr geehrte Damen und Herren der Trompetter GmbH, hiermit bewerbe ich mich ...")
    outcome = engine.match("Trompetter", [target, draft("Bewerbung", [("", "a@b.de")], body="Hallo")])
    assert outcome.status == SINGLE_POSSIBLE
    assert outcome.proposed.draft is target
    assert outcome.proposed.strength == POSSIBLE


def test_umlaut_transliteration(engine):
    target = draft("Bewerbung bei der Müller GmbH", [("", "info@mueller-elektro.de")])
    assert engine.match("Mueller", [target]).status == SINGLE_STRONG
    assert engine.match("Müller", [target]).status == SINGLE_STRONG
    assert engine.match("MÜLLER", [target]).status == SINGLE_STRONG


def test_multiple_strong_matches_require_selection(engine):
    a = draft("Bewerbung Trompetter (Elektroniker)", [("", "jobs@trompetter.de")])
    b = draft("Nachfrage Trompetter", [("", "info@trompetter.de")])
    outcome = engine.match("Trompetter", [a, b])
    assert outcome.status == MULTIPLE
    assert outcome.proposed is None
    assert {m.draft.draft_id for m in outcome.candidates} == {a.draft_id, b.draft_id}
    assert outcome.requires_selection


def test_multiple_body_only_matches_require_selection(engine):
    a = draft("Bewerbung", [("", "a@x-elektro.de")], body="... bei der Trompetter GmbH ...")
    b = draft("Bewerbung", [("", "b@y-elektro.de")], body="... Firma Trompetter ...")
    outcome = engine.match("Trompetter", [a, b])
    assert outcome.status == MULTIPLE
    assert outcome.proposed is None


def test_no_matching_draft(engine):
    outcome = engine.match("Trompetter", [draft("Bewerbung bei Siemens", [("", "jobs@siemens.com")],
                                                body="Sehr geehrte Damen und Herren")])
    assert outcome.status == NO_MATCH
    assert outcome.proposed is None
    assert outcome.is_stop


def test_no_drafts_at_all(engine):
    outcome = engine.match("Trompetter", [])
    assert outcome.status == NO_MATCH


def test_typo_is_only_weak_and_stops(engine):
    """Fuzzy similarity is never sufficient proof."""
    target = draft("Bewerbung bei Trompeter", [("", "x@y.de")])
    outcome = engine.match("Trompetter", [target])
    assert outcome.status == WEAK_ONLY
    assert outcome.proposed is None
    assert outcome.weak_candidates[0].strength == WEAK
    assert outcome.is_stop


def test_partial_multiword_name_is_only_weak(engine):
    target = draft("Bewerbung bei Barth Elektro", [("", "info@barth-elektro.de")])
    outcome = engine.match("Barth und Kerner GmbH", [target])
    assert outcome.status == WEAK_ONLY
    assert outcome.proposed is None


def test_name_inside_longer_word_is_not_a_match(engine):
    target = draft("Bewerbung bei Baumann", [("", "info@baumann.de")], body="Baumann Elektrotechnik")
    outcome = engine.match("Bau", [target])
    assert outcome.status in (NO_MATCH, WEAK_ONLY)
    assert outcome.proposed is None


def test_newest_draft_is_never_preferred(engine):
    """The newest draft belongs to another company; the older one must win."""
    old_correct = draft("Bewerbung - Trompetter", [("", "jobs@trompetter.de")], age_days=10)
    newest_other = draft("Bewerbung - Siemens", [("", "jobs@siemens.com")], age_days=0,
                         body="Ich interessiere mich sehr für Ihr Unternehmen.")
    outcome = engine.match("Trompetter", [newest_other, old_correct])
    assert outcome.proposed.draft is old_correct


def test_newest_draft_not_chosen_among_ambiguous(engine):
    a = draft("Bewerbung Trompetter", [("", "a@trompetter.de")], age_days=0)
    b = draft("Bewerbung Trompetter", [("", "b@trompetter.de")], age_days=5)
    outcome = engine.match("Trompetter", [a, b])
    assert outcome.status == MULTIPLE and outcome.proposed is None


def test_generic_mail_provider_domain_is_not_evidence(engine):
    # "gmx" in gmx.de must not match a company called "GMX Elektro" through the domain
    target = draft("Bewerbung", [("", "max@gmx.de")])
    outcome = engine.match("GMX", [target])
    assert outcome.status in (NO_MATCH, WEAK_ONLY)


def test_local_part_and_body_together_are_strong(engine):
    target = draft("Bewerbung", [("", "trompetter.ausbildung@gmail.com")], body="Liebe Trompetter GmbH")
    outcome = engine.match("Trompetter", [target])
    assert outcome.status == SINGLE_STRONG


def test_single_strong_wins_over_body_mentions(engine):
    strong = draft("Bewerbung bei Siemens", [("", "jobs@siemens.com")])
    mention = draft("Bewerbung bei Bosch", [("", "jobs@bosch.de")], body="Praktikum bei Siemens absolviert")
    outcome = engine.match("Siemens", [mention, strong])
    assert outcome.status == SINGLE_STRONG
    assert outcome.proposed.draft is strong
    assert len(outcome.candidates) == 2  # the body mention is listed as an alternative


def test_company_with_only_legal_form_is_rejected(engine):
    outcome = engine.match("GmbH", [draft("GmbH", [("", "a@gmbh.de")])])
    assert outcome.status == NO_MATCH
    assert outcome.proposed is None


def test_normalize_text():
    assert normalize_text("  Barth   &  Kerner, GmbH!") == "barth kerner gmbh"
    assert normalize_text("Müller e.K.") == "mueller ek"
