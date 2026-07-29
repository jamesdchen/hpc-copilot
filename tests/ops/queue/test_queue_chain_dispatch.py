"""CHAIN-DISPATCH — the run queue's wake edge, one negative case per rule.

``docs/plans/run-queue-placement-2026-07-28.md`` §5: *"a run finishing IS the
moment to re-tick the queue."* Until this hook, nothing invoked ``queue-dispatch``
when a dispatched run RETIRED, so the next waiting ledger item sat until a human
or a drain pass happened by. These tests pin the four properties the wake edge
has to have, each with the case that would fail without it:

* **it actually drains** — a retiring run chains EXACTLY ONE ``queue-dispatch``
  tick and the next waiting item really starts (not "a tick ran");
* **it cannot cost a settlement** — a chain that raises leaves the retiring
  run's terminal recorded, its relay-due marker armed, and the driver's result
  intact; the failure comes back as data;
* **it is cheap and it stops** — an experiment with no intake ledger pays one
  ``stat`` (no dispatcher import, no journal read, no scaffolding) and chains
  nothing, so the recursion terminates on a dry ledger;
* **it does not amplify** — one retirement → one dispatch tick, and a chained
  dispatch cannot re-enter the chain in-process because its only actuation seat
  is ``campaign_run(detach=True)``, whose parent branch returns a handle without
  reaching the synchronous body the hook lives on.

The submit seat is faked at its SOURCE module (``hpc_agent.ops.campaign_run
.campaign_run``) exactly as ``test_queue_dispatch.py`` fakes it; everything else
is real — a real intake ledger, a real ``queue-advance``, a real journal under
``tmp_path``, a real ``clusters.yaml``. Nothing here opens a socket.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest import mock

import pytest

from hpc_agent._wire.workflows.campaign_run import CampaignRunResult, CampaignRunSpec
from hpc_agent._wire.workflows.queue_dispatch import QueueDispatchSpec
from hpc_agent.ops.queue.chain import chain_dispatch_on_retire
from hpc_agent.ops.queue.dispatch import queue_dispatch
from hpc_agent.state.journal import upsert_run
from hpc_agent.state.queue_intake import append_intake_item, read_intake_items
from hpc_agent.state.run_record import RunRecord

if TYPE_CHECKING:
    from pathlib import Path

_NOW = "2026-07-29T12:00:00+00:00"

#: The one actuation seat the queue composes (D1), patched at its source module.
_RUN = "hpc_agent.ops.campaign_run.campaign_run"

_CLUSTERS = """
alpha:
  scheduler: slurm
  host: alpha.edu
  user: me
  max_walltime_sec: 86400
"""


@pytest.fixture(autouse=True)
def _journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


@pytest.fixture(autouse=True)
def _clusters(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "clusters.yaml"
    path.write_text(_CLUSTERS, encoding="utf-8")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(path))


def _exp(tmp_path: Path) -> Path:
    exp = tmp_path / "exp"
    exp.mkdir(exist_ok=True)
    return exp


def _submit_spec(run_id: str) -> dict[str, Any]:
    return {
        "profile": "ml",
        "cluster": "alpha",
        "ssh_target": "me@alpha.edu",
        "remote_path": "/scratch/ml",
        "job_name": "ml_array",
        "run_id": run_id,
        "total_tasks": 1,
        "backend": "slurm",
        "script": ".hpc/templates/cpu_array.sh",
        "job_env": {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
    }


def _enqueue(exp: Path, item_id: str, *, run_id: str, cmd_sha: str) -> None:
    append_intake_item(
        exp,
        record={
            "run_name": "ml",
            "run_id": run_id,
            "cmd_sha": cmd_sha,
            "cluster_pin": "alpha",
            "campaign_base": "study",
            "spec": _submit_spec(run_id),
        },
        request_id=item_id,
    )


def _seed_run(exp: Path, run_id: str, *, status: str, superseded_by: str = "") -> None:
    upsert_run(
        exp,
        RunRecord(
            run_id=run_id,
            profile="ml",
            cluster="alpha",
            ssh_target="me@alpha.edu",
            remote_path="/scratch/ml",
            job_name="ml_array",
            job_ids=["1"],
            total_tasks=1,
            submitted_at="2026-07-29T00:00:00+00:00",
            experiment_dir=str(exp),
            status=status,
            campaign_id="study_alpha",
            superseded_by=superseded_by,
        ),
    )


def _detached(run_id: str, pid: int = 4242) -> CampaignRunResult:
    return CampaignRunResult(
        stage_reached="detached",
        needs_decision=False,
        reason="detached",
        run_id=run_id,
        started=True,
        watch="journal",
        detached_pid=pid,
    )


def _states(exp: Path) -> dict[str, str]:
    return {str(i.get("item_id")): str(i.get("state")) for i in read_intake_items(exp)}


# ── (a) it actually drains ───────────────────────────────────────────────────


def test_retiring_run_chains_one_tick_and_the_next_waiting_item_starts(tmp_path: Path) -> None:
    """THE gap this closes. Two items arrive; the first dispatch starts one and
    leaves the second waiting (``max_dispatches`` is 1 by design). Nothing else
    ever ticks the queue — so without the wake edge item-2 sits forever. When
    item-1's run RETIRES, its driver's chain starts item-2, for real: the ledger
    moves it to ``placed`` and the one actuation seat is called with ITS run id.
    """
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", run_id="ml-aaaa1111", cmd_sha="aaaa1111")
    _enqueue(exp, "item-2", run_id="ml-bbbb2222", cmd_sha="bbbb2222")

    with mock.patch(_RUN, return_value=_detached("ml-aaaa1111")):
        first = queue_dispatch(experiment_dir=exp, spec=QueueDispatchSpec(now=_NOW))
    assert [row.item_id for row in first.dispatched] == ["item-1"]
    assert _states(exp) == {"item-1": "placed", "item-2": "queued"}

    # item-1's run retires — the event the wake edge exists for.
    _seed_run(exp, "ml-aaaa1111", status="complete")

    with mock.patch(_RUN, return_value=_detached("ml-bbbb2222")) as m_run:
        chained = chain_dispatch_on_retire(exp, run_id="ml-aaaa1111", origin="campaign-run")

    assert chained is not None
    assert chained["chained"] is True
    assert chained["origin"] == "campaign-run"
    assert chained["stage_reached"] == "dispatched"
    assert chained["dispatched"] == 1
    assert "retired" in chained["reason"]  # disclosure: never silent

    # EXACTLY ONE tick, and the item that was waiting is the one that started.
    m_run.assert_called_once()
    assert m_run.call_args.kwargs["spec"].aggregate.run_id == "ml-bbbb2222"
    assert _states(exp)["item-2"] == "placed"


def test_supersession_retires_and_chains_like_a_terminal(tmp_path: Path) -> None:
    """``run_occupies`` is the ONE predicate, and it retires a slot on
    ``superseded_by`` as well as on a terminal status. A hook that pattern-matched
    a terminal stage set instead would miss the whole supersession half."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", run_id="ml-aaaa1111", cmd_sha="aaaa1111")
    _seed_run(exp, "ml-cccc3333", status="in_flight", superseded_by="ml-dddd4444")

    with mock.patch(_RUN, return_value=_detached("ml-aaaa1111")) as m_run:
        chained = chain_dispatch_on_retire(exp, run_id="ml-cccc3333", origin="block-drive")

    assert chained is not None and chained["chained"] is True
    assert "superseded by ml-dddd4444" in chained["reason"]
    m_run.assert_called_once()


def test_a_still_occupying_run_does_not_chain(tmp_path: Path) -> None:
    """The negative twin of the retirement gate. Without it the hook would be
    'always chain at a terminal step' — and ``campaign-run``'s ``run_timeout``
    stage IS a terminal step whose cluster jobs are still live, so the queue
    would be ticked on every budget expiry with no capacity freed."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", run_id="ml-aaaa1111", cmd_sha="aaaa1111")
    _seed_run(exp, "ml-cccc3333", status="in_flight")

    with mock.patch(_RUN) as m_run:
        chained = chain_dispatch_on_retire(exp, run_id="ml-cccc3333", origin="campaign-run")

    assert chained is not None
    assert chained["chained"] is False
    assert "still occupies a pool slot" in chained["reason"]
    m_run.assert_not_called()


def test_a_run_with_no_record_retired_nothing(tmp_path: Path) -> None:
    """``run_occupies(None)`` is False, but its docstring says ``None`` means
    'no record', never 'no slot'. A submit that failed before minting a record
    freed no capacity, so it must not read as a retirement."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", run_id="ml-aaaa1111", cmd_sha="aaaa1111")
    _seed_run(exp, "ml-aaaa1111", status="complete")  # a journal namespace exists

    with mock.patch(_RUN) as m_run:
        chained = chain_dispatch_on_retire(exp, run_id="ml-never-existed", origin="campaign-run")

    assert chained is not None
    assert chained["chained"] is False
    assert "nothing retired" in chained["reason"]
    m_run.assert_not_called()


# ── the two driver seats, positively ─────────────────────────────────────────


def test_campaign_run_synchronous_terminal_chains_exactly_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seat 1 — the driver ``queue-dispatch`` itself starts. The detached CHILD
    re-enters this body with ``detach=False``, and THAT is the tick that retires
    the run, so the hook fires here once, tagged ``campaign-run``."""
    import hpc_agent.ops.campaign_run as cr

    exp = _exp(tmp_path)
    _enqueue(exp, "item-2", run_id="ml-bbbb2222", cmd_sha="bbbb2222")
    _seed_run(exp, "ml-aaaa1111", status="complete")

    # Bound BEFORE the patch: the dispatcher resolves this same name lazily, so
    # faking the actuation seat also fakes the function under test.
    real_campaign_run = cr.campaign_run
    monkeypatch.setattr(
        cr,
        "_campaign_run_impl",
        lambda *_a, **_k: CampaignRunResult(
            stage_reached="complete",
            needs_decision=False,
            reason="iteration spine complete",
            run_id="ml-aaaa1111",
        ),
    )
    spec = CampaignRunSpec.model_validate(
        {
            "submit": {"submit": {"submit": _submit_spec("ml-aaaa1111")}},
            "status": {"monitor": {"run_id": "ml-aaaa1111"}},
            "aggregate": {"run_id": "ml-aaaa1111"},
            "campaign_id": "study_alpha",
            "detach": False,
        }
    )
    with mock.patch(_RUN, return_value=_detached("ml-bbbb2222")) as m_run:
        result = real_campaign_run(exp, spec=spec)

    assert result.queue_chain is not None
    assert result.queue_chain["chained"] is True
    assert result.queue_chain["origin"] == "campaign-run"
    assert result.queue_chain["run_id"] == "ml-aaaa1111"
    # The wake really drained: the waiting item entered its lifecycle, once.
    m_run.assert_called_once()
    assert _states(exp)["item-2"] == "placed"


def test_block_drive_terminal_chains_once_and_other_actions_never_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seat 2 — the driver the ``queue-drain`` plan relays. Exactly one hook call
    per tick, on the ``terminal`` return only: a park or a detach handed the run
    on rather than retiring it, and chaining there would tick the queue on every
    intermediate span (the amplification this pins against)."""
    import hpc_agent._kernel.lifecycle.block_drive as bd

    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", run_id="ml-bbbb2222", cmd_sha="bbbb2222")
    _seed_run(exp, "ml-aaaa1111", status="complete")

    calls: list[str] = []
    real_chain = chain_dispatch_on_retire

    def _spy(experiment_dir: Path, **kwargs: Any) -> Any:
        calls.append(str(kwargs.get("origin")))
        return real_chain(experiment_dir, **kwargs)

    def _drive(first: dict[str, Any]) -> Any:
        monkeypatch.setattr(bd, "_run_block_verb", lambda *_a, **_k: (first, 0))
        with (
            mock.patch("hpc_agent.ops.queue.chain.chain_dispatch_on_retire", _spy),
            mock.patch(_RUN, return_value=_detached("ml-bbbb2222")),
        ):
            return bd._chain(
                exp,
                run_id="ml-aaaa1111",
                workflow="aggregate",
                first_verb="aggregate-run",
                first_spec={"run_id": "ml-aaaa1111"},
                first_label="chained",
            )

    # A DETACHED span: the child owns the poll — nothing retired.
    detached, _ = _drive({"stage_reached": "detached", "started": True, "watch": "journal"})
    assert detached.action == "detached"
    assert detached.queue_chain is None
    assert calls == []

    # A span that PARKS for a human: the run is still very much alive.
    monkeypatch.setattr(bd, "park", lambda *_a, **_k: None)
    parked, _ = _drive({"stage_reached": "canary_ok", "needs_decision": True})
    assert parked.action == "awaiting_decision"
    assert parked.queue_chain is None
    assert calls == []

    # The TERMINAL span: one call, tagged with this seat, and the queue drains.
    terminal, code = _drive({"stage_reached": "harvested", "needs_decision": False})
    assert (terminal.action, code) == ("terminal", 0)
    assert calls == ["block-drive"]
    assert terminal.queue_chain is not None
    assert terminal.queue_chain["chained"] is True
    assert _states(exp)["item-1"] == "placed"


# ── (b) a chain failure cannot cost a settlement ─────────────────────────────


def test_chain_failure_is_data_and_never_raises(tmp_path: Path) -> None:
    """Fire-and-forget: a dispatcher that blows up comes back as ``error`` on the
    disclosure, not as an exception into the driver that just settled a run."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", run_id="ml-aaaa1111", cmd_sha="aaaa1111")
    _seed_run(exp, "ml-aaaa1111", status="complete")

    boom = RuntimeError("the ledger is on a full disk")
    with mock.patch("hpc_agent.ops.queue.dispatch.queue_dispatch", side_effect=boom):
        chained = chain_dispatch_on_retire(exp, run_id="ml-aaaa1111", origin="campaign-run")

    assert chained is not None
    assert chained["chained"] is True  # the attempt is disclosed, not hidden
    assert chained["error"] == "RuntimeError: the ledger is on a full disk"
    assert "settled and unaffected" in chained["reason"]


def test_campaign_run_terminal_settles_before_and_despite_a_failing_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The seat contract at the driver ``queue-dispatch`` itself starts: the
    recorded terminal (what a re-invoke REPLAYS) is written BEFORE the wake runs,
    so a chain that raises leaves the iteration's settlement byte-identical and
    the failure rides back as data on the result."""
    import hpc_agent.ops.campaign_run as cr

    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", run_id="ml-aaaa1111", cmd_sha="aaaa1111")
    _seed_run(exp, "ml-aaaa1111", status="complete")

    impl = CampaignRunResult(
        stage_reached="complete",
        needs_decision=False,
        reason="iteration spine complete",
        run_id="ml-aaaa1111",
    )
    monkeypatch.setattr(cr, "_campaign_run_impl", lambda *_a, **_k: impl)
    recorded: list[str] = []
    monkeypatch.setattr(
        cr,
        "_record_campaign_terminal",
        lambda _e, *, key_run_id, result: recorded.append(f"{key_run_id}:{result.stage_reached}"),
    )

    spec = CampaignRunSpec.model_validate(
        {
            "submit": {"submit": {"submit": _submit_spec("ml-aaaa1111")}},
            "status": {"monitor": {"run_id": "ml-aaaa1111"}},
            "aggregate": {"run_id": "ml-aaaa1111"},
            "campaign_id": "study_alpha",
            "detach": False,
        }
    )
    with mock.patch(
        "hpc_agent.ops.queue.dispatch.queue_dispatch", side_effect=RuntimeError("nope")
    ):
        result = cr.campaign_run(exp, spec=spec)

    # The settlement is intact: same stage, recorded terminal written.
    assert result.stage_reached == "complete"
    assert recorded == ["ml-aaaa1111:complete"]
    # …and the wake's failure is visible rather than swallowed.
    assert result.queue_chain is not None
    assert result.queue_chain["error"].startswith("RuntimeError")


def test_block_drive_terminal_survives_a_failing_chain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other driver seat, same contract: the tick still reports ``terminal``
    and its exit code, with the wake's failure carried as disclosure."""
    import hpc_agent._kernel.lifecycle.block_drive as bd

    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", run_id="ml-aaaa1111", cmd_sha="aaaa1111")
    _seed_run(exp, "ml-aaaa1111", status="complete")

    monkeypatch.setattr(
        bd,
        "_run_block_verb",
        lambda *_a, **_k: ({"stage_reached": "harvested", "needs_decision": False}, 0),
    )
    with mock.patch(
        "hpc_agent.ops.queue.dispatch.queue_dispatch", side_effect=RuntimeError("nope")
    ):
        result, code = bd._chain(
            exp,
            run_id="ml-aaaa1111",
            workflow="aggregate",
            first_verb="aggregate-run",
            first_spec={"run_id": "ml-aaaa1111"},
            first_label="chained",
        )

    assert (result.action, code) == ("terminal", 0)
    assert result.queue_chain is not None
    assert result.queue_chain["origin"] == "block-drive"
    assert result.queue_chain["error"].startswith("RuntimeError")


# ── (c) the empty-ledger chain is a cheap no-op that stops ───────────────────


def test_no_intake_ledger_costs_one_stat_and_chains_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§7 relaunch-cheapness, applied to the wake edge. An experiment that never
    used the queue must not pay a journal read, must not import the dispatcher,
    and must NOT scaffold the ``.hpc`` tree it is probing (F46: a read never
    creates). ``None`` — nothing happened, nothing to disclose."""
    import hpc_agent.state.journal as journal

    exp = _exp(tmp_path)
    _seed_run(exp, "ml-aaaa1111", status="complete")

    def _forbidden(*_a: Any, **_k: Any) -> None:
        raise AssertionError("the empty-ledger chain must not read the journal")

    monkeypatch.setattr(journal, "load_run", _forbidden)
    with mock.patch("hpc_agent.ops.queue.dispatch.queue_dispatch") as m_dispatch:
        assert chain_dispatch_on_retire(exp, run_id="ml-aaaa1111", origin="campaign-run") is None

    m_dispatch.assert_not_called()
    assert not (exp / ".hpc" / "queue").exists()


def test_empty_ledger_chain_stops_rather_than_re_chaining(tmp_path: Path) -> None:
    """The recursion terminates on a DRY ledger. A ledger that exists but holds
    nothing runs exactly one tick, that tick reports ``nothing_to_dispatch``,
    and it starts nothing — so no second retirement is manufactured and the
    cadence ends of its own accord rather than by a counter."""
    from hpc_agent.state.queue_intake import intake_path

    exp = _exp(tmp_path)
    ledger = intake_path(exp)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("", encoding="utf-8")
    _seed_run(exp, "ml-aaaa1111", status="complete")

    with mock.patch(_RUN) as m_run:
        chained = chain_dispatch_on_retire(exp, run_id="ml-aaaa1111", origin="campaign-run")

    assert chained is not None
    assert chained["chained"] is True
    assert chained["stage_reached"] == "nothing_to_dispatch"
    assert chained["dispatched"] == 0
    assert chained.get("error") is None
    m_run.assert_not_called()  # nothing started → nothing to retire → no re-chain


# ── (d) no amplification ─────────────────────────────────────────────────────


def test_one_retirement_chains_exactly_one_dispatch_tick(tmp_path: Path) -> None:
    """The bound stated as a count: one call → one ``queue-dispatch`` invocation,
    however many items that tick then places."""
    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", run_id="ml-aaaa1111", cmd_sha="aaaa1111")
    _enqueue(exp, "item-2", run_id="ml-bbbb2222", cmd_sha="bbbb2222")
    _seed_run(exp, "ml-cccc3333", status="complete")

    real = queue_dispatch
    calls: list[QueueDispatchSpec | None] = []

    def _counting(**kwargs: Any) -> Any:
        calls.append(kwargs.get("spec"))
        return real(**kwargs)

    with (
        mock.patch("hpc_agent.ops.queue.dispatch.queue_dispatch", _counting),
        mock.patch(_RUN, return_value=_detached("ml-aaaa1111")),
    ):
        chained = chain_dispatch_on_retire(exp, run_id="ml-cccc3333", origin="campaign-run")

    assert chained is not None and chained["chained"] is True
    assert len(calls) == 1
    # LEDGER-WIDE, never narrowed: placement authority stays queue-advance's.
    assert calls[0] is not None and calls[0].item_ids is None


def test_a_chained_dispatch_cannot_re_enter_the_chain_in_process(tmp_path: Path) -> None:
    """The structural reason the cadence cannot storm: ``queue-dispatch``'s only
    actuation seat is ``campaign_run(spec, detach=True)``, and the DETACHED parent
    branch returns a handle without reaching the synchronous body the hook lives
    on. So the item this tick starts chains later, from its own child — never
    inside this call."""
    import hpc_agent.ops.campaign_run as cr

    exp = _exp(tmp_path)
    _enqueue(exp, "item-1", run_id="ml-aaaa1111", cmd_sha="aaaa1111")
    _seed_run(exp, "ml-cccc3333", status="complete")

    entered: list[str] = []
    real_chain = chain_dispatch_on_retire

    def _spy(experiment_dir: Path, **kwargs: Any) -> Any:
        entered.append(str(kwargs.get("origin")))
        return real_chain(experiment_dir, **kwargs)

    launched: list[str] = []

    class _Launch:
        run_id = "ml-aaaa1111"
        pid = 999
        log_path = None

    def _launch(*, verb: str, experiment_dir: str, spec: dict[str, Any]) -> _Launch:
        assert (verb, experiment_dir) == ("campaign-run", str(exp))
        launched.append(str(spec.get("aggregate", {}).get("run_id")))
        return _Launch()

    with (
        mock.patch("hpc_agent.ops.queue.chain.chain_dispatch_on_retire", _spy),
        mock.patch(
            "hpc_agent._kernel.lifecycle.detached.launch_submit_block_detached",
            _launch,
        ),
    ):
        # The REAL campaign_run runs here — with detach=True, exactly as the
        # dispatcher composes it (ops/queue/dispatch.py::_campaign_run_spec).
        chained = cr.campaign_run(
            exp,
            spec=CampaignRunSpec.model_validate(
                {
                    "submit": {"submit": {"submit": _submit_spec("ml-aaaa1111")}},
                    "status": {"monitor": {"run_id": "ml-aaaa1111"}},
                    "aggregate": {"run_id": "ml-aaaa1111"},
                    "campaign_id": "study_alpha",
                    "detach": True,
                }
            ),
        )

    assert chained.stage_reached == "detached"
    assert chained.queue_chain is None  # the parent retired nothing
    assert launched == ["ml-aaaa1111"]  # the child was spawned…
    assert entered == []  # …and the parent never reached the hook
