"""Company-name extraction from application PDF file names."""

import pytest

from filename_parser import FilenameParser


@pytest.fixture
def parser(parser_settings):
    return FilenameParser(parser_settings)


def test_single_word_company(parser):
    result = parser.parse("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf")
    assert result.ok
    assert result.company == "Trompetter"
    assert result.applicant == "Amine Haouam"
    assert result.application_type == "Ausbildung"
    assert result.job_title == "Elektroniker"
    assert result.filename == "Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf"


def test_multiword_company(parser):
    result = parser.parse("Amine_Haouam_Ausbildung_Elektroniker_Barth_und_Kerner_GmbH.pdf")
    assert result.ok
    assert result.company == "Barth und Kerner GmbH"


@pytest.mark.parametrize("name, expected", [
    ("Amine_Haouam_Ausbildung_Elektroniker_Stadtwerke_Bochum_Netz_GmbH.pdf", "Stadtwerke Bochum Netz GmbH"),
    ("Amine_Haouam_Ausbildung_Elektroniker_Müller_GmbH_&_Co._KG.pdf", "Müller GmbH & Co. KG"),
    ("Amine_Haouam_Ausbildung_Elektroniker_Elektro-Schmidt_e.K..pdf", "Elektro-Schmidt e.K."),
    ("Amine_Haouam_Ausbildung_Elektroniker_ABB.pdf", "ABB"),
])
def test_various_multiword_companies(parser, name, expected):
    result = parser.parse(name)
    assert result.ok, result.error
    assert result.company == expected


def test_original_filename_is_preserved_in_result(parser):
    name = "Amine_Haouam_Ausbildung_Elektroniker_Barth_und_Kerner_GmbH.pdf"
    assert parser.parse(name).filename == name
    # A full path is accepted; only the base name is used.
    import os
    full = os.path.join("Users", "x", "Desktop", "Bewerbungen", name)
    assert parser.parse(full).company == "Barth und Kerner GmbH"


def test_case_insensitive_structure_and_extension(parser):
    result = parser.parse("amine_haouam_AUSBILDUNG_elektroniker_Trompetter.PDF")
    assert result.ok
    assert result.company == "Trompetter"


def test_copy_suffixes_are_removed(parser):
    assert parser.parse("Amine_Haouam_Ausbildung_Elektroniker_Trompetter (1).pdf").company == "Trompetter"
    assert parser.parse("Amine_Haouam_Ausbildung_Elektroniker_Trompetter - Kopie.pdf").company == "Trompetter"
    assert parser.parse("Amine_Haouam_Ausbildung_Elektroniker_Trompetter - Kopie (2).pdf").company == "Trompetter"


def test_version_marker_removed(parser):
    result = parser.parse("Amine_Haouam_Ausbildung_Elektroniker_Trompetter_v2.pdf")
    assert result.company == "Trompetter"
    assert any("v2" in note for note in result.notes)


def test_spaces_and_double_underscores(parser):
    assert parser.parse("Amine Haouam Ausbildung Elektroniker Barth und Kerner GmbH.pdf").company == \
        "Barth und Kerner GmbH"
    assert parser.parse("Amine__Haouam_Ausbildung__Elektroniker_Trompetter.pdf").company == "Trompetter"


@pytest.mark.parametrize("name", [
    "Lebenslauf.pdf",                                          # too few parts
    "Amine_Haouam_Lebenslauf_2026.pdf",                        # no application type
    "Amine_Haouam_Ausbildung_Mechatroniker_Bosch.pdf",         # unknown job title
    "Max_Mustermann_Ausbildung_Elektroniker_Trompetter.pdf",   # different applicant
    "Amine_Haouam_Ausbildung_Elektroniker.pdf",                # no company
    "Amine_Haouam_Ausbildung_Elektroniker_GmbH.pdf",           # only a legal form
    "Amine_Haouam_Ausbildung_Elektroniker_für_Betriebstechnik_Siemens.pdf",  # title longer than configured
    "Amine_Haouam_Ausbildung_Elektroniker_Trompetter.docx",    # not a PDF
])
def test_unexpected_structure_requires_manual_input(parser, name):
    result = parser.parse(name)
    assert not result.ok
    assert result.company is None
    assert result.error


def test_suggestion_for_manual_input(parser):
    result = parser.parse("Amine_Haouam_Ausbildung_Mechatroniker_Bosch.pdf")
    assert not result.ok
    assert "Bosch" in result.suggestion


def test_configurable_job_titles_longest_match_wins(parser_settings):
    parser_settings["job_titles"] = ["Elektroniker", "Elektroniker für Betriebstechnik", "Mechatroniker"]
    parser = FilenameParser(parser_settings)
    assert parser.parse("Amine_Haouam_Ausbildung_Elektroniker_für_Betriebstechnik_Siemens_AG.pdf").company == "Siemens AG"
    assert parser.parse("Amine_Haouam_Ausbildung_Mechatroniker_Bosch.pdf").company == "Bosch"
    assert parser.parse("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf").company == "Trompetter"


def test_any_applicant_name_when_not_configured(parser_settings):
    parser_settings["expected_applicant_name"] = ""
    parser_settings["applicant_name_parts"] = 2
    parser = FilenameParser(parser_settings)
    assert parser.parse("Max_Mustermann_Ausbildung_Elektroniker_Trompetter.pdf").company == "Trompetter"


def test_optional_application_type_and_title(parser_settings):
    parser_settings.update(require_application_type=False, require_job_title=False)
    parser = FilenameParser(parser_settings)
    assert parser.parse("Amine_Haouam_Trompetter.pdf").company == "Trompetter"
    assert parser.parse("Amine_Haouam_Ausbildung_Elektroniker_Trompetter.pdf").company == "Trompetter"
