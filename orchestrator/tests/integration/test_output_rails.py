"""rails.output.release against the migrated test database, as app_rw: every verdict is a real
GUARD_VERDICT on the session's chain, its detail decrypts under the subject key, and the chain
verifies. The guard route answers through httpx.MockTransport. Run with `make check-db`."""

import hashlib
import json
from typing import Any

import httpx
import pytest

from surakshasetu.audit.chain import Conn, decrypt_payload, events, verify_session
from surakshasetu.compose.bundle import load_bundle
from surakshasetu.compose.citations import issue
from surakshasetu.compose.composer import Rendered
from surakshasetu.config import Settings
from surakshasetu.crypto.keys import LocalKeyService
from surakshasetu.gateway import Gateway, Route
from surakshasetu.rails.output import OutputContext, load_pack, release
from surakshasetu.uuid7 import uuid7

pytestmark = pytest.mark.db

TEMPLATE = "How old are you?"


def guard_says_safe(request: httpx.Request) -> httpx.Response:
    assert json.loads(request.content)["model"] == "guard-input"
    answer = json.dumps({"injection_score": 0.01, "safety": "safe"})
    return httpx.Response(
        200, json={"model": "stub-guard", "choices": [{"message": {"content": answer}}]}
    )


def render(sentence: str | None) -> Rendered:
    text = f"{sentence} {TEMPLATE}" if sentence else TEMPLATE
    return Rendered(text, hashlib.sha256(text.encode()).hexdigest(), [], {}, [], {}, {})


@pytest.mark.asyncio
async def test_every_verdict_is_a_guard_verdict_on_the_sessions_chain(
    db: Conn, keys: LocalKeyService
) -> None:
    db.execute("SET LOCAL ROLE app_rw")  # the orchestrator's role: audit INSERT/SELECT only
    session_id, subject_ref = uuid7(), uuid7()
    key_ref = keys.create_subject_key(subject_ref)
    ctx = OutputContext(
        session_id=session_id,
        turn_id=uuid7(),
        subject_ref=subject_ref,
        fsm_state="S2",
        pins={"prompt_bundle": "pb-2026.10.1"},
        key_ref=key_ref,
        locale="en-IN",
        route=Route.GEN_CONVERSE,
        handles=issue([], []),
        customer_text="I have two children.",
    )
    settings = Settings(_env_file=None, gateway_base_url="http://gateway.test/v1")

    async def regenerate(errors: list[str]) -> str | None:
        assert errors == ['LX-SUP-02 sentence 1: "best plan" is not allowed']
        return "Thanks, that helps."

    async with Gateway(settings, httpx.MockTransport(guard_says_safe)) as gateway:
        released = await release(
            db,
            keys,
            gateway,
            ctx,
            pack=load_pack("2026.09.1"),
            settings=settings.model_copy(update={"verify_sample_rate": 0.0}),
            bundle=load_bundle("pb-2026.10.1", env="test"),
            draft="We will find the best plan.",
            regenerate=regenerate,
            render=render,
        )

    assert (released.kind, released.text) == ("regenerated", f"Thanks, that helps. {TEMPLATE}")
    logged = events(db, session_id)
    headers = [
        (e.event_type, e.header["rail"], e.header["rule_id"], e.header["action"]) for e in logged
    ]
    grounding = ["GR-PRODUCT", "GR-PLACEHOLDER", "GR-NUMBER", "GR-HANDLE", "GR-CITATION"]
    assert headers == [
        ("GUARD_VERDICT", "lexicon", "LX-SUP-02", "regenerate"),
        *[("GUARD_VERDICT", "grounding", rule, "pass") for rule in grounding],
        ("GUARD_VERDICT", "grounding", "GR-NLI", "skipped"),
        ("GUARD_VERDICT", "lexicon", "none", "pass"),
        *[("GUARD_VERDICT", "grounding", rule, "pass") for rule in grounding],
        ("GUARD_VERDICT", "grounding", "GR-NLI", "not_sampled"),
        *[
            ("GUARD_VERDICT", "release", rule, "pass")
            for rule in ("RC-DISCLOSURE", "RC-GUARD", "RC-LEAK", "RC-DUMMY")
        ],
    ]
    assert [e.header.get("pack_version") for e in logged[:2]] == ["2026.09.1", None]
    first: Any = decrypt_payload(keys, logged[0])
    assert first == {"detail": ['LX-SUP-02 sentence 1: "best plan" is not allowed']}
    assert verify_session(db, session_id).ok
