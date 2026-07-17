"""Behavior-pinning coverage battery for ``hpc_agent.infra.executor_guard``.

Audit unit 2b — *guard-can-fire is the whole point*. Every rejection branch in
the module is proven to actually trip on a concrete malformed executor, its
exact refusal (error type / ``error_code`` / message contract) is pinned, and
the minimal accept-side boundary that must pass is pinned right next to it.
Each test's docstring names the mutant it kills; assertions are exact values,
both-side boundaries, and polarity — no "assert not None" smoke.

The module's own doctrine (its docstrings) is CONSERVATIVE-BY-CONSTRUCTION:
a guard refuses only on a *provable* miss and no-ops on anything unknowable.
These tests pin BOTH halves of that contract — the refusal fires on the doomed
form AND the guard stays silent on every legitimate form next to it, so a
mutant that widens a guard into a false-positive machine is caught too.
"""

from __future__ import annotations

import warnings

import pytest

from hpc_agent import errors
from hpc_agent.infra import executor_guard as eg

# ─────────────────────────────────────────────────────────────────────────────
# Shared refusal-contract pin. Every guard raises exactly errors.SpecInvalid
# with this envelope; a mutant that swaps the exception class or downgrades the
# category/retry_safe is caught by asserting the contract on a representative.
# ─────────────────────────────────────────────────────────────────────────────


def test_spec_invalid_contract_is_the_refusal_envelope() -> None:
    """Mutant: raising a different exception type, or flipping SpecInvalid's
    error_code / retry_safe / category. Pins the exact refusal envelope every
    guard below relies on (spec_invalid / non-retryable / user-category)."""
    with pytest.raises(errors.SpecInvalid) as ei:
        eg._check_bare_module_executor("pkg.mod:fn")
    exc = ei.value
    assert exc.error_code == "spec_invalid"
    assert exc.retry_safe is False
    assert exc.category == "user"


# ─────────────────────────────────────────────────────────────────────────────
# _check_register_run_executor — the bare-script-of-a-@register_run-file guard.
# Rejection requires ALL of: >=2 parts, python[3] interp, .py script, the file
# exists (resolved vs base_dir), readable, source has BOTH "register_run" and
# "hpc_agent". Each conjunct is a short-circuit accept; pin the fire + each miss.
# ─────────────────────────────────────────────────────────────────────────────


def _write_register_run_file(tmp_path, name="entry.py", *, both=True):
    body = (
        "from hpc_agent import register_run\n\n@register_run\ndef go():\n    ...\n"
        if both
        else "def go():\n    ...\n"  # neither marker
    )
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_register_run_fires_on_bare_script_with_trailing_args(tmp_path) -> None:
    """Mutant: restoring the pre-0.10.11 strict ``len(parts) == 2`` check (the
    trailing-args form must STILL fire — it is the smoking gun, not the safe
    path). ``python entry.py --samples 100000 --seed $SEED`` against a
    register_run file must be refused."""
    _write_register_run_file(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="register_run-decorated file"):
        eg._check_register_run_executor(
            "python entry.py --samples 100000 --seed $SEED", base_dir=tmp_path
        )


def test_register_run_fires_on_exact_two_token_form(tmp_path) -> None:
    """Mutant: inverting the ``_BARE_SCRIPT_RE.match(interp)`` polarity. The
    canonical two-token ``python3 entry.py`` bare form fires."""
    _write_register_run_file(tmp_path)
    with pytest.raises(errors.SpecInvalid, match="bare-script form"):
        eg._check_register_run_executor("python3 entry.py", base_dir=tmp_path)


def test_register_run_accepts_flag_before_script(tmp_path) -> None:
    """Mutant: dropping the ``interp`` bare-name gate so ``python -O entry.py``
    (a flag before the script) is wrongly refused. A flag before the script
    makes ``interp='python'``... wait — parts[1] is ``-O``, not a ``.py``; the
    ``script.endswith('.py')`` conjunct short-circuits. Accept side."""
    _write_register_run_file(tmp_path)
    eg._check_register_run_executor("python -O entry.py", base_dir=tmp_path)  # no raise


def test_register_run_accepts_dash_c_oneliner(tmp_path) -> None:
    """Mutant: widening the interp regex to swallow ``python3 -c``. The
    canonical ``python3 -c "..."`` one-liner is exactly the form to allow
    through — parts[1] is ``-c`` (not ``.py``), short-circuit accept."""
    _write_register_run_file(tmp_path)
    eg._check_register_run_executor(
        "python3 -c \"import runpy; runpy.run_path('entry.py')\"", base_dir=tmp_path
    )  # no raise


def test_register_run_accepts_non_py_script(tmp_path) -> None:
    """Mutant: relaxing the ``script.endswith('.py')`` conjunct. ``python
    entry.sh`` is not a .py script — accept."""
    _write_register_run_file(tmp_path, name="entry.py")
    eg._check_register_run_executor("python entry.sh", base_dir=tmp_path)  # no raise


def test_register_run_accepts_when_file_absent(tmp_path) -> None:
    """Mutant: dropping the ``local_path.is_file()`` guard (would then read a
    nonexistent file / raise). A .py that does not exist under base_dir is a
    different failure mode — this guard no-ops."""
    eg._check_register_run_executor("python ghost.py", base_dir=tmp_path)  # no raise


def test_register_run_base_dir_resolves_relative_script(tmp_path) -> None:
    """Mutant: removing the ``base_dir`` resolution (the #292 Bug A regression —
    a CWD-relative probe that returns False from a worker whose CWD isn't the
    experiment dir, silently PASSING the guard). With base_dir given the
    relative script resolves against it and the guard FIRES."""
    sub = tmp_path / "exp"
    sub.mkdir()
    _write_register_run_file(sub, name="run.py")
    # CWD-relative (base_dir=None) can't find exp/run.py from here → no raise.
    eg._check_register_run_executor("python run.py", base_dir=None)  # no raise
    # base_dir resolves it → the guard fires. This is the both-sides boundary.
    with pytest.raises(errors.SpecInvalid, match="register_run-decorated"):
        eg._check_register_run_executor("python run.py", base_dir=sub)


def test_register_run_accepts_file_missing_a_marker(tmp_path) -> None:
    """Mutant: OR-ing instead of AND-ing the two substring probes, or dropping
    one. A .py file that mentions NEITHER "register_run" nor "hpc_agent" must
    not be refused (the substring probe is the final conjunct)."""
    _write_register_run_file(tmp_path, name="plain.py", both=False)
    eg._check_register_run_executor("python plain.py", base_dir=tmp_path)  # no raise


def test_register_run_accepts_single_token(tmp_path) -> None:
    """Mutant: changing ``len(parts) < 2`` to ``<= 2`` or removing it. A single
    token has no script argument — accept (nothing to check)."""
    eg._check_register_run_executor("python3", base_dir=tmp_path)  # no raise


# ─────────────────────────────────────────────────────────────────────────────
# _path_excluded — the exclusion-proof predicate (glob / bare-name-at-depth /
# anchored-path). Only PROVES an exclusion; an unknown shape must NOT match.
# ─────────────────────────────────────────────────────────────────────────────


def test_path_excluded_glob_on_basename() -> None:
    """Mutant: fnmatch against the full path instead of the basename. ``*.pyc``
    excludes any path whose basename matches, and only those."""
    assert eg._path_excluded("a/b/x.pyc", ["*.pyc"]) is True
    assert eg._path_excluded("a/b/x.py", ["*.pyc"]) is False


def test_path_excluded_bare_name_at_any_depth() -> None:
    """Mutant: anchoring a bare name to the root instead of matching any path
    component (rsync bare-name semantics). ``results`` excludes a/results/b."""
    assert eg._path_excluded("a/results/b.txt", ["results"]) is True
    assert eg._path_excluded("a/results/b.txt", ["results/"]) is True  # trailing / stripped
    assert eg._path_excluded("a/notresults/b.txt", ["results"]) is False  # component-equal only


def test_path_excluded_anchored_relative_path() -> None:
    """Mutant: breaking the exact-match-or-prefix-dir arm for anchored paths.
    ``a/b`` matches itself and anything under ``a/b/``, nothing else."""
    assert eg._path_excluded("a/b", ["a/b"]) is True
    assert eg._path_excluded("a/b/c.py", ["a/b"]) is True
    assert eg._path_excluded("a/bc.py", ["a/b"]) is False  # not a real prefix dir


def test_path_excluded_empty_and_unknown_patterns_never_match() -> None:
    """Mutant: treating an empty/whitespace pattern as a match-all. An empty
    pattern, a slash-only pattern, and a non-matching one all yield False —
    the guard never refuses on a pattern it can't reason about."""
    assert eg._path_excluded("a/b.py", ["", "   ", "/"]) is False
    assert eg._path_excluded("a/b.py", ["other/"]) is False


# ─────────────────────────────────────────────────────────────────────────────
# _check_executor_in_deploy_manifest — refuse a locally-present script that an
# effective rsync exclude would strip from the deploy. Fires ONLY on the
# present-here-absent-there case.
# ─────────────────────────────────────────────────────────────────────────────


def test_deploy_manifest_fires_on_excluded_present_script(tmp_path) -> None:
    """Mutant: inverting the ``_path_excluded`` polarity, or dropping the whole
    guard. A script present locally under executors/ but stripped by an
    ``executors/`` rsync exclude must be refused."""
    (tmp_path / "executors").mkdir()
    (tmp_path / "executors" / "train.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(errors.SpecInvalid, match="executor_not_in_deploy_manifest"):
        eg._check_executor_in_deploy_manifest(
            "python executors/train.py --seed $SEED",
            experiment_dir=tmp_path,
            rsync_excludes=["executors/"],
        )


def test_deploy_manifest_accepts_present_and_included_script(tmp_path) -> None:
    """Accept boundary (mutant: refusing whenever a script token exists). The
    SAME present script with an exclude set that does NOT strip it passes."""
    (tmp_path / "executors").mkdir()
    (tmp_path / "executors" / "train.py").write_text("x = 1\n", encoding="utf-8")
    eg._check_executor_in_deploy_manifest(
        "python executors/train.py",
        experiment_dir=tmp_path,
        rsync_excludes=["*.log"],
    )  # no raise


def test_deploy_manifest_noops_without_experiment_dir(tmp_path) -> None:
    """Mutant: removing the ``experiment_dir is None`` early return. Unknown
    deploy root → skip (can't prove a miss)."""
    eg._check_executor_in_deploy_manifest(
        "python executors/train.py", experiment_dir=None, rsync_excludes=["executors/"]
    )  # no raise


def test_deploy_manifest_noops_on_absolute_or_no_script(tmp_path) -> None:
    """Mutant: dropping the ``script is None or startswith('/')`` skip. An
    absolute path is inherited (not from the deploy tree), and a ``-c``
    one-liner has no script token — both skip."""
    eg._check_executor_in_deploy_manifest(
        "python /abs/train.py", experiment_dir=tmp_path, rsync_excludes=["train.py"]
    )  # no raise — absolute
    eg._check_executor_in_deploy_manifest(
        'python3 -c "print(1)"', experiment_dir=tmp_path, rsync_excludes=["*"]
    )  # no raise — no script token


def test_deploy_manifest_noops_when_script_absent_locally(tmp_path) -> None:
    """Mutant: firing on a locally-ABSENT file (that is the local-presence
    guard's job, not this one). Excluded pattern but no such file → skip."""
    eg._check_executor_in_deploy_manifest(
        "python executors/ghost.py", experiment_dir=tmp_path, rsync_excludes=["executors/"]
    )  # no raise


def test_deploy_manifest_generated_shippable_carveout_is_not_stripped(tmp_path) -> None:
    """Mutant: dropping the ``_GENERATED_SHIPPABLE`` carve-out subtraction. A
    script under ``src/`` is re-included by submit-flow even if ``src`` appears
    as an exclude, so the guard must NOT refuse it."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "train.py").write_text("x = 1\n", encoding="utf-8")
    eg._check_executor_in_deploy_manifest(
        "python src/train.py", experiment_dir=tmp_path, rsync_excludes=["src"]
    )  # no raise — src is generated-shippable


# ─────────────────────────────────────────────────────────────────────────────
# _check_bare_module_executor — refuse a lone ``module:function`` token.
# ─────────────────────────────────────────────────────────────────────────────


def test_bare_module_fires_on_lone_module_function() -> None:
    """Mutant: inverting ``_BARE_MODULE_RE.match`` polarity or the ``len==1``
    gate. ``hpc_wrappers.ridge_imp:ridge_imp`` is the ridge_imp exit-127 form."""
    with pytest.raises(errors.SpecInvalid, match="module:function form"):
        eg._check_bare_module_executor("hpc_wrappers.ridge_imp:ridge_imp")


def test_bare_module_accepts_run_module_dispatch() -> None:
    """Accept boundary (mutant: firing on >1 token). The correct
    ``python3 -m hpc_agent.executor_cli run-module mod:fn`` dispatch is many
    tokens — must pass."""
    eg._check_bare_module_executor(
        "python3 -m hpc_agent.executor_cli run-module pkg.mod:fn"
    )  # no raise


def test_bare_module_accepts_slash_bearing_single_tokens() -> None:
    """Mutant: loosening ``_BARE_MODULE_RE`` to match a slash. A URL and a
    relative path both carry a ``/`` on the function side that the class
    excludes — single tokens that must NOT be mistaken for module:function.

    NB: a Windows ``C:\\x\\y`` is deliberately NOT used here — the guard's
    ``shlex.split(posix=True)`` strips the backslashes to ``C:xy``, which the
    regex DOES match (it fires). See the module docstring's contrary claim,
    reported as a doc/behavior discrepancy — not exercised as an accept case."""
    eg._check_bare_module_executor("http://h/p")  # slash on fn side → no match, no raise
    eg._check_bare_module_executor("pkg/mod:fn")  # leading path component → no match


# ─────────────────────────────────────────────────────────────────────────────
# _check_executor_is_dispatcher — the INVERSE guard: refuse a per-task-shaped
# value placed in job_env['EXECUTOR']. Two doomed shapes: a comma, or ``-c``.
# ─────────────────────────────────────────────────────────────────────────────


def test_is_dispatcher_fires_on_comma() -> None:
    """Mutant: dropping the comma branch. A comma truncates the
    ``qsub -v ...,EXECUTOR=...`` value — refuse, naming the comma."""
    with pytest.raises(errors.SpecInvalid, match="it contains a comma"):
        eg._check_executor_is_dispatcher("python3 -c import argparse, sys")


def test_is_dispatcher_fires_on_dash_c_oneliner() -> None:
    """Mutant: dropping the ``-c`` branch. A ``python -c`` inline one-liner
    can't survive the unquoted ``$EXECUTOR`` word-split — refuse."""
    with pytest.raises(errors.SpecInvalid, match="inline one-liner"):
        eg._check_executor_is_dispatcher('python3 -c "print(1)"')


def test_is_dispatcher_accepts_dispatcher_and_comma_free_command() -> None:
    """Accept boundary (mutant: refusing every EXECUTOR). The dispatcher
    default and a comma-free, ``-c``-free per-task command both pass."""
    eg._check_executor_is_dispatcher("python3 .hpc/_hpc_dispatch.py")  # no raise
    eg._check_executor_is_dispatcher("python3 analyze.py --seed $SEED")  # no raise
    eg._check_executor_is_dispatcher("python .hpc/_hpc_dispatch.py")  # no raise


# ─────────────────────────────────────────────────────────────────────────────
# _check_executor_format_placeholders — refuse str.format {name} tokens.
# ─────────────────────────────────────────────────────────────────────────────


def test_format_placeholders_fires_on_named_token() -> None:
    """Mutant: inverting the ``if not found: return`` polarity into a no-op.
    ``--output results/{run_id}/metrics.json`` carries a {run_id} placeholder
    the dispatcher never expands — refuse, listing the token."""
    with pytest.raises(errors.SpecInvalid, match=r"\{run_id\}"):
        eg._check_executor_format_placeholders(
            "python run.py --output results/{run_id}/metrics.json"
        )


def test_format_placeholders_accepts_shell_and_non_named_braces() -> None:
    """Accept boundary (mutant: matching ``${VAR}`` or ``{}``/``{a,b}``/
    ``{1..9}``). Shell parameter expansion and non-identifier braces must NOT
    be mistaken for a format placeholder."""
    eg._check_executor_format_placeholders('python run.py --out "$RESULT_DIR/m.json"')
    eg._check_executor_format_placeholders("find . -exec rm {} +")  # empty braces
    eg._check_executor_format_placeholders("echo {a,b,c}")  # comma list
    eg._check_executor_format_placeholders("echo {1..9}")  # numeric range
    eg._check_executor_format_placeholders("")  # empty string


# ─────────────────────────────────────────────────────────────────────────────
# _is_bare_script_name / _check_bare_script_executor — refuse a lone
# ``train.py`` (no interpreter, no path separator) → exit 127.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bare", ["train.py", "run.sh", "analyze.R", "sim.jl", "  train.py  "])
def test_is_bare_script_name_true_for_bare_tokens(bare: str) -> None:
    """Mutant: flipping the final ``endswith`` to ``return False`` (no-op), or
    dropping case-insensitivity (``.R``/``.jl``). Each bare script token is
    True; the extension check is case-insensitive and whitespace-trimmed."""
    assert eg._is_bare_script_name(bare) is True


@pytest.mark.parametrize(
    "runnable",
    [
        "python train.py",  # interpreter prefix (whitespace)
        "./train.py",  # path separator
        "a/b/train.py",  # path separator
        "mybinary",  # non-script extension
        "",  # empty
        None,  # None
    ],
)
def test_is_bare_script_name_false_for_runnable(runnable) -> None:
    """Mutant: dropping the whitespace/path-separator short-circuits, or the
    empty/None guard. An interpreter prefix, any path separator, a non-script
    token, empty, and None are all NOT bare-script."""
    assert eg._is_bare_script_name(runnable) is False


def test_check_bare_script_executor_fires_and_accepts() -> None:
    """Mutant: decoupling the raise from ``_is_bare_script_name``. The bare
    token refuses (exit-127 message); the interpreter-prefixed form passes —
    both sides of the boundary."""
    with pytest.raises(errors.SpecInvalid, match="bare script name"):
        eg._check_bare_script_executor("train.py")
    eg._check_bare_script_executor("python train.py")  # no raise


# ─────────────────────────────────────────────────────────────────────────────
# _warn_task_interface_blind_executor — WARN (never refuse) on a single bare
# token with no args and no $ reference. Polarity matters: it must not raise.
# ─────────────────────────────────────────────────────────────────────────────


def test_interface_blind_warns_never_raises() -> None:
    """Mutant: upgrading the warn to a raise (the decision is warn-not-refuse —
    the refusal is unwinnable without the cluster's $PATH). A bare
    ``monte_carlo_pi`` emits exactly one RuntimeWarning and does NOT raise."""
    with pytest.warns(RuntimeWarning, match="TASK-INTERFACE-BLIND"):
        eg._warn_task_interface_blind_executor("monte_carlo_pi")


def test_interface_blind_silent_when_args_or_dollar_present() -> None:
    """Mutant: dropping either early return (args present, or a $ reference).
    A command with arguments, and a bare token that references $HPC_TASK_ID,
    both engage the task interface — no warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # any warning would raise → assert silence
        eg._warn_task_interface_blind_executor("python train.py --out x")
        eg._warn_task_interface_blind_executor("$HPC_TASK_ID")
        eg._warn_task_interface_blind_executor("")  # empty → silent


# ─────────────────────────────────────────────────────────────────────────────
# _check_executor_kwarg_casing — refuse a swept-kwarg $ref in the wrong case.
# ─────────────────────────────────────────────────────────────────────────────


def test_kwarg_casing_fires_on_lowercase_ref_to_real_kwarg() -> None:
    """Mutant: dropping the ``ref != exported`` inequality (the 2026-06-06 demo
    where a correct ``$SEED`` was "fixed" into a broken ``$seed``). ``$seed``
    for the ``seed`` kwarg expands to EMPTY — refuse."""
    with pytest.raises(errors.SpecInvalid, match="expands to EMPTY"):
        eg._check_executor_kwarg_casing("python run.py --seed $seed", kwargs_keys={"seed"})


def test_kwarg_casing_accepts_correct_case_and_hpc_kw() -> None:
    """Accept boundary (mutant: firing on the correctly-cased ref). ``$SEED``
    and ``$HPC_KW_SEED`` are the exported spellings — must pass."""
    eg._check_executor_kwarg_casing("python run.py --seed $SEED", kwargs_keys={"seed"})
    eg._check_executor_kwarg_casing("python run.py --seed $HPC_KW_SEED", kwargs_keys={"seed"})


def test_kwarg_casing_noops_when_kwargs_unknown_or_no_dollar() -> None:
    """Mutant: removing the ``kwargs_keys is None`` / ``'$' not in`` guards
    (only refuse on a provable miss). Unknowable kwargs, or no $ at all → skip
    even for a would-be wrong-case ref."""
    eg._check_executor_kwarg_casing("python run.py --seed $seed", kwargs_keys=None)  # no raise
    eg._check_executor_kwarg_casing("python run.py --seed 5", kwargs_keys={"seed"})  # no $


def test_kwarg_casing_defaulted_ref_is_never_flagged() -> None:
    """Mutant: dropping the ``defaulted`` skip. ``${seed:-1}`` never expands to
    empty on an unset var, so even a wrong-case defaulted ref is safe."""
    eg._check_executor_kwarg_casing(
        "python run.py --seed ${seed:-1}", kwargs_keys={"seed"}
    )  # no raise


# ─────────────────────────────────────────────────────────────────────────────
# _check_executor_var_references — refuse a $VAR the dispatcher never exports.
# Covered (never flagged): job_env key, framework var, inherited shell var,
# defaulted, real kwarg.
# ─────────────────────────────────────────────────────────────────────────────


def test_var_references_fires_on_unexported_var() -> None:
    """Mutant: inverting the ``kwarg in kwargs_keys`` membership, or dropping
    the raise. ``$SAMPLES`` with only a ``seed`` kwarg is unset-expands-to-empty
    — refuse."""
    with pytest.raises(errors.SpecInvalid, match="not a framework or inherited cluster"):
        eg._check_executor_var_references(
            "python run.py --samples $SAMPLES",
            job_env_keys=set(),
            kwargs_keys={"seed"},
        )


def test_var_references_accepts_every_covered_form() -> None:
    """Accept boundary (mutant: narrowing any covered set so a legitimate ref
    false-positives). A job_env key, a framework-injected var, an inherited
    shell var (exact + prefix), a defaulted ref, and a real kwarg (bare +
    HPC_KW_) are each covered."""
    kw = {"seed"}
    eg._check_executor_var_references("--e $CONDA_ENV", job_env_keys={"CONDA_ENV"}, kwargs_keys=kw)
    eg._check_executor_var_references("--out $RESULT_DIR", job_env_keys=set(), kwargs_keys=kw)
    eg._check_executor_var_references("--data $SCRATCH", job_env_keys=set(), kwargs_keys=kw)
    eg._check_executor_var_references("--id $SLURM_JOB_ID", job_env_keys=set(), kwargs_keys=kw)
    eg._check_executor_var_references("--s ${MISSING:-x}", job_env_keys=set(), kwargs_keys=kw)
    eg._check_executor_var_references("--seed $SEED", job_env_keys=set(), kwargs_keys=kw)
    eg._check_executor_var_references("--seed $HPC_KW_SEED", job_env_keys=set(), kwargs_keys=kw)


def test_var_references_noops_when_kwargs_none_or_no_dollar() -> None:
    """Mutant: removing the ``kwargs_keys is None or '$' not in executor``
    early return. Unknowable kwargs, or no $, → skip (only refuse on a provable
    miss)."""
    eg._check_executor_var_references(
        "python run.py --samples $SAMPLES", job_env_keys=set(), kwargs_keys=None
    )  # no raise
    eg._check_executor_var_references(
        "python run.py --samples 5", job_env_keys=set(), kwargs_keys={"seed"}
    )  # no raise — no $


# ─────────────────────────────────────────────────────────────────────────────
# Supporting predicates whose polarity gates the guards above.
# ─────────────────────────────────────────────────────────────────────────────


def test_is_inherited_shell_var_polarity() -> None:
    """Mutant: emptying _INHERITED_SHELL_VARS or the prefix tuple. Exact names
    and prefix families are inherited; an arbitrary name is not."""
    assert eg._is_inherited_shell_var("SCRATCH") is True  # exact
    assert eg._is_inherited_shell_var("SLURM_JOB_ID") is True  # SLURM_ prefix
    assert eg._is_inherited_shell_var("OMP_NUM_THREADS") is True  # OMP_ prefix
    assert eg._is_inherited_shell_var("SAMPLES") is False  # arbitrary kwarg-shaped name


def test_iter_var_refs_extracts_names_and_default_flag() -> None:
    """Mutant: breaking the braced-default detection (_DEFAULT_MOD_RE) or the
    bare-vs-braced grouping. ``$FOO`` is (FOO, not-defaulted); ``${BAR:-x}`` is
    (BAR, defaulted); ``${BAZ}`` is (BAZ, not-defaulted)."""
    refs = dict(eg._iter_var_refs("a $FOO b ${BAR:-x} c ${BAZ}"))
    assert refs == {"FOO": False, "BAR": True, "BAZ": False}


def test_resolve_kwargs_keys_noops_without_tasks_py(tmp_path) -> None:
    """Mutant: dropping the None-safety early returns. No experiment_dir, and a
    dir with no .hpc/tasks.py, both yield None (kwarg set unestablished →
    downstream checks skip)."""
    assert eg._resolve_kwargs_keys(None) is None
    assert eg._resolve_kwargs_keys(tmp_path) is None  # no .hpc/tasks.py


# ─────────────────────────────────────────────────────────────────────────────
# check_per_task_executor — the orchestrator. Each refusing sub-guard must be
# reachable THROUGH the public entry point, and every legitimate form passes.
# ─────────────────────────────────────────────────────────────────────────────


def test_orchestrator_routes_format_placeholder() -> None:
    """Mutant: removing the ``_check_executor_format_placeholders`` call from
    the orchestrator. A {run_id} token must be refused via the public entry."""
    with pytest.raises(errors.SpecInvalid, match="placeholder"):
        eg.check_per_task_executor("python run.py --out results/{run_id}/m.json")


def test_orchestrator_routes_bare_module() -> None:
    """Mutant: removing the ``_check_bare_module_executor`` call. A bare
    module:function must be refused via the public entry."""
    with pytest.raises(errors.SpecInvalid, match="module:function"):
        eg.check_per_task_executor("pkg.mod:fn")


def test_orchestrator_routes_bare_script() -> None:
    """Mutant: removing the ``_check_bare_script_executor`` call. A bare
    ``train.py`` must be refused via the public entry."""
    with pytest.raises(errors.SpecInvalid, match="bare script name"):
        eg.check_per_task_executor("train.py")


def test_orchestrator_warns_interface_blind_without_raising() -> None:
    """Mutant: turning the orchestrator's warn into a raise, or dropping the
    call. A bare non-script token warns (RuntimeWarning) and does NOT raise."""
    with pytest.warns(RuntimeWarning, match="TASK-INTERFACE-BLIND"):
        eg.check_per_task_executor("monte_carlo_pi")


def test_orchestrator_routes_kwarg_casing_with_real_tasks(tmp_path) -> None:
    """Mutant: removing the ``$``-gated casing call (the experiment_dir arm).
    With a real .hpc/tasks.py exporting a ``seed`` kwarg, a wrong-case ``$seed``
    reference is refused THROUGH the public entry."""
    hpc = tmp_path / ".hpc"
    hpc.mkdir()
    (hpc / "tasks.py").write_text(
        "def total():\n    return 1\n\ndef resolve(i):\n    return {'seed': 7}\n",
        encoding="utf-8",
    )
    with pytest.raises(errors.SpecInvalid, match="expands to EMPTY"):
        eg.check_per_task_executor("python run.py --seed $seed", experiment_dir=tmp_path)


def test_orchestrator_accepts_canonical_per_task_command() -> None:
    """Accept boundary (mutant: any sub-guard widened into a false positive).
    A correct per-task command — $RESULT_DIR output, correctly-cased kwarg ref,
    no placeholders — passes cleanly with no warning."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # a stray warning would fail the accept
        eg.check_per_task_executor('python analyze.py --seed $SEED --out "$RESULT_DIR/m.json"')
