"""Stage-swap atomicity drill — AUDIT rank-3 / U4 (primary CLOSED; fallback residual).

AUDIT rank 3 (§4) and §7 'Timeout mid stage-swap `cp -a`': the tar-push stage
swap was the ONE transfer-plane write with a non-atomic torn-live-tree window.
U4 (a′, STAGE-SWAP-SEAM-MAP.md) closed it on the PRIMARY path: when the login
node has rsync (probed for free on the stage-drop leg), the swap is one
``rsync -a --delete --exclude=<protected> <stage>/ <live>/`` leg — temp+atomic-
rename per file, the pre-clean folded into ``--delete``, no partial-merge
window. The first drill pins that closure and must stay GREEN.

The ``cp -a`` merge (:func:`_stage_swap_cmd`) remains as the rsync-ABSENT
fallback and retains the original torn window there — an ACCEPTED RESIDUAL
(login nodes overwhelmingly carry rsync; the fallback exists for the
pathological host; seam-map drift log 2026-07-17). The second drill pins that
residual as ``xfail(strict=True)``: it XPASSes (hard failure) only if the
fallback is ever made atomic or deleted — the mechanical signal to retire the
fallback's residual entry and this xfail together.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra import transport


def test_primary_stage_swap_has_no_torn_live_tree_window() -> None:
    """DOCTRINE (U4 closure pin): the PRIMARY swap into the LIVE tree is
    atomic-per-file — no command that merges file-by-file into the live root
    via a non-atomic copy.
    """
    cmd = transport._stage_swap_rsync_cmd(
        "/scratch/run/.hpc_stage", "/scratch/run", ["results/", "logs/"]
    )
    assert "rsync" in cmd and "--delete" in cmd
    # The torn window was the non-atomic merge-copy into the live root.
    merges_into_live = "cp -a" in cmd and "/. " in cmd
    assert not merges_into_live, (
        "the PRIMARY stage swap regressed to a file-by-file merge-copy into the "
        "live tree — the rank-3 torn window U4 closed"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "ACCEPTED RESIDUAL — AUDIT rank-3 / U4: the rsync-ABSENT fallback swap "
        "is still the non-atomic `cp -a <stage>/. <live>/ && rm -rf <stage>` "
        "merge, leaving a partial-live-tree window on hosts without rsync. "
        "Flips to XPASS (hard-fail) if the fallback is made atomic or deleted — "
        "the signal to retire this drill."
    ),
)
def test_fallback_stage_swap_has_no_torn_live_tree_window() -> None:
    """The rsync-absent fallback retains the torn window by accepted tradeoff
    (STAGE-SWAP-SEAM-MAP.md drift log, 2026-07-17)."""
    cmd = transport._stage_swap_cmd("/scratch/run/.hpc_stage", "/scratch/run")
    merges_into_live = "cp -a" in cmd and "/. " in cmd
    assert not merges_into_live, (
        "fallback stage swap merges into the live tree file-by-file (`cp -a "
        "<stage>/. <live>/`); accepted residual on rsync-less hosts."
    )
