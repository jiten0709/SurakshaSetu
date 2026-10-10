"""The pilot DUMMY gate (Step 24, TDD §7.5 step 6; `make verify-release-gate`).

The scripted S0-S3 conversation is replayed with the whole runtime on env=pilot. Every response
carrying DUMMY text must be blocked by rail 8's RC-DUMMY, so none reaches a pilot customer.

Pilot refuses the DUMMY bundle and the DUMMY privacy FAQ at load. That refusal is product code and
stays as it is: the test asserts it first, then (test-only seam, your decision 2026-10-08) loads the
same bundle and FAQ as dev, so the turns reach the rail. A blocked greeting carries no consent
form, so the replay takes the notice in force from the Consent Service, as a client could not.
The DUMMY disclosure sets are never released in pilot, so no acknowledgment can match and the
session must not reach HANDOFF.

Needs `make up`, `make gateway-up` and `make seed-catalog`; marker golden, so `make eval` runs it
too. Everything the replay creates is deleted.
"""

import hashlib
from pathlib import Path
from typing import Any

import pytest
from harness import load_conversations
from play import Play

from surakshasetu.api.app import create_app
from surakshasetu.compose import bundle
from surakshasetu.config import REQUIRED_OUTSIDE_DEV, Settings
from surakshasetu.graph import nodes, runtime, side_query

pytestmark = [pytest.mark.golden, pytest.mark.asyncio]

SCRIPTED = "scripted-s0-s3"


def pilot_settings(tmp_path: Path) -> Settings:
    """Pilot with every dependency the dev run uses (SS_* from `make verify-release-gate`)."""
    dev = Settings(_env_file=None)
    required: dict[str, Any] = {name: getattr(dev, name) for name in REQUIRED_OUTSIDE_DEV}
    required["tsa_key_path"] = tmp_path / "tsa.pem"  # anchoring only; a turn never reads it
    return Settings(
        _env_file=None,
        env="pilot",
        log_level="INFO",
        log_dir=tmp_path,
        rate_limits={60: 60, 3600: 600},  # as play_conversation: machine-speed turns
        **required,
    )


@pytest.mark.usefixtures("restore_logging")
async def test_the_pilot_profile_blocks_every_dummy_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = pilot_settings(tmp_path)
    (conversation,) = [c for c in load_conversations() if c.id == SCRIPTED]

    # The load-time refusals hold in pilot, untouched.
    with pytest.raises(bundle.BundleError) as refused:
        bundle.load_bundle(settings.prompt_bundle, env="pilot")
    assert refused.value.reason == "DUMMY_REFUSED"
    with pytest.raises(side_query.FaqError) as faq_refused:
        side_query.privacy_faq(conversation.locale, "pilot")
    assert faq_refused.value.reason == "DUMMY_REFUSED"

    # ponytail: test-only seam patching the loaders' module globals by name; the bundle and FAQ
    # load as dev, everything else runs as pilot. A new call site of either loader is not patched:
    # it raises DUMMY_REFUSED and fails this test loudly until it is added here.
    load_bundle, privacy_faq = bundle.load_bundle, side_query.privacy_faq

    def bundle_as_dev(version: str, *, env: str, root: Path = bundle.PROMPT_BUNDLES) -> Any:
        return load_bundle(version, env="dev", root=root)

    def faq_as_dev(locale: str, env: str, root: Path = side_query.FAQ_ROOT) -> Any:
        return privacy_faq(locale, "dev", root)

    monkeypatch.setattr(bundle, "load_bundle", bundle_as_dev)  # load_pinned, every turn
    monkeypatch.setattr(runtime, "load_bundle", bundle_as_dev)  # Runtime.open
    monkeypatch.setattr(side_query, "privacy_faq", faq_as_dev)
    nodes.pinned_bundle.cache_clear()
    scripts = load_bundle(settings.prompt_bundle, env="dev").templates[conversation.locale].scripts

    app = create_app(settings)
    try:
        async with app.router.lifespan_context(app):
            run = Play(app, settings, conversation, tmp_path)
            try:
                t = await run.open()
                notice = await run.runtime.domain.get_current_consent_notice(conversation.locale)
                run.form = {
                    "notice_version": notice.notice_version,
                    "notice_sha256": notice.body_sha256,
                }
                for index, turn in enumerate(conversation.turns):
                    await run.play(index, turn, t)
                trail = run.db.execute(
                    "SELECT event_type, fsm_state, header FROM audit.audit_event"
                    " WHERE session_id = %s ORDER BY seq",
                    (run.session_id,),
                ).fetchall()
            finally:
                await run.close()
    finally:
        nodes.pinned_bundle.cache_clear()

    # Rail 8 runs on every release: its RC-DUMMY verdict (score = the DUMMY count) comes just
    # before the turn's RESPONSE_RELEASED, whose rendered hash names the text the client got.
    releases: list[tuple[str, float, str]] = []
    verdicts: list[tuple[str, float]] = []
    handoff = False
    for event_type, _, header in trail:
        if event_type == "GUARD_VERDICT" and header["rule_id"] == "RC-DUMMY":
            verdicts.append((header["action"], header["score"]))
        elif event_type == "RESPONSE_RELEASED":
            releases.append((*verdicts[-1], header["rendered_sha256"]))
        handoff |= event_type == "HANDOFF" or (
            event_type == "STATE_TRANSITION" and header["to_state"] == "HANDOFF"
        )

    blocked_text = f"{scripts.release_blocked}\n{scripts.advisor_offer}"
    released = [(s.turn, s.released) for s in t.sent if s.released is not None]
    assert len(released) == len(releases) == len(verdicts)
    lines = []
    for (index, body), (action, score, rendered) in zip(released, releases, strict=True):
        assert hashlib.sha256(body["message"]["text"].encode()).hexdigest() == rendered
        lines.append(f"turn {index:2d} {body['state']:<10} RC-DUMMY {action} (DUMMY x{score:g})")
        assert "DUMMY" not in body["message"]["text"], f"turn {index} released DUMMY text"
        if action == "block":
            assert body["message"]["text"] == blocked_text, f"turn {index}"

    blocks = sum(action == "block" for action, _ in verdicts)
    with capsys.disabled():
        print("\n" + "\n".join(lines))
        print(f"BLOCK: {blocks} of {len(verdicts)} releases blocked by RC-DUMMY (env=pilot)")

    assert all(a == ("block" if score > 0 else "pass") for a, score in verdicts), verdicts
    assert blocks >= 1, "no release was blocked: the gate never reached the rail"
    assert any(a == "pass" for a, _ in verdicts), "every release was blocked: no clean turn"
    assert not handoff, "a DUMMY disclosure set was acknowledged and handed off in pilot"
