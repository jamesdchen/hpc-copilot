"""Subprocess-invokes ``scripts/lint_subject_imports.py``.

Cases:

1. **Happy path on the real tree** — exits 0. Both ``src/hpc_agent/ops/``
   and ``src/hpc_agent/meta/`` exist post-reorg; the lint actively scans
   their subject subdirectories and the test pins the contract that
   every subject in-tree respects the cross-subject import rule.
2. **Fixture violation** — build a tiny ``ops/<a>/`` + ``ops/<b>/`` tree
   under a temp dir, have a file in ``ops/a/`` import from ``ops.b``,
   and assert non-zero exit with the cross-subject diagnostic.
3. **Fixture: infra import is fine** — same shape, but the offending
   file imports from ``hpc_agent.infra.x`` instead. Must exit 0.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "scripts" / "lint_subject_imports.py"


def _driver(scan_root: Path) -> str:
    return textwrap.dedent(
        f"""\
        import sys
        sys.path.insert(0, {str(REPO / "scripts")!r})
        from pathlib import Path
        from lint_subject_imports import main
        sys.exit(main(scan_root=Path({str(scan_root)!r})))
        """
    )


def test_lint_subject_imports_passes_on_current_tree() -> None:
    """The script must exit 0 on the current tree.

    Both ``ops/`` and ``meta/`` exist post-reorg and carry real subjects;
    this test pins that every in-tree subject respects the cross-subject
    import rule.
    """
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"lint_subject_imports failed on current tree:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )


def test_lint_subject_imports_rejects_cross_subject(tmp_path: Path) -> None:
    """File in ``ops/a/`` importing from ``ops.b`` must trigger a
    non-zero exit naming the cross-subject pair."""
    ops_a = tmp_path / "ops" / "a"
    ops_b = tmp_path / "ops" / "b"
    ops_a.mkdir(parents=True)
    ops_b.mkdir(parents=True)
    (ops_a / "__init__.py").write_text("", encoding="utf-8")
    (ops_b / "__init__.py").write_text("", encoding="utf-8")
    (ops_b / "things.py").write_text("VALUE = 1\n", encoding="utf-8")
    (ops_a / "uses_b.py").write_text(
        "from hpc_agent.ops.b.things import VALUE\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-c", _driver(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0, (
        f"lint_subject_imports unexpectedly passed on a dirty fixture:\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "cross-subject import: ops/a imports ops/b" in proc.stdout, (
        f"expected cross-subject diagnostic missing:\nstdout={proc.stdout}"
    )


def test_lint_subject_imports_allows_infra(tmp_path: Path) -> None:
    """Cross-cutting imports through ``hpc_agent.infra.*`` are fine
    regardless of which subject the importing file lives in."""
    ops_a = tmp_path / "ops" / "a"
    ops_a.mkdir(parents=True)
    (ops_a / "__init__.py").write_text("", encoding="utf-8")
    (ops_a / "uses_infra.py").write_text(
        "from hpc_agent.infra.parsing import parse_walltime_to_sec\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-c", _driver(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"lint_subject_imports rejected an allowed infra import:\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )


def test_lint_subject_imports_rejects_relative_climb(tmp_path: Path) -> None:
    """A relative import that climbs parents (``from ...meta.r import x``)
    crosses subjects exactly like its absolute spelling — resolved, not
    skipped (the old code skipped all relative imports)."""
    ops_a = tmp_path / "ops" / "a"
    meta_r = tmp_path / "meta" / "r"
    ops_a.mkdir(parents=True)
    meta_r.mkdir(parents=True)
    (ops_a / "__init__.py").write_text("", encoding="utf-8")
    (meta_r / "__init__.py").write_text("", encoding="utf-8")
    (ops_a / "climbs.py").write_text(
        "from ...meta.r import thing\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-c", _driver(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0, (
        f"lint_subject_imports missed a relative-import subject crossing:\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "cross-subject import: ops/a imports meta/r" in proc.stdout, (
        f"expected relative-climb diagnostic missing:\nstdout={proc.stdout}"
    )


def test_lint_subject_imports_rejects_alias_form(tmp_path: Path) -> None:
    """``from hpc_agent.ops import b`` binds subject ``b`` without its dotted
    path appearing in the ``from`` clause — must still fire. A non-subject
    alias (``from hpc_agent.ops import helper_fn``) must NOT fire."""
    ops_a = tmp_path / "ops" / "a"
    ops_b = tmp_path / "ops" / "b"
    ops_a.mkdir(parents=True)
    ops_b.mkdir(parents=True)
    (ops_a / "__init__.py").write_text("", encoding="utf-8")
    (ops_b / "__init__.py").write_text("", encoding="utf-8")
    (ops_a / "aliases.py").write_text(
        "from hpc_agent.ops import b\nfrom hpc_agent.ops import helper_fn\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-c", _driver(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0, (
        f"lint_subject_imports missed an alias-form subject crossing:\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "cross-subject import: ops/a imports ops/b" in proc.stdout, (
        f"expected alias-form diagnostic missing:\nstdout={proc.stdout}"
    )
    assert "helper_fn" not in proc.stdout, (
        f"re-exported helper alias wrongly flagged as a subject:\nstdout={proc.stdout}"
    )


def test_lint_subject_imports_allows_role_root_module(tmp_path: Path) -> None:
    """A role-root MODULE file (``ops/shared_helper.py`` — not inside any
    subject directory) is shared op-level surface, not a subject; importing
    it from a subject in either role must pass. Only directories under a
    role root are subjects (the ``ops/evidence_embed.py`` E-embed class:
    one design-pinned helper consumed by both the meta/campaign greenlight
    seat and the ops-root S1 seat)."""
    ops_root = tmp_path / "ops"
    meta_x = tmp_path / "meta" / "x"
    ops_a = tmp_path / "ops" / "a"
    meta_x.mkdir(parents=True)
    ops_a.mkdir(parents=True)
    (ops_root / "__init__.py").write_text("", encoding="utf-8")
    (ops_root / "shared_helper.py").write_text("def helper():\n    pass\n", encoding="utf-8")
    (meta_x / "__init__.py").write_text("", encoding="utf-8")
    (ops_a / "__init__.py").write_text("", encoding="utf-8")
    (meta_x / "uses_root.py").write_text(
        "from hpc_agent.ops.shared_helper import helper\n",
        encoding="utf-8",
    )
    (ops_a / "uses_root.py").write_text(
        "from hpc_agent.ops.shared_helper import helper\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-c", _driver(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"lint_subject_imports wrongly flagged a role-root module file as a subject:\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )


def test_lint_subject_imports_rejects_meta_to_ops(tmp_path: Path) -> None:
    """A file in ``meta/<x>/`` importing from any ``ops.<y>`` subject is
    also a cross-subject violation (different role still counts)."""
    meta_x = tmp_path / "meta" / "x"
    ops_y = tmp_path / "ops" / "y"
    meta_x.mkdir(parents=True)
    ops_y.mkdir(parents=True)
    (meta_x / "__init__.py").write_text("", encoding="utf-8")
    (ops_y / "__init__.py").write_text("", encoding="utf-8")
    (meta_x / "reaches.py").write_text(
        "from hpc_agent.ops.y import something\n",
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-c", _driver(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0, (
        f"lint_subject_imports missed a meta->ops cross-subject import:\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "cross-subject import: meta/x imports ops/y" in proc.stdout, (
        f"expected cross-role diagnostic missing:\nstdout={proc.stdout}"
    )


def _run(scan_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", _driver(scan_root)],
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Directional role rules — P1 (infra->ops, incorporation->{ops,meta}) and
# N3 (_kernel->ops, state->ops). Each rule ships its synthetic fire path.
# ---------------------------------------------------------------------------


def test_lint_forbids_infra_to_ops(tmp_path: Path) -> None:
    """infra is the bottom substrate — a file under ``infra/`` importing
    any ``hpc_agent.ops.*`` slice must fire (P1)."""
    infra = tmp_path / "infra"
    ops_y = tmp_path / "ops" / "y"
    infra.mkdir(parents=True)
    ops_y.mkdir(parents=True)
    (infra / "reaches.py").write_text(
        "from hpc_agent.ops.y.thing import VALUE\n", encoding="utf-8"
    )

    proc = _run(tmp_path)
    assert proc.returncode != 0, f"infra->ops not caught:\n{proc.stdout}\n{proc.stderr}"
    assert "infra must not import ops" in proc.stdout, proc.stdout


def test_lint_allows_infra_to_infra_and_state(tmp_path: Path) -> None:
    """The directional rule must NOT over-fire: infra reaching sideways
    into infra/state substrate, or up into meta (not an ops import), is
    fine under the infra->ops rule."""
    infra = tmp_path / "infra"
    infra.mkdir(parents=True)
    (infra / "fine.py").write_text(
        "from hpc_agent.infra.parsing import parse\n"
        "from hpc_agent.state.runs import load\n",
        encoding="utf-8",
    )
    proc = _run(tmp_path)
    assert proc.returncode == 0, f"infra substrate import wrongly flagged:\n{proc.stdout}"


def test_lint_forbids_incorporation_to_ops(tmp_path: Path) -> None:
    """incorporation feeds submit; importing ``hpc_agent.ops.*`` back is a
    cycle and must fire (P1)."""
    incorp = tmp_path / "incorporation"
    ops_y = tmp_path / "ops" / "y"
    incorp.mkdir(parents=True)
    ops_y.mkdir(parents=True)
    (incorp / "cycle.py").write_text(
        "from hpc_agent.ops.y import runner\n", encoding="utf-8"
    )
    proc = _run(tmp_path)
    assert proc.returncode != 0, f"incorporation->ops not caught:\n{proc.stdout}"
    assert "incorporation must not import ops/meta" in proc.stdout, proc.stdout


def test_lint_forbids_incorporation_to_meta(tmp_path: Path) -> None:
    """incorporation must not import ``hpc_agent.meta.*`` either (P1)."""
    incorp = tmp_path / "incorporation"
    meta_c = tmp_path / "meta" / "campaign"
    incorp.mkdir(parents=True)
    meta_c.mkdir(parents=True)
    (incorp / "reaches_meta.py").write_text(
        "from hpc_agent.meta.campaign.manifest import read_manifest\n", encoding="utf-8"
    )
    proc = _run(tmp_path)
    assert proc.returncode != 0, f"incorporation->meta not caught:\n{proc.stdout}"
    assert "incorporation must not import ops/meta" in proc.stdout, proc.stdout


def test_lint_forbids_kernel_to_ops(tmp_path: Path) -> None:
    """_kernel must not import ops except the enumerated sanctioned seams;
    a fresh, un-allowlisted kernel->ops edge must fire (N3)."""
    kern = tmp_path / "_kernel" / "hooks"
    ops_y = tmp_path / "ops" / "y"
    kern.mkdir(parents=True)
    ops_y.mkdir(parents=True)
    (kern / "new_guard.py").write_text(
        "from hpc_agent.ops.y.helper import probe\n", encoding="utf-8"
    )
    proc = _run(tmp_path)
    assert proc.returncode != 0, f"_kernel->ops not caught:\n{proc.stdout}"
    assert "_kernel must not import ops" in proc.stdout, proc.stdout


def test_lint_forbids_state_to_ops(tmp_path: Path) -> None:
    """state must not import ops except the documented run-story inversion;
    a fresh state->ops edge must fire (N3)."""
    state = tmp_path / "state"
    ops_y = tmp_path / "ops" / "y"
    state.mkdir(parents=True)
    ops_y.mkdir(parents=True)
    (state / "leaks.py").write_text(
        "from hpc_agent.ops.y.thing import stuff\n", encoding="utf-8"
    )
    proc = _run(tmp_path)
    assert proc.returncode != 0, f"state->ops not caught:\n{proc.stdout}"
    assert "state must not import ops" in proc.stdout, proc.stdout


def test_dir_allowlist_clears_a_sanctioned_edge() -> None:
    """The sanctioned-exception mechanism must actually exempt an entry:
    the state->ops rule clears ``state/run_story.py -> ops.overnight``
    while still forbidding any other state->ops module. Proves the allow
    path (not just the fire path) is live."""
    sys.path.insert(0, str(REPO / "scripts"))
    import lint_subject_imports as lsi

    assert lsi._dir_allowed(
        lsi._STATE_TO_OPS,
        "state/run_story.py",
        "hpc_agent.ops.overnight.read_consumption_ledger",
    )
    assert not lsi._dir_allowed(
        lsi._STATE_TO_OPS, "state/run_story.py", "hpc_agent.ops.monitor.status"
    )
    assert not lsi._dir_allowed(
        lsi._STATE_TO_OPS, "state/other.py", "hpc_agent.ops.overnight"
    )


# ---------------------------------------------------------------------------
# Role-root composes= pass — N2.
# ---------------------------------------------------------------------------


def test_lint_role_root_flags_undeclared_subject_reach(tmp_path: Path) -> None:
    """An ``ops/`` role-root MODULE file reaching into a subject it does
    NOT declare via ``@primitive(composes=[...])`` (and that is not on the
    sanctioned inventory) must fire (N2)."""
    ops = tmp_path / "ops"
    ops_sub = ops / "sub"
    ops_sub.mkdir(parents=True)
    (ops_sub / "__init__.py").write_text("", encoding="utf-8")
    (ops_sub / "thing.py").write_text("VALUE = 1\n", encoding="utf-8")
    (ops / "badroot.py").write_text(
        "from hpc_agent.ops.sub.thing import VALUE\n"
        "def go():\n    return VALUE\n",
        encoding="utf-8",
    )
    proc = _run(tmp_path)
    assert proc.returncode != 0, f"undeclared role-root reach not caught:\n{proc.stdout}"
    assert "role-root cross-subject import not declared via composes=" in proc.stdout, proc.stdout
    assert "ops/badroot.py imports ops/sub" in proc.stdout, proc.stdout


def test_lint_role_root_allows_composed_subject(tmp_path: Path) -> None:
    """When the root file DECLARES the reach via ``composes=`` (the composed
    name resolves through the file's own import binding to that subject),
    the same import is allowed (N2). Proves the composes-derived allow
    path is live, not just the fire path."""
    ops = tmp_path / "ops"
    ops_sub = ops / "sub"
    ops_sub.mkdir(parents=True)
    (ops_sub / "__init__.py").write_text("", encoding="utf-8")
    (ops_sub / "runner.py").write_text("def do_it():\n    return 1\n", encoding="utf-8")
    (ops / "goodroot.py").write_text(
        "from hpc_agent.ops.sub.runner import do_it\n\n"
        "@primitive(composes=[do_it])\n"
        "def my_workflow():\n    return do_it()\n",
        encoding="utf-8",
    )
    proc = _run(tmp_path)
    assert proc.returncode == 0, f"composed subject reach wrongly flagged:\n{proc.stdout}"
