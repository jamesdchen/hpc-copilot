"""Reproduction-receipt dedup lever on ``find_run_by_cmd_sha`` + the
``reproduces`` sidecar provenance field (T1).

``cmd_sha`` is PARAMETER identity (#207), so a deliberate re-run of identical
params would otherwise dedup against — and silently replay — the ORIGINAL run
it means to reproduce. The ``reproduction_of`` lever (the campaign-iteration
lever's sibling) pierces that: a match whose ``run_id`` equals the named
original, OR whose recorded ``reproduces`` equals it (a PRIOR reproduction of
the same original), is skipped, while an UNRELATED same-params prior still
dedups. Lever unset → byte-identical to the historical behaviour.
"""

from __future__ import annotations

import json
import warnings
from typing import TYPE_CHECKING

from hpc_agent.state.runs import find_run_by_cmd_sha, read_run_sidecar, write_run_sidecar

if TYPE_CHECKING:
    from pathlib import Path

_CMD_SHA = "f" * 64


def _write_sidecar(experiment_dir: Path, run_id: str, **fields) -> Path:
    """A minimal on-disk sidecar; identical ``cmd_sha`` → same param identity."""
    target = experiment_dir / ".hpc" / "runs" / f"{run_id}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "sidecar_schema_version": 2,
        "run_id": run_id,
        "cmd_sha": fields.pop("cmd_sha", _CMD_SHA),
        "hpc_agent_version": "0.2.0",
        "submitted_at": "2026-01-01T00:00:00Z",
        "executor": "python3 src/run.py",
        "result_dir_template": "results/{task_id}",
        "task_count": fields.pop("task_count", 4),
        "tasks_py_sha": "1" * 64,
        "job_ids": ["12345"],
    }
    payload.update(fields)
    target.write_text(json.dumps(payload))
    return target


# ─── the lever FIRES (a reproduction skips the run it reproduces) ────────────


def test_reproduction_of_skips_the_original(tmp_path: Path) -> None:
    """Lever set to the original's run_id: the original is NOT a dedup target,
    so with only the original present the scan finds no replay → None (the
    reproduction actually runs)."""
    _write_sidecar(tmp_path, "orig-aaaaaaaa")
    hit = find_run_by_cmd_sha(tmp_path, _CMD_SHA, reproduction_of="orig-aaaaaaaa")
    assert hit is None


def test_reproduction_of_skips_a_prior_reproduction_of_the_same_original(
    tmp_path: Path,
) -> None:
    """A prior reproduction (its sidecar records ``reproduces`` == the original)
    is skipped too — a SECOND reproduction of the same original must not dedup
    against the FIRST one either. Only the original + the prior repro exist, so
    the scan returns None."""
    _write_sidecar(tmp_path, "orig-aaaaaaaa")
    _write_sidecar(tmp_path, "repro1-bbbbbbbb", reproduces="orig-aaaaaaaa")
    hit = find_run_by_cmd_sha(tmp_path, _CMD_SHA, reproduction_of="orig-aaaaaaaa")
    assert hit is None


def test_reproduction_of_leaves_an_unrelated_prior_as_a_dedup_target(
    tmp_path: Path,
) -> None:
    """The lever is surgical: only the named original (and prior repros of it)
    are skipped. An UNRELATED same-params prior — different run_id, no
    ``reproduces`` tag — is still a valid dedup target."""
    _write_sidecar(tmp_path, "orig-aaaaaaaa")
    other = _write_sidecar(tmp_path, "unrelated-cccccccc")
    hit = find_run_by_cmd_sha(tmp_path, _CMD_SHA, reproduction_of="orig-aaaaaaaa")
    assert hit == other


def test_reproduction_of_does_not_skip_a_repro_of_a_DIFFERENT_original(
    tmp_path: Path,
) -> None:
    """A reproduction of a DIFFERENT original (its ``reproduces`` names another
    run) is not skipped by this lever — it is an unrelated same-params prior and
    stays a dedup target."""
    other = _write_sidecar(tmp_path, "repro-of-other-dddddddd", reproduces="some-other-run")
    hit = find_run_by_cmd_sha(tmp_path, _CMD_SHA, reproduction_of="orig-aaaaaaaa")
    assert hit == other


# ─── the lever UNSET is byte-identical to the historical behaviour ───────────


def test_lever_unset_dedups_against_the_original(tmp_path: Path) -> None:
    """No ``reproduction_of``: the historical match-on-identity behaviour —
    the same-params prior is a dedup target."""
    orig = _write_sidecar(tmp_path, "orig-aaaaaaaa")
    assert find_run_by_cmd_sha(tmp_path, _CMD_SHA) == orig


def test_lever_unset_ignores_the_reproduces_field(tmp_path: Path) -> None:
    """A sidecar carrying a ``reproduces`` tag is an ordinary dedup target when
    no lever is passed — the field is inert without the lever."""
    repro = _write_sidecar(tmp_path, "repro1-bbbbbbbb", reproduces="orig-aaaaaaaa")
    assert find_run_by_cmd_sha(tmp_path, _CMD_SHA) == repro


# ─── the `reproduces` field: write / read / omission ─────────────────────────


def test_write_run_sidecar_records_reproduces(tmp_path: Path) -> None:
    """``write_run_sidecar(reproduces=...)`` persists the field so the lever can
    read it back."""
    write_run_sidecar(
        tmp_path,
        run_id="repro-eeeeeeee",
        cmd_sha=_CMD_SHA,
        hpc_agent_version="0.2.0",
        submitted_at="2026-01-01T00:00:00Z",
        executor="python3 src/run.py --seed $SEED",
        result_dir_template="results/{task_id}",
        task_count=4,
        tasks_py_sha="1" * 64,
        reproduces="orig-aaaaaaaa",
    )
    assert read_run_sidecar(tmp_path, "repro-eeeeeeee")["reproduces"] == "orig-aaaaaaaa"


def test_reproduces_none_is_omitted_and_backfills_on_read(tmp_path: Path) -> None:
    """A non-reproduction run (``reproduces=None``) omits the key on write — a
    scope-less/reproduces-less sidecar is byte-identical to one written before
    the field existed — and ``read_run_sidecar`` backfills it to None."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        write_run_sidecar(
            tmp_path,
            run_id="plain-ffffffff",
            cmd_sha=_CMD_SHA,
            hpc_agent_version="0.2.0",
            submitted_at="2026-01-01T00:00:00Z",
            executor="python3 src/run.py --seed $SEED",
            result_dir_template="results/{task_id}",
            task_count=4,
            tasks_py_sha="1" * 64,
        )
    on_disk = json.loads(
        (tmp_path / ".hpc" / "runs" / "plain-ffffffff.json").read_text(encoding="utf-8")
    )
    assert "reproduces" not in on_disk  # omitted on write
    assert read_run_sidecar(tmp_path, "plain-ffffffff")["reproduces"] is None  # backfilled
