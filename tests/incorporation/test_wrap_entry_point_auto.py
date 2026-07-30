"""Tests for the ``wrap-entry-point-auto`` composite primitive (prelude P1.a).

Pins the one-call collapse of detect → pathway table → decorate →
frozen-YAML scan → fixed-params partition, and all FOUR terminal branches:

* ``{onboarded}`` — the deterministic chain ran end to end;
* ``{needs_pick}`` — an entry-point-file or entry-function tie;
* ``{needs_intent}`` — ``goal`` / ``task_generator`` / ``task_count`` or a
  specific uncovered required param;
* ``{needs_wrapper_argv}`` — the wrapper pathway's argv template.

Plus the three promoted prose tables (pathway decision, frozen-YAML
convention scan, fixed-params partition) exercised row by row, and the
REFUSAL pin: code never fabricates ``goal`` / ``task_generator``, and no
escalation branch writes to the repo (the whole tree stays byte-identical).

Fixtures are real tmp repos on disk — the composite's sub-verbs are a
filesystem scan and an AST line-splice, so a tmp tree is both cheaper and
more honest than mocking them out.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.wrap_entry_point_auto import (
    WrapEntryPointAutoInput,
    WrapEntryPointAutoResult,
)
from hpc_agent.incorporation import wrap_entry_point_auto as wep

_GOAL = "measure pi convergence against sample count"


# ── fixture builders ─────────────────────────────────────────────────────


def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _tree_snapshot(root: Path) -> dict[str, str]:
    """Every file's text under *root*, for the byte-identical assertions."""
    return {
        p.relative_to(root).as_posix(): p.read_text(encoding="utf-8")
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _plain_train(params: str = "seed: int") -> str:
    return f'''"""A mature repo's entry point."""


def train({params}) -> dict:
    return {{"ok": True}}
'''


def _decorated_train(params: str = "seed: int") -> str:
    return f'''"""An already-onboarded entry point."""

from hpc_agent import register_run


@register_run
def train({params}) -> dict:
    return {{"ok": True}}
'''


def _spec(**kwargs: Any) -> WrapEntryPointAutoInput:
    """A spec with the human-owned intent filled in unless overridden."""
    base: dict[str, Any] = {
        "goal": _GOAL,
        "task_count": 3,
        "task_generator": {"kind": "items_x_seeds", "params": {"seeds": [0, 1, 2]}},
    }
    base.update(kwargs)
    return WrapEntryPointAutoInput.model_validate(base)


def _run(root: Path, spec: WrapEntryPointAutoInput | None = None) -> dict[str, Any]:
    """Call the composite and validate the result against the wire union."""
    out = wep.wrap_entry_point_auto(root, spec=spec)
    # Every terminal shape must satisfy the discriminated output model — this
    # is what the CLI dispatcher validates the envelope's data block against.
    WrapEntryPointAutoResult.model_validate(out)
    return out


# ── branch 1: onboarded ──────────────────────────────────────────────────


def test_single_register_run_found_onboards(tmp_path: Path) -> None:
    """A sole @register_run on disk IS the entry point; nothing is re-written."""
    _write(tmp_path, "train.py", _decorated_train())
    before = _tree_snapshot(tmp_path)

    out = _run(tmp_path, _spec())

    assert out["onboarded"] is True
    assert out["pathway"] == "decorate"
    assert out["pathway_rule"] == "kwargs_signature"
    assert out["entry_point_kind"] == "register_run"
    assert out["entry_point_path"] == "train.py"
    assert out["run_name"] == "train"
    assert "existing_register_run" in out["entry_point_rule"]
    # Already decorated → the splice verb reports no write.
    assert out["already_decorated"] is True
    assert out["decorated"] is False
    assert out["import_added"] is False
    assert _tree_snapshot(tmp_path) == before


def test_undecorated_sole_candidate_is_decorated(tmp_path: Path) -> None:
    """The default pathway splices @register_run onto the user's function."""
    _write(tmp_path, "train.py", _plain_train())

    out = _run(tmp_path, _spec())

    assert out["onboarded"] is True
    assert out["pathway"] == "decorate"
    assert out["decorated"] is True
    assert out["import_added"] is True
    assert out["entry_point_rule"] == "sole_candidate+sole_public_def"
    text = (tmp_path / "train.py").read_text(encoding="utf-8")
    assert "from hpc_agent import register_run" in text
    assert "@register_run" in text
    # The body is untouched — the splice is two lines, never a rewrite.
    assert 'return {"ok": True}' in text


def test_onboarded_emits_a_ready_interview_spec_fragment(tmp_path: Path) -> None:
    """The fragment carries goal / task_count / task_generator / entry_point."""
    _write(tmp_path, "train.py", _decorated_train())

    out = _run(tmp_path, _spec())

    frag = out["interview_spec"]
    assert frag["goal"] == _GOAL
    assert frag["task_count"] == 3
    assert frag["task_generator"]["kind"] == "items_x_seeds"
    assert frag["entry_point"] == {"kind": "register_run", "run_name": "train"}
    # produced_by is the interview verb's own composer, NOT stamped here.
    assert "produced_by" not in frag


def test_second_call_is_idempotent(tmp_path: Path) -> None:
    """Re-running after a decoration is a pure read with the same verdict."""
    _write(tmp_path, "train.py", _plain_train())

    first = _run(tmp_path, _spec())
    after_first = _tree_snapshot(tmp_path)
    second = _run(tmp_path, _spec())

    assert first["decorated"] is True
    assert second["decorated"] is False
    assert second["already_decorated"] is True
    assert second["onboarded"] is True
    assert _tree_snapshot(tmp_path) == after_first


# ── branch 2: needs_pick ─────────────────────────────────────────────────


def test_two_entry_candidates_need_a_pick(tmp_path: Path) -> None:
    """main.py + train.py, neither decorated → refuse to guess, list both."""
    _write(tmp_path, "main.py", _plain_train())
    _write(tmp_path, "train.py", _plain_train())
    before = _tree_snapshot(tmp_path)

    out = _run(tmp_path, _spec())

    assert out["needs_pick"] is True
    assert out["reason"] == "entry_point_tie"
    assert {c["path"] for c in out["candidates"]} == {"main.py", "train.py"}
    assert out["resolve_with"] == "entry_point_path"
    # The ask NAMES both candidates and why code will not pick.
    assert "main.py" in out["ask"] and "train.py" in out["ask"]
    assert "entry_point_path" in out["ask"]
    # Nothing was written.
    assert _tree_snapshot(tmp_path) == before


def test_caller_entry_point_path_breaks_the_tie(tmp_path: Path) -> None:
    _write(tmp_path, "main.py", _plain_train())
    _write(tmp_path, "train.py", _plain_train())

    out = _run(tmp_path, _spec(entry_point_path="train.py"))

    assert out["onboarded"] is True
    assert out["entry_point_path"] == "train.py"
    assert out["entry_point_rule"].startswith("caller_entry_point_path")
    assert "@register_run" in (tmp_path / "train.py").read_text(encoding="utf-8")
    assert "@register_run" not in (tmp_path / "main.py").read_text(encoding="utf-8")


def test_two_public_defs_need_a_function_pick(tmp_path: Path) -> None:
    """One file, two undecorated public defs, no `main` → entry_function_tie."""
    _write(
        tmp_path,
        "train.py",
        "def alpha(seed: int) -> dict:\n    return {}\n\n\n"
        "def beta(seed: int) -> dict:\n    return {}\n",
    )
    before = _tree_snapshot(tmp_path)

    out = _run(tmp_path, _spec())

    assert out["needs_pick"] is True
    assert out["reason"] == "entry_function_tie"
    assert {c["function"] for c in out["candidates"]} == {"alpha", "beta"}
    assert out["entry_point_path"] == "train.py"
    assert out["resolve_with"] == "run_name"
    assert _tree_snapshot(tmp_path) == before


def test_conventional_main_breaks_a_function_tie(tmp_path: Path) -> None:
    """`main` is substrate convention, so code may apply it without asking."""
    _write(
        tmp_path,
        "train.py",
        "def helper(seed: int) -> dict:\n    return {}\n\n\n"
        "def main(seed: int) -> dict:\n    return {}\n",
    )

    out = _run(tmp_path, _spec())

    assert out["onboarded"] is True
    assert out["run_name"] == "main"
    assert out["entry_point_rule"] == "sole_candidate+conventional_main"


def test_caller_run_name_that_is_not_a_module_level_def_refuses(tmp_path: Path) -> None:
    _write(tmp_path, "train.py", _plain_train())

    with pytest.raises(errors.SpecInvalid) as exc:
        _run(tmp_path, _spec(run_name="nope"))

    assert "nope" in str(exc.value)


# ── branch 3: needs_intent ───────────────────────────────────────────────


def test_missing_task_generator_needs_intent_naming_it(tmp_path: Path) -> None:
    """The sweep recipe is caller-owned: absence escalates, naming the field."""
    _write(tmp_path, "train.py", _decorated_train())
    before = _tree_snapshot(tmp_path)

    out = _run(tmp_path, _spec(task_generator=None))

    assert out["needs_intent"] is True
    assert out["missing_fields"] == ["task_generator"]
    assert out["never_invented"] == ["task_generator"]
    assert "task_generator" in out["ask"]
    # The deterministic context is echoed so the caller need not re-scan.
    assert out["pathway"] == "decorate"
    assert out["entry_point_path"] == "train.py"
    assert out["run_name"] == "train"
    # The partition is absent — it cannot be computed without the axis set.
    assert out["partition"] is None
    assert _tree_snapshot(tmp_path) == before


def test_bare_spec_names_every_human_owned_field(tmp_path: Path) -> None:
    """A bare call escalates with all three intent fields, in a fixed order."""
    _write(tmp_path, "train.py", _decorated_train())

    out = _run(tmp_path, None)

    assert out["needs_intent"] is True
    assert out["missing_fields"] == ["goal", "task_generator", "task_count"]


def test_code_never_fabricates_goal_or_task_generator(tmp_path: Path) -> None:
    """REFUSAL PIN: no branch invents a REQUIRED_CALLER_FIELDS value.

    The escalation may echo detected context (paths, argv_kind, params) but
    must never carry a goal string or a task_generator recipe the caller did
    not supply — the incident-1b class ("safe defaults" justified inventing
    a sweep) is what this pin exists to keep dead.
    """
    _write(tmp_path, "train.py", _decorated_train())
    before = _tree_snapshot(tmp_path)

    out = _run(tmp_path, None)

    assert out["needs_intent"] is True
    assert "goal" in out["never_invented"]
    assert "task_generator" in out["never_invented"]
    # No onboarded payload, so no composed interview_spec can smuggle a value.
    assert "interview_spec" not in out
    assert "onboarded" not in out
    # And no key anywhere in the escalation holds a recipe-shaped value.
    for key, value in out.items():
        assert key not in ("goal", "task_generator", "task_count")
        assert not (isinstance(value, dict) and "kind" in value and "params" in value)
    assert _tree_snapshot(tmp_path) == before


def test_uncovered_required_param_escalates_by_name(tmp_path: Path) -> None:
    """#195: a required non-axis param with no default is a named escalation."""
    _write(tmp_path, "train.py", _decorated_train("seed: int, samples: int"))
    before = _tree_snapshot(tmp_path)

    out = _run(tmp_path, _spec())

    assert out["needs_intent"] is True
    assert out["missing_fields"] == ["entry_point.fixed_params.samples"]
    assert out["never_invented"] == ["entry_point.fixed_params.samples"]
    assert "samples" in out["ask"]
    assert out["partition"]["uncovered_params"] == ["samples"]
    assert out["partition"]["axis_params"] == ["seed"]
    assert _tree_snapshot(tmp_path) == before


def test_caller_fixed_params_clears_the_uncovered_escalation(tmp_path: Path) -> None:
    _write(tmp_path, "train.py", _decorated_train("seed: int, samples: int"))

    out = _run(tmp_path, _spec(fixed_params={"samples": 1000}))

    assert out["onboarded"] is True
    assert out["fixed_params"] == {"samples": 1000}
    assert out["partition"]["uncovered_params"] == []
    assert out["interview_spec"]["entry_point"]["fixed_params"] == {"samples": 1000}


def test_signature_default_covers_a_param(tmp_path: Path) -> None:
    _write(tmp_path, "train.py", _decorated_train("seed: int, samples: int = 1000"))

    out = _run(tmp_path, _spec())

    assert out["onboarded"] is True
    assert out["partition"]["defaulted_params"] == ["samples"]
    assert out["partition"]["uncovered_params"] == []
    # A defaulted param is NOT pinned into fixed_params by code.
    assert out["fixed_params"] == {}


def test_var_keyword_absorbs_everything(tmp_path: Path) -> None:
    _write(tmp_path, "train.py", _decorated_train("seed: int, samples: int, **kwargs"))

    out = _run(tmp_path, _spec())

    assert out["onboarded"] is True
    assert out["partition"]["accepts_var_keyword"] is True
    assert out["partition"]["uncovered_params"] == []


# ── branch 4: needs_wrapper_argv ─────────────────────────────────────────


def test_shell_entry_point_needs_wrapper_argv(tmp_path: Path) -> None:
    """A non-Python entry point carries the argv_kind in the escalation."""
    _write(tmp_path, "run.sh", "#!/bin/sh\necho hi\n")
    before = _tree_snapshot(tmp_path)

    out = _run(tmp_path, _spec())

    assert out["needs_wrapper_argv"] is True
    assert out["argv_kind"] == "shell"
    assert out["pathway_rule"] == "non_python_entry_point"
    assert out["entry_point_path"] == "run.sh"
    assert out["run_name"] == "run"
    assert out["argv_head"] == ["./run.sh"]
    assert out["missing_fields"] == ["argv", "signature"]
    assert out["missing_intent_fields"] == []
    assert "run.sh" in out["ask"]
    assert _tree_snapshot(tmp_path) == before


def test_hydra_entry_point_routes_to_the_wrapper(tmp_path: Path) -> None:
    """SKILL row 4: @hydra.main rewrites the signature → wrapper fallback."""
    _write(
        tmp_path,
        "main.py",
        "import hydra\n\n\n@hydra.main(config_path='conf')\ndef main(cfg) -> None:\n    pass\n",
    )

    out = _run(tmp_path, _spec())

    assert out["needs_wrapper_argv"] is True
    assert out["argv_kind"] == "hydra"
    assert out["pathway_rule"] == "signature_rewriting_decorator"
    assert out["argv_head"] == ["python3", "main.py"]
    assert "hydra" in out["ask"]
    # decorate-entry-point was never called, so nothing was spliced.
    assert "@register_run" not in (tmp_path / "main.py").read_text(encoding="utf-8")


def test_argparse_main_routes_to_the_wrapper(tmp_path: Path) -> None:
    """SKILL row 2: a main() that parses sys.argv is not decoratable."""
    _write(
        tmp_path,
        "main.py",
        "import argparse\n\n\ndef main() -> None:\n"
        "    p = argparse.ArgumentParser()\n"
        "    p.add_argument('--seed')\n"
        "    args = p.parse_args()\n"
        "    print(args)\n",
    )

    out = _run(tmp_path, _spec())

    assert out["needs_wrapper_argv"] is True
    assert out["pathway_rule"] == "body_parses_argv"
    assert out["argv_kind"] == "argparse"


def test_wrapper_argv_discloses_the_intent_gap_too(tmp_path: Path) -> None:
    """One escalation gathers both asks instead of two sequential round trips."""
    _write(tmp_path, "run.sh", "#!/bin/sh\n")

    out = _run(tmp_path, _spec(goal=None, task_generator=None))

    assert out["needs_wrapper_argv"] is True
    assert out["missing_intent_fields"] == ["goal", "task_generator"]
    assert "goal" in out["ask"]


def test_wrapper_pathway_onboards_with_caller_argv(tmp_path: Path) -> None:
    _write(tmp_path, "run.sh", "#!/bin/sh\n")

    out = _run(
        tmp_path,
        _spec(
            argv=["./run.sh", "--seed", "{seed}"],
            signature={"seed": "int"},
        ),
    )

    assert out["onboarded"] is True
    assert out["pathway"] == "wrapper"
    assert out["entry_point_kind"] == "shell_command"
    entry = out["interview_spec"]["entry_point"]
    assert entry["kind"] == "shell_command"
    assert entry["run_name"] == "run"
    assert entry["argv"] == ["./run.sh", "--seed", "{seed}"]
    assert entry["signature"] == {"seed": "int"}
    assert entry["frozen_configs"] == []


def test_package_main_argv_head_is_a_dash_m_invocation(tmp_path: Path) -> None:
    _write(tmp_path, "src/mypkg/__init__.py", "")
    _write(
        tmp_path,
        "src/mypkg/__main__.py",
        "import click\n\n\n@click.command()\ndef cli():\n    pass\n",
    )

    out = _run(tmp_path, _spec())

    assert out["needs_wrapper_argv"] is True
    assert out["argv_head"] == ["python3", "-m", "mypkg"]
    # A Python entry point resolved a FUNCTION, so the wrapper is named after
    # it; only a path with no resolvable function falls back to the stem
    # derivation (`run.sh` -> `run`, tested above).
    assert out["run_name"] == "cli"


def test_caller_forced_shell_command_wins_over_a_decoratable_function(tmp_path: Path) -> None:
    """SKILL row 6: an explicit caller kind always routes to the wrapper."""
    _write(tmp_path, "train.py", _plain_train())

    out = _run(tmp_path, _spec(entry_point_kind="shell_command"))

    assert out["needs_wrapper_argv"] is True
    assert out["pathway_rule"] == "caller_forced_shell_command"
    assert out["argv_head"] == ["python3", "train.py"]
    assert "@register_run" not in (tmp_path / "train.py").read_text(encoding="utf-8")


# ── the promoted frozen-YAML convention scan (SKILL.md:147-159) ───────────


def test_frozen_config_scan_follows_the_convention_globs(tmp_path: Path) -> None:
    _write(tmp_path, "train.py", _decorated_train())
    _write(tmp_path, "configs/exp_42.yaml", "a: 1\n")
    _write(tmp_path, "configs/exp_43.yml", "a: 2\n")
    _write(tmp_path, "conf/base.yaml", "a: 3\n")
    # NOT probed by the prose's globs (and not silently widened here).
    _write(tmp_path, "conf/extra.yml", "a: 4\n")
    _write(tmp_path, "other/nope.yaml", "a: 5\n")

    out = _run(tmp_path, _spec())

    assert out["frozen_configs"] == ["configs/exp_42.yaml", "configs/exp_43.yml", "conf/base.yaml"]
    assert out["frozen_sha_params"] == ["exp_42_sha", "exp_43_sha", "base_sha"]


def test_frozen_sha_params_count_as_covered(tmp_path: Path) -> None:
    """The framework threads <stem>_sha, so such a param is never 'uncovered'."""
    _write(tmp_path, "train.py", _decorated_train("seed: int, exp_42_sha: str"))
    _write(tmp_path, "configs/exp_42.yaml", "a: 1\n")

    out = _run(tmp_path, _spec())

    assert out["onboarded"] is True
    assert out["partition"]["axis_params"] == ["seed", "exp_42_sha"]
    assert out["partition"]["uncovered_params"] == []


def test_caller_frozen_configs_override_including_the_empty_list(tmp_path: Path) -> None:
    _write(tmp_path, "train.py", _decorated_train())
    _write(tmp_path, "configs/exp_42.yaml", "a: 1\n")

    out = _run(tmp_path, _spec(frozen_configs=[]))

    assert out["frozen_configs"] == []
    assert out["frozen_sha_params"] == []


# ── the promoted pathway decision table (SKILL.md:93-104) ────────────────


@pytest.mark.parametrize(
    ("source", "argv_kind", "expected_pathway", "expected_rule"),
    [
        # Row 1 — params are already real kwargs.
        ("def main(seed: int) -> None:\n    pass\n", "__main__", "decorate", "kwargs_signature"),
        # Row 2 — the body parses sys.argv.
        (
            "import sys\n\n\ndef main() -> None:\n    print(sys.argv)\n",
            "argparse",
            "wrapper",
            "body_parses_argv",
        ),
        # Row 5 — a consuming click decorator.
        (
            "import click\n\n\n@click.command()\ndef main() -> None:\n    pass\n",
            "click",
            "wrapper",
            "signature_rewriting_decorator",
        ),
        # No decoratable function at all (all top-level code).
        ("print('hello')\n", "__main__", "wrapper", "no_decoratable_function"),
    ],
)
def test_pathway_table_rows(
    tmp_path: Path, source: str, argv_kind: str, expected_pathway: str, expected_rule: str
) -> None:
    """Each promoted table row decides the pathway it says it decides."""
    tree = ast.parse(source)
    resolved = wep._resolve_entry_function(tree, path="main.py", caller_run_name=None)
    assert not isinstance(resolved, dict)
    node, _rule, already = resolved
    entry = wep._EntryPoint(
        path="main.py",
        argv_kind=argv_kind,
        rule="test",
        function=node.name if node is not None else None,
        func_node=node,
        already_decorated=already,
    )

    pathway, rule = wep._decide_pathway(entry=entry, caller_entry_point_kind=None)

    assert (pathway, rule) == (expected_pathway, expected_rule)


def test_non_python_row_routes_to_the_wrapper() -> None:
    """Row 3 — a shell script / console script has no Python surface."""
    for argv_kind, path in (("shell", "run.sh"), ("console_script", "mytool")):
        entry = wep._EntryPoint(
            path=path,
            argv_kind=argv_kind,
            rule="test",
            function=None,
            func_node=None,
            already_decorated=False,
        )
        assert wep._decide_pathway(entry=entry, caller_entry_point_kind=None) == (
            "wrapper",
            "non_python_entry_point",
        )


# ── the promoted fixed-params partition (SKILL.md:180-193) ───────────────


def test_partition_classes_are_disjoint_and_never_invent() -> None:
    part = wep._partition_params(
        declared=["seed", "samples", "lr", "cfg_sha", "extra"],
        defaulted={"lr"},
        accepts_var_keyword=False,
        axis_params=["seed"],
        frozen_sha_params=["cfg_sha"],
        fixed_params={"extra": 7},
    )

    assert part.axis_params == ("seed", "cfg_sha")
    assert part.defaulted_params == ("lr",)
    # `extra` is covered by the caller's fixed_params; `samples` is not.
    assert part.uncovered_params == ("samples",)
    # No class holds a VALUE — the partition only ever names params.
    assert set(part.all_params) == {"seed", "samples", "lr", "cfg_sha", "extra"}


@pytest.mark.parametrize(
    ("generator", "expected"),
    [
        ({"kind": "items_x_seeds", "params": {"seeds": [0, 1]}}, ["seed"]),
        (
            {"kind": "items_x_seeds", "params": {"items": [{"config": "a.yaml"}], "seeds": [0]}},
            ["config", "seed"],
        ),
        (
            {"kind": "cartesian_product", "params": {"axes": {"seed": [0], "shard": [1]}}},
            ["seed", "shard"],
        ),
        ({"kind": "enumerated", "params": {"items": [{"a": 1}, {"b": 2}]}}, ["a", "b"]),
        (
            {
                "kind": "numeric_linspace",
                "params": {"param": "lr", "low": 0.1, "high": 1.0, "n": 3},
            },
            ["lr"],
        ),
        (
            {"kind": "chunked_series", "params": {"series_length": 100, "chunks": 2, "halo": 1}},
            ["chunk_start", "chunk_end", "halo"],
        ),
    ],
)
def test_axis_params_per_recipe_shape(generator: dict[str, Any], expected: list[str]) -> None:
    spec = _spec(task_generator=generator)
    assert spec.task_generator is not None
    assert wep._axis_params(spec.task_generator) == expected


# ── structural refusals ──────────────────────────────────────────────────


def test_greenfield_refuses_and_names_build_template(tmp_path: Path) -> None:
    """Nothing to onboard: the verb never authors an entry point itself."""
    with pytest.raises(errors.SpecInvalid) as exc:
        _run(tmp_path, _spec())

    assert "greenfield_repo" in str(exc.value)
    assert "build-template" in (exc.value.remediation or "")


def test_unparseable_entry_point_refuses_loudly(tmp_path: Path) -> None:
    _write(tmp_path, "train.py", "def main(:\n")

    with pytest.raises(errors.SpecInvalid) as exc:
        _run(tmp_path, _spec())

    assert "does not parse" in str(exc.value)


def test_two_decorated_files_need_a_pick(tmp_path: Path) -> None:
    """Several already-registered runs: which one is THIS experiment?"""
    _write(tmp_path, "train.py", _decorated_train())
    _write(tmp_path, "main.py", _decorated_train())

    out = _run(tmp_path, _spec())

    assert out["needs_pick"] is True
    assert out["reason"] == "entry_point_tie"
    assert {c["path"] for c in out["candidates"]} == {"main.py", "train.py"}
