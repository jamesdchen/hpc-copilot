"""Subprocess-invokes ``scripts/lint_pure_files.py``.

Two cases:

1. **Happy path on the real tree** — the script must exit 0 against the
   current repository. After PR 0b, only the three planning helpers are
   annotated ``# @pure: no-io``, and each is verifiably I/O-free.
2. **Fixture violation** — build a tiny temp tree with one annotated
   file that imports ``subprocess`` and assert the script exits non-zero
   with the expected ``forbidden I/O import`` message.

The fixture run invokes the script as a one-shot Python process with an
override scan root via an env-var-free path: we monkey-patch by
spawning a small wrapper that calls ``main(scan_root=...)`` directly.
That keeps the script's CLI surface (``python scripts/lint_pure_files.py``)
unchanged while still letting the test exercise a controlled tree.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
SCRIPT = REPO / "scripts" / "lint_pure_files.py"


def test_lint_pure_files_passes_on_current_tree() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"lint_pure_files failed on current tree:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    )


def test_lint_pure_files_rejects_subprocess_import(tmp_path: Path) -> None:
    """An annotated file that imports ``subprocess`` must trigger a
    non-zero exit with a message that names the offending import."""
    dirty = tmp_path / "dirty.py"
    dirty.write_text(
        textwrap.dedent(
            """\
            # @pure: no-io
            \"\"\"A file that lies about being pure.\"\"\"

            import subprocess

            def run() -> None:
                subprocess.run(["true"], check=True)
            """
        ),
        encoding="utf-8",
    )
    # Invoke the script's ``main`` with a custom scan root via a tiny
    # inline driver — keeps the CLI shape unchanged while still letting
    # us point the linter at a controlled fixture tree.
    driver = textwrap.dedent(
        f"""\
        import sys
        sys.path.insert(0, {str(REPO / "scripts")!r})
        from pathlib import Path
        from lint_pure_files import main
        sys.exit(main(scan_root=Path({str(tmp_path)!r})))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode != 0, (
        f"lint_pure_files unexpectedly passed on a dirty fixture:\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
    assert "forbidden I/O import in @pure file: subprocess" in proc.stdout, (
        f"expected violation message missing:\nstdout={proc.stdout}"
    )


def test_lint_pure_files_ignores_unannotated_violation(tmp_path: Path) -> None:
    """A file *without* the marker may import whatever it likes — the
    lint must stay silent for it."""
    clean = tmp_path / "ordinary.py"
    clean.write_text(
        textwrap.dedent(
            """\
            \"\"\"Ordinary module, not annotated.\"\"\"

            import subprocess

            def run() -> None:
                subprocess.run(["true"], check=True)
            """
        ),
        encoding="utf-8",
    )
    driver = textwrap.dedent(
        f"""\
        import sys
        sys.path.insert(0, {str(REPO / "scripts")!r})
        from pathlib import Path
        from lint_pure_files import main
        sys.exit(main(scan_root=Path({str(tmp_path)!r})))
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", driver],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, (
        f"lint_pure_files false-positived on an unannotated file:\n"
        f"stdout={proc.stdout}\nstderr={proc.stderr}"
    )
