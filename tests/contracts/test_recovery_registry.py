"""Contract tests for ``hpc_agent.recovery.registry``.

Two-part contract:

(a) Every :data:`RecoveryKind` Literal value has a registry entry — fully
    enforced once ALL kinds are ported (today: ``xfail`` for the un-ported
    kinds, which is the migration punch list).
(b) Every place ``ErrorEnvelope.remediation`` is set from the registry
    derives that string from :func:`remediation_for` rather than a literal.
    The ported error classes (``AlreadyInFlight``, ``SubmissionIncomplete``,
    ``SpawnWorkerDied``) are checked positively; for the un-ported error
    classes, only a smoke "raises and round-trips remediation through
    pydantic" check fires.

See ``docs/proposals/recovery-registry.md`` for the design + migration
plan; un-ported kinds in the ``xfail`` list ARE the punch list.
"""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.recovery.registry import (
    PORTED_KINDS,
    REGISTRY,
    RecoveryMenu,
    all_kinds,
    menu_for,
    remediation_for,
)

# Kinds that the prototype landed (the registry currently knows about).
# The full Literal vocabulary is the ``all_kinds()`` result; the diff is
# the migration punch list, asserted via xfail below.
_PORTED = sorted(PORTED_KINDS)


# ── (a) every Literal value has a registry entry ──────────────────────────


@pytest.mark.parametrize("kind", _PORTED)
def test_ported_kind_has_registry_entry(kind: str) -> None:
    """The three ported kinds must resolve to a valid :class:`RecoveryMenu`."""
    menu = menu_for(kind)
    assert isinstance(menu, RecoveryMenu)
    assert menu.kind == kind
    assert menu.summary.strip(), f"{kind}: summary is empty"
    assert menu.options, f"{kind}: menu has no options"


@pytest.mark.parametrize("kind", sorted(set(all_kinds()) - PORTED_KINDS))
def test_unported_kind_is_migration_punch_list(kind: str) -> None:
    """The remaining ``RecoveryKind`` Literal values are the migration
    punch list — they're declared but not yet ported.

    This test ``xfails`` (or strict-xpasses once the kind is ported), so
    the diff between ``all_kinds()`` and ``PORTED_KINDS`` is enforced
    bidirectionally: an un-ported kind STAYS xfail, and a kind that gets
    ported flips strict-xpass, surfacing as a test failure until the
    parametrize list is updated.
    """
    if kind in REGISTRY:
        # Strict-xpass: the kind was ported but is still in the
        # "unported" parametrize list. Update _PORTED above.
        pytest.fail(
            f"{kind!r} is now in REGISTRY but still in the unported "
            "parametrize list — move it to _PORTED."
        )
    pytest.xfail(f"{kind!r} not yet ported to the recovery registry")


# ── (b) ErrorEnvelope.remediation derives from the registry ──────────────


def test_already_in_flight_remediation_from_registry() -> None:
    """``AlreadyInFlight``'s remediation must be byte-equal to
    ``remediation_for("already_in_flight", placeholders=...)``."""
    exc = errors.AlreadyInFlight(
        "prior run still in flight",
        run_id="myrun-123",
        scheduler="sge",
        experiment_dir="/tmp/exp",
    )
    expected = remediation_for(
        "already_in_flight",
        placeholders={
            "run_id": "myrun-123",
            "scheduler": "sge",
            "experiment_dir": "/tmp/exp",
        },
    )
    assert exc.remediation == expected


def test_submission_incomplete_remediation_from_registry() -> None:
    """``SubmissionIncomplete``'s remediation must be byte-equal to
    ``remediation_for("submission_incomplete", placeholders=...)``."""
    exc = errors.SubmissionIncomplete(
        "canary sidecar has no job_ids",
        run_id="canary-abc",
        experiment_dir="/tmp/exp",
        ssh_target="user@host",
        remote_path="/u/scratch/u/foo",
    )
    expected = remediation_for(
        "submission_incomplete",
        placeholders={
            "run_id": "canary-abc",
            "experiment_dir": "/tmp/exp",
            "ssh_target": "user@host",
            "remote_path": "/u/scratch/u/foo",
        },
    )
    assert exc.remediation == expected


def test_spawn_worker_died_remediation_from_registry() -> None:
    """``SpawnWorkerDied``'s remediation must be byte-equal to
    ``remediation_for("spawn_worker_died")``."""
    exc = errors.SpawnWorkerDied("claude -p --bare exited 1")
    assert exc.remediation == remediation_for("spawn_worker_died")


def test_per_instance_remediation_override_wins() -> None:
    """The registry value is the default, not a clamp — callers can still
    pass ``remediation=`` to inject context-specific prose."""
    custom = "Custom one-off context — see <run_id>=foo"
    exc = errors.AlreadyInFlight("prior run still in flight", remediation=custom)
    assert exc.remediation == custom


# ── (c) registry-level invariants ──────────────────────────────────────────


def test_remediation_text_substitutes_placeholders() -> None:
    """Token substitution should happen for every supplied placeholder; any
    un-substituted ``<token>`` must pass through verbatim."""
    text = remediation_for(
        "already_in_flight",
        placeholders={"run_id": "FOO", "scheduler": "slurm"},
    )
    assert "FOO" in text
    assert "slurm" in text
    # No supplied experiment_dir placeholder → token passes through.
    assert "<experiment_dir>" in text


def test_remediation_text_sorted_by_safety_rank() -> None:
    """Menus must render options ordered by safety_rank ascending — the
    primary recommendation (rank 0) comes first."""
    text = remediation_for("already_in_flight")
    a_pos = text.index("(a)")
    b_pos = text.index("(b)")
    c_pos = text.index("(c)")
    assert a_pos < b_pos < c_pos
    # /monitor-hpc is rank 0 (the primary recommendation for the still-
    # running case); the ``hpc-agent reconcile`` command literal (rank 1)
    # must appear AFTER it. Searching for the CLI command verbatim avoids
    # matching the word "reconcile" inside another option's prose.
    monitor_pos = text.index("/monitor-hpc")
    reconcile_pos = text.index("hpc-agent reconcile")
    assert monitor_pos < reconcile_pos


def test_every_menu_summary_and_options_are_nonempty() -> None:
    """Smoke check across every ported entry — no empty strings or missing
    options can leak in."""
    for kind, menu in REGISTRY.items():
        assert menu.kind == kind
        assert menu.summary.strip(), f"{kind}: empty summary"
        assert menu.options, f"{kind}: no options"
        for opt in menu.options:
            assert opt.cli_command.strip(), f"{kind}: empty cli_command"
            assert opt.when_to_use.strip(), f"{kind}: empty when_to_use"
            assert opt.safety_rank >= 0


def test_resumable_kill_kinds_offer_resume_from_checkpoint_first() -> None:
    """#294: walltime / node_failure kills deterministically map to a
    resume-from-checkpoint remediation as the rank-0 (primary) option."""
    for kind in ("walltime", "node_failure"):
        text = remediation_for(kind, placeholders={"run_id": "r1", "experiment_dir": "/x"})
        assert "resubmit" in text and "from_checkpoint: true" in text
        # the resume option is rank 0 → rendered as (a), before (b)
        assert text.index("from_checkpoint: true") < text.index("(b)")
        assert "r1" in text  # placeholder substituted


def test_safety_ranks_unique_within_menu() -> None:
    """Two options in the same menu should not share a safety_rank — the
    renderer relies on rank for deterministic ordering."""
    for kind, menu in REGISTRY.items():
        ranks = [opt.safety_rank for opt in menu.options]
        assert len(ranks) == len(set(ranks)), f"{kind}: duplicate safety_rank in menu: {ranks}"
