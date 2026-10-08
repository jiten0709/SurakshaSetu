"""Plays every golden conversation against the running stack (`make eval`; marker golden). The
player is tests/golden/play.py; the global assertions are tests/golden/harness.py.

Needs `make up`, `make gateway-up` and `make seed-catalog`; `make eval` passes the DSNs and tokens.
"""

from pathlib import Path

import pytest
from harness import Conversation, load_conversations
from play import play_conversation

pytestmark = [pytest.mark.golden, pytest.mark.asyncio]

CASES = load_conversations()


@pytest.mark.parametrize("conversation", CASES, ids=[c.id for c in CASES])
@pytest.mark.usefixtures("restore_logging")
async def test_golden_conversation(conversation: Conversation, tmp_path: Path) -> None:
    await play_conversation(conversation, tmp_path)
