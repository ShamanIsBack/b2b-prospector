"""Tests for deterministic title parsing and confidence scoring."""

from __future__ import annotations

import pytest

from grounded_prospector.extract import (
    extract_location_hint,
    is_plausible_person_name,
    parse_title,
    score_prospect,
    split_name,
)

ROLES = ["MICE", "Director", "Head of"]


class TestParseTitle:
    def test_full_three_part_title(self) -> None:
        parsed = parse_title("Jane Doe - MICE Manager - Dune & Palm Events | LinkedIn")
        assert parsed.name == "Jane Doe"
        assert parsed.headline == "MICE Manager"
        assert parsed.company == "Dune & Palm Events"

    def test_two_part_title_has_no_company(self) -> None:
        parsed = parse_title("Jane Doe - Director of Sales | LinkedIn")
        assert parsed.name == "Jane Doe"
        assert parsed.headline == "Director of Sales"
        assert parsed.company is None

    def test_name_only_title(self) -> None:
        parsed = parse_title("Jane Doe | LinkedIn")
        assert parsed.name == "Jane Doe"
        assert parsed.headline is None

    @pytest.mark.parametrize("dash", ["-", "–", "—"])
    def test_en_and_em_dashes_are_valid_separators(self, dash: str) -> None:
        parsed = parse_title(f"Jane Doe {dash} MICE Manager | LinkedIn")
        assert parsed.name == "Jane Doe"
        assert parsed.headline == "MICE Manager"

    def test_hyphenated_names_survive_because_separators_need_spaces(self) -> None:
        parsed = parse_title("Anne-Marie Al-Futtaim - Head of MICE | LinkedIn")
        assert parsed.name == "Anne-Marie Al-Futtaim"
        assert parsed.headline == "Head of MICE"

    def test_multi_part_headline_keeps_middle_segments_together(self) -> None:
        parsed = parse_title("Jane Doe - Head of MICE - EMEA - Acme Travel | LinkedIn")
        assert parsed.headline == "Head of MICE - EMEA"
        assert parsed.company == "Acme Travel"

    @pytest.mark.parametrize(
        "title",
        [
            "Jane Doe - MICE Manager | Professional Profile | LinkedIn",
            "Jane Doe - MICE Manager | LinkedIn",
            "Jane Doe - MICE Manager | Profil zawodowy | LinkedIn",
        ],
    )
    def test_locale_site_furniture_is_stripped(self, title: str) -> None:
        parsed = parse_title(title)
        assert parsed.name == "Jane Doe"
        assert parsed.headline == "MICE Manager"

    @pytest.mark.parametrize("title", ["", "   ", "|", "LinkedIn", " | LinkedIn"])
    def test_degenerate_titles_return_empty_rather_than_raising(self, title: str) -> None:
        parsed = parse_title(title)
        assert parsed.name is None
        assert parsed.headline is None


class TestSplitName:
    def test_simple_two_token_name(self) -> None:
        assert split_name("Jane Doe") == ("Jane", "Doe")

    def test_gulf_name_particles_are_preserved(self) -> None:
        """A capitalisation-based rule would silently drop 'bin'."""
        assert split_name("Ahmed bin Rashid Al Maktoum") == ("Ahmed", "bin Rashid Al Maktoum")

    def test_single_token_name_has_no_surname(self) -> None:
        assert split_name("Madonna") == ("Madonna", None)

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_missing_name_yields_two_nones(self, value: str | None) -> None:
        assert split_name(value) == (None, None)


class TestIsPlausiblePersonName:
    @pytest.mark.parametrize(
        "name",
        ["Jane Doe", "Ahmed bin Rashid Al Maktoum", "Anne-Marie Dubois"],
    )
    def test_accepts_real_looking_names(self, name: str) -> None:
        assert is_plausible_person_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            None,
            "",
            "Jane",  # single token
            "Top 10 Travel Agencies in Dubai 2026",  # digits
            "The Complete Guide To Booking Incentive Travel In The Middle East",  # too long
        ],
    )
    def test_rejects_page_headings_and_fragments(self, name: str | None) -> None:
        assert not is_plausible_person_name(name)


class TestExtractLocationHint:
    def test_leading_segment_before_a_middot_is_the_location(self) -> None:
        snippet = "Dubai, United Arab Emirates · MICE Manager · Acme · Ten years."
        assert extract_location_hint(snippet) == "Dubai, United Arab Emirates"

    def test_explicit_location_label_is_stripped(self) -> None:
        assert extract_location_hint("Location: Warsaw · Director") == "Warsaw"

    @pytest.mark.parametrize(
        "snippet",
        [
            "500+ connections · MICE Manager",
            "1.2K followers · Events",
            "12 years of experience · Director",
        ],
    )
    def test_counts_and_durations_are_not_locations(self, snippet: str) -> None:
        assert extract_location_hint(snippet) is None

    @pytest.mark.parametrize(
        "snippet",
        [
            "الخبرة: Falcon Bay Travel",  # Arabic "Experience:", seen live
            "Erfahrung: Acme GmbH · Direktor",
            "Ausbildung: Some University",
        ],
    )
    def test_localised_field_labels_are_not_locations(self, snippet: str) -> None:
        """Google localises snippet labels, so an English word list is not enough."""
        assert extract_location_hint(snippet) is None

    def test_bidi_control_marks_are_stripped(self) -> None:
        """Real Gulf profile snippets carry invisible direction marks."""
        rtl, ltr = chr(0x200F), chr(0x200E)
        snippet = f"{rtl}Dubai, United Arab Emirates{ltr} · Director"
        assert extract_location_hint(snippet) == "Dubai, United Arab Emirates"

    def test_prose_leading_segment_is_rejected(self) -> None:
        snippet = "Experienced events professional working across the Gulf region and Europe"
        assert extract_location_hint(snippet) is None

    @pytest.mark.parametrize("snippet", [None, "", "   ", "·"])
    def test_missing_snippet_yields_none(self, snippet: str | None) -> None:
        assert extract_location_hint(snippet) is None

    def test_snippet_without_a_middot_still_yields_a_short_place(self) -> None:
        assert extract_location_hint("Dubai") == "Dubai"


class TestScoreProspect:
    def test_perfect_match_scores_one(self) -> None:
        result = score_prospect(
            raw_title="Jane Doe - MICE Manager - Dune & Palm Events | LinkedIn",
            name="Jane Doe",
            headline="MICE Manager - Dune & Palm Events",
            agency="Dune & Palm Events",
            roles=ROLES,
        )
        assert result.score == 1.0
        assert not result.needs_review
        assert result.reasons == []

    def test_right_person_wrong_company_is_flagged(self) -> None:
        """The dominant false positive: a real person who does not work there."""
        result = score_prospect(
            raw_title="Jane Doe - MICE Manager - Some Other Agency | LinkedIn",
            name="Jane Doe",
            headline="MICE Manager",
            agency="Dune & Palm Events",
            roles=ROLES,
        )
        assert result.needs_review
        assert any("not found in the result" in reason for reason in result.reasons)

    def test_company_stopwords_do_not_block_a_match(self) -> None:
        result = score_prospect(
            raw_title="Jane Doe - Director - Dune & Palm Events LLC | LinkedIn",
            name="Jane Doe",
            headline="Director",
            agency="Dune & Palm Events",
            roles=ROLES,
        )
        assert not result.needs_review

    def test_agency_match_is_case_and_punctuation_insensitive(self) -> None:
        result = score_prospect(
            raw_title="Jane Doe - Director - GulfCo, Events! | LinkedIn",
            name="Jane Doe",
            headline="Director",
            agency="GulfCo Events",
            roles=ROLES,
        )
        assert not result.needs_review

    def test_missing_role_keyword_lowers_score_but_keeps_company_credit(self) -> None:
        result = score_prospect(
            raw_title="Jane Doe - Barista - Dune & Palm Events | LinkedIn",
            name="Jane Doe",
            headline="Barista",
            agency="Dune & Palm Events",
            roles=ROLES,
        )
        assert result.score == pytest.approx(0.75)
        assert any("role keyword" in reason for reason in result.reasons)

    def test_unparseable_title_scores_zero_and_explains_why(self) -> None:
        result = score_prospect(
            raw_title="Top 10 Event Agencies in Dubai",
            name=None,
            headline=None,
            agency="Dune & Palm Events",
            roles=ROLES,
        )
        assert result.score == 0.0
        assert result.needs_review
        assert len(result.reasons) == 4

    def test_company_only_in_the_snippet_scores_but_still_needs_review(self) -> None:
        """A snippet mention may be a former employer, so it cannot clear the gate.

        Observed on a live 433-prospect run: a "Retired Banker" scored full marks
        because the target company appeared somewhere in their snippet.
        """
        result = score_prospect(
            raw_title="Jane Doe - MICE Manager | LinkedIn",
            name="Jane Doe",
            headline="MICE Manager",
            agency="Dune & Palm Events",
            roles=ROLES,
            snippet="Dubai · MICE Manager · Dune & Palm Events · Incentive programmes.",
        )
        assert result.needs_review
        assert any("former employer" in reason for reason in result.reasons)
        # Still outranks a result that never mentions the company at all.
        assert result.score == pytest.approx(0.80)

    def test_a_title_match_outranks_a_snippet_match(self) -> None:
        common = {
            "name": "Jane Doe",
            "headline": "MICE Manager",
            "agency": "Dune & Palm Events",
            "roles": ROLES,
        }
        titled = score_prospect(
            raw_title="Jane Doe - MICE Manager - Dune & Palm Events | LinkedIn", **common
        )
        snipped = score_prospect(
            raw_title="Jane Doe - MICE Manager | LinkedIn",
            snippet="Previously at Dune & Palm Events",
            **common,
        )
        assert titled.score > snipped.score
        assert not titled.needs_review
        assert snipped.needs_review

    def test_score_never_exceeds_one(self) -> None:
        result = score_prospect(
            raw_title="Jane Doe - MICE Director Head of Events - Dune & Palm Events | LinkedIn",
            name="Jane Doe",
            headline="MICE Director Head of Events",
            agency="Dune & Palm Events",
            roles=ROLES,
        )
        assert result.score <= 1.0
