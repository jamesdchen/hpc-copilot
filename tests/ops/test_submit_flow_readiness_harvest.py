"""The submit flow's readiness HARVEST sites (s2-readiness pillar 3).

A submit already learns, in passing, three of the five invariants S2 needs:
staging proves the scratch filesystem took real bytes, the array dispatch proves
the scheduler answered, and the activation-class preflight proves the remote env
resolved. Throwing those away is why the NEXT submit rediscovers them from a
dead worker's log.

The one rule these sites live under is **harvest, never probe**. Every test in
this module runs under the autouse no-network tripwire, so a feed that grew a
dial — the failure mode that survived a whole suite unnoticed on 2026-07-30 —
fails here loudly instead of silently costing a connection per submit.

What is pinned: the atom lands, under the SUBJECT a sensor would use (so a
harvested and a sensed reading share one ledger row rather than disagreeing on
two), with the verdict the evidence actually supports — and NOTHING lands where
the flow holds no verdict about that invariant.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from hpc_agent import errors
from hpc_agent.ops import submit_flow
from hpc_agent.state import readiness
from tests._no_network import no_network  # noqa: F401 — autouse zero-network tripwire

HOST = "login.example.edu"
TARGET = f"someone@{HOST}"
REMOTE_PATH = "/u/scratch/someone/proj"


@pytest.fixture(autouse=True)
def _journal_home(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the machine-scoped ledger at a per-test dir (never the real home)."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))


def _atoms(host: str = HOST) -> dict[str, dict[str, Any]]:
    """The ledger's atoms keyed by sensor (one per sensor at these feed sites)."""
    stored = readiness.read_ledger(host)["atoms"]
    assert isinstance(stored, list)
    return {str(atom["sensor"]): atom for atom in stored}


# ── staging → scratch ────────────────────────────────────────────────────────


def _stage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    *,
    push_rc: int = 0,
    push_stderr: str = "",
) -> None:
    """Drive ``_push_and_deploy_once`` with both transport legs faked out."""
    monkeypatch.setattr(
        submit_flow,
        "rsync_push",
        lambda **_kw: subprocess.CompletedProcess(["rsync"], push_rc, "", push_stderr),
    )
    monkeypatch.setattr(submit_flow, "deploy_runtime", lambda **_kw: None)
    monkeypatch.setattr(submit_flow, "_code_trees_enabled", lambda: False)
    submit_flow._push_and_deploy_once(
        experiment_dir=tmp_path,
        ssh_target=TARGET,
        remote_path=REMOTE_PATH,
        rsync_excludes=None,
    )


def test_a_landed_stage_feeds_scratch_ok(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Stronger evidence than any sensor takes: the filesystem accepted real bytes
    and a real write, not a ``test -d``."""
    _stage(monkeypatch, tmp_path)
    scratch = _atoms()["scratch"]
    assert scratch["verdict"] == "ok"
    assert scratch["target"] == REMOTE_PATH
    assert scratch["route"] == "effective"
    assert scratch["source"] == "submit-flow"


def test_the_scratch_atom_is_filed_under_the_PATH_so_a_sensor_agrees(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """``sense_scratch`` files its atom under the scratch PATH. If the harvest
    filed under the host instead, one cluster would carry two ``scratch`` rows
    that disagree and the render would show both as current.

    The sensor half of this pairing is pinned in
    ``tests/infra/test_readiness_invariant_sensors.py`` — deliberately NOT here:
    running a sensor inside a module that promises no network is a contradiction,
    and the tripwire says so (``ssh_argv`` shells ``ssh -V`` to pick its crypto).
    """
    _stage(monkeypatch, tmp_path)
    assert readiness.atom_identity(_atoms()["scratch"]) == ("scratch", "effective", REMOTE_PATH)


def test_the_scheduler_atom_is_filed_under_the_BACKEND_FAMILY() -> None:
    """Same pairing for the dispatch feed: ``sense_scheduler``'s subject is the
    family, so a harvested "the scheduler took an array" and a sensed "the CLI
    answered" land on ONE row instead of two that can disagree."""
    submit_flow._harvest_readiness(TARGET, "scheduler", "ok", target="sge")
    assert readiness.atom_identity(_atoms()["scheduler"]) == ("scheduler", "effective", "sge")


def test_a_storage_failure_feeds_scratch_down(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    with pytest.raises(errors.RemoteCommandFailed):
        _stage(
            monkeypatch,
            tmp_path,
            push_rc=11,
            push_stderr="rsync: write failed: No space left on device (28)",
        )
    scratch = _atoms()["scratch"]
    assert scratch["verdict"] == "down"
    assert "No space left on device" in scratch["detail"]


def test_a_transport_failure_records_NOTHING_about_scratch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """A severed tunnel is a transport fact. Recording ``scratch: down`` for it
    would send the next human to the filesystem while the VPN is what is broken —
    the 2026-07-30 misdiagnosis class, reproduced one layer down.

    Silence here is the correct behaviour, not a gap: the flap classifier already
    owns this failure, and a feed site that guesses is worse than one that abstains.
    """
    with pytest.raises(errors.RemoteCommandFailed):
        _stage(
            monkeypatch,
            tmp_path,
            push_rc=255,
            push_stderr="ssh_exchange_identification: Connection closed by remote host",
        )
    assert "scratch" not in _atoms()


def test_the_scratch_atom_lands_only_after_the_deploy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """The atom means "staging landed", not "the transfer started". A deploy that
    raises must leave no ``ok`` claiming otherwise."""
    monkeypatch.setattr(
        submit_flow,
        "rsync_push",
        lambda **_kw: subprocess.CompletedProcess(["rsync"], 0, "", ""),
    )

    def _boom(**_kw: object) -> None:
        raise errors.RemoteCommandFailed("deploy died")

    monkeypatch.setattr(submit_flow, "deploy_runtime", _boom)
    with pytest.raises(errors.RemoteCommandFailed):
        submit_flow._push_and_deploy_once(
            experiment_dir=tmp_path,
            ssh_target=TARGET,
            remote_path=REMOTE_PATH,
            rsync_excludes=None,
        )
    assert "scratch" not in _atoms()


# ── activation-class preflight → env ─────────────────────────────────────────

UV_ENV = {
    "HPC_RUNTIME": "uv",
    "MODULES": "python/3.11",
    "CONDA_SOURCE": "/opt/conda/etc/profile.d/conda.sh",
    "CONDA_ENV": "hpc",
}


def test_a_passing_activation_preflight_feeds_env_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe runs the cluster's OWN activation and then asks the activated env
    a question — which IS the env invariant."""
    monkeypatch.setattr(submit_flow, "_preflight_runtime_check", lambda *_a, **_k: None)
    submit_flow._run_uv_preflight_for_batch(
        ssh_target=TARGET, job_envs=[dict(UV_ENV)], skip_preflight=False
    )
    env = _atoms()["env"]
    assert env["verdict"] == "ok"
    assert env["source"] == "submit-flow"


def test_a_failing_activation_preflight_feeds_env_down_and_still_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harvest must not swallow the error it harvests: the caller still needs
    the actionable ``SpecInvalid``."""

    def _boom(*_a: object, **_k: object) -> None:
        raise errors.SpecInvalid("uv was not found on PATH after activating the cluster env")

    monkeypatch.setattr(submit_flow, "_preflight_runtime_check", _boom)
    with pytest.raises(errors.SpecInvalid):
        submit_flow._run_uv_preflight_for_batch(
            ssh_target=TARGET, job_envs=[dict(UV_ENV)], skip_preflight=False
        )
    env = _atoms()["env"]
    assert env["verdict"] == "down"
    assert "uv was not found" in env["detail"]


def test_a_severed_tunnel_records_NOTHING_about_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The preflight raises on a dead tunnel too, and ``env: down`` for that is
    the 2026-07-30 misdiagnosis one layer down — the human sent to the conda env
    while the VPN is what broke.

    ``_stage_failure_is_flap`` is this module's ONE transport-class definition, so
    the ledger and the staging retry cannot disagree about the same exception.
    """

    def _severed(*_a: object, **_k: object) -> None:
        raise errors.SshUnreachable(
            "ssh: connect to host login.example.edu port 22: Connection refused"
        )

    monkeypatch.setattr(submit_flow, "_preflight_runtime_check", _severed)
    with pytest.raises(errors.SshUnreachable):
        submit_flow._run_uv_preflight_for_batch(
            ssh_target=TARGET, job_envs=[dict(UV_ENV)], skip_preflight=False
        )
    assert "env" not in _atoms()
    # ...and the classification really is the shared one, not a local re-derivation.
    assert submit_flow._stage_failure_is_flap(
        errors.SshUnreachable("ssh: connect to host h port 22: Connection refused")
    )


def test_a_timeout_records_NOTHING_about_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The wrapper's own bound is transport, not an env verdict."""

    def _slow(*_a: object, **_k: object) -> None:
        raise TimeoutError("ssh timed out after 60s")

    monkeypatch.setattr(submit_flow, "_preflight_runtime_check", _slow)
    with pytest.raises(TimeoutError):
        submit_flow._run_uv_preflight_for_batch(
            ssh_target=TARGET, job_envs=[dict(UV_ENV)], skip_preflight=False
        )
    assert "env" not in _atoms()


def test_a_cache_hit_feeds_nothing_rather_than_forging_an_age(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """NOTHING was observed on a TTL cache hit. Stamping an atom anyway would mint
    a fresh age for a stale fact — the ledger's one job is to be honest about age,
    so a feed that re-stamps cached evidence defeats the whole substrate.
    """
    from hpc_agent.state import preflight_cache

    monkeypatch.setattr(preflight_cache, "is_preflight_fresh", lambda _key: True)

    def _must_not_run(*_a: object, **_k: object) -> None:
        raise AssertionError("the cache hit should have skipped the probe entirely")

    monkeypatch.setattr(submit_flow, "_preflight_runtime_check", _must_not_run)
    submit_flow._run_uv_preflight_for_batch(
        ssh_target=TARGET, job_envs=[dict(UV_ENV)], skip_preflight=False
    )
    assert _atoms() == {}


def test_a_skipped_preflight_feeds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """``skip_preflight`` means no probe ran, so there is no verdict to harvest."""
    monkeypatch.setattr(submit_flow, "_preflight_runtime_check", lambda *_a, **_k: None)
    submit_flow._run_uv_preflight_for_batch(
        ssh_target=TARGET, job_envs=[dict(UV_ENV)], skip_preflight=True
    )
    assert _atoms() == {}


def test_a_non_uv_batch_feeds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """No activation probe runs for a non-uv runtime, so nothing is learned."""
    monkeypatch.setattr(submit_flow, "_preflight_runtime_check", lambda *_a, **_k: None)
    submit_flow._run_uv_preflight_for_batch(
        ssh_target=TARGET, job_envs=[{"HPC_RUNTIME": "conda"}], skip_preflight=False
    )
    assert _atoms() == {}


# ── dispatch → scheduler, driven through the real submit path ────────────────
#
# The direct-call tests above pin the FEED; these pin that it is REACHABLE. A
# harvest site whose call is only ever exercised by calling it directly is not a
# verified guard — the line could sit in a branch nothing takes and every test
# would still pass.


def _flow_spec(**overrides: object) -> Any:
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

    base: dict[str, Any] = {
        "profile": "p",
        "cluster": "c",
        "ssh_target": TARGET,
        "remote_path": REMOTE_PATH,
        "job_name": "j",
        "run_id": "rX",
        "total_tasks": 4,
        # The wire spec validates ``backend`` against the registered (lower-case)
        # names, so this is already the family the sensor uses as its subject —
        # the feed's own normalization is belt-and-braces on top of that, kept so
        # both sides compute the subject with the SAME expression.
        "backend": "sge",
        "script": "run.sh",
        "job_env": {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
        "result_dir_template": "results/{run_id}/task_{task_id}",
    }
    base.update(overrides)
    return SubmitFlowSpec(**base)


def _drive_dispatch(tmp_path: Any, monkeypatch: pytest.MonkeyPatch, *, dispatch: Any) -> None:
    """Run ``_submit_one_spec`` with every remote leg stubbed out."""
    monkeypatch.setattr(
        submit_flow,
        "_augment_job_env",
        lambda *_a, **_k: {"EXECUTOR": "python3 .hpc/_hpc_dispatch.py"},
    )
    monkeypatch.setattr(submit_flow, "build_remote_backend", lambda *_a, **_k: object())
    monkeypatch.setattr(submit_flow, "_submit_main_array", dispatch)
    monkeypatch.setattr(submit_flow, "submit_and_record", lambda *_a, **_k: None)
    submit_flow._submit_one_spec(
        experiment_dir=tmp_path, spec=_flow_spec(), canary_decision=(False, "not under test")
    )


def test_an_accepted_array_feeds_scheduler_ok_from_the_real_path(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, journal_home: Any
) -> None:
    """The scheduler ACCEPTED an array and returned ids — stronger evidence than
    any version-banner touch can give (a banner proves the CLI answered; this
    proves the scheduler took work)."""
    _drive_dispatch(tmp_path, monkeypatch, dispatch=lambda *_a, **_k: (["300", "301"], None))
    scheduler = _atoms()["scheduler"]
    assert scheduler["verdict"] == "ok"
    # Filed under the backend family — the same subject ``sense_scheduler`` uses.
    assert scheduler["target"] == "sge"
    assert "300,301" in scheduler["detail"]


def test_a_refused_dispatch_feeds_scheduler_down_from_the_real_path(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, journal_home: Any
) -> None:
    """...and the typed failure still propagates: the harvest observes, it does
    not intercept."""

    def _refuse(*_a: object, **_k: object) -> None:
        raise errors.RemoteCommandFailed("qsub: Unauthorized Request MSG=qsub not allowed")

    with pytest.raises(errors.RemoteCommandFailed):
        _drive_dispatch(tmp_path, monkeypatch, dispatch=_refuse)
    scheduler = _atoms()["scheduler"]
    assert scheduler["verdict"] == "down"
    assert "Unauthorized Request" in scheduler["detail"]
    # The discriminator really fired rather than the verdict landing by default:
    # this exception is NOT transport-class.
    assert not submit_flow._stage_failure_is_flap(
        errors.RemoteCommandFailed("qsub: Unauthorized Request")
    )


def test_a_severed_tunnel_records_NOTHING_about_the_scheduler(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, journal_home: Any
) -> None:
    """A dispatch dies on a dead tunnel as readily as on a rejection, and
    ``scheduler: down`` for that sends the next human to the queue system while
    the VPN is what broke.

    This handler would otherwise classify ONE exception two ways: the comment
    block a dozen lines below it treats exactly this failure as a flap for job-id
    recovery purposes. Routing both through ``_stage_failure_is_flap`` is what
    makes that impossible.
    """

    def _severed(*_a: object, **_k: object) -> None:
        raise errors.RemoteCommandFailed(
            "ssh: connect to host login.example.edu port 22: Connection refused"
        )

    with pytest.raises(errors.RemoteCommandFailed):
        _drive_dispatch(tmp_path, monkeypatch, dispatch=_severed)
    assert "scheduler" not in _atoms()


def test_an_open_circuit_records_NOTHING_about_the_scheduler(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch, journal_home: Any
) -> None:
    """The breaker already judged the transport; nothing reached the scheduler."""

    def _fenced(*_a: object, **_k: object) -> None:
        raise errors.SshCircuitOpen("ssh circuit for host 'login.example.edu' is OPEN")

    with pytest.raises(errors.SshCircuitOpen):
        _drive_dispatch(tmp_path, monkeypatch, dispatch=_fenced)
    assert "scheduler" not in _atoms()


# ── the feed itself: fail-open, and never a probe ────────────────────────────


def test_a_broken_ledger_never_perturbs_a_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Readiness is a freshness signal, never a correctness gate. A ledger that
    explodes must not be the reason a healthy submit fails."""

    def _explode(*_a: object, **_k: object) -> None:
        raise RuntimeError("ledger on fire")

    monkeypatch.setattr(readiness, "record_observation", _explode)
    submit_flow._harvest_readiness(TARGET, "scratch", "ok")  # must not raise


def test_an_unknown_sensor_name_creates_no_phantom_atom() -> None:
    """A typo at a feed site must not invent a ledger row nothing can interpret."""
    submit_flow._harvest_readiness(TARGET, "storage", "ok")
    assert _atoms() == {}


def test_the_feed_invents_no_latency() -> None:
    """These sites measure nothing of their own. A fabricated duration would be
    the single dishonest field in an evidence record — and ``latency_ms`` is what
    a reader uses to rank "slow but up" against "down"."""
    submit_flow._harvest_readiness(TARGET, "scheduler", "ok", target="sge")
    assert _atoms()["scheduler"]["latency_ms"] is None
