"""Tests for the ``detect-entry-point`` composite primitive (WS5 #4).

Pins the entry-point discovery scan that collapses the six raw-shell
probes ``hpc-wrap-entry-point`` SKILL.md duplicated across Step 0
(greenfield) and Step 1 (mature repo). Each test builds a tmp
experiment dir with fixture entry-point files and asserts the
``kind`` / ``candidates`` / ``argv_kind`` / ``decoration_found`` the
verb reports, one case per probe + per argv style the classifier can
emit.
"""

from __future__ import annotations

import json
from pathlib import Path

from hpc_agent.ops import detect_entry_point as dep


def _argv_kind_for(candidates: list[dict[str, str]], path: str) -> str | None:
    """Return the ``argv_kind`` of the candidate whose ``path`` == *path*."""
    return next((c["argv_kind"] for c in candidates if c["path"] == path), None)


class TestGreenfield:
    """An empty repo (no entry point, no decoration) is ``greenfield``."""

    def test_empty_dir_is_greenfield(self, tmp_path: Path) -> None:
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert result["kind"] == "greenfield"
        assert result["candidates"] == []
        assert result["decoration_found"] == []

    def test_str_and_path_experiment_dir_agree(self, tmp_path: Path) -> None:
        # The CLI passes a str; in-process callers pass a Path. Both coerce.
        from_path = dep.detect_entry_point(experiment_dir=tmp_path)
        from_str = dep.detect_entry_point(experiment_dir=str(tmp_path))
        assert from_path == from_str


class TestArgvClassification:
    """Each Python candidate's CLI surface classifies to the right argv_kind."""

    def test_argparse(self, tmp_path: Path) -> None:
        (tmp_path / "train.py").write_text(
            "import argparse\n"
            "def main():\n"
            "    p = argparse.ArgumentParser()\n"
            "    p.add_argument('--seed', type=int)\n"
            'if __name__ == "__main__":\n'
            "    main()\n"
        )
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert result["kind"] == "detected"
        assert _argv_kind_for(result["candidates"], "train.py") == "argparse"

    def test_click(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text(
            "import click\n"
            "@click.command()\n"
            "@click.option('--seed', type=int)\n"
            "def run(seed):\n"
            "    ...\n"
        )
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert _argv_kind_for(result["candidates"], "main.py") == "click"

    def test_typer(self, tmp_path: Path) -> None:
        (tmp_path / "run.py").write_text(
            "import typer\napp = typer.Typer()\n@app.command()\ndef run(seed: int):\n    ...\n"
        )
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert _argv_kind_for(result["candidates"], "run.py") == "typer"

    def test_hydra(self, tmp_path: Path) -> None:
        # A hydra entry point also imports argparse in some repos; the
        # @hydra.main decorator must win (it rewrites the signature).
        (tmp_path / "train.py").write_text(
            "import argparse\n"
            "import hydra\n"
            '@hydra.main(config_path="conf")\n'
            "def main(cfg):\n"
            "    ...\n"
        )
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert _argv_kind_for(result["candidates"], "train.py") == "hydra"

    def test_fire(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text(
            "import fire\n"
            "def run(seed=0):\n"
            "    ...\n"
            'if __name__ == "__main__":\n'
            "    fire.Fire(run)\n"
        )
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert _argv_kind_for(result["candidates"], "main.py") == "fire"

    def test_bare_main_block(self, tmp_path: Path) -> None:
        # No CLI library, just a bare __main__ block → "__main__".
        (tmp_path / "experiment.py").write_text(
            "def main():\n    print('hi')\nif __name__ == \"__main__\":\n    main()\n"
        )
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert _argv_kind_for(result["candidates"], "experiment.py") == "__main__"


class TestPackageMain:
    """``find ... -name __main__.py`` — package modules are ``python -m`` targets."""

    def test_package_main_detected(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__main__.py").write_text("print('run me with python -m mypkg')\n")
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert _argv_kind_for(result["candidates"], "mypkg/__main__.py") == "__main__"

    def test_package_main_with_argparse(self, tmp_path: Path) -> None:
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "__main__.py").write_text("import argparse\nargparse.ArgumentParser()\n")
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert _argv_kind_for(result["candidates"], "mypkg/__main__.py") == "argparse"

    def test_dotfile_dir_main_excluded(self, tmp_path: Path) -> None:
        # -not -path '*/.*' — a __main__.py under a dotfile dir (.venv) is
        # skipped, exactly as the shell ``find`` probe would skip it.
        venv = tmp_path / ".venv" / "lib"
        venv.mkdir(parents=True)
        (venv / "__main__.py").write_text("...\n")
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert result["candidates"] == []
        assert result["kind"] == "greenfield"

    def test_too_deep_main_excluded(self, tmp_path: Path) -> None:
        # -maxdepth 4 — a/b/c/d/__main__.py (5 parts) is past the cap.
        deep = tmp_path / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (deep / "__main__.py").write_text("...\n")
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert result["candidates"] == []


class TestSrcCandidates:
    """The second ``ls src/main.py src/train.py src/run.py`` probe."""

    def test_src_train_detected(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "train.py").write_text("import argparse\nargparse.ArgumentParser()\n")
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert _argv_kind_for(result["candidates"], "src/train.py") == "argparse"


class TestConsoleScripts:
    """``grep -A1 '[project.scripts]' pyproject.toml`` → console_script candidates."""

    def test_project_scripts_detected(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text(
            "[project]\n"
            'name = "demo"\n'
            "[project.scripts]\n"
            'mytool = "demo.cli:main"\n'
            'othercmd = "demo.other:run"\n'
        )
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        names = {c["path"] for c in result["candidates"]}
        assert {"mytool", "othercmd"} <= names
        assert _argv_kind_for(result["candidates"], "mytool") == "console_script"
        assert result["kind"] == "detected"

    def test_pyproject_without_scripts_table(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "demo"\n')
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert result["candidates"] == []
        assert result["kind"] == "greenfield"


class TestShellCandidates:
    """``ls run.sh launch.sh ./simulator`` → shell / binary entry points."""

    def test_run_sh_detected(self, tmp_path: Path) -> None:
        (tmp_path / "run.sh").write_text("#!/bin/sh\necho hi\n")
        (tmp_path / "simulator").write_text("binary-ish\n")
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert _argv_kind_for(result["candidates"], "run.sh") == "shell"
        assert _argv_kind_for(result["candidates"], "simulator") == "shell"
        assert result["kind"] == "detected"


class TestDecoration:
    """``grep -rln '@register_run' notebooks/ src/ *.py`` → decoration_found."""

    def test_root_py_decoration(self, tmp_path: Path) -> None:
        (tmp_path / "train.py").write_text(
            "from hpc_agent import register_run\n@register_run\ndef run(seed: int):\n    ...\n"
        )
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert "train.py" in result["decoration_found"]
        # A @register_run on disk is itself a non-greenfield signal.
        assert result["kind"] == "detected"

    def test_src_decoration(self, tmp_path: Path) -> None:
        src = tmp_path / "src" / "pkg"
        src.mkdir(parents=True)
        (src / "model.py").write_text("@register_run\ndef run():\n    ...\n")
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert "src/pkg/model.py" in result["decoration_found"]

    def test_decoration_only_is_not_greenfield(self, tmp_path: Path) -> None:
        # No conventional entry-point file, but a decorated helper exists:
        # the repo is already (partially) onboarded → detected, not greenfield.
        helper = tmp_path / "src"
        helper.mkdir()
        (helper / "helpers.py").write_text("@register_run\ndef go():\n    ...\n")
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert result["candidates"] == []
        assert result["decoration_found"] == ["src/helpers.py"]
        assert result["kind"] == "detected"

    def test_no_decoration_when_absent(self, tmp_path: Path) -> None:
        (tmp_path / "train.py").write_text("import argparse\nargparse.ArgumentParser()\n")
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert result["decoration_found"] == []


class TestMultipleCandidates:
    """Multiple entry points all surface (the skill refuses on the tie itself)."""

    def test_two_python_candidates_both_listed(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text("import argparse\nargparse.ArgumentParser()\n")
        (tmp_path / "train.py").write_text("import click\n@click.command()\ndef r():\n    ...\n")
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        paths = {c["path"] for c in result["candidates"]}
        assert {"main.py", "train.py"} <= paths
        assert _argv_kind_for(result["candidates"], "main.py") == "argparse"
        assert _argv_kind_for(result["candidates"], "train.py") == "click"


def _write_interview(root: Path, entry_point: dict | None, *, rel: str = "interview.json") -> None:
    """Write a minimal ``interview.json`` with the given materialized entry point."""
    materialized: dict = {"at": "2026-06-08T00:00:00", "cmd_sha": "deadbeef", "total_tasks": 1}
    if entry_point is not None:
        materialized["entry_point"] = entry_point
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"goal": "g", "task_count": 1, "_materialized": materialized}))


class TestMaterializedEntryPoint:
    """The optional ``materialized`` field surfaced from interview.json."""

    def test_shell_command_block_surfaced(self, tmp_path: Path) -> None:
        # The fallback path: a wrapper was materialized. The worker honors it
        # at Step 0b. ``frozen_shas`` is an internal detail and must NOT leak.
        _write_interview(
            tmp_path,
            {
                "kind": "shell_command",
                "run_name": "myrun",
                "wrapper_path": ".hpc/wrappers/myrun.py",
                "executor_cmd": "python3 .hpc/wrappers/myrun.py",
                "frozen_shas": {"exp.yaml": "abc123"},
                "data_axis": {"kind": "independent"},
            },
        )
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        mat = result["materialized"]
        assert mat["kind"] == "shell_command"
        assert mat["run_name"] == "myrun"
        assert mat["wrapper_path"] == ".hpc/wrappers/myrun.py"
        assert mat["executor_cmd"] == "python3 .hpc/wrappers/myrun.py"
        assert mat["data_axis"] == {"kind": "independent"}
        # Internal identity detail is intentionally dropped.
        assert "frozen_shas" not in mat

    def test_register_run_block_surfaced(self, tmp_path: Path) -> None:
        _write_interview(
            tmp_path,
            {
                "kind": "register_run",
                "run_name": "train",
                "executor_cmd": "python3 -c '...'",
            },
        )
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        mat = result["materialized"]
        assert mat == {
            "kind": "register_run",
            "run_name": "train",
            "executor_cmd": "python3 -c '...'",
        }

    def test_python_module_block_surfaced(self, tmp_path: Path) -> None:
        _write_interview(
            tmp_path,
            {"kind": "python_module", "module": "my_pkg.train", "function": "main"},
        )
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert result["materialized"] == {
            "kind": "python_module",
            "module": "my_pkg.train",
            "function": "main",
        }

    def test_hpc_dir_interview_fallback(self, tmp_path: Path) -> None:
        # A ``.hpc/interview.json`` is accepted as a fallback location.
        _write_interview(
            tmp_path,
            {"kind": "register_run", "run_name": "r", "executor_cmd": "cmd"},
            rel=".hpc/interview.json",
        )
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert result["materialized"]["kind"] == "register_run"

    def test_root_interview_preferred_over_hpc(self, tmp_path: Path) -> None:
        # When both exist, the canonical campaign-dir-root file wins.
        _write_interview(
            tmp_path,
            {"kind": "register_run", "run_name": "root", "executor_cmd": "c"},
        )
        _write_interview(
            tmp_path,
            {"kind": "register_run", "run_name": "hpc", "executor_cmd": "c"},
            rel=".hpc/interview.json",
        )
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert result["materialized"]["run_name"] == "root"

    def test_absent_interview_no_materialized_key(self, tmp_path: Path) -> None:
        # No interview.json → field absent, repo scan stands.
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert "materialized" not in result

    def test_interview_without_materialized_entry_point(self, tmp_path: Path) -> None:
        # interview.json present but no _materialized.entry_point → absent.
        _write_interview(tmp_path, None)
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert "materialized" not in result

    def test_malformed_interview_is_absent(self, tmp_path: Path) -> None:
        # A half-written / malformed interview.json is treated as absent —
        # the repo scan stands rather than crashing.
        (tmp_path / "interview.json").write_text("{ this is not valid json")
        (tmp_path / "train.py").write_text("import argparse\nargparse.ArgumentParser()\n")
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert "materialized" not in result
        # Repo-scan path is unchanged.
        assert _argv_kind_for(result["candidates"], "train.py") == "argparse"

    def test_repo_scan_unchanged_when_interview_absent(self, tmp_path: Path) -> None:
        # The existing repo-scan output is byte-identical with no interview.json.
        (tmp_path / "main.py").write_text("import click\n@click.command()\ndef r():\n    ...\n")
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert result == {
            "kind": "detected",
            "candidates": [{"path": "main.py", "argv_kind": "click"}],
            "decoration_found": [],
        }

    def test_materialized_alongside_repo_scan(self, tmp_path: Path) -> None:
        # A materialized block coexists with repo-scan candidates — both surface.
        (tmp_path / "main.py").write_text("import argparse\nargparse.ArgumentParser()\n")
        _write_interview(
            tmp_path,
            {"kind": "register_run", "run_name": "main", "executor_cmd": "c"},
        )
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert result["materialized"]["kind"] == "register_run"
        assert _argv_kind_for(result["candidates"], "main.py") == "argparse"


class TestSolverDetection:
    """A candidate whose source contains a recognizable solver-library solve
    loop carries the optional ``solver`` field, so onboarding can offer the
    checkpoint-instrumented wrapper for it."""

    def test_petsc_ts_candidate_flagged(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text(
            "import argparse\n"
            "from petsc4py import PETSc\n"
            "def main():\n"
            "    ts = PETSc.TS().create()\n"
            "    ts.setFromOptions()\n"
            "    ts.solve(u)\n"
            'if __name__ == "__main__":\n'
            "    main()\n"
        )
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        (candidate,) = [c for c in result["candidates"] if c["path"] == "main.py"]
        # Solver detection is orthogonal to argv classification.
        assert candidate["argv_kind"] == "argparse"
        assert candidate["solver"] == "petsc"

    def test_non_solver_candidate_omits_field(self, tmp_path: Path) -> None:
        (tmp_path / "train.py").write_text("import argparse\nprint('no solver here')\n")
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        (candidate,) = [c for c in result["candidates"] if c["path"] == "train.py"]
        assert "solver" not in candidate
