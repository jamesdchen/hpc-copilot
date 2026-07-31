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
from hpc_agent._wire.actions.interview import InterviewSpec
from hpc_agent._wire.actions.wrap_entry_point_auto import (
    WrapEntryPointAutoInput,
    WrapEntryPointAutoResult,
)
from hpc_agent.incorporation import wrap_entry_point_auto as wep

_GOAL = "measure pi convergence against sample count"

# The detect-entry-point surface carries for an entry point the scan classified
# but could not read parameters off (and recognized no solver library in). Used
# by the pathway-table unit tests, which build ``_EntryPoint`` directly — the
# table decides on the AST, never on the extraction verdict.
_NO_SURFACE: dict[str, Any] = {
    "argv_extraction": "unsupported",
    "argv_params": None,
    "detected_solver": None,
}


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
    # produced_by is REQUIRED by interview.input.json, so the fragment carries
    # the minimal who-CLASS suggestion. The OPERATOR is deliberately absent:
    # the interview's own composer fills it from git config and discloses it.
    assert frag["produced_by"] == {"kind": "human"}


def test_the_fragment_is_submittable_to_the_interview_verb(tmp_path: Path) -> None:
    """The fragment validates against the interview's OWN input model.

    The point of emitting a fragment rather than a sketch: the caller hands it
    on unedited. ``produced_by`` is one of interview.input.json's three
    required fields, so a fragment omitting it was never submittable — this
    pin fails the moment the key is dropped again.
    """
    _write(tmp_path, "train.py", _decorated_train())

    frag = _run(tmp_path, _spec())["interview_spec"]

    assert "produced_by" in set(InterviewSpec.model_json_schema()["required"])
    spec = InterviewSpec.model_validate(frag)
    # Composed one layer in (P1.c), never hand-authored here.
    assert spec.produced_by.kind == "human"
    assert spec.produced_by.operator is None


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
    """The sweep recipe is caller-owned: absence escalates, naming the field.

    Fixture is UNDECORATED on purpose: a stray decoration inside an escalation
    branch would move the snapshot. With an already-decorated fixture the
    splice is a no-op and the pin is vacuous.
    """
    _write(tmp_path, "train.py", _plain_train())
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

    Fixture is UNDECORATED so the byte-identical assertion is live.
    """
    _write(tmp_path, "train.py", _plain_train())
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
    """#195: a required non-axis param with no default is a named escalation.

    Fixture is UNDECORATED so the byte-identical assertion is live — this is
    the LATEST escalation branch (it fires after the partition), so it is the
    one a "decorate before escalating" regression would slip past first.
    """
    _write(tmp_path, "train.py", _plain_train("seed: int, samples: int"))
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


def test_unextractable_argv_parks_for_the_HUMAN_never_the_llm(tmp_path: Path) -> None:
    """THE actor split, pinned at the op (P2.c F4).

    ``needs_wrapper_argv`` routes to the LLM (``block_chain.AGENT_PARKS``) ONLY
    because the CLI parameters were already read mechanically off the AST — the
    ask is transcription. A shell script has NO Python surface to extract from, so
    there is nothing to transcribe and the flag names are the caller's own
    knowledge of their tool. Routing THAT to the LLM is the dangerous direction:
    it would invent flags that no parser accepts, and the failure surfaces only
    after a submit round-trip, once per task.

    So the op must emit the DISTINCT ``needs_wrapper_argv_unsupported`` stage, and
    the actor registry must read ``human`` off it. Without this pin, mutating the
    op's else-branch to emit the agent stage passes the whole suite.
    """
    from hpc_agent.infra import block_chain

    _write(tmp_path, "run.sh", "#!/bin/sh\necho hi\n")

    out = _run(tmp_path, _spec())

    assert out["needs_wrapper_argv"] is True
    # Nothing was extractable — that is the evidence the split reads…
    assert out["argv_extraction"] == "unsupported"
    assert out["argv_params"] is None
    # …so the stage is the HUMAN one, and the registry agrees.
    assert out["stage_reached"] == "needs_wrapper_argv_unsupported"
    assert block_chain.park_actor("wrap-entry-point-auto", out["stage_reached"]) == "human"
    assert out["needs_decision"] is True
    assert out["next_block"] is None


def test_extractable_argv_parks_for_the_AGENT(tmp_path: Path) -> None:
    """The other side of the split: extracted params → the LLM transcribes them.

    The twin of the test above. Both directions are asserted because a split with
    only one side pinned collapses silently the moment the predicate is inverted.
    """
    from hpc_agent.infra import block_chain

    _write(
        tmp_path,
        "main.py",
        "import argparse\n\n\ndef main() -> None:\n"
        "    p = argparse.ArgumentParser()\n"
        "    p.add_argument('--seed', type=int)\n"
        "    args = p.parse_args()\n"
        "    print(args)\n",
    )

    out = _run(tmp_path, _spec())

    assert out["needs_wrapper_argv"] is True
    assert out["argv_extraction"] == "extracted"
    assert out["argv_params"]
    assert out["stage_reached"] == "needs_wrapper_argv"
    assert block_chain.park_actor("wrap-entry-point-auto", out["stage_reached"]) == "agent"
    # The park's brief carries the evidence the ask points at, so the LLM composes
    # from a code-produced list rather than from a source read.
    assert out["brief"]["argv_params"] == out["argv_params"]
    assert out["brief"]["argv_head"] == out["argv_head"]


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


# ── no escalation branch writes (the "decorate is LAST" invariant) ───────


@pytest.mark.parametrize(
    ("scenario", "spec_kwargs"),
    [
        # needs_intent, earliest branch (before the partition).
        ("intent", {"goal": None, "task_generator": None, "task_count": None}),
        ("intent_partial", {"task_generator": None}),
        # needs_intent, LATEST branch (after the partition) — the one a
        # decorate-before-escalating regression reaches last.
        ("uncovered", {}),
        # needs_wrapper_argv, via the explicit caller override.
        ("wrapper", {"entry_point_kind": "shell_command"}),
    ],
)
def test_no_escalation_branch_writes_to_the_repo(
    tmp_path: Path, scenario: str, spec_kwargs: dict[str, Any]
) -> None:
    """Decoration is the LAST step, so every escalation leaves the tree intact.

    The fixture is UNDECORATED and declares an uncovered required param, so a
    stray `decorate_entry_point` call anywhere before the escalation MOVES the
    snapshot — the assertion is live for all four branches, not vacuous.
    """
    _write(tmp_path, "train.py", _plain_train("seed: int, samples: int"))
    before = _tree_snapshot(tmp_path)

    out = _run(tmp_path, _spec(**spec_kwargs))

    assert "onboarded" not in out, scenario
    after = _tree_snapshot(tmp_path)
    assert after == before, scenario
    assert "@register_run" not in after["train.py"], scenario


def test_the_snapshot_pin_can_actually_fire(tmp_path: Path) -> None:
    """Guard-can-fire check for the pin above: a decoration DOES move it.

    Without this, `_tree_snapshot(...) == before` would silently pass on a
    fixture no write could ever perturb (the vacuous-pin class).
    """
    _write(tmp_path, "train.py", _plain_train())
    before = _tree_snapshot(tmp_path)

    # The onboarded path DOES decorate — so the same assertion must fail here.
    _run(tmp_path, _spec())

    assert _tree_snapshot(tmp_path) != before


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
        **_NO_SURFACE,
    )

    pathway, rule = wep._decide_pathway(entry=entry, caller_entry_point_kind=None)

    assert (pathway, rule) == (expected_pathway, expected_rule)


@pytest.mark.parametrize(
    ("forced_kind", "expected_rule"),
    [
        ("shell_command", "caller_forced_shell_command"),
        ("python_module", "caller_forced_python_module"),
    ],
)
def test_override_first_beats_a_detected_row(
    tmp_path: Path, forced_kind: str, expected_rule: str
) -> None:
    """ORDERING PIN: the caller-override rows run BEFORE the detected rows.

    The entry point carries `@hydra.main`, so the DETECTED verdict is
    `signature_rewriting_decorator`. With override-first the caller's kind
    wins and the rule id is the override's. Relocating the override rows below
    the detected ones (the relocation mutation) makes every case here report
    `signature_rewriting_decorator` instead — red.
    """
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/main.py",
        "import hydra\n\n\n@hydra.main(config_path='conf')\n"
        "def main(seed: int) -> None:\n    pass\n",
    )

    out = _run(tmp_path, _spec(entry_point_path="pkg/main.py", entry_point_kind=forced_kind))

    assert out.get("pathway_rule") == expected_rule


def test_override_first_pin_can_fire_on_the_default_kind(tmp_path: Path) -> None:
    """Same ordering pin for `register_run` — which REFUSES on a rewriter.

    Override-first reaches `caller_forced_register_run`, whose guard names the
    contradiction. Under the relocation mutation the detected
    `signature_rewriting_decorator` row would fire first and silently route to
    the wrapper instead, so this raise is itself an ordering pin.
    """
    _write(
        tmp_path,
        "main.py",
        "import hydra\n\n\n@hydra.main(config_path='conf')\n"
        "def main(seed: int) -> None:\n    pass\n",
    )

    with pytest.raises(errors.SpecInvalid) as exc:
        _run(tmp_path, _spec(entry_point_kind="register_run"))

    msg = str(exc.value)
    assert "hydra.main" in msg
    assert "register_run" in msg
    # Both remedies named; the caller decides (an override is not silently
    # rerouted, and never buys the un-introspectable executor SKILL.md:104
    # warns about).
    assert "shell_command" in (exc.value.remediation or "")


def test_forced_register_run_on_a_non_python_entry_point_refuses(tmp_path: Path) -> None:
    _write(tmp_path, "run.sh", "#!/bin/sh\n")

    with pytest.raises(errors.SpecInvalid) as exc:
        _run(tmp_path, _spec(entry_point_kind="register_run"))

    assert "no Python function to decorate" in str(exc.value)


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
            **_NO_SURFACE,
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


# ── the python_module kind (SKILL.md:98's second option for row 2) ────────


def test_python_module_kind_is_representable(tmp_path: Path) -> None:
    """All THREE InterviewSpec entry-point kinds are reachable, not just two."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/train.py", _plain_train())
    before = _tree_snapshot(tmp_path)

    out = _run(tmp_path, _spec(entry_point_path="pkg/train.py", entry_point_kind="python_module"))

    assert out["onboarded"] is True
    assert out["pathway"] == "module"
    assert out["pathway_rule"] == "caller_forced_python_module"
    assert out["entry_point_kind"] == "python_module"
    # The wire shape is {kind, module, function} — no run_name, no argv.
    assert out["interview_spec"]["entry_point"] == {
        "kind": "python_module",
        "module": "pkg.train",
        "function": "train",
    }
    # python_module edits NOTHING — the whole point of the kind.
    assert out["decorated"] is False
    assert _tree_snapshot(tmp_path) == before


def test_python_module_partitions_the_real_signature(tmp_path: Path) -> None:
    """The framework introspects the undecorated function, so the partition runs."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/train.py", _plain_train("seed: int, samples: int = 10"))

    out = _run(tmp_path, _spec(entry_point_path="pkg/train.py", entry_point_kind="python_module"))

    assert out["partition"]["axis_params"] == ["seed"]
    assert out["partition"]["defaulted_params"] == ["samples"]
    assert out["partition"]["uncovered_params"] == []


def test_python_module_uncovered_param_names_a_satisfiable_remedy(tmp_path: Path) -> None:
    """python_module carries no fixed_params, so the ask must not name it."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/train.py", _plain_train("seed: int, samples: int"))

    out = _run(tmp_path, _spec(entry_point_path="pkg/train.py", entry_point_kind="python_module"))

    assert out["needs_intent"] is True
    assert out["partition"]["uncovered_params"] == ["samples"]
    assert "no fixed_params on the wire" in out["ask"]
    assert "default in the function's own signature" in out["ask"]


def test_python_module_refuses_fixed_params_rather_than_dropping_them(tmp_path: Path) -> None:
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/train.py", _plain_train("seed: int, samples: int"))

    with pytest.raises(errors.SpecInvalid) as exc:
        _run(
            tmp_path,
            _spec(
                entry_point_path="pkg/train.py",
                entry_point_kind="python_module",
                fixed_params={"samples": 1000},
            ),
        )

    assert "not representable on a python_module entry point" in str(exc.value)


def test_python_module_refuses_an_unimportable_src_layout(tmp_path: Path) -> None:
    """A src-layout package is not importable from the campaign dir."""
    _write(tmp_path, "src/mypkg/__init__.py", "")
    _write(tmp_path, "src/mypkg/train.py", _plain_train())

    with pytest.raises(errors.SpecInvalid) as exc:
        _run(
            tmp_path,
            _spec(entry_point_path="src/mypkg/train.py", entry_point_kind="python_module"),
        )

    assert "no dotted module name is importable" in str(exc.value)
    assert "__init__.py" in (exc.value.remediation or "")


def test_wrapper_escalation_discloses_the_python_module_alternative(tmp_path: Path) -> None:
    """SKILL.md:98's other option is NAMED, not silently unrepresented."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(
        tmp_path,
        "pkg/main.py",
        "import argparse\n\n\ndef main() -> None:\n    argparse.ArgumentParser().parse_args()\n",
    )

    out = _run(tmp_path, _spec(entry_point_path="pkg/main.py"))

    assert out["needs_wrapper_argv"] is True
    assert out["pathway_rule"] == "body_parses_argv"
    assert out["python_module_alternative"] == {"module": "pkg.main", "function": "main"}
    assert "python_module" in out["ask"]
    assert "pkg.main:main" in out["ask"]


def test_no_python_module_alternative_when_none_is_importable(tmp_path: Path) -> None:
    """Absent (not fabricated) for a shell entry point / src-layout package."""
    _write(tmp_path, "run.sh", "#!/bin/sh\n")

    out = _run(tmp_path, _spec())

    assert out["needs_wrapper_argv"] is True
    assert out["python_module_alternative"] is None
    assert "python_module" not in out["ask"]


# ── the carried argv extraction (no second detect-entry-point call) ──────


_ARGPARSE_MAIN = (
    "import argparse\n\n\n"
    "def main() -> None:\n"
    "    p = argparse.ArgumentParser()\n"
    "    p.add_argument('--seed', type=int, default=0)\n"
    "    p.add_argument('--lr', type=float)\n"
    "    args = p.parse_args()\n"
    "    print(args)\n"
)


def test_needs_wrapper_argv_carries_the_extracted_params(tmp_path: Path) -> None:
    """The composite ran detect in-process — the extraction rides the escalation.

    Dropping it forced the caller to run ``detect-entry-point`` a SECOND time
    to compose the argv template, re-opening the produce→consume seam this
    composite exists to close.
    """
    _write(tmp_path, "main.py", _ARGPARSE_MAIN)

    out = _run(tmp_path, _spec())

    assert out["needs_wrapper_argv"] is True
    assert out["argv_kind"] == "argparse"
    assert out["argv_extraction"] == "extracted"
    params = out["argv_params"]
    assert [p["dest"] for p in params] == ["seed", "lr"]
    assert [p["names"] for p in params] == [["--seed"], ["--lr"]]
    assert params[0]["type"] == "int"
    assert params[0]["default"] == 0
    # The ask points at the carried params rather than at another scan.
    assert "argv_params" in out["ask"]
    assert "seed, lr" in out["ask"]


def test_needs_wrapper_argv_is_honest_when_extraction_is_unsupported(tmp_path: Path) -> None:
    """A framework whose flags are not declared as literals reports it plainly."""
    _write(
        tmp_path,
        "main.py",
        "import hydra\n\n\n@hydra.main(config_path='conf')\ndef main(cfg) -> None:\n    pass\n",
    )

    out = _run(tmp_path, _spec())

    assert out["needs_wrapper_argv"] is True
    assert out["argv_extraction"] == "unsupported"
    assert out["argv_params"] is None
    # No phantom claim about parameters we never read.
    assert "argv_params" not in out["ask"]


def test_a_caller_named_path_the_scan_never_saw_reports_unsupported(tmp_path: Path) -> None:
    """No classified surface → the same honest verdict, never an absent field."""
    _write(tmp_path, "tools/launch.py", _ARGPARSE_MAIN)

    out = _run(tmp_path, _spec(entry_point_path="tools/launch.py"))

    assert out["needs_wrapper_argv"] is True
    assert out["argv_extraction"] == "unsupported"
    assert out["argv_params"] is None


# ── the two wrapper-ONLY interview fields (#260) ─────────────────────────


_HALO_HINT: dict[str, Any] = {"kind": "bounded_halo", "halo": {"expr": "train_window * 48"}}


def _wrapper_spec(**kwargs: Any) -> WrapEntryPointAutoInput:
    """A spec that clears the needs_wrapper_argv escalation."""
    return _spec(argv=["./run.sh", "--seed", "{seed}"], signature={"seed": "int"}, **kwargs)


def test_data_axis_hint_rides_the_wrapper_fragment_verbatim(tmp_path: Path) -> None:
    """The hint is load-bearing HERE: a subprocess body is uninspectable."""
    _write(tmp_path, "run.sh", "#!/bin/sh\n")

    out = _run(tmp_path, _wrapper_spec(data_axis_hint=_HALO_HINT))

    assert out["onboarded"] is True
    entry = out["interview_spec"]["entry_point"]
    assert entry["data_axis_hint"] == _HALO_HINT
    # And the whole fragment still satisfies the interview's own model.
    InterviewSpec.model_validate(out["interview_spec"])


def test_wrapper_fragment_omits_the_hint_when_none_was_given(tmp_path: Path) -> None:
    _write(tmp_path, "run.sh", "#!/bin/sh\n")

    out = _run(tmp_path, _wrapper_spec())

    assert "data_axis_hint" not in out["interview_spec"]["entry_point"]


@pytest.mark.parametrize(
    ("field", "value", "spec_kwargs", "expected_kind"),
    [
        ("data_axis_hint", _HALO_HINT, {}, "register_run"),
        ("data_axis_hint", _HALO_HINT, {"entry_point_kind": "python_module"}, "python_module"),
        ("solver", {"kind": "petsc"}, {}, "register_run"),
        ("solver", {"kind": "petsc"}, {"entry_point_kind": "python_module"}, "python_module"),
    ],
)
def test_wrapper_only_fields_are_refused_on_an_introspectable_pathway(
    tmp_path: Path,
    field: str,
    value: dict[str, Any],
    spec_kwargs: dict[str, Any],
    expected_kind: str,
) -> None:
    """#260: neither field exists on register_run / python_module.

    Both wire shapes are ``extra="forbid"`` and declare neither field, so a
    caller value could only be silently DROPPED on the way to the fragment.
    That is the class this verb already refuses for fixed_params — and the
    refusal names why the field is wrapper-only rather than merely rejecting.
    """
    _write(tmp_path, "train.py", _plain_train())
    before = _tree_snapshot(tmp_path)

    with pytest.raises(errors.SpecInvalid) as exc:
        _run(tmp_path, _spec(**{field: value}, **spec_kwargs))

    assert field in str(exc.value)
    assert expected_kind in str(exc.value)
    remediation = exc.value.remediation or ""
    assert "shell_command" in remediation
    # Refused BEFORE the decoration write — the repo is byte-identical.
    assert _tree_snapshot(tmp_path) == before


def test_the_wrapper_only_refusal_names_why_the_hint_exists(tmp_path: Path) -> None:
    """Contract-taught-by-refusal: the ask explains the introspection asymmetry."""
    _write(tmp_path, "train.py", _plain_train())

    with pytest.raises(errors.SpecInvalid) as exc:
        _run(tmp_path, _spec(data_axis_hint=_HALO_HINT))

    assert "#260" in str(exc.value)
    assert "subprocess" in (exc.value.remediation or "")


# ── the solver hint (detected, or caller-overridden) ─────────────────────


_PETSC_MAIN = (
    "import argparse\n"
    "from petsc4py import PETSc\n\n\n"
    "def main() -> None:\n"
    "    ts = PETSc.TS().create()\n"
    "    ts.setFromOptions()\n"
    "    ts.solve(u)\n"
)


def test_detected_solver_rides_the_wrapper_fragment(tmp_path: Path) -> None:
    """detect-entry-point recognized the library; the fragment carries it.

    Dropping it costs a long solve its preemption-safety silently — the
    wrapper simply never gets the checkpoint hooks.
    """
    _write(tmp_path, "main.py", _PETSC_MAIN)

    out = _run(
        tmp_path,
        _spec(
            entry_point_kind="shell_command",
            argv=["python3", "main.py", "--seed", "{seed}"],
            signature={"seed": "int"},
        ),
    )

    assert out["onboarded"] is True
    assert out["interview_spec"]["entry_point"]["solver"] == {
        "kind": "petsc",
        "solver_object": "ts",
    }
    InterviewSpec.model_validate(out["interview_spec"])


def test_caller_solver_override_wins_over_the_detected_adapter(tmp_path: Path) -> None:
    _write(tmp_path, "main.py", _PETSC_MAIN)

    out = _run(
        tmp_path,
        _spec(
            entry_point_kind="shell_command",
            argv=["python3", "main.py", "--seed", "{seed}"],
            signature={"seed": "int"},
            solver={"kind": "petsc", "solver_object": "snes", "resume_flag": "-restart_file"},
        ),
    )

    assert out["interview_spec"]["entry_point"]["solver"] == {
        "kind": "petsc",
        "solver_object": "snes",
        "resume_flag": "-restart_file",
    }


def test_no_solver_field_when_none_detected_and_none_supplied(tmp_path: Path) -> None:
    _write(tmp_path, "run.sh", "#!/bin/sh\n")

    out = _run(tmp_path, _wrapper_spec())

    assert "solver" not in out["interview_spec"]["entry_point"]


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
