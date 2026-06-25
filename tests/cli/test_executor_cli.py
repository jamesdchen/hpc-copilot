"""Smoke tests for the executor-side CLI helpers.

These pin the public surface used by every auto-generated
``.hpc/tasks.py`` and ``.hpc/cli.py`` — anything that breaks here
breaks every experiment repo's executor invocations.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hpc_agent.executor_cli import (
    build_parser_from_flags,
    flag,
    generic_args,
    gpu_args,
    main,
    run_module,
    run_registered,
)


def test_flag_underscore_name_becomes_hyphenated_cli() -> None:
    p = argparse.ArgumentParser()
    flag("output_file", str, required=True).add_to(p)
    args = p.parse_args(["--output-file", "out.csv"])
    assert args.output_file == "out.csv"


def test_flag_default_is_set_when_optional() -> None:
    p = argparse.ArgumentParser()
    flag("seed", int, default=42).add_to(p)
    args = p.parse_args([])
    assert args.seed == 42


def test_flag_optional_with_no_default_resolves_to_none() -> None:
    p = argparse.ArgumentParser()
    flag("epochs", int).add_to(p)
    args = p.parse_args([])
    assert args.epochs is None


def test_flag_required_aborts_when_missing() -> None:
    p = argparse.ArgumentParser()
    flag("output_file", str, required=True).add_to(p)
    with pytest.raises(SystemExit):
        p.parse_args([])


def test_flag_choices_enforced() -> None:
    p = argparse.ArgumentParser()
    flag("segment", str, choices=["am", "pm", "all"]).add_to(p)
    args = p.parse_args(["--segment", "am"])
    assert args.segment == "am"
    with pytest.raises(SystemExit):
        p.parse_args(["--segment", "noon"])


def test_generic_args_includes_required_output_file() -> None:
    flags = generic_args()
    by_name = {f.name: f for f in flags}
    assert "output_file" in by_name
    assert by_name["output_file"].required is True
    # The rest are optional and have stable defaults.
    assert by_name["seed"].default == 42
    assert by_name["start"].default == 0
    assert by_name["end"].default == -1


def test_gpu_args_present() -> None:
    flags = gpu_args()
    by_name = {f.name: f for f in flags}
    assert {"gpu_count", "epochs", "batch_size", "learning_rate"} <= set(by_name)
    assert by_name["gpu_count"].default == 1


def test_build_parser_from_flags_accepts_dict_entries() -> None:
    flags = [{"name": "horizon", "type": int, "default": 1}, flag("seed", int, default=42)]
    p = build_parser_from_flags(flags, description="mixed")
    args = p.parse_args(["--horizon", "5"])
    assert args.horizon == 5
    assert args.seed == 42


def test_build_parser_from_flags_rejects_bad_entry_type() -> None:
    with pytest.raises(TypeError, match="must be Flag instances or dicts"):
        build_parser_from_flags(["not a flag"])  # type: ignore[list-item]


def test_realistic_tasks_py_shape() -> None:
    """End-to-end: the FLAGS dict shape an auto-generated tasks.py would have."""
    FLAGS = {
        "src.ml_ridge": [
            *generic_args(),
            flag("horizon", int, default=1),
            flag("segment", str, choices=("am", "pm", "all")),
        ],
        "src.dl_patchts": [
            *generic_args(),
            *gpu_args(),
            flag("horizon", int, default=1),
        ],
    }
    # Each per-executor parser is built independently, no flag bleed.
    ridge = build_parser_from_flags(FLAGS["src.ml_ridge"], description="src.ml_ridge")
    args = ridge.parse_args(["--output-file", "r.csv", "--horizon", "5", "--segment", "pm"])
    assert args.horizon == 5
    assert args.segment == "pm"
    # ml_ridge parser does NOT know about --epochs.
    with pytest.raises(SystemExit):
        ridge.parse_args(["--output-file", "r.csv", "--epochs", "10"])

    patchts = build_parser_from_flags(FLAGS["src.dl_patchts"], description="src.dl_patchts")
    args = patchts.parse_args(["--output-file", "p.csv", "--epochs", "10"])
    assert args.epochs == 10


# --------------------------------------------------------------------------- #
# run-registered: the deterministic @register_run dispatcher (#351)
# --------------------------------------------------------------------------- #


def _write_register_run_module(path: Path, body: str) -> None:
    """Write a @register_run module file (imports register_run from the package)."""
    path.write_text("from hpc_agent import register_run\n" + body, encoding="utf-8")


def test_run_registered_coerces_env_strings_and_writes_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatch coerces HPC_KW_* strings to annotated types (#350/#351) and
    writes the returned dict to $RESULT_DIR/metrics.json — the exact path the
    2026-06-24 demo failed on (``range("1000000")`` TypeError)."""
    _write_register_run_module(
        tmp_path / "mc.py",
        "@register_run\n"
        "def estimate(samples: int = 5, seed: int = 0) -> dict:\n"
        "    return {'doubled': samples * 2, 'seed_type': type(seed).__name__}\n",
    )
    result_dir = tmp_path / "out"
    monkeypatch.setenv("REPO_DIR", str(tmp_path))
    monkeypatch.setenv("RESULT_DIR", str(result_dir))
    # Strings, exactly as the cluster dispatcher exports them:
    monkeypatch.setenv("HPC_KW_SAMPLES", "1000000")
    monkeypatch.setenv("HPC_KW_SEED", "3")

    rc = run_registered(["mc.py", "--run-name", "estimate"])

    assert rc == 0
    metrics = json.loads((result_dir / "metrics.json").read_text())
    assert metrics["doubled"] == 2_000_000  # coerced int, not "1000000" * 2
    assert metrics["seed_type"] == "int"


def test_run_registered_via_dash_m_subprocess(tmp_path: Path) -> None:
    """The real cluster invocation form — ``python -m hpc_agent.executor_cli
    run-registered ...`` — actually resolves and runs, with no nested-quote
    shell fragility. This is the exact argv stamped into the sidecar
    executor_cmd, so it pins the form the cluster will execute."""
    _write_register_run_module(
        tmp_path / "mc.py",
        "@register_run\n"
        "def estimate(samples: int = 5) -> dict:\n"
        "    return {'doubled': samples * 2}\n",
    )
    result_dir = tmp_path / "out"
    env = {
        **os.environ,
        "REPO_DIR": str(tmp_path),
        "RESULT_DIR": str(result_dir),
        "HPC_KW_SAMPLES": "21",
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hpc_agent.executor_cli",
            "run-registered",
            "mc.py",
            "--run-name",
            "estimate",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads((result_dir / "metrics.json").read_text())["doubled"] == 42


def test_run_registered_defaults_output_to_result_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No HPC_KW_OUTPUT_FILE → output_file defaults to $RESULT_DIR/metrics.json,
    so a function that just returns a dict lands its result without the user
    wiring up the kwarg (0.10.3 demo: dict silently dropped)."""
    _write_register_run_module(
        tmp_path / "mc.py",
        "@register_run\ndef estimate(seed: int = 0) -> dict:\n    return {'seed': seed}\n",
    )
    result_dir = tmp_path / "out"
    monkeypatch.setenv("REPO_DIR", str(tmp_path))
    monkeypatch.setenv("RESULT_DIR", str(result_dir))
    monkeypatch.setenv("HPC_KW_SEED", "7")

    assert run_registered(["mc.py"]) == 0
    assert json.loads((result_dir / "metrics.json").read_text())["seed"] == 7


def test_run_registered_explicit_output_file_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit HPC_KW_OUTPUT_FILE (declared FLAGS) wins over the default
    via setdefault — the result lands where the operator asked, not the fallback."""
    _write_register_run_module(
        tmp_path / "mc.py",
        "@register_run\ndef estimate(seed: int = 0) -> dict:\n    return {'seed': seed}\n",
    )
    explicit = tmp_path / "custom" / "answer.json"
    monkeypatch.setenv("REPO_DIR", str(tmp_path))
    monkeypatch.setenv("RESULT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("HPC_KW_SEED", "1")
    monkeypatch.setenv("HPC_KW_OUTPUT_FILE", str(explicit))

    assert run_registered(["mc.py"]) == 0
    assert explicit.is_file()
    assert not (tmp_path / "out" / "metrics.json").exists()


def test_run_registered_stale_run_name_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A --run-name absent from the module's _RUNS registry fails loudly here
    (naming the mismatch as a stale spec), not by silently running the wrong
    module — the abandoned-vs-failed misdiagnosis class (#351)."""
    _write_register_run_module(
        tmp_path / "mc.py",
        "@register_run\ndef estimate(seed: int = 0) -> dict:\n    return {'seed': seed}\n",
    )
    monkeypatch.setenv("REPO_DIR", str(tmp_path))
    monkeypatch.setenv("RESULT_DIR", str(tmp_path / "out"))
    with pytest.raises(SystemExit, match="ghost"):
        run_registered(["mc.py", "--run-name", "ghost"])


def test_run_registered_module_without_compute_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module with no @register_run (no injected compute) is rejected with an
    actionable message rather than an opaque AttributeError."""
    (tmp_path / "plain.py").write_text("def estimate():\n    return {}\n", encoding="utf-8")
    monkeypatch.setenv("REPO_DIR", str(tmp_path))
    monkeypatch.setenv("RESULT_DIR", str(tmp_path / "out"))
    with pytest.raises(SystemExit, match="no compute"):
        run_registered(["plain.py"])


def test_run_registered_missing_module_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module_rel that doesn't exist under $REPO_DIR fails with a clear message."""
    monkeypatch.setenv("REPO_DIR", str(tmp_path))
    with pytest.raises(SystemExit, match="cannot import"):
        run_registered(["nope.py"])


# --------------------------------------------------------------------------- #
# run-module: the deterministic python_module dispatcher (#351 follow-up)
# --------------------------------------------------------------------------- #


def test_run_module_coerces_env_strings_and_writes_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run-module imports an UNDECORATED function by dotted name, coerces
    HPC_KW_* strings to its annotated types (the same #350 body run-registered
    uses), and writes the returned dict to $RESULT_DIR/metrics.json — closing
    the python_module gap (the entry kind that previously shipped no executor)."""
    (tmp_path / "rm_train.py").write_text(
        "def main(samples: int = 5, seed: int = 0) -> dict:\n"
        "    return {'doubled': samples * 2, 'seed_type': type(seed).__name__}\n",
        encoding="utf-8",
    )
    result_dir = tmp_path / "out"
    monkeypatch.setenv("REPO_DIR", str(tmp_path))
    monkeypatch.setenv("RESULT_DIR", str(result_dir))
    # Strings, exactly as the cluster dispatcher exports them:
    monkeypatch.setenv("HPC_KW_SAMPLES", "1000000")
    monkeypatch.setenv("HPC_KW_SEED", "3")

    assert run_module(["rm_train:main"]) == 0
    metrics = json.loads((result_dir / "metrics.json").read_text())
    assert metrics["doubled"] == 2_000_000  # coerced int, not "1000000" * 2
    assert metrics["seed_type"] == "int"


def test_run_module_via_dash_m_subprocess(tmp_path: Path) -> None:
    """The real cluster invocation form — ``python -m hpc_agent.executor_cli
    run-module my_pkg.train:main`` — resolves and runs end-to-end. This is the
    exact argv ``python_module_executor_cmd`` stamps into the sidecar."""
    pkg = tmp_path / "rm_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "train.py").write_text(
        "def main(samples: int = 5) -> dict:\n    return {'doubled': samples * 2}\n",
        encoding="utf-8",
    )
    result_dir = tmp_path / "out"
    env = {
        **os.environ,
        "REPO_DIR": str(tmp_path),
        "RESULT_DIR": str(result_dir),
        "HPC_KW_SAMPLES": "21",
    }
    proc = subprocess.run(
        [sys.executable, "-m", "hpc_agent.executor_cli", "run-module", "rm_pkg.train:main"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads((result_dir / "metrics.json").read_text())["doubled"] == 42


def test_run_module_defaults_output_to_result_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No HPC_KW_OUTPUT_FILE → output_file defaults to $RESULT_DIR/metrics.json."""
    (tmp_path / "rm_def.py").write_text(
        "def main(seed: int = 0) -> dict:\n    return {'seed': seed}\n", encoding="utf-8"
    )
    result_dir = tmp_path / "out"
    monkeypatch.setenv("REPO_DIR", str(tmp_path))
    monkeypatch.setenv("RESULT_DIR", str(result_dir))
    monkeypatch.setenv("HPC_KW_SEED", "7")

    assert run_module(["rm_def:main"]) == 0
    assert json.loads((result_dir / "metrics.json").read_text())["seed"] == 7


def test_run_module_no_default_output_suppresses_metrics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--no-default-output → the framework injects no output_file, so a dict
    return has nowhere to land and no metrics.json is written."""
    (tmp_path / "rm_nodef.py").write_text(
        "def main(seed: int = 0) -> dict:\n    return {'seed': seed}\n", encoding="utf-8"
    )
    result_dir = tmp_path / "out"
    monkeypatch.setenv("REPO_DIR", str(tmp_path))
    monkeypatch.setenv("RESULT_DIR", str(result_dir))
    monkeypatch.setenv("HPC_KW_SEED", "7")

    assert run_module(["rm_nodef:main", "--no-default-output"]) == 0
    assert not (result_dir / "metrics.json").exists()


def test_run_module_malformed_spec_is_loud() -> None:
    """A spec missing the ``<module>:<function>`` colon fails loudly with guidance."""
    with pytest.raises(SystemExit, match="expected <module>:<function>"):
        run_module(["just_a_module"])


def test_run_module_missing_function_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A function absent from the module fails loudly (stale spec), not silently."""
    (tmp_path / "rm_nofn.py").write_text("def other() -> dict:\n    return {}\n", encoding="utf-8")
    monkeypatch.setenv("REPO_DIR", str(tmp_path))
    monkeypatch.setenv("RESULT_DIR", str(tmp_path / "out"))
    with pytest.raises(SystemExit, match="has no attribute"):
        run_module(["rm_nofn:main"])


def test_run_module_not_callable_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A target that exists but isn't callable fails loudly."""
    (tmp_path / "rm_notcall.py").write_text("main = 42\n", encoding="utf-8")
    monkeypatch.setenv("REPO_DIR", str(tmp_path))
    monkeypatch.setenv("RESULT_DIR", str(tmp_path / "out"))
    with pytest.raises(SystemExit, match="not callable"):
        run_module(["rm_notcall:main"])


def test_run_module_missing_module_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A module that doesn't import under $REPO_DIR fails with a clear message."""
    monkeypatch.setenv("REPO_DIR", str(tmp_path))
    with pytest.raises(SystemExit, match="cannot import"):
        run_module(["rm_does_not_exist_xyz:main"])


def test_executor_cli_main_unknown_subcommand_returns_usage(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`python -m hpc_agent.executor_cli <bogus>` returns a non-zero usage code,
    the usage lists both dispatchers, and the helper-only import surface is
    never run by accident."""
    assert main(["bogus"]) == 2
    err = capsys.readouterr().err
    assert "run-registered" in err and "run-module" in err
    assert main([]) == 2
