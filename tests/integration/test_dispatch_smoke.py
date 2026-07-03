"""Layer B — in-process dispatch smoke tests (``pytest -m integration``).

WHY THIS TIER EXISTS
--------------------
The unit tests here fake the ``hpc-agent <verb> --spec`` subprocess seam: they
call the primitive function directly (or mock the composed rings), so a bug in
the REAL dispatch path passes green — a verb absent from the CLI verb-map, a
parser that rejects the spec shape, an envelope regression, or a gate that never
actually fires. ``block-drive`` shipped in exactly this state (it did not
dispatch until a regen ran, and no unit test could see it).

Layer B drives verbs through the SHIPPED in-process CLI dispatch
(:func:`hpc_agent._kernel.extension.mcp_server._in_process_cli_runner`, reached
via the ``dispatch_envelope`` fixture in ``tests/integration/conftest.py``) —
the SAME ``cli.dispatch.main`` code path a real ``hpc-agent <verb>`` invocation
takes: parser → ``model_validate`` → primitive → JSON envelope. It asserts the
REAL envelope, patching ONLY the SSH boundary. It is generalized from
``tests/test_mcp_curated.py::test_in_process_and_subprocess_runners_have_envelope_parity``.

The four checks:

1. **Every spec-verb DISPATCHES** — the verb-map / wiring net. Each registry
   verb with a ``spec_model`` is driven with an intentionally-empty (or
   minimal-invalid) spec; the envelope must be a REAL structured envelope
   (``spec_invalid`` for a required-field verb, or a clean ``ok`` for an
   all-optional one) — never an argparse/usage error (exit 2, empty body) or an
   unknown-verb rejection. A verb that fails this is a ``block-drive``-shaped
   wiring bug.
2. **Block verbs reach their real ``stage_reached``** via real dispatch,
   journal-only, no SSH (``status-snapshot`` clean + anomaly, ``block-drive``).
3. **The greenlight gate actually fires** through real dispatch (``aggregate-run``
   with no journaled greenlight / a nudge).
4. **A cluster-touching verb runs up to the SSH boundary** with SSH patched
   (``status-watch``).

All hermetic: the per-run journal is redirected via ``HPC_JOURNAL_DIR`` and the
SSH seam is patched at the Python level. No test reaches a real ``ssh``/``scp``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent import errors
from hpc_agent._kernel.registry.primitive import get_meta, get_registry
from hpc_agent.cli._dispatch import CliShape, _leaf_verb
from hpc_agent.state.decision_journal import append_decision
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

pytestmark = pytest.mark.integration


# ── Effective SSH poll seam ───────────────────────────────────────────────────
#
# ``tests/integration/conftest.py`` documents SSH_STATUS_REPORT =
# ``hpc_agent.infra.cluster_status.ssh_status_report``. But ``ops/monitor/status``
# binds a module-local alias at import time (``_ssh_status_report =
# ssh_status_report``) and the poll loop calls the alias — so patching the source
# module does NOT redirect an in-flight poll. The alias below is the seam a real
# poll actually resolves; patching it is what keeps ``status-watch`` off a real
# binary. ``harvest_on_terminal`` is monitor-flow's guaranteed-terminal harvest
# (its ``finally``); stubbed so the ``status-watch`` failure path cannot fall
# through to a real ``scp`` combine on this native-Windows box (where the
# hermetic-binary PATH shim is skipped).
_SSH_POLL_SEAM = "hpc_agent.ops.monitor.status._ssh_status_report"
_HARVEST_SEAM = "hpc_agent.ops.monitor_flow.harvest_on_terminal"


# Verbs whose CLI requires additional mandatory args beyond ``--spec`` (e.g.
# ``interview --campaign-dir``, ``resubmit --run-id --task-ids``) — firing them
# with only ``--spec`` exits via argparse with usage-help and no JSON envelope,
# so the spec-validate path is unreachable from a ``--spec``-only probe. Mirrors
# ``tests/contract/test_primitive_remediation.py::NEEDS_EXTRA_CLI_ARGS``. The
# per-verb probe below ALSO detects any required extra CliArg dynamically, so a
# newly-added one is skipped even if it never lands in this set.
NEEDS_EXTRA_CLI_ARGS: frozenset[str] = frozenset({"interview", "resubmit"})


def _spec_dispatch_verbs() -> list[tuple[str, str]]:
    """Every registry verb driveable from ``--spec`` alone → ``(verb, primitive_name)``.

    Filters to primitives whose ``cli`` is a :class:`CliShape` carrying a
    ``spec_model`` (the input-bearing dispatch surface), excluding verb-grouped
    verbs (need a group prefix) and Tier-2 ``handler=`` verbs (their own
    hand-written adapter, not the generic spec dispatcher). Built at import time
    — the root conftest populates the registry at collection time.
    """
    out: list[tuple[str, str]] = []
    for name, meta in sorted(get_registry().items()):
        cli = meta.cli
        if not isinstance(cli, CliShape) or cli.spec_model is None:
            continue
        if cli.group is not None or cli.handler is not None:
            continue
        out.append((_leaf_verb(name, cli), name))
    return out


_SPEC_DISPATCH_VERBS: list[tuple[str, str]] = _spec_dispatch_verbs()


# ── test 1: every spec-verb dispatches (the wiring net) ───────────────────────


@pytest.mark.parametrize(
    "verb,primitive_name",
    _SPEC_DISPATCH_VERBS,
    ids=[verb for verb, _ in _SPEC_DISPATCH_VERBS],
)
def test_spec_verb_dispatches_to_structured_envelope(
    verb: str,
    primitive_name: str,
    dispatch_envelope: Callable[..., dict[str, Any]],
) -> None:
    """Every ``--spec`` verb produces a REAL structured envelope on real dispatch.

    Drives the verb through the shipped in-process CLI with an intentionally-bad
    spec and asserts the envelope is well-formed — proving the verb is wired into
    the CLI (parser accepted it) AND its spec is validated (``model_validate``
    ran). The failure this guards is a verb present in the registry but NOT
    dispatchable — an argparse/usage error or unknown-verb rejection that emits
    NO envelope (the ``block-drive``-shaped regen gap).

    The probe spec is ``{}`` for a required-spec verb (rejected as
    ``spec_invalid`` — "``--spec`` is required") and a minimal unknown-field dict
    for an all-optional (``spec_required=False``) verb, so validation runs
    instead of the primitive executing on a ``None`` spec.
    """
    cli = get_meta(primitive_name).cli
    assert isinstance(cli, CliShape)  # narrowed for mypy; filtered in _spec_dispatch_verbs

    required_extra = [a.flag for a in cli.args if a.required and a.flag.startswith("-")]
    if verb in NEEDS_EXTRA_CLI_ARGS or required_extra:
        pytest.skip(
            f"{verb}: CLI requires mandatory args beyond --spec "
            f"({required_extra or 'e.g. --campaign-dir / --run-id'}); the "
            "spec-validate path is unreachable from a --spec-only probe "
            "(NEEDS_EXTRA_CLI_ARGS)."
        )

    probe_spec: dict[str, Any] = {} if cli.spec_required else {"__hpc_smoke_invalid_field__": True}
    env = dispatch_envelope(verb, probe_spec)
    exit_code = env.get("_exit_code")

    # A real structured envelope was emitted — NOT an empty body from an argparse
    # usage error / unknown-verb rejection (which would carry no "ok" key).
    assert "ok" in env, (
        f"{verb}: real dispatch produced NO structured envelope (exit={exit_code}). "
        "The verb is in the registry but a real invocation emits an argparse/usage "
        "error or unknown-verb rejection instead of an envelope — a block-drive-"
        "shaped WIRING BUG (verb not reachable through the CLI verb-map / parser)."
    )
    assert exit_code in {0, 1, 2, 3}, f"{verb}: envelope has an unknown exit code {exit_code!r}."

    if env["ok"] is False:
        # The intentionally-bad spec must be rejected as a spec-shape error —
        # proving parser → model_validate actually ran — not surfaced as an
        # internal crash, a cluster error, or an unknown verb.
        assert env.get("error_code") == "spec_invalid", (
            f"{verb}: an intentionally-"
            f"{'empty' if cli.spec_required else 'invalid'} spec should reject as "
            f"error_code='spec_invalid' (parser → model_validate ran), got "
            f"{env.get('error_code')!r}: {env.get('message')!r}"
        )
    # env['ok'] is True → an all-optional verb legitimately ran on the probe spec;
    # a well-formed ok envelope is itself proof the verb dispatched.


# ── shared journal fixture + helper ───────────────────────────────────────────


@pytest.fixture
def hermetic_experiment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the per-run journal into ``tmp_path`` and return a fresh experiment dir.

    ``HPC_JOURNAL_DIR`` wins over every other homedir lookup (see
    ``state/run_record._current_homedir``) and is read at call time, so the
    in-process dispatch under test writes/reads the journal entirely inside
    ``tmp_path`` — no ``~/.claude/hpc`` leakage.
    """
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    exp = tmp_path / "exp"
    exp.mkdir()
    return exp


def _journal_run(exp: Path, run_id: str, *, status: str, **overrides: Any) -> None:
    """Write a real per-run journal :class:`RunRecord` the dispatch path will read."""
    record = RunRecord(
        run_id=run_id,
        profile="test",
        cluster="hoffman2",
        ssh_target="user@hoffman2.example.edu",
        remote_path="/u/scratch/run",
        job_name="job",
        job_ids=["100"],
        total_tasks=4,
        submitted_at="2026-01-01T00:00:00Z",
        experiment_dir=str(exp),
        status=status,
        **overrides,
    )
    upsert_run(exp, record)


# ── test 2: block verbs reach their real stage_reached (journal-only) ─────────


def test_status_snapshot_reaches_anomaly_stage(
    hermetic_experiment: Path, dispatch_envelope: Callable[..., dict[str, Any]]
) -> None:
    """A journaled FAILED run → ``status-snapshot`` digests it to ``snapshot_anomaly``.

    Real dispatch: parser → ``StatusSnapshotSpec`` → the ``status-snapshot``
    primitive → the journal read → the anomaly digest. No SSH (journal-first).
    """
    exp = hermetic_experiment
    _journal_run(exp, "ml_run_fail", status="failed", last_status={"failed": 4})

    env = dispatch_envelope("status-snapshot", {"run_id": "ml_run_fail"}, experiment_dir=exp)

    assert env.get("ok") is True, env
    data = env["data"]
    assert data["block"] == "snapshot"
    assert data["stage_reached"] == "snapshot_anomaly"
    assert data["needs_decision"] is True
    # the code-digested anomaly evidence carries the failed run + a recommendation.
    assert data["brief"]["anomalies"][0]["status"] == "failed"


def test_status_snapshot_reaches_clean_stage(
    hermetic_experiment: Path, dispatch_envelope: Callable[..., dict[str, Any]]
) -> None:
    """A journaled live (in_flight) run with nothing amiss → ``snapshot_clean``."""
    exp = hermetic_experiment
    _journal_run(exp, "ml_run_live", status="in_flight", last_status={"running": 4})

    env = dispatch_envelope("status-snapshot", {"run_id": "ml_run_live"}, experiment_dir=exp)

    assert env.get("ok") is True, env
    data = env["data"]
    assert data["block"] == "snapshot"
    assert data["stage_reached"] == "snapshot_clean"
    assert data["needs_decision"] is False


def test_block_drive_dispatches_result_envelope(
    hermetic_experiment: Path, dispatch_envelope: Callable[..., dict[str, Any]]
) -> None:
    """``block-drive`` DISPATCHES and returns a ``BlockDriveResult`` envelope.

    This is the exact bug that shipped — ``block-drive`` in the registry but not
    dispatchable. A dry-run tick over a fabricated run must reach the real driver
    and return a structured ``{action, ...}`` result (proving the regen wiring
    holds), touching no cluster.
    """
    exp = hermetic_experiment
    _journal_run(exp, "ml_run_live", status="in_flight", last_status={"running": 4})

    env = dispatch_envelope(
        "block-drive",
        {"workflow": "status", "dry_run": True, "run_id": "ml_run_live"},
        experiment_dir=exp,
    )

    assert env.get("ok") is True, env
    data = env["data"]
    # BlockDriveResult shape — the `action` decision-as-data field is mandatory.
    assert "action" in data, data
    assert data["action"] in {
        "awaiting_decision",
        "advanced",
        "reran",
        "chained",
        "detached",
        "terminal",
        "skip",
    }


# ── test 3: the greenlight gate fires through real dispatch ───────────────────


def test_greenlight_gate_fires_without_a_journaled_decision(
    hermetic_experiment: Path, dispatch_envelope: Callable[..., dict[str, Any]]
) -> None:
    """``aggregate-run`` with a fully-valid spec but NO journaled greenlight → gate fires.

    The spec (``{"aggregate": {"run_id": ...}}``) is minimal-but-VALID against
    ``AggregateRunSpec`` (its nested ``AggregateFlowSpec`` needs only ``run_id``),
    so validation passes and the ``assert_greenlit_target`` precondition gate — a
    pure journal read — is the thing that must reject. It fails loudly as
    ``spec_invalid`` naming the missing greenlight, proving the gate is REACHABLE
    through real dispatch, not bypassed. No SSH: the gate raises before
    ``aggregate-flow`` runs.
    """
    exp = hermetic_experiment
    _journal_run(exp, "ml_run_agg", status="complete", last_status={"complete": 4})

    env = dispatch_envelope(
        "aggregate-run", {"aggregate": {"run_id": "ml_run_agg"}}, experiment_dir=exp
    )

    assert env.get("ok") is False, env
    assert env.get("error_code") == "spec_invalid", env
    assert "no journaled greenlight" in (env.get("message") or ""), env


def test_greenlight_gate_rejects_a_nudge_as_not_a_greenlight(
    hermetic_experiment: Path, dispatch_envelope: Callable[..., dict[str, Any]]
) -> None:
    """A journaled NUDGE (``response != "y"``) is not a greenlight → the gate fires.

    Proves the gate distinguishes a greenlight from a nudge on the real decision
    journal the dispatch path reads — not merely "any record present".
    """
    exp = hermetic_experiment
    _journal_run(exp, "ml_run_agg", status="complete", last_status={"complete": 4})
    append_decision(
        exp,
        scope_kind="run",
        scope_id="ml_run_agg",
        block="aggregate-check",
        response="lower the min-rows threshold first",  # a nudge, not "y"
    )

    env = dispatch_envelope(
        "aggregate-run", {"aggregate": {"run_id": "ml_run_agg"}}, experiment_dir=exp
    )

    assert env.get("ok") is False, env
    assert env.get("error_code") == "spec_invalid", env
    assert "nudge, not a" in (env.get("message") or ""), env


# ── test 4: a cluster-touching verb runs up to the SSH boundary ───────────────


def test_status_watch_runs_up_to_the_ssh_boundary(
    hermetic_experiment: Path, dispatch_envelope: Callable[..., dict[str, Any]]
) -> None:
    """``status-watch`` drives parser → dispatch → monitor-flow → the SSH poll seam.

    With the poll seam patched to raise :class:`errors.SshUnreachable` (and the
    guaranteed-terminal harvest stubbed so the failure path cannot fall through
    to a real ``scp`` on this platform), the whole plumbing runs and the envelope
    is a REAL network/cluster error — proving the path is wired end-to-end, with
    ONLY the connection stubbed. It is NOT ``spec_invalid`` (spec was valid) and
    NOT an unknown-verb rejection (the verb dispatched).
    """
    exp = hermetic_experiment
    _journal_run(exp, "ml_run_watch", status="in_flight", last_status={"running": 4})

    spec = {
        "monitor": {
            "run_id": "ml_run_watch",
            "poll_interval_seconds": 5,
            "wall_clock_budget_seconds": 30,
        }
    }
    with (
        mock.patch(_SSH_POLL_SEAM, side_effect=errors.SshUnreachable("stubbed: no host")),
        mock.patch(_HARVEST_SEAM, return_value=None),
    ):
        env = dispatch_envelope("status-watch", spec, experiment_dir=exp)

    assert env.get("ok") is False, env
    # The failure is a genuine connection-class error surfaced at the SSH seam —
    # not spec_invalid, not unknown-verb — so parser → dispatch → SSH-seam is wired.
    assert env.get("error_code") in {"ssh_unreachable", "remote_command_failed"}, env
    assert env.get("error_code") != "spec_invalid", env
