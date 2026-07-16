"""Fire-path tests for ``scripts/build_principles_index.py``.

The section index in ``docs/internals/engineering-principles.md`` is GENERATED
from each ``docs/internals/principles/<slug>.md`` file's frontmatter. These
tests pin the generator's contract:

* the real tree is UP TO DATE (``--check`` green) — the committed index equals
  what ``--write`` would produce;
* every section file appears in the listing EXACTLY once;
* the guard actually FIRES — mutating a section file's frontmatter header
  turns ``--check`` red, and ``--write`` heals it back to green.

The fire path runs against a throwaway copy so it never mutates the tree.
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "build_principles_index.py"
_PRINCIPLES_DIR = _REPO_ROOT / "docs" / "internals" / "principles"
_INDEX = _REPO_ROOT / "docs" / "internals" / "engineering-principles.md"


def _load_module():
    spec = importlib.util.spec_from_file_location("_build_principles_index_under_test", _SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_MOD = _load_module()


# ── real-tree state ────────────────────────────────────────────────────────


def test_real_tree_index_is_up_to_date() -> None:
    """The committed index equals a fresh regen (CI-parity for the regen step)."""
    assert _MOD.build(write=False) == 0


def test_every_section_file_appears_exactly_once() -> None:
    """The listing contains each section slug's link exactly once — no drop,
    no duplicate."""
    sections = _MOD.load_sections(_PRINCIPLES_DIR)
    table = _MOD.render_table(sections)
    slugs_on_disk = sorted(p.stem for p in _PRINCIPLES_DIR.glob("*.md"))
    # Every file is represented exactly once (order is by `order`, not alpha).
    assert sorted(s["slug"] for s in sections) == slugs_on_disk
    for slug in slugs_on_disk:
        link = f"(principles/{slug}.md)"
        assert table.count(link) == 1, slug


def test_bare_invocation_refused() -> None:
    assert _MOD.main([]) == 2


def test_both_modes_refused() -> None:
    assert _MOD.main(["--check", "--write"]) == 2


# ── the fire path (throwaway copy) ─────────────────────────────────────────


def _clone_tree(tmp_path: Path) -> tuple[Path, Path]:
    sections_dir = tmp_path / "principles"
    sections_dir.mkdir()
    for p in _PRINCIPLES_DIR.glob("*.md"):
        shutil.copy(p, sections_dir / p.name)
    index = tmp_path / "engineering-principles.md"
    shutil.copy(_INDEX, index)
    return sections_dir, index


def test_check_fires_red_on_header_change_then_write_heals(tmp_path: Path) -> None:
    """Change a section file's frontmatter title (the header the index pins)
    without regenerating → ``--check`` is RED; ``--write`` heals → green."""
    sections_dir, index = _clone_tree(tmp_path)

    # Baseline: the cloned tree is in sync.
    assert _MOD.build(sections_dir=sections_dir, index_path=index, write=False) == 0

    victim = sections_dir / "registration-kernel.md"
    text = victim.read_text(encoding="utf-8")
    mutated = text.replace(
        'title: "The registration kernel:',
        'title: "The registration KERNEL (edited):',
        1,
    )
    assert mutated != text, "frontmatter title not found to mutate"
    victim.write_text(mutated, encoding="utf-8")

    # The header drifted from the generated listing → check is red.
    assert _MOD.build(sections_dir=sections_dir, index_path=index, write=False) == 1
    # Regenerate → green, and the new title is now in the listing.
    assert _MOD.build(sections_dir=sections_dir, index_path=index, write=True) == 0
    assert _MOD.build(sections_dir=sections_dir, index_path=index, write=False) == 0
    assert "The registration KERNEL (edited):" in index.read_text(encoding="utf-8")


def test_duplicate_order_is_refused(tmp_path: Path) -> None:
    """Two section files claiming the same ``order`` is a hard error — the
    listing order must be total."""
    sections_dir, _index = _clone_tree(tmp_path)
    victim = sections_dir / "multi-human.md"
    text = victim.read_text(encoding="utf-8")
    mutated = text.replace("order: 7", "order: 1", 1)
    assert mutated != text
    victim.write_text(mutated, encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate 'order'"):
        _MOD.load_sections(sections_dir)


def test_slug_must_match_filename(tmp_path: Path) -> None:
    """A frontmatter slug that disagrees with the filename is refused (a
    rename that forgot the frontmatter)."""
    sections_dir, _index = _clone_tree(tmp_path)
    victim = sections_dir / "multi-human.md"
    text = victim.read_text(encoding="utf-8")
    victim.write_text(text.replace("slug: multi-human", "slug: multi-humans", 1), encoding="utf-8")
    with pytest.raises(ValueError, match="slug"):
        _MOD.load_sections(sections_dir)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
