"""Plays the red-team suite (content/redteam/, Step 23) against the running stack (`make eval`;
marker redteam). Every attack scripts the models to comply; redteam.judge says whether it got
through, and any success, or any global assertion failing, fails the conversation and the release.

Needs `make up`, `make gateway-up` and `make seed-catalog`; `make eval` passes the DSNs and tokens.
"""

from pathlib import Path

import pytest
from harness import Conversation, load_redteam
from play import play_conversation
from redteam import judge

pytestmark = [pytest.mark.redteam, pytest.mark.asyncio]

CASES = load_redteam()


@pytest.mark.parametrize("conversation", CASES, ids=[c.id for c in CASES])
@pytest.mark.usefixtures("restore_logging")
async def test_red_team_attack(conversation: Conversation, tmp_path: Path) -> None:
    await play_conversation(conversation, tmp_path, kind="redteam", judge=judge)
