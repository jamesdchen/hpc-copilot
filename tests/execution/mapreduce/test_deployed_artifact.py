"""The deployed-combiner artifact contract (U5).

Covers the ONE definition every combine and deploy leg now shares: where the
artifact lives, what its bytes hash to, the two shell fragments (guard +
probe), and the readers that turn their output back into a verdict.

The shell fragments are exercised through a REAL ``sh``, not asserted as
strings. A quoting or field-position bug in a snippet that is only ever
string-compared ships happily — the first cut of the probe extracted the
FILENAME instead of the digest (``sha256sum`` prints ``<hash>  <file>``, so
``$NF`` is the path) and every string assertion in the world would have passed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hpc_agent.execution.mapreduce import deployed_artifact as D

_SH = shutil.which("sh") or shutil.which("bash")
requires_sh = pytest.mark.skipif(_SH is None, reason="no POSIX shell available")


def _deploy_root(tmp_path: Path, *, contents: bytes | None) -> Path:
    """A fake deploy root; *contents* ``None`` means the artifact is absent."""
    root = tmp_path / "remote"
    (root / ".hpc").mkdir(parents=True, exist_ok=True)
    target = root / D.COMBINER_REL
    if contents is None:
        if target.exists():
            target.unlink()
    else:
        target.write_bytes(contents)
    return root


def _run(script: str) -> subprocess.CompletedProcess[str]:
    assert _SH is not None
    return subprocess.run(  # noqa: S603 — fixed argv, snippet under test
        [_SH, "-c", script], capture_output=True, text=True, check=False, timeout=60
    )


# ── identity ────────────────────────────────────────────────────────────────


def test_local_sha_matches_the_deploy_item_sha() -> None:
    """The sha we compare against MUST be the sha the deploy cache records.

    ``_build_deploy_items`` mints ``.hpc/_hpc_combiner.py`` from
    ``execution/mapreduce/combiner.py``; if the two ever computed a digest over
    different bytes, the presence-aware cache would either re-ship on every
    deploy (always "stale") or never (always "matches"). Both are silent.
    """
    from hpc_agent.infra.transport._deploy_items import _build_deploy_items

    items = {it.dst_rel: it.sha for it in _build_deploy_items(scheduler="slurm")}
    assert D.COMBINER_REL in items, "the deploy no longer ships the combiner under this path"
    assert items[D.COMBINER_REL] == D.local_combiner_sha()


def test_combiner_source_path_is_the_shipped_module() -> None:
    src = D.combiner_source_path()
    assert src.name == "combiner.py"
    assert src.is_file()
    assert hashlib.sha256(src.read_bytes()).hexdigest() == D.local_combiner_sha()


# ── probe verdicts ──────────────────────────────────────────────────────────


def test_probe_states_discriminate_absent_stale_present_unknown() -> None:
    expected = D.local_combiner_sha()
    absent = D.CombinerProbe(present=False, sha=None, expected_sha=expected)
    stale = D.CombinerProbe(present=True, sha="0" * 64, expected_sha=expected)
    present = D.CombinerProbe(present=True, sha=expected, expected_sha=expected)
    unknown = D.CombinerProbe(present=True, sha=None, expected_sha=expected)

    assert (absent.state, stale.state, present.state, unknown.state) == (
        "absent",
        "stale",
        "present",
        "present",
    )
    assert absent.needs_redeploy and stale.needs_redeploy
    assert not present.needs_redeploy
    # Absence of a sha is absence of evidence — an unhashable login node must
    # NOT become a permanent cache miss.
    assert not unknown.needs_redeploy
    assert not unknown.sha_known
    assert not unknown.matches and not unknown.stale


def test_split_returns_none_and_untouched_stdout_when_no_probe_line() -> None:
    """No probe line is UNKNOWN, never 'absent' — the fail-open contract."""
    payload = '{"files": {"a": "b"}}'
    probe, rest = D.split_combiner_probe(payload)
    assert probe is None
    assert rest == payload


def test_split_handles_a_probe_glued_to_a_newline_less_payload() -> None:
    """The bug this reader was rewritten for.

    A run sidecar is ``cat``-ed verbatim and ``json.dump`` writes no trailing
    newline, so a probe folded after it lands on the SAME line. A
    ``startswith`` reader sees no probe (UNKNOWN — the gate silently opens)
    AND hands ``{...}__HPC_COMBINER_SHA__ absent`` to ``json.loads``, which
    raises. Both halves are wrong, and both were live before the reader
    matched by substring and kept the head.
    """
    payload = '{"task_count": 2}'
    glued = f"{payload}{D.COMBINER_PROBE_PREFIX} absent"
    probe, rest = D.split_combiner_probe(glued)

    assert probe is not None and probe.state == "absent"
    assert rest == payload
    assert json.loads(rest) == {"task_count": 2}


def test_split_tolerates_the_leading_blank_line_the_emitter_adds() -> None:
    payload = '{"task_count": 2}'
    probe, rest = D.split_combiner_probe(
        f"{payload}\n{D.COMBINER_PROBE_PREFIX} {D.local_combiner_sha()}\n"
    )

    assert probe is not None and probe.matches
    assert json.loads(rest) == {"task_count": 2}


# ── the shell fragments, through a real shell ───────────────────────────────


@requires_sh
def test_probe_reports_the_real_digest_not_the_filename(tmp_path: Path) -> None:
    """The regression that motivates running these through a shell at all."""
    src = D.combiner_source_path()
    root = _deploy_root(tmp_path, contents=src.read_bytes())
    proc = _run(D.combiner_probe_snippet(root=root.as_posix()))

    assert proc.returncode == 0
    probe, rest = D.split_combiner_probe(proc.stdout)
    assert probe is not None
    assert probe.sha == D.local_combiner_sha()
    assert probe.matches and probe.state == "present"
    assert rest.strip() == ""


@requires_sh
def test_probe_reports_absent_and_stale(tmp_path: Path) -> None:
    root = _deploy_root(tmp_path, contents=None)
    probe, _ = D.split_combiner_probe(_run(D.combiner_probe_snippet(root=root.as_posix())).stdout)
    assert probe is not None and probe.state == "absent" and probe.needs_redeploy

    _deploy_root(tmp_path, contents=b"not the combiner\n")
    probe2, _ = D.split_combiner_probe(_run(D.combiner_probe_snippet(root=root.as_posix())).stdout)
    assert probe2 is not None and probe2.state == "stale" and probe2.needs_redeploy


@requires_sh
def test_probe_folds_in_without_disturbing_the_payload_stdout(tmp_path: Path) -> None:
    """It is folded into execs whose stdout someone else parses (a manifest
    ``cat``, a ``MISSING:`` scan). Peeling the line back off must be exact."""
    root = _deploy_root(tmp_path, contents=D.combiner_source_path().read_bytes())
    payload = '{"pkg_version": "1.2.3", "files": {}}'
    script = D.combiner_probe_snippet(root=root.as_posix()) + f"printf '%s\\n' '{payload}'"
    probe, rest = D.split_combiner_probe(_run(script).stdout)

    assert probe is not None and probe.present
    assert rest.strip() == payload


@requires_sh
def test_guard_is_transparent_when_the_artifact_is_present(tmp_path: Path) -> None:
    root = _deploy_root(tmp_path, contents=b"#!/usr/bin/env python3\n")
    script = f"cd {root.as_posix()} && {D.combiner_guard_snippet()}echo RAN"
    proc = _run(script)

    assert proc.returncode == 0
    assert proc.stdout.strip() == "RAN"
    assert D.COMBINER_ABSENT_SENTINEL not in proc.stderr


@requires_sh
def test_guard_refuses_by_sentinel_and_rc_when_absent(tmp_path: Path) -> None:
    root = _deploy_root(tmp_path, contents=None)
    script = f"cd {root.as_posix()} && {D.combiner_guard_snippet()}echo RAN"
    proc = _run(script)

    assert proc.returncode == D.COMBINER_ABSENT_RC
    assert "RAN" not in proc.stdout, "the combiner invocation must not be reached"
    assert D.combiner_absent_in(proc.stdout, proc.stderr)


@requires_sh
def test_guard_keeps_the_and_chain_so_a_bad_cd_still_gates(tmp_path: Path) -> None:
    """The guard ends in ``&&``, not ``;``.

    With ``;`` a failed ``cd`` would stop gating the rest of the line and the
    combiner would run in the login shell's home directory — the guard would
    have WEAKENED the very chain it was added to strengthen.
    """
    missing = (tmp_path / "definitely-not-here").as_posix()
    proc = _run(f"cd {missing} && {D.combiner_guard_snippet()}echo RAN")

    assert proc.returncode != 0
    assert "RAN" not in proc.stdout


def test_absent_detection_is_sentinel_keyed_not_prose_keyed() -> None:
    """A user's combiner printing 'No such file' is not a deploy dropout."""
    assert not D.combiner_absent_in("", "python3: No such file or directory")
    assert not D.combiner_absent_in("[combiner] ERROR: no _combiner/x/wave_*.json", "")
    assert D.combiner_absent_in("", f"{D.COMBINER_ABSENT_SENTINEL} .hpc/_hpc_combiner.py")
    # The fused batch folds stderr into stdout (``2>&1``) — scan both streams.
    assert D.combiner_absent_in(f"{D.COMBINER_ABSENT_SENTINEL} x", "")


def test_redeploy_command_names_a_real_verb() -> None:
    """The remediation must name a command that EXISTS.

    The whole point of the verb is that the 2026-07-30 incident had no command
    to name, so a human ran scp by hand. A remediation string that drifts off a
    registered verb regresses to exactly that.
    """
    import hpc_agent
    from hpc_agent._kernel.registry.primitive import get_registry

    hpc_agent.register_primitives()
    cmd = D.redeploy_command(experiment_dir="/exp", run_id="ml_abcd1234")
    assert cmd.startswith("hpc-agent redeploy-runtime ")
    assert "--run-id ml_abcd1234" in cmd
    assert "redeploy-runtime" in get_registry()
