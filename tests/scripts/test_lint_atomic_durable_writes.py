"""Tests for the atomic-durable-writes lint (generator G12).

Pins these invariants (mirrors ``test_lint_remote_read_ack.py``):

1. The real tree passes — every durable-artifact writer routes through the
   ``infra/io`` atomic helpers today (the four G12 members are fixed).
2. The lint can actually FIRE, for every durable signal:
   - a ``write_text`` on a durable-named variable,
   - a ``write_text`` on a variable tainted by a durable-basename literal,
   - a truncating ``ZipFile(<durable>, "w")``.
3. A non-durable write (inline / throwaway target) does NOT fire.
4. Read/append ``ZipFile`` modes do NOT fire.
5. The excluded subtrees (conformance, templates) are skipped.
6. The cited ALLOWLIST exempts a ``path::function``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "lint_atomic_durable_writes", REPO_ROOT / "scripts" / "lint_atomic_durable_writes.py"
)
assert _SPEC is not None and _SPEC.loader is not None
lint = importlib.util.module_from_spec(_SPEC)
sys.modules["lint_atomic_durable_writes"] = lint
_SPEC.loader.exec_module(lint)


def test_real_tree_is_clean() -> None:
    """No durable artifact is written with a truncating call on the current tree."""
    assert lint.main() == 0


def _module(tmp_path: Path, body: str, *, rel: str = "hpc_agent/ops/demo.py") -> Path:
    """Write *body* at *rel* under a synthetic scan root and return the root."""
    root = tmp_path / "src"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return root


# write_text on a durable-named variable.
_DURABLE_VARNAME = (
    "def reseal(manifest_path, text):\n    manifest_path.write_text(text, encoding='utf-8')\n"
)

# write_text on a variable tainted by a durable-basename literal.
_TAINTED = (
    "from pathlib import Path\n"
    "\n"
    "def pin(result_dir, payload):\n"
    "    p = Path(result_dir) / 'experiment_meta.json'\n"
    "    p.write_text(payload, encoding='utf-8')\n"
)

# Truncating ZipFile on a durable-named variable.
_ZIP_TRUNCATE = (
    "import zipfile\n"
    "\n"
    "def seal(archive_path, entries):\n"
    "    with zipfile.ZipFile(archive_path, 'w') as zf:\n"
    "        zf.writestr('manifest.json', entries)\n"
)


def test_fires_on_durable_varname(tmp_path: Path, capsys) -> None:
    root = _module(tmp_path, _DURABLE_VARNAME)
    path = root / "hpc_agent" / "ops" / "demo.py"
    findings = lint.lint_file(path)
    assert findings, "expected a finding for the durable-named write_text"
    assert any("durable write not atomic" in msg for _, msg in findings)
    assert lint.main(root) == 1
    assert "reseal" in capsys.readouterr().out


def test_fires_on_basename_tainted_var(tmp_path: Path) -> None:
    root = _module(tmp_path, _TAINTED)
    assert lint.main(root) == 1


def test_fires_on_truncating_zipfile(tmp_path: Path) -> None:
    root = _module(tmp_path, _ZIP_TRUNCATE)
    findings = lint.lint_file(root / "hpc_agent" / "ops" / "demo.py")
    assert any("ZipFile" in msg for _, msg in findings)
    assert lint.main(root) == 1


def test_non_durable_write_is_clean(tmp_path: Path) -> None:
    """A write on an inline / non-durable-named target does not fire."""
    body = (
        "from pathlib import Path\n"
        "\n"
        "def dump(out_dir, text):\n"
        "    (Path(out_dir) / 'notes.txt').write_text(text, encoding='utf-8')\n"
        "    scratch = Path(out_dir) / 'scratch.log'\n"
        "    scratch.write_text(text, encoding='utf-8')\n"
    )
    root = _module(tmp_path, body)
    assert lint.main(root) == 0


def test_read_and_append_zipfile_are_clean(tmp_path: Path) -> None:
    body = (
        "import zipfile\n"
        "\n"
        "def peek(archive_path):\n"
        "    with zipfile.ZipFile(archive_path) as zf:\n"
        "        return zf.namelist()\n"
        "\n"
        "def add(archive_path, name, data):\n"
        "    with zipfile.ZipFile(archive_path, 'a') as zf:\n"
        "        zf.writestr(name, data)\n"
    )
    root = _module(tmp_path, body)
    assert lint.main(root) == 0


def test_excluded_subtrees_are_skipped(tmp_path: Path) -> None:
    """A durable-shaped write under conformance/ or templates/ is not scanned."""
    for rel in (
        "hpc_agent/conformance/adapters/demo.py",
        "hpc_agent/execution/mapreduce/templates/demo.py",
    ):
        root = _module(tmp_path, _DURABLE_VARNAME, rel=rel)
        assert lint.main(root) == 0, rel


def test_allowlist_exempts_a_path_function(tmp_path: Path, monkeypatch) -> None:
    root = _module(tmp_path, _DURABLE_VARNAME)
    key = "hpc_agent/ops/demo.py::reseal"
    assert lint.main(root) == 1  # fires without the exemption
    monkeypatch.setattr(lint, "ALLOWLIST", frozenset({key}))
    assert lint.main(root) == 0  # cited exemption clears it
