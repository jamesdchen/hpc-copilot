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
