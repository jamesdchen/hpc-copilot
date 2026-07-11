"""Tests for hpc_agent.infra.failure_signatures.classify."""

from __future__ import annotations

from hpc_agent.infra.failure_signatures import (
    CATALOG,
    CONDA_RUN_BLIND_CLASS,
    classify,
    classify_conda_run_blind,
)


def test_catalog_size() -> None:
    """The catalog covers the 19 documented failure modes (segv was
    removed when the SEGV blacklist feature was deleted; preempted was
    added when dispatch.py learned to trap SIGTERM; five empirical canary
    signatures — ``uv_not_on_path`` / ``conda_command_not_found`` /
    ``output_file_required`` / ``module_not_found_hpc_agent`` /
    ``undefined_var_expansion`` — were added so the verifier surfaces a
    structured remediation instead of a bare ``dispatcher_failed``; three
    multi-rank signatures — ``mpi_launcher_missing`` / ``mpi_pe_invalid`` /
    ``mpi_init_failed`` — for #293; and ``cluster_env_init`` for the Grid
    Engine / Lmod contentless env-init flake, notebook-audit Addendum 10
    item 15)."""
    assert len(CATALOG) == 19


def test_preempted_matches_exit_130() -> None:
    """Cluster-side dispatch.py exits 130 after trapping SIGTERM —
    classify() must surface this as ``preempted`` so the harness can
    resubmit cleanly without escalating to the user."""
    out = classify("[hpc-agent] SIGTERM received; cluster preemption imminent\n", 130)
    assert out["error_class"] == "preempted"
    assert out["suggested_fix"] == {"action": "resubmit-preempted"}


def test_preempted_matches_exit_130_alone() -> None:
    """Even without the dispatch.py stderr line (e.g. log clipped),
    exit code 130 alone is enough to classify as preempted because the
    catalog entry runs at priority>=90."""
    out = classify("", 130)
    assert out["error_class"] == "preempted"


def test_gpu_oom_matches_cuda_pattern() -> None:
    out = classify("CUDA out of memory: tried to allocate 2GB", None)
    assert out["error_class"] == "gpu_oom"
    assert out["suggested_fix"] == {"action": "increase-mem-per-gpu", "factor": 1.5}


def test_system_oom_matches() -> None:
    out = classify("oom-killer killed pid 1234", 137)
    assert out["error_class"] == "system_oom"
    assert out["suggested_fix"] == {"action": "increase-mem", "factor": 1.5}


def test_walltime_matches() -> None:
    out = classify("DUE TO TIME LIMIT", 271)
    assert out["error_class"] == "walltime"
    assert out["suggested_fix"] == {"action": "increase-walltime", "factor": 1.5}


def test_segv_falls_through() -> None:
    """SEGV entry was removed from the catalog when the SEGV blacklist
    feature was deleted. A bare 'Segmentation fault' line now falls
    through to python_traceback or unknown — classify() must not return
    the deleted 'segv' error_class."""
    out = classify("Segmentation fault (core dumped)", 139)
    assert out["error_class"] != "segv"


def test_node_failure_matches() -> None:
    out = classify("NODE_FAIL on node foo", None)
    assert out["error_class"] == "node_failure"


def test_file_not_found_matches() -> None:
    out = classify("FileNotFoundError: [Errno 2]", 2)
    assert out["error_class"] == "file_not_found"


def test_import_error_matches() -> None:
    out = classify("ModuleNotFoundError: numpy", 1)
    assert out["error_class"] == "import_error"


def test_permission_denied_matches() -> None:
    out = classify("PermissionError: [Errno 13]", 13)
    assert out["error_class"] == "permission_denied"


def test_disk_full_matches() -> None:
    out = classify("No space left on device", 28)
    assert out["error_class"] == "disk_full"


def test_python_traceback_fallback() -> None:
    out = classify("Traceback (most recent call last):\n  File 'foo.py', line 1, in <module>", 1)
    assert out["error_class"] == "python_traceback"


def test_unknown_fallback() -> None:
    out = classify("some unrecognized error", None)
    assert out["error_class"] == "unknown"
    assert out["suggested_fix"] == {"action": "user-debug"}


def test_priority_high_beats_low() -> None:
    """When stderr matches both a high- and low-priority pattern, the
    high-priority one wins (gpu_oom over python_traceback)."""
    stderr = (
        "Traceback (most recent call last):\n"
        "  File 'foo.py', line 1, in <module>\n"
        "RuntimeError: CUDA out of memory: tried to allocate 2GB"
    )
    out = classify(stderr, 1)
    assert out["error_class"] == "gpu_oom"


def test_exit_code_alone_walltime() -> None:
    """Exit 271 alone should still pick walltime (priority >= 90)."""
    out = classify("", 271)
    assert out["error_class"] == "walltime"


def test_exit_code_alone_low_priority_does_not_match() -> None:
    """Exit 1 alone should NOT pick python_traceback (priority < 90)."""
    out = classify("", 1)
    assert out["error_class"] == "unknown"


def test_exit_code_alone_system_oom() -> None:
    """Exit 137 (128 + SIGKILL) alone still surfaces system_oom — a
    genuinely discriminating scheduler/signal code (flagged
    exit_code_sufficient)."""
    assert classify("", 137)["error_class"] == "system_oom"


def test_pattern_less_rc2_is_not_misdiagnosed_uv_missing() -> None:
    """bug-sweep #34: exit code 2 is a GENERIC argparse/usage code shared by
    several priority-95 empirical-config signatures (uv_not_on_path,
    output_file_required). A pattern-less rc=2 must NOT be misread as any of
    them — it is not discriminating, so the exit-code-alone fallback skips it
    and the result is ``unknown``."""
    out = classify("", 2)
    assert out["error_class"] == "unknown"
    assert out["error_class"] not in {"uv_not_on_path", "output_file_required"}


def test_pattern_less_rc127_is_not_misdiagnosed_mpi_launcher_missing() -> None:
    """bug-sweep #34: exit code 127 (command-not-found) is generic; a
    pattern-less rc=127 must NOT be misread as ``mpi_launcher_missing``."""
    out = classify("", 127)
    assert out["error_class"] == "unknown"
    assert out["error_class"] != "mpi_launcher_missing"


def test_generic_exit_code_rows_are_not_exit_code_sufficient() -> None:
    """The generic rc=2 / rc=127 rows must stay flagged non-sufficient, while
    the three genuinely-discriminating codes (130/137/271) stay sufficient —
    the structural guard behind the two tests above."""
    sufficient = {sig.error_class for sig in CATALOG if sig.exit_code_sufficient}
    assert sufficient == {"preempted", "system_oom", "walltime"}
    generic = {
        sig.error_class for sig in CATALOG if sig.exit_code in (2, 127) and sig.exit_code_sufficient
    }
    assert not generic


def test_exit_code_keyed_rows_still_pattern_match() -> None:
    """Excluding the generic rows from the exit-code-alone fallback must not
    weaken their PATTERN path: the rc=2 / rc=127 empirical rows still classify
    when their stderr marker is present (exit code as tiebreaker)."""
    assert (
        classify("[template] HPC_RUNTIME=uv but 'uv' not on PATH", 2)["error_class"]
        == "uv_not_on_path"
    )
    assert classify("srun: command not found", 127)["error_class"] == "mpi_launcher_missing"


def test_classify_returns_a_fresh_dict() -> None:
    """suggested_fix must be a copy, not a reference to CATALOG state."""
    a = classify("CUDA out of memory", None)["suggested_fix"]
    a["action"] = "mutated"
    b = classify("CUDA out of memory", None)["suggested_fix"]
    assert b["action"] == "increase-mem-per-gpu"


# ── empirical canary signatures (Fix 1) ─────────────────────────────────────


def test_uv_not_on_path_matches() -> None:
    """The cluster preamble error token when HPC_RUNTIME=uv but uv is missing.

    Matches the verbatim string the preamble emits, not the operator's
    paraphrase — the catalog row is a structural-failure remediation, not a
    spell-check on the user's stderr.
    """
    out = classify("[template] HPC_RUNTIME=uv but 'uv' not on PATH", 2)
    assert out["error_class"] == "uv_not_on_path"
    assert out["suggested_fix"]["action"] == "drop-runtime-uv-or-install"
    assert "drop" in out["suggested_fix"]["hint"].lower()


def test_conda_command_not_found_matches() -> None:
    """Conda activation failed because conda was never sourced — usually a
    bad ``conda_source`` in clusters.yaml."""
    out = classify("conda: command not found", None)
    assert out["error_class"] == "conda_command_not_found"
    assert out["suggested_fix"]["action"] == "fix-cluster-conda-source"
    out2 = classify("bash: conda: command not found", None)
    assert out2["error_class"] == "conda_command_not_found"


def test_output_file_required_matches() -> None:
    """Executor's argparse rejects its invocation because --output-file
    wasn't auto-injected. Points at a register_run / kind misconfig."""
    out = classify(
        "executor.py: error: the following arguments are required: --output-file",
        2,
    )
    assert out["error_class"] == "output_file_required"
    assert out["suggested_fix"]["action"] == "verify-register-run-on-disk"


def test_module_not_found_hpc_agent_matches() -> None:
    """Cluster-side python can't import hpc_agent — wrong env activation."""
    out = classify("ModuleNotFoundError: No module named 'hpc_agent'", 1)
    assert out["error_class"] == "module_not_found_hpc_agent"
    assert out["suggested_fix"]["action"] == "fix-cluster-env-activation"


def test_module_not_found_hpc_agent_beats_generic_import_error() -> None:
    """The hpc_agent-specific signature wins over the generic import_error
    catalog row — higher priority (85 > 80)."""
    out = classify(
        "Traceback (most recent call last):\nModuleNotFoundError: No module named 'hpc_agent'\n",
        1,
    )
    assert out["error_class"] == "module_not_found_hpc_agent"


def test_undefined_var_expansion_matches() -> None:
    """An executor flag expected a value but got an empty string from
    ``--samples $SAMPLES`` when SAMPLES was unexported."""
    out = classify("executor.py: error: argument --samples: expected one argument", 2)
    assert out["error_class"] == "undefined_var_expansion"
    assert out["suggested_fix"]["action"] == "fix-empty-env-var-in-executor"


def test_uv_not_on_path_beats_python_traceback() -> None:
    """When the stderr carries both a uv structural marker and an
    incidental Traceback, the structural marker wins (priority 95 > 10)."""
    stderr = (
        "Traceback (most recent call last):\n"
        '  File "x.py", ...\n'
        "[template] HPC_RUNTIME=uv but 'uv' not on PATH"
    )
    out = classify(stderr, 2)
    assert out["error_class"] == "uv_not_on_path"


# ── multi-rank (MPI) signatures (#293 PR4) ──────────────────────────────────


def test_classify_mpi_launcher_missing() -> None:
    out = classify("srun: command not found", 127)
    assert out["error_class"] == "mpi_launcher_missing"
    assert out["suggested_fix"]["action"] == "fix-mpi-launcher"


def test_classify_mpi_pe_invalid() -> None:
    out = classify('error: parallel environment "mpi" does not exist', None)
    assert out["error_class"] == "mpi_pe_invalid"
    assert out["suggested_fix"]["action"] == "fix-mpi-pe-name"


def test_classify_mpi_not_enough_slots() -> None:
    # OpenMPI's ranks>capacity error surfaces as mpi_init_failed.
    out = classify(
        "There are not enough slots available in the system to satisfy the 8 slots",
        None,
    )
    assert out["error_class"] == "mpi_init_failed"
    assert out["suggested_fix"]["action"] == "check-mpi-topology"


def test_classify_mpi_init_wins_over_traceback() -> None:
    # A launch failure that also dumps a Python traceback still classifies as
    # the structural MPI error (priority above the bare-traceback fallback).
    stderr = "Traceback (most recent call last):\n  ...\nMPI_Init failed: PMIx error"
    assert classify(stderr, 1)["error_class"] == "mpi_init_failed"


# ── cluster env-init signature (notebook-audit Addendum 10, item 15) ─────────


def test_cluster_env_init_matches_grid_engine_message() -> None:
    """The exact run-#11 Grid Engine (UGE) contentless message classifies as
    ``cluster_env_init`` with the retry-forward remediation.

    HOFFMAN2 emitted this verbatim string on ONE array instance while its
    siblings ran healthily — a transient per-task/per-node flake, not a
    run-wide fault. The remediation must lead with RETRY, not "check the
    stderr" (which punts when the tail is contentless).
    """
    out = classify("Unable to initialize environment because of error", None)
    assert out["error_class"] == "cluster_env_init"
    assert out["suggested_fix"]["action"] == "retry-task"
    assert "retry" in out["suggested_fix"]["hint"].lower()
    # The remediation names the real suspects in priority order.
    hint = out["suggested_fix"]["hint"].lower()
    assert "quota" in hint
    assert "cache" in hint
    assert "per-task" in hint or "per-node" in hint


def test_cluster_env_init_matches_lmod_lookalike() -> None:
    """Lmod's lookalike init failure (no trailing "because of error") also
    classifies as ``cluster_env_init`` — one conservative phrase covers both
    dialects without a scheduler branch."""
    out = classify("Lmod has detected the following error: Unable to initialize environment", None)
    assert out["error_class"] == "cluster_env_init"


def test_cluster_env_init_case_insensitive() -> None:
    out = classify("unable to initialize environment", None)
    assert out["error_class"] == "cluster_env_init"


def test_cluster_env_init_near_misses_do_not_match() -> None:
    """Benign / adjacent strings must NOT classify as ``cluster_env_init`` —
    the stem is specific enough that only the real env-init failure hits."""
    # Success message — opposite meaning.
    ok = classify("environment initialized successfully", None)
    assert ok["error_class"] != "cluster_env_init"
    # A module *load* failure names the environment but not the init stem.
    assert classify("Unable to load module 'gcc/11'", None)["error_class"] != "cluster_env_init"
    # "the environment" (extra word between init and environment) is not the
    # Grid Engine / Lmod message shape — must fall through, not false-positive.
    out = classify("Failed to initialize the environment variable PATH", None)
    assert out["error_class"] != "cluster_env_init"


def test_cluster_env_init_never_fires_on_exit_code_alone() -> None:
    """Pattern-only row (exit_code=None): a bare exit code must never surface
    ``cluster_env_init`` — only the message shape does."""
    assert classify("", 1)["error_class"] != "cluster_env_init"
    assert classify("", 271)["error_class"] != "cluster_env_init"


# ── conda-run blindness (silent-success signature; separate seam) ────────────


def test_conda_run_blind_fires_on_empty_stdout_rc0() -> None:
    """`conda run` + empty stdout + rc 0 = the silent-blindness class. The
    remediation names the DIRECT env-python path and is NOT retry-worthy."""
    out = classify_conda_run_blind(
        command="conda run -n rlin_tune python -m analysis.summarize",
        stdout="",
        exit_code=0,
    )
    assert out is not None
    assert out["error_class"] == CONDA_RUN_BLIND_CLASS
    assert out["suggested_fix"]["retry_worthy"] is False
    # The remediation names the direct env-python invocation.
    hint = out["suggested_fix"]["hint"]
    assert "~/.conda/envs/<env>/bin/python" in hint
    assert out["suggested_fix"]["action"] == "use-direct-env-python"


def test_conda_run_blind_ignores_whitespace_only_stdout() -> None:
    """Whitespace-only stdout is still 'empty' — the wrapper produced nothing."""
    out = classify_conda_run_blind(
        command="conda run -n env printf ''", stdout="  \n\t ", exit_code=0
    )
    assert out is not None
    assert out["error_class"] == CONDA_RUN_BLIND_CLASS


def test_conda_run_blind_does_not_fire_on_non_conda_empty_command() -> None:
    """A legitimately-empty NON-conda command must never be tagged blind."""
    assert (
        classify_conda_run_blind(command="python -m analysis.summarize", stdout="", exit_code=0)
        is None
    )
    # A bare `true` / no-op that legitimately prints nothing.
    assert classify_conda_run_blind(command="true", stdout="", exit_code=0) is None


def test_conda_run_blind_does_not_fire_when_stdout_present() -> None:
    """Real output from `conda run` means conda WAS initialized — not blind."""
    assert (
        classify_conda_run_blind(
            command="conda run -n env python -c 'print(1)'", stdout="1\n", exit_code=0
        )
        is None
    )


def test_conda_run_blind_does_not_fire_on_nonzero_rc() -> None:
    """A `conda run` that failed loudly (rc != 0) is a normal failure, not the
    silent-success blindness class."""
    assert (
        classify_conda_run_blind(command="conda run -n env python -m foo", stdout="", exit_code=1)
        is None
    )
