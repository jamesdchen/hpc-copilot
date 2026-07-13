"""C-horizon at the deployment boundary — bug-sweep #48 arm (a), RULING 2.

The fix's contract (live-conformance C-horizon; the 2026-07-12 ruling "the
time-aware queue owns the deployment gate"):

* ``verify-registration``'s ``status`` is TIME-INDEPENDENT by design — it passes
  ``now=None`` into ``reduce_registration``, so a lapsed ``review_horizon`` does
  NOT flip its status, and its R6 ``view_sha`` is byte-identical to the same
  registration with no horizon at all. Pinned by the monkeypatched unit test.
* the caller-side deployment gate (``examples/toy_registration/deploy.py``) is
  therefore rewired to ALSO consult the TIME-aware attention queue: a horizon-
  lapsed registration reads ``current`` from verify but IS refused at the deploy
  boundary through ``ops/attention_queue.py::horizon_lapsed_registration_ids``,
  which reuses the ONE ``reduce_registration`` (now-threaded) — no second horizon
  evaluation. Pinned by the real-substrate deploy tests (lapsed + control).

TOY VOCABULARY ONLY (the plan's fixture rule): a widget-batch lineage.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from hpc_agent._wire.actions.verify_registration import VerifyRegistrationSpec
from hpc_agent.ops.registration import verify_op
from hpc_agent.state.decision_journal import append_decision, read_decisions
from hpc_agent.state.registration import reduce_registration
from tests.fixtures import toy_registration as toy
from tests.ops.registration.test_verify_op import (
    _DOSSIER_SHA,
    _install,
    _registration_record,
    _write_template,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_deploy() -> Any:
    """Load the shipped caller-side deploy script from examples/ (a real consumer)."""
    path = _REPO_ROOT / "examples" / "toy_registration" / "deploy.py"
    spec = importlib.util.spec_from_file_location("toy_deploy", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


deploy = _load_deploy()

# A horizon strictly in the past of ``_NOW`` (lapsed) and one strictly after it.
_PAST_HORIZON = "2026-06-01T00:00:00Z"
_FUTURE_HORIZON = "2027-01-01T00:00:00Z"
_NOW = "2026-07-12T00:00:00Z"  # after _PAST_HORIZON, before _FUTURE_HORIZON


# ── (a) verify-registration stays time-independent (R6 view_sha byte-identity) ──


def test_lapsed_horizon_still_reads_current_with_byte_identical_view_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lapsed ``review_horizon`` never enters verify's status OR its ``view_sha``.

    verify passes ``now=None`` into ``reduce_registration`` (no horizon evaluation),
    and the horizon lives OUTSIDE the ``view_sha`` projection — so the lapsed-horizon
    registration reads ``current`` and binds the exact same witness as the identical
    registration carrying no horizon at all.
    """
    _write_template(tmp_path)

    # The control: an ordinary current registration, no conformance/horizon block.
    control = _registration_record()

    # The subject: byte-identical legs, PLUS a conformance block whose review_horizon
    # has already lapsed relative to any real ``now``.
    lapsed = _registration_record()
    lapsed["resolved"]["conformance"] = {"review_horizon": _PAST_HORIZON}

    _install(monkeypatch, records=[control], live_sha=_DOSSIER_SHA)
    control_res = verify_op.verify_registration(
        experiment_dir=tmp_path, spec=VerifyRegistrationSpec(registration_id="reg-widgets")
    )

    _install(monkeypatch, records=[lapsed], live_sha=_DOSSIER_SHA)
    lapsed_res = verify_op.verify_registration(
        experiment_dir=tmp_path, spec=VerifyRegistrationSpec(registration_id="reg-widgets")
    )

    # Time-independent: the lapsed-horizon registration still reads current...
    assert lapsed_res.status == "current"
    # ...and its signed witness is byte-identical to the horizon-free control (R6).
    assert lapsed_res.view_sha == control_res.view_sha
    assert lapsed_res.brief == control_res.brief


# ── (b) the deployment gate refuses a lapsed horizon via the queue leg ─────────


def _register_with_horizon(experiment_dir: Path, horizon: str | None) -> None:
    """Lay real substrate + a CURRENT registration whose winner carries *horizon*.

    Builds the toy substrate and registers through the gated append (all legs
    live-current), then appends a fresh registration record — via the STATE layer,
    copying the winner's exact recompute legs — that additionally carries
    ``conformance.review_horizon``. The copied legs keep every edit-drift leg
    current, so the ONLY time-based staleness is the horizon.
    """
    experiment_dir = Path(experiment_dir)
    toy.build_substrate(experiment_dir)
    toy.register(experiment_dir)

    records = read_decisions(experiment_dir, "registration", toy.REG_ID)
    winner = reduce_registration(records, registration_id=toy.REG_ID, live_dossier_sha=None).winner
    assert winner is not None
    resolved: dict[str, Any] = dict(winner)
    if horizon is not None:
        resolved["conformance"] = {"review_horizon": horizon}
    append_decision(
        experiment_dir,
        scope_kind="registration",
        scope_id=toy.REG_ID,
        block="registration",
        response=f"re-register {toy.REG_ID} with a review horizon",
        resolved=resolved,
    )


def test_deploy_refuses_current_but_horizon_lapsed_via_queue_leg(tmp_path: Path) -> None:
    """A registration that verify reads ``current`` is still refused when horizon-lapsed.

    verify's edit-drift leg is green (dossier/template/prereqs all live-current),
    but the deploy gate's TIME leg — the attention queue's now-threaded reduction —
    names the registration horizon-lapsed, so ``deploy_or_refuse`` refuses.
    """
    _register_with_horizon(tmp_path, _PAST_HORIZON)

    # verify (time-independent) reports current...
    res = verify_op.verify_registration(
        experiment_dir=tmp_path, spec=VerifyRegistrationSpec(registration_id=toy.REG_ID)
    )
    assert res.status == "current"

    # ...but the deploy gate refuses via the queue's horizon-lapsed leg.
    with pytest.raises(SystemExit) as exc:
        deploy.deploy_or_refuse(tmp_path, toy.REG_ID, now=_NOW)
    assert "review horizon" in str(exc.value)


def test_deploy_clears_current_with_non_lapsed_horizon(tmp_path: Path) -> None:
    """Control: a non-lapsed (future) horizon leaves the deploy gate cleared."""
    _register_with_horizon(tmp_path, _FUTURE_HORIZON)

    result = deploy.deploy_or_refuse(tmp_path, toy.REG_ID, now=_NOW)
    assert getattr(result, "status", None) == "current"


def test_queue_helper_names_only_the_lapsed_registration(tmp_path: Path) -> None:
    """The read helper reuses the queue reduction: lapsed → named; future → not.

    Directly pins ``horizon_lapsed_registration_ids`` (the one-definition route the
    deploy gate consults) so the queue/reporter divergence #48 flagged can never
    silently reopen.
    """
    from hpc_agent.ops.attention_queue import horizon_lapsed_registration_ids

    _register_with_horizon(tmp_path, _PAST_HORIZON)
    assert horizon_lapsed_registration_ids(tmp_path, now=_NOW) == {toy.REG_ID}
    # Before the horizon lapses, the same registration is not named.
    assert horizon_lapsed_registration_ids(tmp_path, now=_PAST_HORIZON) == set()
