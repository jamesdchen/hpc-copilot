"""Contract: the wheel ships product; the dev loop lives repo-side.

The 2026-07-28 separation (user-ordered: "the wheel build should not have
dev loop stuff") drew the line at the package boundary. Maintainer/dev-loop
procedure — the ``release`` skill, every lint and regen script — lives in
the repo tree (``.claude/skills/``, ``scripts/``, ``docs/plans/``) and
never travels in the wheel. (The campaign workflow PLANS are product —
researcher-lifecycle drivers shipped in ``slash_commands/workflows/`` and
installed by ``agent_assets`` since 2026-07-29 — not dev-loop material.)
The ONE devx affordance the product keeps is session tagging (the
``tag-session`` verb): an end-user session can mark itself as data for the
dev loop to ingest, and nothing more.

Mechanized here:

* the shipped asset tree (``src/hpc_agent/slash_commands/``) contains NO
  internal/maintainer-flagged skill — the flag's presence in the package is
  exactly the mixing the separation removed. The checker is
  :func:`hpc_agent.agent_assets._skill_is_internal` itself (the installer's
  own resolver — one definition, not a re-implementation);
* the relocated maintainer surface is really covered by the agent-prose
  lints: ``iter_maintainer_prose_targets`` finds ``.claude/skills/release/
  SKILL.md`` on the real tree. Without this, the 2026-07-04 lesson repeats —
  a maintainer skill drifting outside every lint's scan root (the reason
  ``release`` was pulled INTO the tracked tree in the first place; the
  separation moved it out of the WHEEL, not out of coverage);
* the flag resolver actually fires (synthetic ``internal: true`` /
  ``distribution: maintainer`` trees detected; a plain skill passes) — the
  no-internal-in-package sweep above is only as strong as its checker.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hpc_agent.agent_assets import _skill_is_internal
from tests._paths import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from _agent_prose_targets import iter_maintainer_prose_targets  # noqa: E402

_SHIPPED_SKILLS = REPO_ROOT / "src/hpc_agent/slash_commands/skills"


def test_no_internal_skill_ships_in_the_package() -> None:
    """The wheel's asset tree is 100% end-user product: a skill that needs the
    internal/maintainer flag belongs in ``.claude/skills/``, not here."""
    flagged = sorted(
        skill.name
        for skill in _SHIPPED_SKILLS.iterdir()
        if skill.is_dir() and _skill_is_internal(skill)
    )
    assert flagged == [], (
        f"internal/maintainer-flagged skill(s) in the shipped package tree: {flagged}. "
        "Dev-loop procedure lives at the repo level (.claude/skills/) — the wheel "
        "ships product only (2026-07-28 separation)."
    )


def test_maintainer_surface_is_lint_covered() -> None:
    """`release` is repo-tracked AND scanned: the prose lints' maintainer glob
    must resolve it, or the pre-2026-07-04 drift class returns."""
    found = {p.relative_to(REPO_ROOT).as_posix() for p in iter_maintainer_prose_targets(REPO_ROOT)}
    assert ".claude/skills/release/SKILL.md" in found, (
        f"the maintainer-skill scan must cover the relocated release skill, found: {sorted(found)}"
    )


def test_internal_flag_resolver_fires(tmp_path: Path) -> None:
    """Both spellings of the flag are detected; an unflagged skill is not."""
    cases = {
        "internal": ("---\nname: x\ninternal: true\n---\nBody.\n", True),
        "maintainer": ("---\nname: x\ndistribution: maintainer\n---\nBody.\n", True),
        "plain": ("---\nname: x\n---\nBody.\n", False),
    }
    for name, (text, expected) in cases.items():
        skill = tmp_path / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(text, encoding="utf-8")
        assert _skill_is_internal(skill) is expected, name
