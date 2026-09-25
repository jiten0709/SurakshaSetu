import time
import uuid

from surakshasetu.uuid7 import uuid7


def test_version_and_variant_bits() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_ids_strictly_increase_even_within_one_millisecond() -> None:
    ids = [uuid7().int for _ in range(10_000)]
    assert ids == sorted(set(ids))
    # Far fewer distinct milliseconds than ids: the counter, not the clock, ordered most of them.
    assert len({value >> 80 for value in ids}) < len(ids)


def test_timestamp_is_unix_milliseconds() -> None:
    before = time.time_ns() // 1_000_000
    stamp = uuid7().int >> 80
    after = time.time_ns() // 1_000_000
    assert before <= stamp <= after + 1  # +1: a full counter carries into the next millisecond
