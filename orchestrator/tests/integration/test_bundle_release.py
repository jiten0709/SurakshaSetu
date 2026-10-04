"""Activating a prompt bundle records one CONFIG_RELEASE on the system chain (TDD §4.5), and a
released version never changes."""

import dataclasses

import pytest

from surakshasetu.audit.chain import (
    SYSTEM_SESSION,
    AuditEvent,
    Conn,
    decrypt_payload,
    events,
    verify_session,
)
from surakshasetu.compose.bundle import BundleError, PromptBundle, activate, load_bundle
from surakshasetu.crypto.keys import SYSTEM_KEY_REF, LocalKeyService

pytestmark = pytest.mark.db


@pytest.fixture
def app_db(db: Conn) -> Conn:
    db.execute("SET LOCAL ROLE app_rw")  # the orchestrator's role: audit INSERT/SELECT only
    return db


@pytest.fixture
def bundle() -> PromptBundle:
    return load_bundle("pb-2026.10.3", env="test")


def releases(conn: Conn, bundle: PromptBundle) -> list[AuditEvent]:
    return [
        e
        for e in events(conn, SYSTEM_SESSION)
        if e.event_type == "CONFIG_RELEASE" and e.header.get("version") == bundle.version
    ]


def test_activation_writes_one_config_release(
    app_db: Conn, keys: LocalKeyService, bundle: PromptBundle
) -> None:
    assert activate(app_db, keys, bundle) is True

    [event] = releases(app_db, bundle)
    assert (event.fsm_state, event.pins, event.key_ref) == ("SYSTEM", {}, SYSTEM_KEY_REF)
    assert event.header == {
        "artefact": "prompt_bundle",
        "version": "pb-2026.10.3",
        "sha256": bundle.sha256,
        "approvals_count": 2,
    }
    assert decrypt_payload(keys, event) == {
        "approved_by": [a.model_dump(mode="json") for a in bundle.manifest.approved_by],
        "files": bundle.manifest.files,
    }
    assert verify_session(app_db, SYSTEM_SESSION).ok


def test_activating_the_same_release_again_writes_nothing(
    app_db: Conn, keys: LocalKeyService, bundle: PromptBundle
) -> None:
    activate(app_db, keys, bundle)

    assert activate(app_db, keys, bundle) is False
    assert len(releases(app_db, bundle)) == 1


def test_a_released_version_with_another_hash_is_refused(
    app_db: Conn, keys: LocalKeyService, bundle: PromptBundle
) -> None:
    activate(app_db, keys, bundle)
    changed = dataclasses.replace(bundle, sha256="cd" * 32)

    with pytest.raises(BundleError, match="RELEASED_WITH_OTHER_HASH"):
        activate(app_db, keys, changed)
    assert len(releases(app_db, bundle)) == 1
