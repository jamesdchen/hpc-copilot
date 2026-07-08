"""Contract: the notebook-audit skill pins the relay-due close-the-loop rule.

The relay boundary has two sides — distortion (``verify-relay`` + the Stop
hook's contradiction pass) and SILENCE. Tonight's proving run hit the silence
side: ``notebook-status`` computed ``passed`` and the agent never relayed it,
so the human never saw the verdict. The gate is mechanical (the relay-due
marker + the Stop hook's discharge pass), but the skill prose is the belt to
the gate's suspenders: the agent should relay the terminal verdict because the
loop says so, not only because a block forces it.

That sentence lived nowhere until now. This binds it (the
``test_detached_worker_brief_guidance`` model: prose that a gate depends on is
pinned by a contract test, so dropping any half — the relay-verbatim binding,
the open-loop framing, or the Stop-hook enforcement pointer — fails CI).
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SKILL = _REPO_ROOT / "src/slash_commands/skills/hpc-notebook-audit/SKILL.md"


def test_the_notebook_audit_skill_pins_the_relay_due_close() -> None:
    text = _SKILL.read_text(encoding="utf-8")

    # (a) the close: the loop closes by relaying the notebook-status result
    # VERBATIM — the code-computed verdict, not a paraphrase.
    assert re.search(r"loop closes by relaying the `notebook-status` result verbatim", text), (
        f"{_SKILL.name}: the audit loop must close by relaying the "
        f"`notebook-status` result verbatim — the omission side of rule 10"
    )

    # (b) the framing: an unrelayed terminal state is an OPEN loop.
    assert re.search(r"unrelayed terminal state is an open loop", text), (
        f"{_SKILL.name}: must frame an unrelayed terminal state as an open "
        f"loop — a computed verdict the human never saw is not a finished audit"
    )

    # (c) the enforcement pointer: the Stop hook is the gate behind the prose
    # (the relay-due marker discharge pass in relay_audit_stop).
    assert re.search(r"Stop hook enforces this", text) and "relay-due" in text, (
        f"{_SKILL.name}: must name the Stop hook as the enforcement of the "
        f"relay-due close — prose without the gate pointer invites treating "
        f"the block as a surprise instead of the contract"
    )
