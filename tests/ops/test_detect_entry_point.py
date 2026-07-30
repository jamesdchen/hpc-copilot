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

import pytest

from hpc_agent import errors
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

    def test_malformed_interview_raises_spec_invalid(self, tmp_path: Path) -> None:
        # A half-written / malformed interview.json is a loud SpecInvalid,
        # never a silent fallthrough to the repo scan — a corrupt file must
        # not change which entry point the worker targets unnoticed.
        (tmp_path / "interview.json").write_text("{ this is not valid json")
        (tmp_path / "train.py").write_text("import argparse\nargparse.ArgumentParser()\n")
        with pytest.raises(errors.SpecInvalid, match="not valid JSON"):
            dep.detect_entry_point(experiment_dir=tmp_path)

    def test_non_object_interview_raises_spec_invalid(self, tmp_path: Path) -> None:
        # Parseable JSON with a non-object top level is just as corrupt.
        (tmp_path / "interview.json").write_text("[1, 2, 3]")
        with pytest.raises(errors.SpecInvalid, match="JSON object at the top level"):
            dep.detect_entry_point(experiment_dir=tmp_path)

    def test_repo_scan_unchanged_when_interview_absent(self, tmp_path: Path) -> None:
        # The whole repo-scan output, key for key, with no interview.json: no
        # ``materialized`` key, and every candidate carries the argv-extraction
        # pair (a click command declaring no options extracts to an EMPTY list —
        # "no flags", which is different from "not knowable").
        (tmp_path / "main.py").write_text("import click\n@click.command()\ndef r():\n    ...\n")
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        assert result == {
            "kind": "detected",
            "candidates": [
                {
                    "path": "main.py",
                    "argv_kind": "click",
                    "argv_extraction": "extracted",
                    "argv_params": [],
                }
            ],
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


def _candidate(result: dict, path: str) -> dict:
    """The candidate whose ``path`` == *path* (unpack-loud if absent)."""
    (found,) = [c for c in result["candidates"] if c["path"] == path]
    assert isinstance(found, dict)
    return found


def _param(candidate: dict, dest: str) -> dict:
    """The extracted param whose ``dest`` == *dest*."""
    (found,) = [p for p in candidate["argv_params"] if p["dest"] == dest]
    assert isinstance(found, dict)
    return found


class TestArgvParamExtraction:
    """P1.d: argparse + click params are read MECHANICALLY off the AST; every
    other surface reports an honest ``unsupported`` + ``argv_params: None``
    so the LLM keeps that leg instead of the framework guessing."""

    def test_argparse_names_types_defaults_required(self, tmp_path: Path) -> None:
        (tmp_path / "train.py").write_text(
            "import argparse\n"
            "def main():\n"
            "    p = argparse.ArgumentParser()\n"
            "    p.add_argument('--seed', '-s', type=int, default=0)\n"
            "    p.add_argument('--config', type=str, required=True)\n"
            "    p.add_argument('--verbose', action='store_true')\n"
            "    p.add_argument('outdir')\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "train.py")
        assert candidate["argv_kind"] == "argparse"
        assert candidate["argv_extraction"] == "extracted"
        # Declaration order is preserved (it is the order argparse binds them).
        assert [p["dest"] for p in candidate["argv_params"]] == [
            "seed",
            "config",
            "verbose",
            "outdir",
        ]
        seed = _param(candidate, "seed")
        assert seed["names"] == ["--seed", "-s"]
        assert seed["type"] == "int"
        assert seed["default"] == 0
        assert seed["positional"] is False
        assert "required" not in seed  # not written → not claimed
        config = _param(candidate, "config")
        assert config["required"] is True
        assert config["type"] == "str"
        # action=store_true is a value-less flag: the wrapper appends the flag
        # alone, never ``--verbose <value>``.
        assert _param(candidate, "verbose")["is_flag"] is True
        outdir = _param(candidate, "outdir")
        assert outdir["positional"] is True
        assert outdir["names"] == ["outdir"]

    def test_argparse_dest_and_group_receivers(self, tmp_path: Path) -> None:
        # An explicit dest= wins over the derived name; a dashed long option
        # derives argparse's own dest; add_argument on an argument GROUP counts
        # (a receiver-name filter would silently drop group-scoped flags).
        (tmp_path / "main.py").write_text(
            "import argparse\n"
            "p = argparse.ArgumentParser()\n"
            "g = p.add_argument_group('grp')\n"
            "g.add_argument('--learning-rate', type=float)\n"
            "p.add_argument('-n', dest='n_iters', type=int)\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "main.py")
        assert _param(candidate, "learning_rate")["names"] == ["--learning-rate"]
        assert _param(candidate, "n_iters")["names"] == ["-n"]

    def test_argparse_non_literal_default_reported_as_source(self, tmp_path: Path) -> None:
        # A default the AST cannot evaluate is NOT invented: ``default_source``
        # carries the expression verbatim so the caller sees a default exists.
        (tmp_path / "run.py").write_text(
            "import argparse, os\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--workers', type=int, default=os.cpu_count())\n"
        )
        workers = _param(
            _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "run.py"), "workers"
        )
        assert "default" not in workers
        assert workers["default_source"] == "os.cpu_count()"

    def test_argparse_parser_built_elsewhere_is_unsupported(self, tmp_path: Path) -> None:
        # No ArgumentParser() in THIS file: the add_argument calls we can see
        # are not provably the whole surface → honest unsupported, not a
        # partial list.
        (tmp_path / "train.py").write_text(
            "import argparse\n"
            "from .cli import build_parser\n"
            "p = build_parser()\n"
            "p.add_argument('--extra', type=int)\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "train.py")
        assert candidate["argv_kind"] == "argparse"
        assert candidate["argv_extraction"] == "unsupported"
        assert candidate["argv_params"] is None

    def test_argparse_subparsers_is_unsupported(self, tmp_path: Path) -> None:
        # Subcommand-scoped flags do not flatten into one parameter list.
        (tmp_path / "main.py").write_text(
            "import argparse\n"
            "p = argparse.ArgumentParser()\n"
            "sub = p.add_subparsers()\n"
            "one = sub.add_parser('one')\n"
            "one.add_argument('--seed', type=int)\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "main.py")
        assert candidate["argv_extraction"] == "unsupported"
        assert candidate["argv_params"] is None

    def test_click_options_and_arguments(self, tmp_path: Path) -> None:
        (tmp_path / "main.py").write_text(
            "import click\n"
            "@click.command()\n"
            "@click.option('--seed', '-s', type=int, default=7)\n"
            "@click.option('--dry-run', is_flag=True)\n"
            "@click.argument('infile', required=True)\n"
            "def run(seed, dry_run, infile):\n"
            "    ...\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "main.py")
        assert candidate["argv_kind"] == "click"
        assert candidate["argv_extraction"] == "extracted"
        # Source order == click's final parameter order.
        assert [p["dest"] for p in candidate["argv_params"]] == ["seed", "dry_run", "infile"]
        seed = _param(candidate, "seed")
        assert seed["names"] == ["--seed", "-s"]
        assert seed["type"] == "int"
        assert seed["default"] == 7
        assert _param(candidate, "dry_run")["is_flag"] is True
        infile = _param(candidate, "infile")
        assert infile["positional"] is True
        assert infile["required"] is True

    def test_click_bare_decorator_name_form(self, tmp_path: Path) -> None:
        # The bare-name decorator form (``from click import option``) reads the
        # same as the ``@click.option`` attribute form. ``import click`` stays
        # on the file because that is what the *classifier* keys on — this test
        # pins the EXTRACTOR's spelling tolerance, not the classifier's.
        (tmp_path / "run.py").write_text(
            "import click\n"
            "from click import command, option\n"
            "@command()\n"
            "@option('--epochs', type=int, default=3)\n"
            "def main(epochs):\n"
            "    ...\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "run.py")
        assert candidate["argv_extraction"] == "extracted"
        assert _param(candidate, "epochs")["default"] == 3

    def test_click_group_of_several_commands_is_unsupported(self, tmp_path: Path) -> None:
        # Two commands' parameters are not one flat surface.
        (tmp_path / "main.py").write_text(
            "import click\n"
            "@click.group()\n"
            "def cli():\n"
            "    ...\n"
            "@cli.command()\n"
            "@click.option('--a', type=int)\n"
            "def one(a):\n"
            "    ...\n"
            "@cli.command()\n"
            "@click.option('--b', type=int)\n"
            "def two(b):\n"
            "    ...\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "main.py")
        assert candidate["argv_extraction"] == "unsupported"
        assert candidate["argv_params"] is None

    def test_click_without_command_declaration_is_unsupported(self, tmp_path: Path) -> None:
        # ``import click`` alone classifies as click, but with no command
        # declared here an EMPTY param list would read as "takes no flags" —
        # a claim the file does not support.
        (tmp_path / "train.py").write_text("import click\nCLI = None\n")
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "train.py")
        assert candidate["argv_kind"] == "click"
        assert candidate["argv_extraction"] == "unsupported"
        assert candidate["argv_params"] is None

    @pytest.mark.parametrize(
        ("filename", "source", "expected_kind"),
        [
            (
                "train.py",
                "import typer\napp = typer.Typer()\n@app.command()\ndef r(seed: int):\n    ...\n",
                "typer",
            ),
            (
                "main.py",
                'import hydra\n@hydra.main(config_path="conf")\ndef m(cfg):\n    ...\n',
                "hydra",
            ),
            ("run.py", "import fire\ndef r(seed=0):\n    ...\nfire.Fire(r)\n", "fire"),
            ("experiment.py", 'if __name__ == "__main__":\n    print(1)\n', "__main__"),
        ],
    )
    def test_unsupported_frameworks_are_honest(
        self, tmp_path: Path, filename: str, source: str, expected_kind: str
    ) -> None:
        # typer derives its CLI from type hints, hydra from a composed YAML
        # tree, fire from a live signature, ``__main__`` declares nothing —
        # none is mechanically extractable, so NONE is guessed.
        (tmp_path / filename).write_text(source)
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), filename)
        assert candidate["argv_kind"] == expected_kind
        assert candidate["argv_extraction"] == "unsupported"
        assert candidate["argv_params"] is None

    def test_console_script_and_shell_candidates_are_unsupported(self, tmp_path: Path) -> None:
        # No Python source to read: uniform ``unsupported`` rather than an
        # ABSENT field a consumer would have to interpret.
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "demo"\n[project.scripts]\nmytool = "demo.cli:main"\n'
        )
        (tmp_path / "run.sh").write_text("#!/bin/sh\necho hi\n")
        result = dep.detect_entry_point(experiment_dir=tmp_path)
        for path in ("mytool", "run.sh"):
            candidate = _candidate(result, path)
            assert candidate["argv_extraction"] == "unsupported"
            assert candidate["argv_params"] is None

    def test_unparseable_python_candidate_is_unsupported(self, tmp_path: Path) -> None:
        # A syntax error must not crash the scan; the candidate still stands.
        (tmp_path / "train.py").write_text("import argparse\ndef broken(:\n")
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "train.py")
        assert candidate["argv_extraction"] == "unsupported"
        assert candidate["argv_params"] is None

    # ── the never-a-guess rule at PARAM level ──────────────────────────────
    #
    # A single unreadable ARGUMENT does not degrade the candidate's verdict
    # (the param list is still complete in count and names) — the param is
    # emitted with an ``unextracted`` marker NAMING what the consumer must
    # read from the source. Verdict degradation stays reserved for a
    # whole-surface unknown (the structural bails above).

    def test_click_boolean_pair_extracted_correctly(self, tmp_path: Path) -> None:
        # ``--shout/--no-shout`` is ONE boolean param, not a name containing a
        # slash: the primary opt names it, the secondary is surfaced, and it
        # takes NO value (a consumer must never emit ``--shout/--no-shout 1``).
        (tmp_path / "main.py").write_text(
            "import click\n"
            "@click.command()\n"
            "@click.option('--shout/--no-shout', default=True)\n"
            "def run(shout):\n"
            "    ...\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "main.py")
        assert candidate["argv_extraction"] == "extracted"
        shout = _param(candidate, "shout")
        assert shout["names"] == ["--shout"]
        assert shout["secondary_names"] == ["--no-shout"]
        assert shout["is_flag"] is True
        assert shout["default"] is True
        # The dest is a usable attribute name, and nothing was left unread.
        assert shout["dest"].isidentifier()
        assert "unextracted" not in shout

    def test_click_pair_mixed_with_plain_aliases(self, tmp_path: Path) -> None:
        # The mixed case: a pair and a plain alias pair in one command. Only
        # the boolean pair carries ``secondary_names``, which is exactly what
        # distinguishes it from ``('--verbose', '-v')``.
        (tmp_path / "main.py").write_text(
            "import click\n"
            "@click.command()\n"
            "@click.option('--upper/--lower', default=False)\n"
            "@click.option('--verbose', '-v', is_flag=True)\n"
            "@click.option('--seed', type=int, default=0)\n"
            "def run(upper, verbose, seed):\n"
            "    ...\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "main.py")
        assert candidate["argv_extraction"] == "extracted"
        assert [p["dest"] for p in candidate["argv_params"]] == ["upper", "verbose", "seed"]
        assert _param(candidate, "upper")["secondary_names"] == ["--lower"]
        verbose = _param(candidate, "verbose")
        assert verbose["is_flag"] is True
        assert "secondary_names" not in verbose
        assert "is_flag" not in _param(candidate, "seed")

    def test_click_explicit_name_declaration_wins(self, tmp_path: Path) -> None:
        # click's authoritative rule: a decl not starting with ``-`` IS the
        # parameter name and outranks any derivation from the option strings
        # (the analogue of argparse's ``dest=``).
        (tmp_path / "main.py").write_text(
            "import click\n"
            "@click.command()\n"
            "@click.option('--a-very-long-flag-name', 'x', type=int)\n"
            "@click.option('-n', '--iterations')\n"
            "def run(x, iterations):\n"
            "    ...\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "main.py")
        explicit = _param(candidate, "x")
        assert explicit["names"] == ["--a-very-long-flag-name"]
        assert "unextracted" not in explicit
        # No explicit name → click's first-LONG-option rule (not longest name).
        derived = _param(candidate, "iterations")
        assert derived["names"] == ["-n", "--iterations"]

    def test_argparse_nargs_and_choices_represented(self, tmp_path: Path) -> None:
        # Arity and value domain are literals in the source, so they are
        # carried rather than dropped — a consumer emitting ``--tags <one>``
        # for an ``nargs='+'`` flag would build the wrong argv.
        (tmp_path / "train.py").write_text(
            "import argparse\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--tags', nargs='+')\n"
            "p.add_argument('--mode', choices=['fast', 'slow'])\n"
            "p.add_argument('--pair', nargs=2, type=int)\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "train.py")
        assert candidate["argv_extraction"] == "extracted"
        assert _param(candidate, "tags")["nargs"] == "+"
        assert _param(candidate, "pair")["nargs"] == 2
        assert _param(candidate, "mode")["choices"] == ["fast", "slow"]
        for dest in ("tags", "mode", "pair"):
            assert "unextracted" not in _param(candidate, dest)

    def test_argparse_unmodeled_action_is_named_not_dropped(self, tmp_path: Path) -> None:
        # ``append`` is a literal (so it is recorded) whose repeat semantics are
        # NOT modeled → named in ``unextracted``. A custom Action class is not
        # even a literal → named, with no ``action`` field to mislead.
        (tmp_path / "main.py").write_text(
            "import argparse\n"
            "class MyAction(argparse.Action):\n"
            "    pass\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--extra', action='append')\n"
            "p.add_argument('--custom', action=MyAction)\n"
            "p.add_argument('--plain', action='store', type=int)\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "main.py")
        # Verdict is NOT degraded — the param list is complete in count/names.
        assert candidate["argv_extraction"] == "extracted"
        extra = _param(candidate, "extra")
        assert extra["action"] == "append"
        assert extra["unextracted"] == ["action"]
        custom = _param(candidate, "custom")
        assert "action" not in custom
        assert custom["unextracted"] == ["action"]
        plain = _param(candidate, "plain")
        assert plain["action"] == "store"
        assert "unextracted" not in plain

    def test_non_literal_nargs_and_choices_are_named(self, tmp_path: Path) -> None:
        (tmp_path / "run.py").write_text(
            "import argparse\n"
            "N = 3\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--a', nargs=N)\n"
            "p.add_argument('--b', choices=range(3))\n"
            "p.add_argument('--c', required=SOME_FLAG)\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "run.py")
        assert _param(candidate, "a")["unextracted"] == ["nargs"]
        assert "nargs" not in _param(candidate, "a")
        assert _param(candidate, "b")["unextracted"] == ["choices"]
        assert _param(candidate, "c")["unextracted"] == ["required"]
        assert "required" not in _param(candidate, "c")

    def test_argparse_non_literal_dest_is_named(self, tmp_path: Path) -> None:
        # The derived fallback is not what argparse will bind, so say so.
        (tmp_path / "main.py").write_text(
            "import argparse\n"
            "KEY = 'k'\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--x', dest=KEY)\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "main.py")
        assert _param(candidate, "x")["unextracted"] == ["dest"]

    def test_click_multiple_and_count(self, tmp_path: Path) -> None:
        # ``multiple=True`` is fully modeled (one argv occurrence per value);
        # ``count=True`` is value-less but its repeat-to-integer collection is
        # not modeled, so the flag half is reported and the rest is NAMED.
        (tmp_path / "main.py").write_text(
            "import click\n"
            "@click.command()\n"
            "@click.option('--tag', multiple=True)\n"
            "@click.option('-v', '--verbose', count=True)\n"
            "def run(tag, verbose):\n"
            "    ...\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "main.py")
        tag = _param(candidate, "tag")
        assert tag["multiple"] is True
        assert "unextracted" not in tag
        verbose = _param(candidate, "verbose")
        assert verbose["is_flag"] is True
        assert verbose["unextracted"] == ["count"]

    def test_click_variadic_argument_nargs(self, tmp_path: Path) -> None:
        # click's ``nargs=-1`` variadic argument, and the argument name's
        # dash→underscore lowering.
        (tmp_path / "main.py").write_text(
            "import click\n"
            "@click.command()\n"
            "@click.argument('IN-FILES', nargs=-1)\n"
            "def run(in_files):\n"
            "    ...\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "main.py")
        param = _param(candidate, "in_files")
        assert param["positional"] is True
        assert param["nargs"] == -1
        assert "unextracted" not in param

    def test_every_extracted_dest_is_an_identifier_or_named(self, tmp_path: Path) -> None:
        # The invariant the ``--shout/--no-shout`` bug violated, pinned over a
        # mixed file: a dest that is not a usable attribute name may never be
        # emitted as if authoritative.
        (tmp_path / "main.py").write_text(
            "import click\n"
            "@click.command()\n"
            "@click.option('--a/--no-a')\n"
            "@click.option('--b-c', '-b')\n"
            "@click.argument('D-E')\n"
            "def run(a, b_c, d_e):\n"
            "    ...\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "main.py")
        for param in candidate["argv_params"]:
            assert param["dest"].isidentifier() or "dest" in param.get("unextracted", []), param

    def test_extraction_never_imports_user_code(self, tmp_path: Path) -> None:
        # Pure AST: a candidate importing a module that does not exist (and
        # whose body would raise on execution) still extracts.
        (tmp_path / "main.py").write_text(
            "import argparse\n"
            "import definitely_not_installed_xyz\n"
            "raise SystemExit('never runs')\n"
            "p = argparse.ArgumentParser()\n"
            "p.add_argument('--seed', type=int)\n"
        )
        candidate = _candidate(dep.detect_entry_point(experiment_dir=tmp_path), "main.py")
        assert candidate["argv_extraction"] == "extracted"
        assert _param(candidate, "seed")["type"] == "int"


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
