"""#207: opt-in code-iteration safety for the cmd_sha dedup gate.

``cmd_sha`` is PARAMETER identity, not code identity: it is hashed solely
from the materialized swept params (``compute_cmd_sha``), so editing an
executor's body without changing any swept parameter keeps the same
``cmd_sha``. A re-submit then dedups against the prior run and replays the
OLD code — by design (params define the experiment; the executor body is
provenance, captured separately as ``tasks_py_sha`` on the sidecar).

These tests pin the three contracts the issue asks for, all at the
``find_run_by_cmd_sha`` seam (the shared dedup gate):

(a) DEFAULT — dedup still matches on params alone. With the lever off,
    a sidecar whose ``tasks_py_sha`` differs from the current code is
    STILL returned (param-only dedup is unchanged); no exception, and
    the behaviour is byte-for-byte the historical one.
(b) OPT-IN — with ``invalidate_on_code_change=True`` a code-only change
    (same cmd_sha, different ``tasks_py_sha``) is NOT a valid replay
    target, so the lookup returns ``None`` (or an older same-code run),
    forcing a fresh submit.
(c) DRIFT WARNING — when the current ``tasks_py_sha`` is supplied and a
    matched run recorded a different one, a ``UserWarning`` fires that
    names the prior run and points at ``--invalidate-on-code-change`` —
    a safety net that NEVER changes the dedup decision on its own.
"""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING

import pytest

from hpc_agent.state.runs import find_run_by_cmd_sha, write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

# Same swept parameters across every run in these tests → identical
# cmd_sha. Only the executor-body provenance (tasks_py_sha) varies, which
# is the exact situation #207 is about.
_CMD_SHA = "c" * 64
_CODE_V1 = "1" * 64  # tasks.py drift sha BEFORE the executor edit
_CODE_V2 = "2" * 64  # tasks.py drift sha AFTER the executor edit


def _write_run(tmp_path: Path, run_id: str, *, cmd_sha: str, tasks_py_sha: str) -> None:
    """Write a v2 sidecar carrying *cmd_sha* (params) + *tasks_py_sha* (code)."""
    write_run_sidecar(
        tmp_path,
        run_id=run_id,
        cmd_sha=cmd_sha,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 .hpc/_hpc_dispatch.py",
        result_dir_template="results/{task_id}",
        task_count=4,
        tasks_py_sha=tasks_py_sha,
    )


# ─── (a) default behaviour is param-only and unchanged ──────────────────


def test_default_dedups_on_params_even_when_code_changed(tmp_path: Path) -> None:
    """Lever OFF: a matched run whose code differs is STILL returned.

    The dedup key stays parameter identity. Passing no drift sha at all
    is the historical call shape and must keep matching purely on
    cmd_sha — this is the behaviour we are explicitly NOT changing.
    """
    _write_run(tmp_path, "20260101-000000-aaaaaaa", cmd_sha=_CMD_SHA, tasks_py_sha=_CODE_V1)

    # Historical 2-arg call → param-only match.
    hit = find_run_by_cmd_sha(tmp_path, _CMD_SHA)
    assert hit is not None
    assert hit.stem == "20260101-000000-aaaaaaa"


def test_default_with_drift_sha_supplied_still_dedups(tmp_path: Path) -> None:
    """Lever OFF but current drift sha supplied: a code change warns yet
    STILL dedups. The warning is observability; it never flips the
    dedup decision on its own (issue proposal #3)."""
    _write_run(tmp_path, "20260101-000000-aaaaaaa", cmd_sha=_CMD_SHA, tasks_py_sha=_CODE_V1)

    with pytest.warns(UserWarning, match="invalidate-on-code-change"):
        hit = find_run_by_cmd_sha(tmp_path, _CMD_SHA, tasks_py_sha=_CODE_V2)
    assert hit is not None
    assert hit.stem == "20260101-000000-aaaaaaa"


def test_same_code_no_warning_no_invalidation(tmp_path: Path) -> None:
    """No drift when the recorded code matches the current code: the
    lookup returns the run and emits NO warning, lever on or off."""
    _write_run(tmp_path, "20260101-000000-aaaaaaa", cmd_sha=_CMD_SHA, tasks_py_sha=_CODE_V1)

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any UserWarning would fail the test
        hit = find_run_by_cmd_sha(
            tmp_path,
            _CMD_SHA,
            tasks_py_sha=_CODE_V1,
            invalidate_on_code_change=True,
        )
    assert hit is not None
    assert hit.stem == "20260101-000000-aaaaaaa"


# ─── (b) opt-in lever forces a fresh run on a code-only change ───────────


def test_opt_in_invalidates_drifted_match(tmp_path: Path) -> None:
    """Lever ON: a same-params/different-code run is not a replay target,
    so the lookup returns None and the caller submits fresh."""
    _write_run(tmp_path, "20260101-000000-aaaaaaa", cmd_sha=_CMD_SHA, tasks_py_sha=_CODE_V1)

    hit = find_run_by_cmd_sha(
        tmp_path,
        _CMD_SHA,
        tasks_py_sha=_CODE_V2,
        invalidate_on_code_change=True,
    )
    assert hit is None


def test_opt_in_still_matches_older_same_code_run(tmp_path: Path) -> None:
    """Lever ON: invalidation skips only the DRIFTED match. An older run
    with the same params AND the same current code is a legitimate dedup
    target, so scanning continues to it rather than forcing a needless
    resubmit."""
    import os

    # Newer-by-mtime run carries the OLD code (drifted); older-by-mtime
    # run carries the CURRENT code (a valid replay target).
    _write_run(tmp_path, "20260101-000000-oldcode", cmd_sha=_CMD_SHA, tasks_py_sha=_CODE_V2)
    _write_run(tmp_path, "20260102-000000-drifted", cmd_sha=_CMD_SHA, tasks_py_sha=_CODE_V1)
    from hpc_agent.state.runs import run_sidecar_path

    t0 = 1_700_000_000.0
    # Make the drifted run the newest by mtime so it is scanned first.
    os.utime(run_sidecar_path(tmp_path, "20260101-000000-oldcode"), (t0, t0))
    os.utime(run_sidecar_path(tmp_path, "20260102-000000-drifted"), (t0 + 10, t0 + 10))

    hit = find_run_by_cmd_sha(
        tmp_path,
        _CMD_SHA,
        tasks_py_sha=_CODE_V2,
        invalidate_on_code_change=True,
    )
    assert hit is not None
    assert hit.stem == "20260101-000000-oldcode"


def test_opt_in_dedups_when_code_unchanged(tmp_path: Path) -> None:
    """Lever ON but no code change: ordinary param-and-code dedup — the
    run is returned (we only force a fresh run when the code actually
    drifted)."""
    _write_run(tmp_path, "20260101-000000-aaaaaaa", cmd_sha=_CMD_SHA, tasks_py_sha=_CODE_V1)

    hit = find_run_by_cmd_sha(
        tmp_path,
        _CMD_SHA,
        tasks_py_sha=_CODE_V1,
        invalidate_on_code_change=True,
    )
    assert hit is not None
    assert hit.stem == "20260101-000000-aaaaaaa"


# ─── empty/absent recorded drift sha is NOT treated as drift ─────────────


def test_absent_recorded_tasks_py_sha_is_not_drift(tmp_path: Path) -> None:
    """A run whose recorded tasks_py_sha is empty (drift detection was
    disabled for it — e.g. tasks.py was unreadable at submit time) must
    NOT be read as a code change: we cannot prove the code differs, so we
    fall back to param-only dedup and neither warn nor invalidate."""
    _write_run(tmp_path, "20260101-000000-aaaaaaa", cmd_sha=_CMD_SHA, tasks_py_sha="")

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        hit = find_run_by_cmd_sha(
            tmp_path,
            _CMD_SHA,
            tasks_py_sha=_CODE_V2,
            invalidate_on_code_change=True,
        )
    # Param-only dedup still applies; the run is returned despite the lever.
    assert hit is not None
    assert hit.stem == "20260101-000000-aaaaaaa"
