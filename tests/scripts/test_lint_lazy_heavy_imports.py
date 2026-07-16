"""Tests for the lazy-heavy-imports lint (latency plan B1).

Pins these invariants (mirrors ``test_lint_atomic_durable_writes.py``):

1. The real tree passes — after the B1 lazy-import move no module imports a
   heavy library (``jsonschema``) at module scope.
2. The lint FIRES on a module-scope ``import jsonschema`` and on a module-scope
   ``from jsonschema import ...``.
3. A LAZY import (inside a function) does NOT fire — that is the whole point.
4. An ``if TYPE_CHECKING:``-guarded heavy import does NOT fire.
5. A non-heavy library (``json``) at module scope does NOT fire.
6. The cited ALLOWLIST exempts a file.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from tests._paths import REPO_ROOT

_SPEC = importlib.util.spec_from_file_location(
    "lint_lazy_heavy_imports", REPO_ROOT / "scripts" / "lint_lazy_heavy_imports.py"
)
assert _SPEC is not None and _SPEC.loader is not None
lint = importlib.util.module_from_spec(_SPEC)
sys.modules["lint_lazy_heavy_imports"] = lint
_SPEC.loader.exec_module(lint)


def test_real_tree_is_clean() -> None:
    """No module imports jsonschema at module scope on the current tree."""
    assert lint.main() == 0


_REL = "hpc_agent/meta/campaign/atoms/demo.py"


def _module(tmp_path: Path, body: str, *, rel: str = _REL) -> Path:
    """Write *body* at *rel* under a synthetic scan root and return the root."""
    root = tmp_path / "src"
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return root


def test_fires_on_module_scope_import(tmp_path: Path, capsys) -> None:
    body = "import json\nimport jsonschema\n\n\ndef f():\n    return jsonschema\n"
    root = _module(tmp_path, body)
    findings = lint.lint_file(root / "hpc_agent" / "meta" / "campaign" / "atoms" / "demo.py", root)
    assert findings, "expected a finding for the module-scope import jsonschema"
    assert any("heavy import not lazy" in msg for _, msg in findings)
    assert lint.main(root) == 1
    assert "jsonschema" in capsys.readouterr().out


def test_fires_on_module_scope_from_import(tmp_path: Path) -> None:
    body = "from jsonschema import ValidationError\n\n\ndef f():\n    raise ValidationError\n"
    root = _module(tmp_path, body)
    findings = lint.lint_file(root / "hpc_agent" / "meta" / "campaign" / "atoms" / "demo.py", root)
    assert any("heavy import not lazy" in msg for _, msg in findings)
    assert lint.main(root) == 1


def test_lazy_import_is_clean(tmp_path: Path) -> None:
    """A jsonschema import inside a function does not fire."""
    body = (
        "import json\n"
        "\n"
        "\n"
        "def validate(payload):\n"
        "    import jsonschema\n"
        "\n"
        "    try:\n"
        "        return payload\n"
        "    except jsonschema.ValidationError:\n"
        "        return None\n"
    )
    root = _module(tmp_path, body)
    assert lint.main(root) == 0


def test_type_checking_guard_is_clean(tmp_path: Path) -> None:
    """A TYPE_CHECKING-guarded heavy import is indented, so it does not fire."""
    body = "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    import jsonschema\n"
    root = _module(tmp_path, body)
    assert lint.main(root) == 0


def test_non_heavy_module_is_clean(tmp_path: Path) -> None:
    """A module-scope import of a non-heavy library does not fire."""
    body = "import json\nfrom pathlib import Path\n\n\ndef f(p: Path):\n    return json.dumps({})\n"
    root = _module(tmp_path, body)
    assert lint.main(root) == 0


def test_allowlist_exempts_a_file(tmp_path: Path, monkeypatch) -> None:
    body = "import jsonschema\n\n\ndef f():\n    return jsonschema\n"
    root = _module(tmp_path, body)
    rel = "hpc_agent/meta/campaign/atoms/demo.py"
    assert lint.main(root) == 1  # fires without the exemption
    monkeypatch.setattr(lint, "ALLOWLIST", frozenset({rel}))
    assert lint.main(root) == 0  # cited exemption clears it
