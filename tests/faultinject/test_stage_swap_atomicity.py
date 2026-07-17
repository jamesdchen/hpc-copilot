"""Stage-swap atomicity drill — AUDIT rank-3 / U4 (a CONFIRMED OPEN gap).

AUDIT rank 3 (§4) and §7 'Timeout mid stage-swap `cp -a`': the tar-push stage
swap is the ONE transfer-plane write with a non-atomic torn-live-tree window. The
swap is ``cp -a <stage>/. <live>/ && rm -rf <stage>`` — a purely additive MERGE,
not an atomic rename. A drop mid-`cp -a` leaves a PARTIALLY-merged live tree that
a concurrent array could import (every other transfer step is atomic temp+rename
or staged-and-swapped).

The audit rules this closed by step-2 unit U4 (atomic-rename discipline or a
marker-guarded two-phase commit). Until U4 lands there is a genuine window, so
this drill asserts the DOCTRINE (the swap has no partial-merge-into-live window)
and is expected to FAIL. It is marked ``xfail(strict=True)``: when U4 makes the
swap atomic this test XPASSes, and the strict marker turns that xpass into a hard
failure — the mechanical signal that the rank-3 gap is closed and this drill
should be un-xfailed.

The code deliberately uses ``cp -a`` because ``mv`` cannot move a directory onto
an existing non-empty one (the pre-clean preserves protected paths). So closing
this is real design work (U4), not a one-line fix — which is exactly why it is a
tracked xfail rather than an inline TODO.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra import transport


@pytest.mark.xfail(
    strict=True,
    reason=(
        "CONFIRMED GAP — AUDIT rank-3 / U4: the stage swap is a non-atomic "
        "`cp -a <stage>/. <live>/ && rm -rf <stage>` merge, leaving a "
        "partial-live-tree window a concurrent array could import. Flips to XPASS "
        "(hard-fail) when U4 lands an atomic-rename / two-phase-commit swap — the "
        "signal to un-xfail this drill."
    ),
)
def test_stage_swap_has_no_torn_live_tree_window() -> None:
    """DOCTRINE: the swap into the LIVE tree must be all-or-nothing — no command
    that merges file-by-file into the live root (a mid-op drop must never leave a
    partially-updated tree a concurrent reader can observe).
    """
    cmd = transport._stage_swap_cmd("/scratch/run/.hpc_stage", "/scratch/run")
    # A non-atomic merge-copy directly into the live root is the torn window.
    merges_into_live = "cp -a" in cmd and "/. " in cmd
    assert not merges_into_live, (
        "stage swap merges into the live tree file-by-file (`cp -a <stage>/. "
        "<live>/`); a drop mid-copy leaves a partially-merged live tree (rank-3)."
    )
