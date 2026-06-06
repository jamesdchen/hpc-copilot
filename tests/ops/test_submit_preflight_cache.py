"""TTL preflight cache + its submit-flow wiring (#255).

The cache skips the cluster-side ``command -v uv`` round-trip when the same
``(host, env-activation, framework-version)`` was validated within the TTL.
These tests pin the freshness/invalidation semantics and assert submit-flow
consults the cache before paying the SSH probe.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from hpc_agent.state import preflight_cache


@pytest.fixture(autouse=True)
def _redirect_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path))
    monkeypatch.delenv("HPC_NO_PREFLIGHT_CACHE", raising=False)
    monkeypatch.delenv("HPC_PREFLIGHT_TTL_SEC", raising=False)


def _key(activation="m|s|env", version="0.10.5"):
    return preflight_cache.preflight_cache_key(
        host="hoffman2", activation=activation, version=version
    )


def test_key_changes_with_activation_and_version():
    base = _key()
    assert base != _key(activation="m2|s|env")  # conda env / modules edit
    assert base != _key(version="0.10.6")  # framework bump
    assert base == _key()  # stable for identical inputs


def test_miss_then_hit_within_ttl():
    key = _key()
    assert preflight_cache.is_preflight_fresh(key) is False  # nothing recorded yet
    preflight_cache.record_preflight(key, checks=["uv_present"])
    assert preflight_cache.is_preflight_fresh(key) is True


def test_expired_entry_is_not_fresh():
    key = _key()
    preflight_cache.record_preflight(key)
    # Look at the entry from far in the future — past the default TTL.
    future = datetime.now(timezone.utc) + timedelta(seconds=preflight_cache.DEFAULT_TTL_SEC + 1)
    assert preflight_cache.is_preflight_fresh(key, now=future) is False


def test_ttl_override_env(monkeypatch):
    monkeypatch.setenv("HPC_PREFLIGHT_TTL_SEC", "5")
    key = _key()
    preflight_cache.record_preflight(key)
    now = datetime.now(timezone.utc)
    assert preflight_cache.is_preflight_fresh(key, now=now + timedelta(seconds=3)) is True
    assert preflight_cache.is_preflight_fresh(key, now=now + timedelta(seconds=8)) is False


def test_disable_env_forces_miss(monkeypatch):
    key = _key()
    preflight_cache.record_preflight(key)
    assert preflight_cache.is_preflight_fresh(key) is True
    monkeypatch.setenv("HPC_NO_PREFLIGHT_CACHE", "1")
    # Disabled: never fresh (re-run), and record is a no-op.
    assert preflight_cache.is_preflight_fresh(key) is False
    preflight_cache.record_preflight(_key(activation="other"))
    monkeypatch.delenv("HPC_NO_PREFLIGHT_CACHE")
    assert preflight_cache.is_preflight_fresh(_key(activation="other")) is False


def test_corrupt_cache_file_is_a_miss(tmp_path):
    # A torn / non-JSON cache must collapse to "re-run", never raise.
    path = preflight_cache._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    assert preflight_cache.is_preflight_fresh(_key()) is False


# --- submit-flow wiring -----------------------------------------------------


def test_submit_flow_skips_runtime_check_when_cache_fresh(monkeypatch):
    """A fresh cache entry means the cluster-side uv probe is NOT issued."""
    from hpc_agent.ops import submit_flow as sf

    calls: list[str] = []

    def _spy_runtime_check(ssh_target, *, job_env, skip):
        calls.append(ssh_target)

    monkeypatch.setattr(sf, "_preflight_runtime_check", _spy_runtime_check)

    # Pre-seed a fresh entry for the key submit-flow will compute.
    activation = "|".join(("modA", "/c/etc/profile.d/conda.sh", "envX"))
    key = preflight_cache.preflight_cache_key(
        host="u@hoffman2", activation=activation, version=_ver()
    )
    preflight_cache.record_preflight(key, checks=["uv_present"])

    job_env = {
        "HPC_RUNTIME": "uv",
        "MODULES": "modA",
        "CONDA_SOURCE": "/c/etc/profile.d/conda.sh",
        "CONDA_ENV": "envX",
    }
    sf._run_uv_preflight_for_batch(
        ssh_target="u@hoffman2",
        job_envs=[job_env],
        skip_preflight=False,
    )
    assert calls == [], "cache was fresh; runtime check should have been skipped"


def test_submit_flow_runs_and_records_on_miss(monkeypatch):
    from hpc_agent.ops import submit_flow as sf

    calls: list[str] = []
    monkeypatch.setattr(
        sf,
        "_preflight_runtime_check",
        lambda ssh_target, *, job_env, skip: calls.append(ssh_target),
    )

    job_env = {
        "HPC_RUNTIME": "uv",
        "MODULES": "modB",
        "CONDA_SOURCE": "/c/conda.sh",
        "CONDA_ENV": "envY",
    }
    sf._run_uv_preflight_for_batch(ssh_target="u@host2", job_envs=[job_env], skip_preflight=False)
    # First time: probe runs once, and the success is now cached.
    assert calls == ["u@host2"]
    activation = "|".join(("modB", "/c/conda.sh", "envY"))
    key = preflight_cache.preflight_cache_key(host="u@host2", activation=activation, version=_ver())
    assert preflight_cache.is_preflight_fresh(key) is True

    # Second time within TTL: no further probe.
    sf._run_uv_preflight_for_batch(ssh_target="u@host2", job_envs=[job_env], skip_preflight=False)
    assert calls == ["u@host2"]


def _ver() -> str:
    from hpc_agent import __version__

    return __version__ or ""


# --- Fix 2: uv preflight gate is closed (no agent-skip path) ----------------


def test_skip_preflight_resolution_refuses_agent_spec_field(monkeypatch):
    """#275 Fix 2: ``skip_preflight`` is operator-only. The internal kwarg is
    threaded by trusted Python callers (submit_and_verify Phase 2); the env
    var is the operator opt-in. Neither is reachable from an agent-authored
    spec. This pins the resolution function so a future refactor that re-adds
    a spec field falls a test, not a production submit."""
    from hpc_agent.ops import submit_flow as sf

    monkeypatch.delenv("HPC_AGENT_SKIP_PREFLIGHT", raising=False)
    # No internal opinion, no env var → preflight must run.
    assert sf._skip_preflight_requested(None) is False
    # Operator env var honoured.
    monkeypatch.setenv("HPC_AGENT_SKIP_PREFLIGHT", "1")
    assert sf._skip_preflight_requested(None) is True
    # Internal-trusted kwarg honoured.
    monkeypatch.delenv("HPC_AGENT_SKIP_PREFLIGHT", raising=False)
    assert sf._skip_preflight_requested(True) is True
    # Non-"1" values do NOT bypass — guard against e.g. "true" / "yes".
    monkeypatch.setenv("HPC_AGENT_SKIP_PREFLIGHT", "yes")
    assert sf._skip_preflight_requested(None) is False
    monkeypatch.setenv("HPC_AGENT_SKIP_PREFLIGHT", "true")
    assert sf._skip_preflight_requested(None) is False


def test_uv_preflight_always_runs_when_runtime_uv_and_not_skipped(monkeypatch):
    """When ``HPC_RUNTIME=uv`` is in the spec and preflight is not opted out,
    the ``command -v uv`` probe MUST fire — there is no spec-field bypass.
    This is the structural guarantee that closes the 0.10.x failure where a
    uv-on-a-uv-less-cluster spec slipped past preflight and doomed every
    task."""
    from hpc_agent.ops import submit_flow as sf

    calls: list[dict] = []

    def _spy(ssh_target, *, job_env, skip):
        calls.append({"ssh_target": ssh_target, "skip": skip, "job_env": dict(job_env)})

    monkeypatch.setattr(sf, "_preflight_runtime_check", _spy)
    # Cache disabled so the spy fires every time (we're testing reachability,
    # not freshness).
    monkeypatch.setenv("HPC_NO_PREFLIGHT_CACHE", "1")

    job_env = {
        "HPC_RUNTIME": "uv",
        "MODULES": "modA",
        "CONDA_SOURCE": "/c/conda.sh",
        "CONDA_ENV": "envX",
    }
    sf._run_uv_preflight_for_batch(ssh_target="u@host", job_envs=[job_env], skip_preflight=False)
    assert len(calls) == 1
    assert calls[0]["skip"] is False, "probe must be invoked with skip=False so it actually runs"
    assert calls[0]["job_env"]["HPC_RUNTIME"] == "uv"


def test_uv_preflight_skipped_when_no_uv_runtime(monkeypatch):
    """The probe is keyed on ``HPC_RUNTIME=uv``: a spec without it pays NO
    cluster round-trip, even if other fields are present."""
    from hpc_agent.ops import submit_flow as sf

    calls: list[str] = []
    monkeypatch.setattr(
        sf,
        "_preflight_runtime_check",
        lambda ssh_target, *, job_env, skip: calls.append(ssh_target),
    )
    job_env = {"MODULES": "modA", "CONDA_SOURCE": "/c/conda.sh", "CONDA_ENV": "envX"}
    sf._run_uv_preflight_for_batch(ssh_target="u@host", job_envs=[job_env], skip_preflight=False)
    assert calls == [], "no HPC_RUNTIME=uv → no probe"


def test_runtime_uv_preflight_raises_spec_invalid_when_uv_missing(monkeypatch):
    """The underlying ``runtime_uv_preflight`` function refuses the spec
    with :class:`errors.SpecInvalid` when ``command -v uv`` exits non-zero
    on the cluster. The error message must name uv and suggest concrete
    remediations — the closing 'refuse the spec at build time' the task
    asked us to verify."""
    from hpc_agent import errors as _errors
    from hpc_agent.infra import runtime_preflight as rp

    class _FakeProc:
        def __init__(self, rc: int, stdout: str = "", stderr: str = ""):
            self.returncode = rc
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr(
        rp, "ssh_run", lambda cmd, *, ssh_target: _FakeProc(rc=1, stderr="uv: not found")
    )
    with pytest.raises(_errors.SpecInvalid) as excinfo:
        rp.runtime_uv_preflight(
            "u@host",
            job_env={
                "HPC_RUNTIME": "uv",
                "CONDA_ENV": "envX",
                "CONDA_SOURCE": "/c/conda.sh",
                "MODULES": "",
            },
            skip=False,
        )
    msg = str(excinfo.value)
    assert "uv" in msg
    assert "drop `runtime: uv`" in msg or "pip install uv" in msg


def test_runtime_uv_preflight_no_op_when_runtime_not_uv():
    """The probe is a no-op when HPC_RUNTIME is anything other than 'uv' —
    so a non-uv-runtime spec can never fail the probe."""
    from hpc_agent.infra import runtime_preflight as rp

    # If the function tried to ssh, this would raise AttributeError; the
    # early-return must fire before any ssh call is attempted.
    rp.runtime_uv_preflight("u@host", job_env={"HPC_RUNTIME": "conda"}, skip=False)
    rp.runtime_uv_preflight("u@host", job_env={}, skip=False)


def test_submit_flow_spec_has_no_skip_preflight_field():
    """Pin the structural guarantee: ``SubmitFlowSpec`` does NOT carry a
    ``skip_preflight`` field. The only ways to bypass the preflight are the
    operator env var ``HPC_AGENT_SKIP_PREFLIGHT=1`` and the internal Python
    kwarg ``_skip_preflight=True`` (trusted callers like submit_and_verify
    Phase 2). If a future refactor re-adds the spec field, this test fails
    and the reviewer is forced to re-justify the bypass."""
    from hpc_agent._wire.workflows.submit_flow import SubmitFlowSpec

    assert "skip_preflight" not in SubmitFlowSpec.model_fields, (
        "SubmitFlowSpec must not expose a `skip_preflight` field — Fix 2 "
        "demoted it to operator-only (env var + internal kwarg). A spec "
        "field would let an agent-authored spec silence the uv guard."
    )
