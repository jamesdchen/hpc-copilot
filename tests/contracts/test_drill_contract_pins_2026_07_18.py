"""Plan-unit U5 — the 2026-07-18 drill's class-1 snags as permanent hermetic pins.

``docs/plans/sandbox-proving-run-2026-07-18.md`` §4-U5: encode the five class-1
snags of the 2026-07-18 drill attempt (the ones an autonomous sandbox run would
have eaten silently) as regression assertions. Every test docstring carries the
incident date.

Placement: ``tests/contracts/`` — every pin invokes the REAL public CLI envelope
(via :func:`tests.contracts.conftest.invoke_cli`, the same ``cli.dispatch.main``
path the MCP warm runner drives), which is this directory's stated charter:
contract tests "take the public CLI as their boundary — never reach into module
internals". Fixture seeding goes through the shared helpers
(``tests.conftest.write_hpc_tasks``), the state layer's brief journal writer,
and the U2 ``sandbox_seed`` seeder — the ``test_cli_contract_status.py``
precedent of planting substrate, then exercising the wire.

Hermetic only: no docker, no SSH, no cluster. ``HPC_JOURNAL_DIR`` /
``HPC_CLUSTERS_CONFIG`` are monkeypatched to tmp so the pins are deterministic
on any dev box.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.conftest import write_hpc_tasks
from tests.integration.scheduler.sandbox_seed import seed_utterance

from .conftest import invoke_cli

pytestmark = pytest.mark.contract

# The U2-proven authorship pair (tests/integration/scheduler/test_sandbox_seed.py):
# every >=4-char goal token appears in the utterance, and its numbers derive the
# task_generator's claims (20 stated; 19 = 20-1; 0 by the zero rule; 1M -> 1e6).
_GOAL_UTTERANCE = "please fit the garch volatility model sweep with 20 seeds at 1M samples"
_GOAL_VALUE = "garch volatility model sweep"
_TASK_GENERATOR_VALUE: dict[str, Any] = {
    "kind": "items_x_seeds",
    "params": {"seeds": list(range(20)), "items": [{"samples": 1_000_000}]},
}
_UNRELATED_UTTERANCE = "what should we order for lunch today"
_SEED_RUN_REF = "sandbox-u5-pins"


@pytest.fixture(autouse=True)
def _empty_clusters_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate ``clusters.yaml`` from the dev box's real config.

    An EMPTY config is the sanctioned ad-hoc/test pass-through
    (``build_submit_spec._cross_check_cluster_identity``: "An EMPTY config …
    has nothing to typo against, so it stays a pass-through"), so the resolve /
    walk pins neither depend on nor refuse against a populated user config.
    """
    cfg = tmp_path / "clusters.yaml"
    cfg.write_text("")
    monkeypatch.setenv("HPC_CLUSTERS_CONFIG", str(cfg))


@pytest.fixture
def sandbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An ephemeral journal home under tmp, named by ``HPC_JOURNAL_DIR``.

    ``monkeypatch.setenv`` is the documented override idiom: the env var
    out-ranks the ``HPC_HOMEDIR`` attribute the session-wide autouse
    ``_isolated_journal_home`` fixture patches (tests/conftest.py), and the U2
    seeder's structural guard REQUIRES the env pointer.
    """
    home = tmp_path / "sandbox_journal_home"
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(home))
    return home


def _invoke(
    verb: str, spec: dict[str, Any], tmp_path: Path, *extra_argv: str
) -> tuple[int, dict[str, Any], str]:
    """Write *spec* to a JSON file and invoke the REAL CLI; return (rc, envelope, stderr)."""
    spec_file = tmp_path / f"spec_{verb}.json"
    spec_file.write_text(json.dumps(spec))
    rc, stdout, stderr = invoke_cli([verb, "--spec", str(spec_file), *extra_argv])
    envelope = json.loads(stdout.strip().splitlines()[-1])
    return rc, envelope, stderr


def _append_spec(scope_id: str, resolved: dict[str, Any]) -> dict[str, Any]:
    """An append-decision input shaped the way a driver commits a greenlight."""
    return {
        "scope_kind": "run",
        "scope_id": scope_id,
        "block": "s1",
        "response": "y",
        "resolved": resolved,
    }


# ── Pin 1: bare block-drive fresh-start returns the actionable skip ───────────


def test_pin1_bare_block_drive_submit_fresh_start_returns_actionable_skip(
    tmp_path: Path,
) -> None:
    """2026-07-18 snag 1 — a bare ``block-drive {"workflow": "submit"}`` tick
    dead-ended the drill: submit-s1's fresh start needs inputs beyond
    ``(run_id, workflow)``, and the tick must SAY that (an actionable skip,
    exit 0), never crash or hang."""
    rc, env, stderr = _invoke(
        "block-drive", {"workflow": "submit"}, tmp_path, "--experiment-dir", str(tmp_path)
    )
    assert rc == 0, stderr
    assert env["ok"] is True
    data = env["data"]
    assert data["action"] == "skip"
    assert data["next_verb"] == "submit-s1"
    reason = data["reason"]
    assert "cannot fresh-start submit-s1" in reason
    assert "goal/task_generator/walk" in reason


# ── Pin 2: walk *_resolved flags are booleans and honor recorded resolutions ──

_WALK_BASE: dict[str, Any] = {
    "cluster": "test-cluster",
    "configured_clusters": ["test-cluster"],
    "goal": "estimate pi by monte carlo",
    "task_generator": {"kind": "cartesian_product", "params": {"axes": {"seed": [1, 2]}}},
    "tasks_py_present": True,
    "walltime_sec": 3600,
}


def test_pin2_walk_resolved_flags_are_booleans(tmp_path: Path) -> None:
    """2026-07-18 snag 2 (shape half) — the drill passed ``*_resolved`` as the
    STRING ``"false"`` (truthy in any hand-rolled read). The wire schema types
    the flags boolean, so the string refuses loudly at the boundary."""
    rc, env, _ = _invoke("walk-submit-ambiguities", {"entry_point_resolved": "false"}, tmp_path)
    assert rc != 0
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
    assert "entry_point_resolved" in env["message"]


def test_pin2_walk_honors_recorded_repo_resolutions(tmp_path: Path) -> None:
    """2026-07-18 snag 2 (honor half) — when the repo HAS recorded resolutions,
    the ``*_resolved=True`` flags must route those fields OUT of ambiguities
    with ``resolved_on_disk`` provenance (the drill re-asked for fields the
    repo already knew); with the flags absent, the SAME walk must surface them."""
    spec = {
        **_WALK_BASE,
        "experiment_dir": str(tmp_path),
        "entry_point_resolved": True,
        "data_axis_resolved": True,
        "homogeneous_axes_resolved": True,
    }
    rc, env, stderr = _invoke("walk-submit-ambiguities", spec, tmp_path)
    assert rc == 0, stderr
    data = env["data"]
    fields = {a["field"] for a in data["ambiguities"]}
    assert "entry_point" not in fields
    assert "data_axis" not in fields
    assert "homogeneous_axes" not in fields
    provenance = data["provenance"]
    assert provenance["entry_point"] == "resolved_on_disk"
    assert provenance["data_axis"] == "resolved_on_disk"
    assert provenance["homogeneous_axes"] == "resolved_on_disk"

    # Control: flags absent (default False) → all three surface as ambiguities.
    rc2, env2, stderr2 = _invoke(
        "walk-submit-ambiguities", {**_WALK_BASE, "experiment_dir": str(tmp_path)}, tmp_path
    )
    assert rc2 == 0, stderr2
    fields2 = {a["field"] for a in env2["data"]["ambiguities"]}
    assert {"entry_point", "data_axis", "homogeneous_axes"} <= fields2


def test_pin2_walk_honors_interview_materialized_hints(tmp_path: Path) -> None:
    """2026-07-18 snag 2 (recorded-resolution half) — interview.json's
    ``_materialized`` block IS a recorded repo resolution: the walk must surface
    the recorded data-axis classification as the ``data_axis`` safe_default
    (``interview_hint`` provenance) instead of the ``sequential`` fail-safe, and
    label an interview-materialized tasks.py ``interview_materialized`` rather
    than hand-written (run-#12 finding 14, the same recorded-resolution class)."""
    hint = {
        "kind": "bounded_halo",
        "series_length": 2700,
        "halo_expr": "train_window * 48",
        "chunks": 4,
    }
    (tmp_path / "interview.json").write_text(
        json.dumps(
            {
                "_materialized": {
                    "entry_point": {"data_axis": hint},
                    "tasks_py_origin": "interview_materialized",
                }
            }
        )
    )
    spec = {
        "cluster": "test-cluster",
        "configured_clusters": ["test-cluster"],
        "goal": "fit the model",
        "tasks_py_present": True,
        "walltime_sec": 3600,
        "experiment_dir": str(tmp_path),
        # The *_resolved flags are deliberately ABSENT: the interview's recorded
        # hints (not a caller flag) are what this pin exercises.
    }
    rc, env, stderr = _invoke("walk-submit-ambiguities", spec, tmp_path)
    assert rc == 0, stderr
    data = env["data"]
    by_field = {a["field"]: a for a in data["ambiguities"]}
    assert by_field["data_axis"]["safe_default"] == hint
    assert data["provenance"]["data_axis"] == "interview_hint"
    # A present tasks.py the caller didn't re-declare: never an ambiguity, and
    # the provenance label is the interview's recorded origin.
    assert "task_generator" not in by_field
    assert data["provenance"]["task_generator"] == "interview_materialized"


# ── Pin 3: resolve placeholders — override the valid, refuse the invalid ──────


def _resolve_spec() -> dict[str, Any]:
    """A resolve-submit-inputs spec with SCHEMA-VALID placeholder identity.

    ``run_id="PLACEHOLDER"`` matches ``^[A-Za-z0-9._-]+$``; ``cmd_sha="0"*64``
    matches ``^[0-9a-f]{8,64}$`` — both pass the wire, so the chain (never the
    caller) owns the identity it mints. ``modules`` is declared because the
    env-activation guard (``Activation.__post_init__``) refuses a submission
    with no modules/conda activation at all — the empty-config pass-through
    covers cluster IDENTITY, not env activation.
    """
    return {
        "run_name": "pi-drill",
        "submit": {
            "profile": "pi",
            "cluster": "test-cluster",
            "ssh_target": "me@example.edu",
            "remote_path": "/scratch/me/pi",
            "run_id": "PLACEHOLDER",
            "cmd_sha": "0" * 64,
            "total_tasks": 2,
            "backend": "slurm",
            "modules": "python/3.12",
        },
        "sidecar": {
            "run_id": "PLACEHOLDER",
            "cmd_sha": "0" * 64,
            "executor": "python -m monte_carlo_pi --samples $SAMPLES",
            "result_dir_template": "results/{run_id}/task_{task_id}",
            "task_count": 2,
        },
    }


def test_pin3a_schema_valid_placeholders_are_overridden_by_compute_run_id(
    tmp_path: Path,
) -> None:
    """2026-07-18 snag 3 (override half) — the drill hand-authored run_id /
    cmd_sha and the submission nearly went out under the placeholder identity.
    The chain must override schema-valid placeholders with compute-run-id's
    values so the result, the built submit spec, and the on-disk sidecar all
    share ONE minted identity."""
    write_hpc_tasks(tmp_path / ".hpc", [{"samples": 1000}, {"samples": 2000}])
    rc, env, stderr = _invoke(
        "resolve-submit-inputs",
        _resolve_spec(),
        tmp_path,
        "--experiment-dir",
        str(tmp_path),
    )
    assert rc == 0, stderr
    data = env["data"]
    assert data["stage_reached"] == "resolved"
    run_id = data["run_id"]
    cmd_sha = data["cmd_sha"]
    assert run_id != "PLACEHOLDER"
    assert run_id.startswith("pi-drill-")
    assert cmd_sha != "0" * 64
    assert len(cmd_sha) == 64
    # The built submit-flow spec carries the OVERRIDDEN identity (run_id as a
    # top-level field; cmd_sha stamped into job_env per the build_submit_spec
    # contract — the dispatcher reads HPC_CMD_SHA, never a spec field).
    assert data["submit_spec"]["run_id"] == run_id
    assert data["submit_spec"]["job_env"]["HPC_CMD_SHA"] == cmd_sha
    assert data["submit_spec"]["job_env"]["HPC_RUN_ID"] == run_id
    # …and so does the written per-run sidecar (the #171 write-first path).
    sidecar = json.loads((tmp_path / ".hpc" / "runs" / f"{run_id}.json").read_text())
    assert sidecar["run_id"] == run_id
    assert sidecar["cmd_sha"] == cmd_sha
    assert sidecar["task_count"] == 2


def test_pin3b_invalid_placeholder_shape_refuses_with_spec_skeleton(tmp_path: Path) -> None:
    """2026-07-18 snag 3 (refusal half) — ``cmd_sha="PLACEHOLDER"`` is NOT
    schema-valid (``^[0-9a-f]{8,64}$``): the refusal must fire at the wire with
    the ``spec_skeleton`` remediation, so the caller fills a code-generated
    minimal valid instance instead of reconstructing the shape by hand."""
    spec = _resolve_spec()
    spec["submit"]["cmd_sha"] = "PLACEHOLDER"
    rc, env, _ = _invoke("resolve-submit-inputs", spec, tmp_path, "--experiment-dir", str(tmp_path))
    assert rc != 0
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
    assert "cmd_sha" in env["message"]
    assert isinstance(env.get("spec_skeleton"), dict)
    assert "spec_skeleton" in env["remediation"]


# ── Pin 4: provenance gate — full-input-spec refuses, brief-shaped passes ─────

_PIN4_RUN = "drill-run-1"


def _persist_pin4_brief(tmp_path: Path) -> None:
    """Persist the S1 brief the gate diffs against (the block's recommendations)."""
    from hpc_agent.state.decision_briefs import append_brief

    append_brief(
        tmp_path,
        run_id=_PIN4_RUN,
        block="s1",
        brief={"resolved": {"cluster": "hoffman2", "walltime_sec": 3600}},
    )


def test_pin4a_full_input_spec_resolved_refuses(tmp_path: Path) -> None:
    """2026-07-18 snag 4 (refusal half) — the drill greenlit by pasting the
    whole S1 INPUT spec (``{walk, resolve, run_preflight}``) into ``resolved``.
    Conduct rule 9: a greenlight may commit only what the block's persisted
    brief recommended (or a nudge / explicit override named) — the input-spec
    keys divert and must REFUSE, named."""
    _persist_pin4_brief(tmp_path)
    full_input_spec_resolved = {
        "walk": {"cluster": "hoffman2", "configured_clusters": ["hoffman2"]},
        "resolve": {"run_name": "pi-drill"},
        "run_preflight": True,
    }
    rc, env, _ = _invoke(
        "append-decision",
        _append_spec(_PIN4_RUN, full_input_spec_resolved),
        tmp_path,
        "--experiment-dir",
        str(tmp_path),
    )
    assert rc != 0
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
    message = env["message"]
    assert "provenance gate" in message
    for key in ("walk", "resolve", "run_preflight"):
        assert key in message


def test_pin4b_brief_shaped_resolved_passes(tmp_path: Path) -> None:
    """2026-07-18 snag 4 (pass half) — the brief-shaped ``resolved`` (exactly
    the fields the persisted brief recommended) greenlights clean; the
    machine-owned ``next_block`` the chain defaults in is meta-exempt."""
    _persist_pin4_brief(tmp_path)
    rc, env, stderr = _invoke(
        "append-decision",
        _append_spec(_PIN4_RUN, {"cluster": "hoffman2", "walltime_sec": 3600}),
        tmp_path,
        "--experiment-dir",
        str(tmp_path),
    )
    assert rc == 0, stderr
    assert env["ok"] is True
    assert env["data"]["count"] == 1


# ── Pin 5: authorship gate in the sandbox — refuse / accept / decoy namespace ──


def test_pin5a_unuttered_goal_and_task_generator_refuse_in_sandbox(
    sandbox_home: Path, tmp_path: Path
) -> None:
    """2026-07-18 snag 5 (negative control) — a greenlight whose ``goal`` /
    ``task_generator`` were never uttered must REFUSE even inside the seeded
    sandbox namespace (a sandbox proves the gate FIRES; it never proves a human
    approved anything). The namespace's log is non-empty (an unrelated seed),
    so the lock tier — not the friction fallback — is what refuses, with the
    E2 authorship marker on the envelope."""
    exp = tmp_path / "exp"
    seed_utterance(sandbox_home, exp, _UNRELATED_UTTERANCE, run_ref=_SEED_RUN_REF)
    resolved = {"goal": _GOAL_VALUE, "task_generator": _TASK_GENERATOR_VALUE}
    rc, env, _ = _invoke(
        "append-decision",
        _append_spec("sandbox-run-1", resolved),
        tmp_path,
        "--experiment-dir",
        str(exp),
    )
    assert rc != 0
    assert env["ok"] is False
    assert env["error_code"] == "spec_invalid"
    assert "human-authorship gate" in env["message"]
    assert env["failure_features"] == {"authorship_evidence": "missing"}


def test_pin5b_seeded_utterance_unlocks_the_gate(sandbox_home: Path, tmp_path: Path) -> None:
    """2026-07-18 snag 5 (accept half) — with the human's utterance seeded into
    the sandbox namespace, the SAME greenlight commits through the REAL
    append-decision envelope: every goal word token overlaps the utterance and
    every task_generator number is human-derivable from it."""
    exp = tmp_path / "exp"
    seed_utterance(sandbox_home, exp, _GOAL_UTTERANCE, run_ref=_SEED_RUN_REF)
    resolved = {"goal": _GOAL_VALUE, "task_generator": _TASK_GENERATOR_VALUE}
    rc, env, stderr = _invoke(
        "append-decision",
        _append_spec("sandbox-run-1", resolved),
        tmp_path,
        "--experiment-dir",
        str(exp),
    )
    assert rc == 0, stderr
    assert env["ok"] is True
    assert env["data"]["count"] == 1


def test_pin5c_decoy_namespace_does_not_unlock(sandbox_home: Path, tmp_path: Path) -> None:
    """2026-07-18 snag 5 (the namespace-coupling pin) — the utterance log is
    keyed by experiment_dir's repo hash: an utterance seeded in namespace A
    must NOT unlock a gate read scoped to namespace B under the SAME sandbox
    home. B's log is non-empty (its own unrelated seed), so B's refusal is the
    lock tier firing — never the friction-tier fallback — while the SAME
    commit passes in the namespace that actually holds the utterance."""
    from hpc_agent.state.utterances import utterances_path

    exp_a = tmp_path / "exp_a"
    exp_b = tmp_path / "exp_b"
    seed_utterance(sandbox_home, exp_a, _GOAL_UTTERANCE, run_ref=_SEED_RUN_REF)
    seed_utterance(sandbox_home, exp_b, _UNRELATED_UTTERANCE, run_ref=_SEED_RUN_REF)

    # One shared sandbox home, two distinct namespaces — the decoy is planted
    # BESIDE the target, not in some other journal root.
    assert utterances_path(exp_a) != utterances_path(exp_b)

    resolved = {"goal": _GOAL_VALUE, "task_generator": _TASK_GENERATOR_VALUE}
    # Scoped to B the gate must REFUSE A's utterance — with the E2 marker, i.e.
    # it is the authorship bar firing, not some structural refusal.
    rc_b, env_b, _ = _invoke(
        "append-decision",
        _append_spec("sandbox-run-1", resolved),
        tmp_path,
        "--experiment-dir",
        str(exp_b),
    )
    assert rc_b != 0
    assert env_b["ok"] is False
    assert "human-authorship gate" in env_b["message"]
    assert env_b["failure_features"] == {"authorship_evidence": "missing"}

    # …and the SAME commit passes in namespace A — the pin is scoping, not a
    # broken gate.
    rc_a, env_a, stderr_a = _invoke(
        "append-decision",
        _append_spec("sandbox-run-1", resolved),
        tmp_path,
        "--experiment-dir",
        str(exp_a),
    )
    assert rc_a == 0, stderr_a
    assert env_a["ok"] is True
    assert env_a["data"]["count"] == 1
