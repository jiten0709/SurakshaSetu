"""Citations use only this turn's handles and render as sources (TDD §2.5)."""

from datetime import date

import pytest
from compose_support import bundle, chunk

from surakshasetu.compose.citations import (
    CitationError,
    EngineFact,
    Source,
    cited,
    issue,
    render,
    source_list,
)

FIT = EngineFact(
    rule="FIT-01", label="Suitability rules 2026.09.1, FIT-01", content={"fit": "TERM"}
)
HANDLES = issue([chunk("E1"), chunk("E2"), chunk("E3", parent=True)], [FIT])


def test_handles_keep_retrievals_e_numbers_and_issue_r_numbers() -> None:
    assert list(HANDLES.evidence) == ["E1", "E2", "E3"]
    assert dict(HANDLES.engine) == {"R1": FIT}
    assert HANDLES.evidence_map() == {
        "E1": "product:doc:e1:abc123",
        "E2": "product:doc:e2:abc123",
        "E3": "product:doc:e3:abc123",
        "R1": "FIT-01",
    }


def test_cited_lists_every_handle_in_order_with_repeats() -> None:
    assert cited("A [E2]. B [R1, E2]. C [E1 , E3]. Not [E 4] or (E5).") == [
        "E2",
        "R1",
        "E2",
        "E1",
        "E3",
    ]


def test_valid_handles_render_as_sources() -> None:
    cited = render("It pays the nominee [E2]. It fits [R1]. Both apply [E1, E2].", HANDLES)

    assert cited.text == (
        "It pays the nominee [Source: SurakshaTermShield_999N001V02_PolicyWording §2]."
        " It fits [Source: Suitability rules 2026.09.1, FIT-01]."
        " Both apply [Source: SurakshaTermShield_999N001V02_PolicyWording §1]"
        " [Source: SurakshaTermShield_999N001V02_PolicyWording §2]."
    )
    assert cited.handles == ["E2", "R1", "E1"]
    assert [s.section for s in cited.sources] == ["2. S", "1. S"]  # one per chunk, first-cited


@pytest.mark.parametrize("text", ["Made up [E9].", "Old turn [R2].", "Mixed [E1, E7]."])
def test_a_handle_not_issued_this_turn_is_refused(text: str) -> None:
    with pytest.raises(CitationError):
        render(text, HANDLES)


def test_the_source_list_has_title_section_version_date_and_link() -> None:
    source = Source(
        "Suraksha Term Shield: Policy Wording v2",
        "2.1 Death benefit",
        "v2",
        date(2026, 9, 1),
        "uri",
    )

    listed = source_list([source], bundle().templates["en-IN"].recommendation)

    assert listed == (
        "Sources\n"
        "Suraksha Term Shield: Policy Wording v2, 2.1 Death benefit, version v2,"
        " effective 1 Sep 2026: uri"
    )


@pytest.mark.parametrize(
    "content",
    [
        {"sum_assured_inr": "10000000"},
        {"option": {"annual_premium": "12345"}},
        {"options": [{"quote": None}]},
    ],
)
def test_an_engine_fact_carrying_money_never_reaches_the_prompt(content: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="money"):
        issue([], [EngineFact(rule="RANK", label="Ranker", content=content)])
