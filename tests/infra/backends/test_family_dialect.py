"""Per-family scheduler capability matrix (G9 upstream leg).

The G9 "scheduler-dialect-monoculture" class fired because one family's
grammar/exit-code rule was copied into a sibling's branch (PBS Pro got
SGE's rc==0 liveness rule #5, TORQUE's/SLURM's ``%N`` cap suffix #32, and
the comma-array split keyed on an ad-hoc set #6). The fix moves those
capabilities onto a per-family :class:`FamilyDialect` and drives the engine
off it. This module is the contract fixture matrix: every KNOWN family has a
dialect, and the dialect's capability values MUST match the command/verdict
the engine actually emits for that family — so a family the primary dev loop
doesn't exercise (pbspro/torque) can't silently carry another's assumption.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra.backends import get_backend, get_backend_class
from hpc_agent.infra.backends.profile import (
    FAMILY_DIALECTS,
    KNOWN_FAMILIES,
    dialect_for,
)


def _noop_ssh(_cmd):
    from types import SimpleNamespace

    return SimpleNamespace(stdout="", stderr="", returncode=0)


def _backend(family, tmp_path):
    # Construction differs by family: the local SLURM/SGE backends take a
    # log_dir; the ssh-only PBS backends take ssh_run/remote_repo. Both reach
    # the same ``_build_command`` engine path under test.
    if family == "slurm":
        from hpc_agent.infra.backends.slurm import SlurmBackend

        return SlurmBackend(script=str(tmp_path / "j.slurm"), log_dir=str(tmp_path / "logs"))
    if family == "sge":
        from hpc_agent.infra.backends.sge import SGEBackend

        return SGEBackend(script=str(tmp_path / "j.sh"), log_dir=str(tmp_path / "logs"))
    return get_backend(
        family, script="j.pbs", ssh_run=_noop_ssh, remote_repo="/r", pass_env_keys=("K",)
    )


# ---------------------------------------------------------------------------
# The matrix itself — one row per family, pinning every capability so a new
# family cannot be added without declaring its own (or a test fails rather
# than inheriting SGE's).
# ---------------------------------------------------------------------------

_EXPECTED = {
    "slurm": {
        "supports_comma_array_ranges": True,
        "cap_style": "range_suffix",
        "explicit_id_liveness_query": True,
    },
    "torque": {
        "supports_comma_array_ranges": True,
        "cap_style": "range_suffix",
        "explicit_id_liveness_query": True,
    },
    "sge": {
        "supports_comma_array_ranges": False,
        "cap_style": "tc_flag",
        "explicit_id_liveness_query": False,
    },
    "pbspro": {
        "supports_comma_array_ranges": False,
        "cap_style": "max_run_subjobs",
        "explicit_id_liveness_query": True,
    },
}


def test_every_known_family_has_exactly_one_dialect():
    # Keyset equality: a new KNOWN family without a dialect entry (or a stray
    # dialect for an unknown family) fails here rather than fallthrough-
    # inheriting a sibling's rule.
    assert set(FAMILY_DIALECTS) == set(KNOWN_FAMILIES) == set(_EXPECTED)


@pytest.mark.parametrize("family", sorted(_EXPECTED))
def test_dialect_capability_values(family):
    d = dialect_for(family)
    exp = _EXPECTED[family]
    assert d.family == family
    assert d.supports_comma_array_ranges == exp["supports_comma_array_ranges"]
    assert d.cap_style == exp["cap_style"]
    assert d.explicit_id_liveness_query == exp["explicit_id_liveness_query"]


def test_pbspro_and_torque_diverge_on_both_cap_and_comma():
    # The exact monoculture hazard: the two PBS forks are NOT interchangeable.
    pp, tq = dialect_for("pbspro"), dialect_for("torque")
    assert pp.cap_style != tq.cap_style
    assert pp.supports_comma_array_ranges != tq.supports_comma_array_ranges


# ---------------------------------------------------------------------------
# Dialect ↔ observed behaviour: the capability value must predict what the
# engine emits/decides, per family (so the flag can't drift from the branch).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family", sorted(_EXPECTED))
def test_cap_style_predicts_emitted_cap_syntax(family, tmp_path):
    backend = _backend(family, tmp_path)
    cmd = " ".join(backend._build_command("1-100", "job", {"K": "V"}, concurrency_cap=8))
    style = dialect_for(family).cap_style
    if style == "range_suffix":
        assert "1-100%8" in cmd and "max_run_subjobs" not in cmd and "-tc" not in cmd
    elif style == "tc_flag":
        assert "-tc 8" in cmd and "1-100%8" not in cmd
    elif style == "max_run_subjobs":
        assert "max_run_subjobs=8" in cmd and "1-100%8" not in cmd
    else:  # pragma: no cover - guards a future unhandled style
        raise AssertionError(f"unhandled cap_style {style!r}")


@pytest.mark.parametrize("family", sorted(_EXPECTED))
def test_explicit_id_query_predicts_nonzero_rc_verdict(family):
    # ack present + non-zero rc: explicit-id families read 'ran'; the whole-queue
    # family (SGE) reads a binary failure (UNKNOWN).
    cls = get_backend_class(family)
    _, ok = cls.scheduler_query_ran("__HPC_SCHED_ACK__=1\n")
    assert ok is dialect_for(family).explicit_id_liveness_query
    # ack absent is UNKNOWN for every family regardless of the flag.
    _, ok_absent = cls.scheduler_query_ran("")
    assert ok_absent is False
