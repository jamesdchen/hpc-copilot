"""``register_run`` decorator + injected ``compute`` + ``save_artifact`` (Layer 1)."""

from __future__ import annotations

import argparse
import json
import types
from pathlib import Path

import pytest


def _exec_module(src: str, name: str = "hpc_tmpl_test_mod") -> types.ModuleType:
    """Exec *src* as a fresh module so ``run.__globals__`` is isolated."""
    mod = types.ModuleType(name)
    exec(compile(src, f"<{name}>", "exec"), mod.__dict__)
    return mod


def test_register_run_injects_compute_and_registry() -> None:
    mod = _exec_module(
        "from hpc_agent.experiment_kit import register_run\n"
        "\n"
        "@register_run\n"
        "def run(alpha: float = 1.0):\n"
        "    return {'alpha': alpha, 'n_samples': 3}\n"
    )
    assert callable(mod.compute)
    assert "run" in mod._RUNS
    assert mod._RUNS["run"].gpu is False


def test_injected_compute_dumps_dict_return_to_output_file(tmp_path: Path) -> None:
    mod = _exec_module(
        "from hpc_agent.experiment_kit import register_run\n"
        "\n"
        "@register_run\n"
        "def run(alpha: float = 1.0):\n"
        "    return {'alpha': alpha, 'n_samples': 3}\n"
    )
    out = tmp_path / "nested" / "out.json"
    mod.compute(argparse.Namespace(alpha=2.5, output_file=str(out)))
    data = json.loads(out.read_text())
    assert data == {"alpha": 2.5, "n_samples": 3}


def test_compute_forwards_only_matching_kwargs(tmp_path: Path) -> None:
    # ``args`` carries generic-args extras (seed, start, ...) the run
    # never declared; compute must not forward them.
    mod = _exec_module(
        "from hpc_agent.experiment_kit import register_run\n"
        "\n"
        "@register_run\n"
        "def run(alpha: float = 1.0):\n"
        "    return {'alpha': alpha}\n"
    )
    out = tmp_path / "o.json"
    mod.compute(
        argparse.Namespace(alpha=9.0, seed=42, start=0, end=-1, halo=0, output_file=str(out))
    )
    assert json.loads(out.read_text()) == {"alpha": 9.0}


def test_save_artifact_writes_next_to_output_file(tmp_path: Path) -> None:
    mod = _exec_module(
        "from hpc_agent.experiment_kit import register_run, save_artifact\n"
        "\n"
        "@register_run\n"
        "def run(n: int = 1):\n"
        "    save_artifact('blob.txt', 'hello world')\n"
        "    save_artifact('data.bin', b'\\x00\\x01')\n"
        "    return {'n': n}\n"
    )
    out = tmp_path / "task_0" / "out.json"
    mod.compute(argparse.Namespace(n=5, output_file=str(out)))
    assert (tmp_path / "task_0" / "blob.txt").read_text() == "hello world"
    assert (tmp_path / "task_0" / "data.bin").read_bytes() == b"\x00\x01"


def test_register_run_gpu_flag() -> None:
    mod = _exec_module(
        "from hpc_agent.experiment_kit import register_run\n"
        "\n"
        "@register_run(gpu=True)\n"
        "def run(epochs: int = 10):\n"
        "    return {}\n"
    )
    spec = mod._RUNS["run"]
    assert spec.gpu is True
    assert spec.name == "run"


def test_compute_injects_resume_from_for_opted_in_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # #294 PR3: a run that declares resume_from / checkpoint_dir receives the
    # dispatcher-provided values (HPC_RESUME_FROM set on `resubmit --from-checkpoint`).
    mod = _exec_module(
        "from hpc_agent.experiment_kit import register_run\n"
        "\n"
        "@register_run\n"
        "def run(alpha: float = 1.0, resume_from=None, checkpoint_dir=None):\n"
        "    return {\n"
        "        'alpha': alpha,\n"
        "        'resume_from': resume_from,\n"
        "        'checkpoint_dir': checkpoint_dir,\n"
        "    }\n"
    )
    monkeypatch.setenv("HPC_RESUME_FROM", "/ck/checkpoint-7.pkl")
    monkeypatch.setenv("HPC_CHECKPOINT_DIR", "/ck")
    out = tmp_path / "o.json"
    mod.compute(argparse.Namespace(alpha=2.0, output_file=str(out)))
    data = json.loads(out.read_text())
    assert data["resume_from"] == "/ck/checkpoint-7.pkl"
    assert data["checkpoint_dir"] == "/ck"


def test_compute_resume_from_none_when_env_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HPC_RESUME_FROM", raising=False)
    monkeypatch.delenv("HPC_CHECKPOINT_DIR", raising=False)
    mod = _exec_module(
        "from hpc_agent.experiment_kit import register_run\n"
        "\n"
        "@register_run\n"
        "def run(resume_from=None):\n"
        "    return {'resume_from': resume_from}\n"
    )
    out = tmp_path / "o.json"
    mod.compute(argparse.Namespace(output_file=str(out)))
    assert json.loads(out.read_text())["resume_from"] is None


def test_compute_unaffected_for_run_without_resume_param(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Backwards-compat: a run that doesn't declare resume_from is untouched even
    # when the env vars are set (the kwargs filter drops the unaccepted keys).
    monkeypatch.setenv("HPC_RESUME_FROM", "/ck/checkpoint-1.pkl")
    monkeypatch.setenv("HPC_CHECKPOINT_DIR", "/ck")
    mod = _exec_module(
        "from hpc_agent.experiment_kit import register_run\n"
        "\n"
        "@register_run\n"
        "def run(alpha: float = 1.0):\n"
        "    return {'alpha': alpha}\n"
    )
    out = tmp_path / "o.json"
    mod.compute(argparse.Namespace(alpha=1.0, output_file=str(out)))
    assert json.loads(out.read_text()) == {"alpha": 1.0}
