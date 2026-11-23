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


# -- Gastronomie, hotels (and Pflege) -------------------------------------

@pytest.mark.parametrize("name, title, company", [
    # File names of the Gastronomie application skill
    ("Amine_Haouam_Ausbildung_Gastronomie_Krone.pdf", "Gastronomie", "Krone"),
    ("Amine_Haouam_Ausbildung_Gastronomie_Hotel_zur_Post.pdf", "Gastronomie", "Hotel zur Post"),
    # Job names written out - für / fuer / fur all work
    ("Amine_Haouam_Ausbildung_Fachkraft_fur_Restaurants_und_Veranstaltungsgastronomie_Hotel_Adler.pdf",
     "Fachkraft fur Restaurants und Veranstaltungsgastronomie", "Hotel Adler"),
    ("Amine_Haouam_Ausbildung_Fachmann_für_Restaurants_und_Veranstaltungsgastronomie_Seehof.pdf",
     "Fachmann für Restaurants und Veranstaltungsgastronomie", "Seehof"),
    ("Amine_Haouam_Ausbildung_Fachfrau_fuer_Systemgastronomie_Burgerhaus_GmbH.pdf",
     "Fachfrau fuer Systemgastronomie", "Burgerhaus GmbH"),
    ("Amine_Haouam_Ausbildung_Fachkraft_fuer_Gastronomie_Brauhaus_Sion.pdf",
     "Fachkraft fuer Gastronomie", "Brauhaus Sion"),
    ("Amine_Haouam_Ausbildung_Fachkraft_im_Gastgewerbe_Gasthof_Linde.pdf",
     "Fachkraft im Gastgewerbe", "Gasthof Linde"),
    ("Amine_Haouam_Ausbildung_Hotelfachmann_Parkhotel_Bremen.pdf", "Hotelfachmann", "Parkhotel Bremen"),
    ("Amine_Haouam_Ausbildung_Hotelfachfrau_Seehotel_Maier.pdf", "Hotelfachfrau", "Seehotel Maier"),
    ("Amine_Haouam_Ausbildung_Kaufmann_für_Hotelmanagement_Steigenberger.pdf",
     "Kaufmann für Hotelmanagement", "Steigenberger"),
    ("Amine_Haouam_Ausbildung_Restaurantfachmann_Ratskeller.pdf", "Restaurantfachmann", "Ratskeller"),
    ("Amine_Haouam_Ausbildung_Koch_Restaurant_Bella_Italia.pdf", "Koch", "Restaurant Bella Italia"),
    ("Amine_Haouam_Ausbildung_Köchin_Gasthof_Zur_Post.pdf", "Köchin", "Gasthof Zur Post"),
    ("Amine_Haouam_Ausbildung_Kochin_Gasthof_Zur_Post.pdf", "Kochin", "Gasthof Zur Post"),
    ("Amine_Haouam_Ausbildung_Fachkraft_Küche_Landgasthof_Linde.pdf", "Fachkraft Küche", "Landgasthof Linde"),
    ("Amine_Haouam_Ausbildung_Pflegefachmann_Caritas.pdf", "Pflegefachmann", "Caritas"),
    # Elektroniker unchanged
    ("Amine_Haouam_Ausbildung_Elektroniker_Barth_und_Kerner_GmbH.pdf", "Elektroniker", "Barth und Kerner GmbH"),
])
def test_gastronomy_professions(parser, name, title, company):
    result = parser.parse(name)
    assert result.ok, result.error
    assert (result.job_title, result.company) == (title, company)


def test_full_gastronomy_title_wins_over_shorter_one(parser):
    """'Fachkraft fuer Restaurants' is a title too - the full name must win,
    otherwise 'und Veranstaltungsgastronomie' would end up in the company."""
    result = parser.parse("Amine_Haouam_Ausbildung_Fachkraft_fuer_Restaurants_und_Veranstaltungsgastronomie_"
                          "Krone.pdf")
    assert result.company == "Krone"
    assert parser.parse("Amine_Haouam_Ausbildung_Fachkraft_fuer_Restaurants_Krone.pdf").company == "Krone"


def test_unknown_profession_still_fails_with_short_message(parser):
    result = parser.parse("Amine_Haouam_Ausbildung_Baecker_Krone.pdf")
    assert not result.ok and result.company is None
    assert "found 'Baecker'" in result.error and "in Settings" in result.error
    assert len(result.error) < 250


def test_only_part_of_a_job_name_fails_safely(parser):
    """'Fachkraft' alone is no job title: the company would start with 'fuer'."""
    result = parser.parse("Amine_Haouam_Ausbildung_Fachkraft_fuer_Bar_und_Lounge_Krone.pdf")
    assert not result.ok
