"""The JS runtime the EXECUTED arms need — and the switch that makes a missing
one a failure rather than a silent pass.

Several contracts in this suite do not read a shipped workflow plan, they RUN
it (``tests/contracts/test_workflow_plan_commands.py``,
``tests/ops/queue/test_queue_drain_contract.py``). That is deliberate: a static
arm can only ask whether the source *mentions* the right words, and a reader
that mentions every right word while handing back the wrong object is exactly
the bug those contracts exist to catch. The executed arm is the one that cannot
be fooled.

Which makes "the executed arm quietly did not run" the failure mode that
matters. It was live: the arms were ``@pytest.mark.skipif(shutil.which("node")
is None)`` and the CI test job had no ``actions/setup-node`` step — they ran
only because ``ubuntu-latest`` happens to ship node. A base-image change would
have turned every executed arm into a skip with the suite still green, leaving
only the static arm that two conforming-LOOKING bad readers already pass.

So the runtime is now *declared*: CI installs node (``actions/setup-node`` on
the ``test`` job) and sets ``HPC_REQUIRE_NODE=1`` beside it, which turns "no
node" from a skip into a hard failure. Locally the default stays a skip, so a
contributor without node still gets a usable suite and still runs every static
arm.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import TYPE_CHECKING, NoReturn

import pytest

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["REQUIRE_NODE_ENV", "node_is_required", "require_node", "run_node"]

#: Set to a truthy value (CI does) to make an absent JS runtime FAIL the
#: executed arms instead of skipping them.
REQUIRE_NODE_ENV = "HPC_REQUIRE_NODE"

#: Values that read as "not required" — so ``HPC_REQUIRE_NODE=0`` in a shell
#: profile does not accidentally arm the gate.
_FALSEY = frozenset({"", "0", "false", "no", "off"})


def node_is_required() -> bool:
    """Whether a missing JS runtime is a failure here. Read at CALL time, so a
    test can monkeypatch the env and pin both halves of the switch."""
    return os.environ.get(REQUIRE_NODE_ENV, "").strip().lower() not in _FALSEY


def require_node() -> str:
    """Path to ``node``, or end the test — skipping or failing per the switch."""
    found = shutil.which("node")
    if found is not None:
        return found
    if node_is_required():
        _fail_missing()
    pytest.skip(
        "no JS runtime available to execute the shipped plan "
        f"(set {REQUIRE_NODE_ENV}=1 to make this a failure, as CI does)"
    )


def _fail_missing() -> NoReturn:
    pytest.fail(
        f"{REQUIRE_NODE_ENV} is set but no `node` is on PATH: the EXECUTED arm of this "
        "contract is the only one a conforming-looking-but-wrong plan cannot pass, so a "
        "silent skip here would be a green suite with the gate switched off. Install a "
        "JS runtime (CI does this with actions/setup-node)."
    )


def run_node(
    script: Path, source: str, *args: str, timeout: float = 60
) -> subprocess.CompletedProcess[str]:
    """Write *source* to *script* and execute it under node with *args*.

    *script* should end in ``.mjs`` — every plan here is an ES module (they open
    with ``export const meta``), and node only treats a file as one by
    extension. The completed process is returned unchecked: a caller that
    EXPECTS a throw (the ``validatePlan`` fire path) reads ``returncode``
    itself.
    """
    node = require_node()
    script.write_text(source, encoding="utf-8")
    return subprocess.run(  # noqa: S603 — a temp script we just wrote, under the found node
        [node, str(script), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
