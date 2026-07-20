"""Hermetic tests for ``scripts/run_sandbox_proving.py`` — the U3 driver.

Every test here is rung-1: no cluster, no docker, no subprocess, no sibling
modules (the fixture/seed bridges are exercised against synthetic tmp
modules, never the real ``tests/integration/scheduler/`` files, which another
lane owns). The pins cover the parts of the driver that are pure contract:

1. The §3 journal-home guard (refuses unset / production / production-subdir;
   accepts an ephemeral tmpdir and a sibling namespace).
2. The CLI-envelope contract assertions (block + block-drive shapes).
3. The spec composers mirroring ``block_chain``'s S2/S3/S4 shapes, and the
   schema-valid placeholders the U5.3 pin demands.
4. The provenance self-check mirroring the gate's name pool (meta-exempt).
5. The journal/lease/terminal path helpers U4 consumes.
6. The seeded-utterance text builder (goal + every number verbatim; never
   harness-injected).
7. The cluster-config selection + recorded-resolution reflection.
8. The ``main()`` refusal order (guard → sweep parse → sweep keys → cluster
   source) — each exits 2 before any cluster/sibling work happens.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "run_sandbox_proving", REPO_ROOT / "scripts" / "run_sandbox_proving.py"
)
assert _SPEC is not None and _SPEC.loader is not None
driver = importlib.util.module_from_spec(_SPEC)
sys.modules["run_sandbox_proving"] = driver
_SPEC.loader.exec_module(driver)

SandboxRefusal = driver.SandboxRefusal


# ── §3 journal-home guard ────────────────────────────────────────────────────


def test_guard_refuses_unset() -> None:
    with pytest.raises(SandboxRefusal, match="HPC_JOURNAL_DIR is unset"):
        driver.require_ephemeral_journal_home({})


def test_guard_refuses_production_home() -> None:
    production = Path.home() / ".claude" / "hpc"
    with pytest.raises(SandboxRefusal, match="production journal home"):
        driver.require_ephemeral_journal_home({"HPC_JOURNAL_DIR": str(production)})


def test_guard_refuses_production_subdir() -> None:
    production = Path.home() / ".claude" / "hpc"
    with pytest.raises(SandboxRefusal, match="production journal home"):
        driver.require_ephemeral_journal_home({"HPC_JOURNAL_DIR": str(production / "nested")})


def test_guard_accepts_ephemeral_tmp(tmp_path: Path) -> None:
    home = driver.require_ephemeral_journal_home({"HPC_JOURNAL_DIR": str(tmp_path / "j")})
    assert home == (tmp_path / "j").resolve()


def test_guard_accepts_sibling_namespace() -> None:
    # A namespace NEXT TO the production home is fine; only INSIDE is refused.
    sibling = Path.home() / ".claude" / "hpc-sandbox-test"
    home = driver.require_ephemeral_journal_home({"HPC_JOURNAL_DIR": str(sibling)})
    assert home == sibling.resolve()


# ── spec writing / envelope parsing ──────────────────────────────────────────


def test_write_spec_round_trip(tmp_path: Path) -> None:
    path = driver.write_spec(tmp_path, "s1.walk", {"walk": {"goal": "g"}, "run_preflight": False})
    assert path.name == "s1.walk.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "walk": {"goal": "g"},
        "run_preflight": False,
    }


def test_write_spec_refuses_unsafe_name(tmp_path: Path) -> None:
    with pytest.raises(SandboxRefusal, match="not filesystem-safe"):
        driver.write_spec(tmp_path, "../escape", {})


def test_parse_envelope_last_line_wins() -> None:
    stdout = (
        'noise line\n{"ok": false, "error_code": "SpecInvalid"}\n{"ok": true, "data": {"a": 1}}\n'
    )
    envelope = driver.parse_envelope(stdout)
    assert envelope == {"ok": True, "data": {"a": 1}}


def test_parse_envelope_none_without_envelope() -> None:
    assert driver.parse_envelope('no json here\n{"not_an_envelope": true}\n') is None


def test_read_jsonl_skips_blank_and_torn_lines(tmp_path: Path) -> None:
    path = tmp_path / "j.jsonl"
    path.write_text(
        '{"a": 1}\n\n{"b": 2}\n{"torn": \n["not", "a", "dict"]\n{"c": 3}\n', encoding="utf-8"
    )
    assert driver.read_jsonl(path) == [{"a": 1}, {"b": 2}, {"c": 3}]
    assert driver.read_jsonl(tmp_path / "absent.jsonl") == []


def test_dict_or_empty() -> None:
    assert driver._dict_or_empty({"x": 1}) == {"x": 1}
    assert driver._dict_or_empty(None) == {}
    assert driver._dict_or_empty(["not-a-dict"]) == {}


# ── envelope contract assertions ─────────────────────────────────────────────


def _good_block_data() -> dict:
    return {
        "block": "submit-s2",
        "stage_reached": "detached",
        "needs_decision": False,
        "reason": "worker spawned",
        "run_id": "sandbox-pi-deadbeef",
        "brief": {"verified": True},
        "next_block": None,
    }


def test_assert_block_envelope_passes_on_full_shape() -> None:
    assert driver.assert_block_envelope(_good_block_data(), verb="submit-s2") == []


def test_assert_block_envelope_flags_missing_keys() -> None:
    problems = driver.assert_block_envelope({"block": "submit-s2"}, verb="submit-s2")
    assert any("stage_reached" in p for p in problems)
    assert any("needs_decision" in p for p in problems)
    assert any("brief" in p for p in problems)


def test_assert_block_envelope_flags_bad_types() -> None:
    data = _good_block_data()
    data["needs_decision"] = "yes"  # must be a real boolean
    data["brief"] = ["not", "a", "dict"]
    problems = driver.assert_block_envelope(data, verb="submit-s2")
    assert any("needs_decision must be a boolean" in p for p in problems)
    assert any("brief must be an object" in p for p in problems)


def test_assert_block_envelope_flags_non_dict() -> None:
    problems = driver.assert_block_envelope(["nope"], verb="submit-s2")
    assert problems and "not an object" in problems[0]


def test_assert_block_drive_envelope_pass_and_missing() -> None:
    good = {
        "action": "skip",
        "run_id": None,
        "workflow": "submit",
        "current_verb": None,
        "next_verb": "submit-s1",
        "reason": "cannot fresh-start submit-s1: goal/task_generator required",
    }
    assert driver.assert_block_drive_envelope(good) == []
    problems = driver.assert_block_drive_envelope({"action": "skip"})
    assert any("next_verb" in p for p in problems)
    assert any("reason" in p for p in problems)


def test_assert_run_id_minted() -> None:
    assert driver.assert_run_id_minted("sandbox-pi-deadbeef", "sandbox-pi") == []


def test_assert_run_id_minted_rejects_bad_shapes() -> None:
    assert driver.assert_run_id_minted("sandbox-pi-DEADBEEF", "sandbox-pi")  # uppercase
    assert driver.assert_run_id_minted("other-deadbeef", "sandbox-pi")  # wrong name
    assert driver.assert_run_id_minted("sandbox-pi-deadbeef00", "sandbox-pi")  # 10 hex
    assert driver.assert_run_id_minted(None, "sandbox-pi")  # non-str


# ── greenlight building + provenance self-check ──────────────────────────────


def test_build_s1_greenlight_resolved_copies_brief_verbatim() -> None:
    brief = {"resolved": {"goal": "g", "cluster": "slurmci", "walltime_sec": 900}}
    resolved = driver.build_s1_greenlight_resolved(brief)
    assert resolved == {
        "goal": "g",
        "cluster": "slurmci",
        "walltime_sec": 900,
        "next_block": "submit-s2",
    }


def test_build_s1_greenlight_resolved_refuses_empty() -> None:
    with pytest.raises(SandboxRefusal, match="no 'resolved' block"):
        driver.build_s1_greenlight_resolved({"resolved": {}})
    with pytest.raises(SandboxRefusal, match="no 'resolved' block"):
        driver.build_s1_greenlight_resolved({})


def test_collect_brief_names_keys_and_string_scalars() -> None:
    brief = {
        "a": {"b": ["scalar-one", {"c": 3}]},
        "d": "scalar-two",
        "n": 42,  # numbers are not names
    }
    names = driver.collect_brief_names(brief)
    assert {"a", "b", "c", "d", "n", "scalar-one", "scalar-two"} <= names
    assert "42" not in names


def test_provenance_shape_problems_meta_exempt_and_diverging() -> None:
    brief = {"resolved": {"goal": "g", "cluster": "slurmci"}}
    # next_block is meta-exempt; goal/cluster appear in the brief's name pool.
    assert (
        driver.provenance_shape_problems(
            {"goal": "g", "cluster": "slurmci", "next_block": "submit-s2"}, brief
        )
        == []
    )
    # A key the brief never recommended is named, sorted.
    assert driver.provenance_shape_problems({"zzz_alien": 1, "goal": "g"}, brief) == ["zzz_alien"]


# ── spec composers (mirror block_chain's S2/S3/S4 shapes) ───────────────────


def test_compose_s2_spec_wraps_the_resolved_submit_flow() -> None:
    brief = {"resolve": {"submit_spec": {"profile": "cpu", "run_id": "x-deadbeef"}}}
    spec = driver.compose_s2_spec(brief)
    assert spec == {
        "submit": {"submit": {"profile": "cpu", "run_id": "x-deadbeef"}},
        "detach": True,
    }


def test_compose_s2_spec_refuses_missing_submit_spec() -> None:
    with pytest.raises(SandboxRefusal, match="resolve.submit_spec"):
        driver.compose_s2_spec({"resolve": {}})


def test_compose_s3_spec_threads_canary_ids_and_monitor_identity() -> None:
    s2_spec = {"submit": {"submit": {"profile": "cpu"}}, "detach": True}
    s2_brief = {"canary_run_id": "x-deadbeef-canary", "canary_job_ids": ["101"]}
    spec = driver.compose_s3_spec(s2_spec, "x-deadbeef", s2_brief)
    assert spec["submit"] == {"submit": {"profile": "cpu"}}
    assert spec["monitor"] == {"run_id": "x-deadbeef"}
    assert spec["canary_run_id"] == "x-deadbeef-canary"
    assert spec["canary_job_ids"] == ["101"]
    assert spec["detach"] is True


def test_compose_s3_spec_refuses_without_submit() -> None:
    with pytest.raises(SandboxRefusal, match="no 'submit' sub-spec"):
        driver.compose_s3_spec({}, "x-deadbeef")


def test_compose_s4_spec_is_the_identity_shape() -> None:
    assert driver.compose_s4_spec("x-deadbeef") == {
        "aggregate": {"run_id": "x-deadbeef"},
        "detach": True,
    }


# ── path helpers (the U4 consumption contract) ───────────────────────────────


def test_journal_terminal_successor_lease_paths(tmp_path: Path) -> None:
    experiment = tmp_path / "experiment"
    run_id = "sandbox-pi-deadbeef"
    assert driver.decision_journal_path(experiment, run_id) == (
        experiment / ".hpc" / "runs" / f"{run_id}.decisions.jsonl"
    )
    assert driver.terminal_record_path(experiment, run_id, "submit-s2") == (
        experiment / ".hpc" / "runs" / f"{run_id}.submit-s2.terminal.json"
    )
    assert driver.materialized_successor_path(experiment, run_id, "submit-s3") == (
        experiment / ".hpc" / "specs" / "next" / f"{run_id}.submit-s3.json"
    )
    journal_home = tmp_path / "journal"
    assert driver.detached_lease_path(journal_home, run_id, "submit-s3") == (
        journal_home / "_detached" / f"submit-s3-{run_id}.lease.json"
    )


def test_read_detached_lease(tmp_path: Path) -> None:
    journal_home = tmp_path / "journal"
    run_id = "sandbox-pi-deadbeef"
    assert driver.read_detached_lease(journal_home, run_id, "submit-s3") is None
    lease = driver.detached_lease_path(journal_home, run_id, "submit-s3")
    lease.parent.mkdir(parents=True)
    lease.write_text("{corrupt", encoding="utf-8")
    assert driver.read_detached_lease(journal_home, run_id, "submit-s3") is None
    lease.write_text(json.dumps({"pid": 4321, "create_time": 1700000000.0}), encoding="utf-8")
    payload = driver.read_detached_lease(journal_home, run_id, "submit-s3")
    assert payload is not None and payload["pid"] == 4321


def test_worker_present_states_excludes_no_lease() -> None:
    # poll-detached's state vocabulary minus 'no_lease' (the invoke-directly case).
    assert sorted(driver._WORKER_PRESENT_STATES) == [
        "exited_recorded",
        "exited_unrecorded",
        "running",
    ]


# ── greenlight journal scan ──────────────────────────────────────────────────


def test_find_greenlight_returns_newest_matching_y() -> None:
    records = [
        {"response": "y", "block": "submit-s1", "resolved": {"next_block": "submit-s2"}},
        {"response": "nudge text", "block": "submit-s1", "resolved": {"next_block": "submit-s2"}},
        {"response": "y", "block": "submit-s1", "resolved": {"next_block": "submit-s9"}},
        {"response": "y", "block": "submit-s2", "resolved": {"next_block": "submit-s3"}},
    ]
    found = driver.find_greenlight(records, block="submit-s1", next_block="submit-s2")
    assert found is records[0]
    # Without a next_block constraint the newest y for the block wins.
    assert driver.find_greenlight(records, block="submit-s1") is records[2]
    assert driver.find_greenlight(records, block="submit-s1", next_block="submit-s4") is None


# ── seeded-utterance text builder ────────────────────────────────────────────


def test_utterance_states_goal_and_every_number_verbatim() -> None:
    goal = "sandbox-prove the submit block loop end to end"
    task_generator = {
        "kind": "items_x_seeds",
        "params": {"items": [{"n_samples": 100_000}], "seeds": [0, 1, 2, 3, 4, 5, 6, 7]},
    }
    text = driver.build_utterance_text(goal, task_generator)
    assert goal in text
    for number in (100_000, 0, 1, 2, 3, 4, 5, 6, 7):
        assert repr(number) in text


def test_utterance_states_string_values_and_is_not_harness_injected() -> None:
    from hpc_agent.state.utterances import is_harness_injected

    task_generator = {
        "kind": "enumerated",
        "params": {"items": [{"dataset": "ticks-2024", "n_samples": 5_000}]},
    }
    text = driver.build_utterance_text("prove it", task_generator)
    assert "ticks-2024" in text
    assert "5000" in text
    # The seed mimics a human-typed prompt; it must NOT carry the harness
    # injection marker (the write API refuses / the gate discounts those).
    assert not is_harness_injected(text)


def test_iter_numbers_skips_bools_and_collects_nested() -> None:
    value = {"a": [1, True, {"b": 2.5}], "c": False}
    assert sorted(driver._iter_numbers(value)) == [1, 2.5]


# ── evidence build + render ──────────────────────────────────────────────────


def test_build_evidence_verdict_and_failed_steps() -> None:
    rows = [
        driver.build_evidence_row("s1.walk", "submit-s1", "booleans honored", True),
        driver.build_evidence_row("s1.resolve", "submit-s1", "run_id minted", False, "bad shape"),
    ]
    evidence = driver.build_evidence({"run_ref": "r1"}, rows)
    assert evidence["verdict"] == "fail"
    assert evidence["failed_steps"] == ["s1.resolve"]
    passing = driver.build_evidence({"run_ref": "r2"}, rows[:1])
    assert passing["verdict"] == "pass"
    assert passing["failed_steps"] == []


def test_render_markdown_mirrors_the_run15_table() -> None:
    rows = [driver.build_evidence_row("s1.walk", "submit-s1 brief", "booleans honored", True)]
    md = driver.render_markdown(driver.build_evidence({"run_ref": "r1"}, rows))
    assert "| Step | Where | Mechanical check | Pass |" in md
    assert "| s1.walk | submit-s1 brief | booleans honored | yes |" in md
    assert "Rung-2 jurisdiction" in md


# ── cluster config selection ─────────────────────────────────────────────────


def _write_clusters(path: Path, names: list[str]) -> None:
    import yaml

    config = {
        name: {
            "host": f"{name}.example",
            "user": "hpcuser",
            "scheduler": "slurm",
            "scratch": f"/scratch/{name}",
        }
        for name in names
    }
    path.write_text(yaml.safe_dump(config), encoding="utf-8")


def test_load_cluster_config_missing_and_empty(tmp_path: Path) -> None:
    with pytest.raises(SandboxRefusal, match="not found"):
        driver.load_cluster_config(tmp_path / "absent.yaml")
    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(SandboxRefusal, match="empty or not a mapping"):
        driver.load_cluster_config(empty)


def test_select_cluster_named_single_and_ambiguous(tmp_path: Path) -> None:
    single = tmp_path / "one.yaml"
    _write_clusters(single, ["slurmci"])
    config = driver.load_cluster_config(single)
    name, stanza = driver.select_cluster(config, None)
    assert name == "slurmci"
    assert driver.stanza_ssh_target(stanza) == "hpcuser@slurmci.example"
    assert driver.stanza_backend(stanza) == "slurm"
    assert driver.stanza_remote_path(stanza, Path("/exp/myexp")) == "/scratch/slurmci/myexp"

    multi = tmp_path / "two.yaml"
    _write_clusters(multi, ["a", "b"])
    config = driver.load_cluster_config(multi)
    with pytest.raises(SandboxRefusal, match="pass --cluster"):
        driver.select_cluster(config, None)
    with pytest.raises(SandboxRefusal, match="not in the config"):
        driver.select_cluster(config, "zzz")
    name, _ = driver.select_cluster(config, "b")
    assert name == "b"


def test_stanza_accessors_require_keys() -> None:
    with pytest.raises(SandboxRefusal, match="'user' and 'host'"):
        driver.stanza_ssh_target({"host": "h"})
    with pytest.raises(SandboxRefusal, match="'scheduler'"):
        driver.stanza_backend({})
    with pytest.raises(SandboxRefusal, match="'scratch'"):
        driver.stanza_remote_path({}, Path("/exp/x"))


def test_local_clusters_yaml_declares_the_login_shell_noop(tmp_path: Path) -> None:
    """The stanza ``--local`` writes must pass the #281 Activation guard the
    submit-s1 resolve leg runs — the 2026-07-19 CI failure (run 29708199144)
    was a cosmetic ``modules: [" "]`` that resolve_activation stripped to
    empty and refused. The sanctioned form is the explicit
    ``login_shell_activation`` declaration; pin that the written stanza
    carries it AND that the guard admits the stanza as composed."""
    from hpc_agent.infra.clusters import resolve_activation

    path = tmp_path / "ci_clusters.yaml"
    path.write_text(driver._LOCAL_CLUSTERS_YAML, encoding="utf-8")
    config = driver.load_cluster_config(path)
    name, stanza = driver.select_cluster(config, None)
    assert name == "slurmci"
    assert stanza.get("modules") == []
    assert stanza.get("login_shell_activation") is True
    # The exact composition build_submit_spec performs: no caller fields, the
    # stanza is the only source. The guard must admit it as the DECLARED
    # no-op (and the job_env stays empty — the preamble skips all setup).
    activation = resolve_activation(cluster_cfg=stanza)
    assert activation.login_shell is True
    assert activation.as_job_env() == {"MODULES": "", "CONDA_SOURCE": "", "CONDA_ENV": ""}


# ── recorded-resolution reflection + spec builders ───────────────────────────


def test_compute_recorded_resolutions_empty_dir(tmp_path: Path) -> None:
    recorded = driver.compute_recorded_resolutions(tmp_path, "run")
    assert recorded == {
        "entry_point_resolved": False,
        "data_axis_resolved": False,
        "homogeneous_axes_resolved": False,
        "tasks_py_present": False,
    }


def test_compute_recorded_resolutions_full_fixture(tmp_path: Path) -> None:
    import yaml

    (tmp_path / "interview.json").write_text("{}", encoding="utf-8")
    hpc = tmp_path / ".hpc"
    hpc.mkdir()
    (hpc / "tasks.py").write_text("# tasks", encoding="utf-8")
    (hpc / "axes.yaml").write_text(
        yaml.safe_dump({"executors": {"run": {"data_axis": {"kind": "sequential"}}}}),
        encoding="utf-8",
    )
    recorded = driver.compute_recorded_resolutions(tmp_path, "run")
    assert recorded["entry_point_resolved"] is True
    assert recorded["data_axis_resolved"] is True
    assert recorded["tasks_py_present"] is True
    assert recorded["homogeneous_axes_resolved"] is False
    # The executor name matters: a different executor's entry is not ours.
    other = driver.compute_recorded_resolutions(tmp_path, "other_executor")
    assert other["data_axis_resolved"] is False
    # homogeneous_axes recorded when present.
    (hpc / "axes.yaml").write_text(
        yaml.safe_dump({"executors": {"run": {}}, "homogeneous_axes": {"a": [1, 2]}}),
        encoding="utf-8",
    )
    assert driver.compute_recorded_resolutions(tmp_path, "run")["homogeneous_axes_resolved"] is True


def test_build_walk_spec_carries_booleans_and_task_generator() -> None:
    walk = driver.build_walk_spec(
        cluster="slurmci",
        configured_clusters=["slurmci"],
        goal="g",
        task_generator={"kind": "enumerated", "params": {"items": [{}]}},
        profile="cpu",
        executor_run_name="run",
        walltime_sec=900,
        experiment_dir=Path("/exp"),
        recorded={"entry_point_resolved": True, "data_axis_resolved": False},
    )
    assert walk["entry_point_resolved"] is True
    assert walk["data_axis_resolved"] is False
    assert walk["homogeneous_axes_resolved"] is False
    assert walk["tasks_py_present"] is False
    assert walk["task_generator"] == {"kind": "enumerated", "params": {"items": [{}]}}
    assert walk["uncovered_required_params"] == []


def test_build_resolve_spec_placeholders_are_schema_valid() -> None:
    spec = driver.build_resolve_spec(
        run_name="sandbox-pi",
        profile="cpu",
        cluster="slurmci",
        ssh_target="hpcuser@slurmci",
        remote_path="/scratch/slurmci/experiment",
        backend="slurm",
        total_tasks=8,
        executor_cmd="python train.py",
        walltime_sec=900,
    )
    submit = spec["submit"]
    # The U5.3 pin: placeholders are VALID shapes compute-run-id overrides —
    # a slug run_id (RunIdStrict) and 8 lowercase hex cmd_sha, never the
    # all-caps "PLACEHOLDER" refusal literal.
    assert submit["run_id"] == "placeholder-run"
    assert re.fullmatch(r"[A-Za-z0-9._-]+", submit["run_id"])
    assert re.fullmatch(r"[0-9a-f]{8}", submit["cmd_sha"])
    assert submit["script"] == ".hpc/templates/cpu_array.slurm"
    assert spec["run_name"] == "sandbox-pi"
    assert spec["sidecar"]["task_count"] == 8
    sge = driver.build_resolve_spec(
        run_name="r",
        profile="cpu",
        cluster="c",
        ssh_target="u@h",
        remote_path="/s/x",
        backend="sge",
        total_tasks=1,
        executor_cmd="python t.py",
        walltime_sec=60,
    )
    assert sge["submit"]["script"] == ".hpc/templates/cpu_array.sh"


# ── sibling loading (synthetic tmp modules — never the real lane files) ──────


def test_load_sibling_module_missing_file(tmp_path: Path) -> None:
    with pytest.raises(SandboxRefusal, match="built concurrently"):
        driver.load_sibling_module(tmp_path / "absent.py", label="synthetic")


def test_load_sibling_module_and_entrypoint(tmp_path: Path) -> None:
    module_path = tmp_path / "synth.py"
    module_path.write_text("def entry(x):\n    return x + 1\n", encoding="utf-8")
    module = driver.load_sibling_module(module_path, label="synthetic")
    entry = driver.sibling_entrypoint(module, "entry", label="synthetic")
    assert entry(41) == 42


def test_sibling_entrypoint_missing_names_the_contract(tmp_path: Path) -> None:
    module_path = tmp_path / "synth.py"
    module_path.write_text("OTHER = 1\n", encoding="utf-8")
    module = driver.load_sibling_module(module_path, label="synthetic")
    with pytest.raises(SandboxRefusal, match="does not export a callable 'expected_name'"):
        driver.sibling_entrypoint(module, "expected_name", label="synthetic")


def test_load_sibling_module_import_failure_is_a_refusal(tmp_path: Path) -> None:
    module_path = tmp_path / "broken.py"
    module_path.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    with pytest.raises(SandboxRefusal, match="failed to import: boom"):
        driver.load_sibling_module(module_path, label="synthetic")


# ── sweep kwargs mapping ─────────────────────────────────────────────────────


def test_fixture_kwargs_from_sweep_defaults_and_values() -> None:
    assert driver.fixture_kwargs_from_sweep({}) == {}
    kwargs = driver.fixture_kwargs_from_sweep({"seeds": [0, 1, 2], "n_samples": 50_000})
    assert kwargs == {"seeds": (0, 1, 2), "n_samples": 50_000}


def test_fixture_kwargs_from_sweep_refusals() -> None:
    with pytest.raises(SandboxRefusal, match="not fixture knobs"):
        driver.fixture_kwargs_from_sweep({"lr": 0.01})
    with pytest.raises(SandboxRefusal, match="non-empty list"):
        driver.fixture_kwargs_from_sweep({"seeds": []})
    with pytest.raises(SandboxRefusal, match="must be ints"):
        driver.fixture_kwargs_from_sweep({"seeds": ["a"]})
    with pytest.raises(SandboxRefusal, match=">= 1"):
        driver.fixture_kwargs_from_sweep({"n_samples": 0})


# ── CliOutcome ────────────────────────────────────────────────────────────────


def test_cli_outcome_ok_data_and_failure_description() -> None:
    outcome = driver.CliOutcome(
        verb="submit-s1",
        rc=0,
        envelope={"ok": True, "data": {"stage_reached": "resolved"}},
        stdout="",
        stderr="",
    )
    assert outcome.ok is True
    assert outcome.data == {"stage_reached": "resolved"}

    red = driver.CliOutcome(
        verb="submit-s1",
        rc=2,
        envelope={"ok": False, "error_code": "SpecInvalid", "message": "bad spec"},
        stdout="",
        stderr="",
    )
    assert red.ok is False
    assert red.data == {}
    description = red.describe_failure()
    assert "rc=2" in description and "SpecInvalid" in description and "bad spec" in description

    no_envelope = driver.CliOutcome(
        verb="submit-s1", rc=1, envelope=None, stdout="line1\nlast-out", stderr="last-err"
    )
    assert no_envelope.ok is False
    assert "last-out" in no_envelope.describe_failure()


# ── main() refusal order (each exits 2 before any cluster/sibling work) ──────


def test_main_refuses_without_journal_env(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.delenv("HPC_JOURNAL_DIR", raising=False)
    assert driver.main([]) == 2
    assert "HPC_JOURNAL_DIR is unset" in capsys.readouterr().err


def test_main_refuses_invalid_sweep_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    assert driver.main(["--sweep", "not json"]) == 2
    assert "not valid JSON" in capsys.readouterr().err


def test_main_refuses_unknown_sweep_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    assert driver.main(["--sweep", '{"lr": 0.01}']) == 2
    assert "not fixture knobs" in capsys.readouterr().err


def test_main_refuses_without_cluster_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    assert driver.main([]) == 2
    assert "--clusters-config" in capsys.readouterr().err


def test_main_refuses_missing_clusters_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    rc = driver.main(["--clusters-config", str(tmp_path / "absent.yaml")])
    assert rc == 1  # the chain records the setup refusal as evidence, exit 1
    evidence = list(tmp_path.glob("hpc-sandbox-*"))  # no workdir flag → tmpdir; nothing to find
    assert evidence == []  # (evidence lives in the fresh tmpdir, not tmp_path)


# ── the fixture n_samples → task-walltime mapping (the 2026-07-19 window fix) ──
#
# sacct is DISABLED on the container, so a completed array vanishes from
# squeue instantly and the array's squeue-visibility window IS its task
# walltime. The old 100k-sample default ran ~160ms tasks (a 0.9–1.4s window
# that parked inside the kill drill's old 2s poll gap — the deterministic 3/3
# miss of run 29709733724). The default sweep must keep fixture tasks in the
# 5–10s band.


def test_fixture_n_samples_default_hits_the_5_to_10s_band() -> None:
    seconds = driver.DEFAULT_FIXTURE_N_SAMPLES / driver.FIXTURE_SAMPLES_PER_SEC
    assert 5.0 <= seconds <= 10.0
    # …and the mapping constant is the measured container rate (≈1.6µs/sample).
    assert driver.FIXTURE_SAMPLES_PER_SEC >= 100_000  # sanity: not ns/sample


def test_main_default_sweep_uses_the_fixture_band(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The default sweep (no --sweep flag) must ride DEFAULT_FIXTURE_N_SAMPLES
    # plus the pid-offset freshness bump — pinned end-to-end through main()'s
    # evidence meta (the setup refusal path writes evidence before exiting 1).
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "journal"))
    out = tmp_path / "evidence.json"
    rc = driver.main(
        [
            "--clusters-config",
            str(tmp_path / "absent.yaml"),
            "--workdir",
            str(tmp_path / "work"),
            "--out",
            str(out),
            "--markdown",
            str(tmp_path / "evidence.md"),
        ]
    )
    assert rc == 1
    evidence = json.loads(out.read_text(encoding="utf-8"))
    n_samples = evidence["meta"]["sweep"]["n_samples"]
    assert n_samples >= driver.DEFAULT_FIXTURE_N_SAMPLES
    assert n_samples < driver.DEFAULT_FIXTURE_N_SAMPLES + 50_000
