"""V7's acknowledgment (Step 21): an ack binds only to the registry set and the documents this
render showed, while the registry still holds that set; the hand-off also needs every ack for the
chosen plan to match and its quote valid on the IST date."""

from datetime import UTC, date, datetime
from typing import Any

import pytest
from compose_support import ROP, TERM, option, quote
from runtime_support import seed_set

from surakshasetu.graph.state import DisclosureAck, RecommendationPayload, Shown
from surakshasetu.graph.states.s3 import acks_valid, check_ack, quote_valid

REGISTRY = seed_set(TERM)
SHOWN = Shown(
    registry_version=REGISTRY.registry_version,
    set_sha256=REGISTRY.set_sha256,
    documents={"CIS": "11" * 32, "POLICY_WORDING": "22" * 32},
)


def acked(**update: Any) -> dict[str, Any]:
    return {
        "uin": TERM,
        "registry_version": SHOWN.registry_version,
        "disclosure_set_sha256": SHOWN.set_sha256,
        "document_sha256": dict(SHOWN.documents),
    } | update


def stored(**update: Any) -> DisclosureAck:
    fields = acked(**update)
    return DisclosureAck(
        uin=fields["uin"],
        registry_version=fields["registry_version"],
        disclosure_set_sha256=fields["disclosure_set_sha256"],
        document_sha256=fields["document_sha256"],
        acked_at=datetime(2026, 10, 5, tzinfo=UTC),
    )


def rec(*acks: DisclosureAck) -> RecommendationPayload:
    return RecommendationPayload(
        options=[option(1, TERM), option(2, ROP)],
        ranker_version="ranker-2026.09.1",
        suitability_inputs_sha256="ab" * 32,
        evidence_map={},
        rendered_sha256="cd" * 32,
        shown={TERM: SHOWN},
        acks=list(acks),
    )


def test_an_ack_of_what_was_shown_binds() -> None:
    assert check_ack(acked(), SHOWN, REGISTRY) == []


def test_a_wrong_set_hash_is_refused() -> None:
    assert check_ack(acked(disclosure_set_sha256="00" * 32), SHOWN, REGISTRY) == ["SET_NOT_SHOWN"]


def test_another_registry_version_is_refused() -> None:
    assert check_ack(acked(registry_version="2026.10.1"), SHOWN, REGISTRY) == [
        "REGISTRY_VERSION_NOT_SHOWN"
    ]


def test_a_registry_that_moved_since_the_render_is_refused() -> None:
    moved = REGISTRY.model_copy(update={"registry_version": "2026.10.1", "set_sha256": "ef" * 32})
    assert check_ack(acked(), SHOWN, moved) == ["REGISTRY_MOVED"]


@pytest.mark.parametrize(
    "documents",
    [
        {"CIS": "00" * 32, "POLICY_WORDING": "22" * 32},  # a document hash not shown
        {"CIS": "11" * 32},  # the policy wording not acknowledged
        {"CIS": "11" * 32, "POLICY_WORDING": "22" * 32, "BI": "33" * 32},  # a BI never shown
        None,
    ],
)
def test_a_wrong_or_missing_document_hash_is_refused(documents: Any) -> None:
    assert check_ack(acked(document_sha256=documents), SHOWN, REGISTRY) == ["DOCUMENTS_NOT_SHOWN"]


def test_a_bi_shown_must_be_acknowledged_too() -> None:
    with_bi = SHOWN.model_copy(update={"documents": {**SHOWN.documents, "BI": "33" * 32}})
    assert check_ack(acked(), with_bi, REGISTRY) == ["DOCUMENTS_NOT_SHOWN"]
    assert check_ack(acked(document_sha256=dict(with_bi.documents)), with_bi, REGISTRY) == []


def test_acks_are_valid_only_when_every_ack_for_the_plan_matches() -> None:
    assert acks_valid(rec(stored()), TERM)
    assert not acks_valid(rec(), TERM)  # none yet
    assert not acks_valid(rec(stored(), stored(disclosure_set_sha256="00" * 32)), TERM)
    assert not acks_valid(rec(stored(uin=ROP)), ROP)  # nothing was shown for it


def test_an_expired_quote_blocks_the_hand_off() -> None:
    q = quote(TERM)  # valid until 31 Oct 2026, IST

    assert quote_valid(q, date(2026, 10, 31))
    assert not quote_valid(q, date(2026, 11, 1))
    assert not quote_valid(None, date(2026, 10, 1))
