"""The environment-lock leg — U-ENV1 (reproducibility program, 2026-07-17).

Covers the RESOLVED-environment capture the #2 crisis gap needs:

1. **The pure reducer** — ``env_lock_sha`` is stable + source-tagged + refuses an
   empty snapshot; ``resolve_env_lock`` honours the pip_freeze → lockfile →
   python_env order and degrades to could-not-capture (never a raise);
   ``env_drift_disclosure`` is match / drifted / unknown (disclose, never gate).
2. **The sidecar stamp** — ``stamp_run_sidecar_env_lock`` is strictly additive
   (never overwrites a recorded sha), records a could-not-capture status even with
   a null sha (no-silent-caps), and an OLD sidecar without the field reads
   not-captured (backfilled None).

Toy fixtures only — opaque ``pkg==x`` lines, never a real dependency graph.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hpc_agent.state.env_lock import (
    STATUS_CAPTURED,
    STATUS_COULD_NOT_CAPTURE,
    env_drift_disclosure,
    env_lock_sha,
    resolve_env_lock,
)
from hpc_agent.state.runs import (
    read_run_sidecar,
    run_sidecar_path,
    stamp_run_sidecar_env_lock,
    write_run_sidecar,
)

PIP_A = "widget==1.0\ngadget==2.0\n"
PIP_B = "widget==1.1\ngadget==2.0\n"  # a silent bump of one package


# --------------------------------------------------------------------------- #
# 1 — the pure reducer
# --------------------------------------------------------------------------- #
def test_env_lock_sha_is_stable_and_order_insensitive() -> None:
    a = env_lock_sha("pip_freeze", "widget==1.0\ngadget==2.0\n")
    b = env_lock_sha("pip_freeze", "gadget==2.0\n\nwidget==1.0")  # reordered + blank
    assert a == b and len(a) == 64
    # A changed package version moves the sha.
    assert env_lock_sha("pip_freeze", PIP_B) != a


def test_env_lock_sha_folds_in_source_tag() -> None:
    # The SAME normalized lines under different sources never collide.
    assert env_lock_sha("pip_freeze", PIP_A) != env_lock_sha("python_env", PIP_A)


def test_env_lock_sha_refuses_empty_snapshot() -> None:
    with pytest.raises(ValueError, match="empty snapshot"):
        env_lock_sha("pip_freeze", "   \n# comment only\n")


def test_resolve_prefers_pip_freeze_then_lockfile_then_python_env() -> None:
    # All three present → pip_freeze wins.
    snap = resolve_env_lock(pip_freeze=PIP_A, lockfile="lock-x", python_env="python 3.11.9")
    assert snap.resolved and snap.source == "pip_freeze" and snap.status == STATUS_CAPTURED
    # No pip_freeze → lockfile wins.
    snap2 = resolve_env_lock(pip_freeze=None, lockfile="lock-x", python_env="python 3.11.9")
    assert snap2.source == "lockfile"
    # Only python_env → the fallback resolves.
    snap3 = resolve_env_lock(python_env="python 3.11.9")
    assert snap3.source == "python_env" and snap3.sha is not None


def test_resolve_could_not_capture_when_all_empty() -> None:
    snap = resolve_env_lock(pip_freeze="   ", lockfile=None, python_env="")
    assert not snap.resolved
    assert snap.source is None and snap.sha is None
    assert snap.status == STATUS_COULD_NOT_CAPTURE
    assert "could not be resolved" in snap.detail


def test_env_drift_disclosure_match_drifted_unknown() -> None:
    assert env_drift_disclosure("a" * 64, "a" * 64)["status"] == "match"
    drift = env_drift_disclosure("a" * 64, "b" * 64)
    assert drift["status"] == "drifted"
    assert drift["recorded"] == "a" * 64 and drift["current"] == "b" * 64
    # Either side absent → unknown, disclosed, never a refusal.
    assert env_drift_disclosure(None, "a" * 64)["status"] == "unknown"
    assert env_drift_disclosure("a" * 64, None)["status"] == "unknown"
    assert env_drift_disclosure(None, None)["status"] == "unknown"


# --------------------------------------------------------------------------- #
# 2 — the sidecar stamp (additive) + old-record backfill
# --------------------------------------------------------------------------- #
def _write_sidecar(exp: Path, run_id: str, **over: Any) -> None:
    kwargs: dict[str, Any] = {
        "run_id": run_id,
        "cmd_sha": "a" * 64,
        "hpc_agent_version": "0.11.0",
        "submitted_at": "2026-01-01T00:00:00Z",
        "executor": "python train.py",
        "result_dir_template": "results/{task_id}",
        "task_count": 1,
        "tasks_py_sha": "b" * 64,
        "cluster": "widgetcluster",
    }
    kwargs.update(over)
    write_run_sidecar(exp, **kwargs)


def test_old_sidecar_without_field_reads_not_captured(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "run-x")
    raw = json.loads(run_sidecar_path(tmp_path, "run-x").read_text(encoding="utf-8"))
    # Byte-identical to a pre-U-ENV1 sidecar — the field is not written.
    assert "env_lock_sha" not in raw and "env_lock_status" not in raw
    # read_run_sidecar backfills both to None → "environment identity not captured".
    sc = read_run_sidecar(tmp_path, "run-x")
    assert sc["env_lock_sha"] is None and sc["env_lock_status"] is None


def test_stamp_records_sha_and_status(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "run-x")
    sha = env_lock_sha("pip_freeze", PIP_A)
    stamp_run_sidecar_env_lock(tmp_path, "run-x", env_lock_sha=sha, env_lock_status=STATUS_CAPTURED)
    sc = read_run_sidecar(tmp_path, "run-x")
    assert sc["env_lock_sha"] == sha and sc["env_lock_status"] == STATUS_CAPTURED


def test_stamp_is_additive_never_overwrites(tmp_path: Path) -> None:
    _write_sidecar(tmp_path, "run-x")
    first = env_lock_sha("pip_freeze", PIP_A)
    stamp_run_sidecar_env_lock(
        tmp_path, "run-x", env_lock_sha=first, env_lock_status=STATUS_CAPTURED
    )
    # A later stamp with a DIFFERENT sha must not rewrite recorded provenance.
    second = env_lock_sha("pip_freeze", PIP_B)
    stamp_run_sidecar_env_lock(
        tmp_path, "run-x", env_lock_sha=second, env_lock_status=STATUS_CAPTURED
    )
    assert read_run_sidecar(tmp_path, "run-x")["env_lock_sha"] == first


def test_stamp_could_not_capture_records_status_with_null_sha(tmp_path: Path) -> None:
    # No-silent-caps: an unresolvable env records the status even with no sha.
    _write_sidecar(tmp_path, "run-x")
    stamp_run_sidecar_env_lock(
        tmp_path, "run-x", env_lock_sha=None, env_lock_status=STATUS_COULD_NOT_CAPTURE
    )
    sc = read_run_sidecar(tmp_path, "run-x")
    assert sc["env_lock_sha"] is None
    assert sc["env_lock_status"] == STATUS_COULD_NOT_CAPTURE
