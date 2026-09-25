import hashlib
from uuid import UUID

from surakshasetu.audit.anchor import leaf, merkle_root


def h(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


LEAVES = [h(bytes([i])) for i in range(5)]


def test_an_empty_day_has_the_empty_hash() -> None:
    assert merkle_root([]) == h(b"")


def test_one_leaf_is_its_own_root() -> None:
    assert merkle_root(LEAVES[:1]) == LEAVES[0]


def test_pairs_hash_left_then_right() -> None:
    assert merkle_root(LEAVES[:2]) == h(LEAVES[0] + LEAVES[1])
    assert merkle_root(LEAVES[:2]) != merkle_root([LEAVES[1], LEAVES[0]])


def test_an_odd_leaf_is_promoted_unchanged() -> None:
    a, b, c, d, e = LEAVES
    assert merkle_root(LEAVES[:3]) == h(h(a + b) + c)
    assert merkle_root(LEAVES) == h(h(h(a + b) + h(c + d)) + e)


def test_known_vector() -> None:
    # Computed once with plain hashlib, independently of merkle_root.
    expected = "5174b138f822e56503c04bce38e368672593b4a2694466c2e60f1216caf234be"
    assert merkle_root(LEAVES).hex() == expected


def test_leaf_binds_the_session_to_its_last_hash() -> None:
    session_id = UUID("0190a0c4-0000-7000-8000-000000000001")
    assert leaf(session_id, bytes(32)) == h(session_id.bytes + bytes(32))
    assert leaf(session_id, bytes(32)) != leaf(UUID(int=0), bytes(32))
