"""Contract: the delegation boundary is mechanized, not merely written down.

`docs/design/agent-delegation.md` admits FREEFORM subagents at exactly one
level — a **context firewall for read-only reconnaissance**, beside the
execution path, never inside it (the retired level was the haiku-pinned
`hpc-worker` spawn transport, removed with the worker fence; rule 5's wider
plan-relay grant belongs to validated workflow plans only, guarded by the
sibling `test_workflow_plan_delegation.py`). Two halves of that boundary are
LLM-facing prose with no lint behind them: the per-skill `## Delegation
(hpc-recon)` sections that say what may be handed off, and the `hpc-recon`
agent charter that says what the agent may do. Prose drifts; this binds it:

* every verb a `- delegable:` bullet names is `verb=query` or `verb=validate`
  in the LIVE registry (`hpc_agent._kernel.registry.primitive::get_registry`).
  The locked set is **resolved, never listed** — a hand-written roll of
  mutating verbs would be one more thing to forget when a primitive lands, and
  the whole point of D4 is that a new `verb=mutate` primitive is locked the
  moment it is decorated, with no edit here;
* every skill carrying the section also carries the `- locked:` restatement
  naming `append-decision` (the read-and-sign rendezvous is undelegable) and
  `Task` in its frontmatter `allowed-tools` — a section that cannot spawn is a
  section that lies;
* `hpc-recon.md` ships, its `tools:` exclude `Write`/`Edit` (read-only by
  charter), and its body carries the never-paraphrase-a-verbatim-render rule;
* the checker actually fires: a synthetic skill offering `append-decision` as
  delegable is refused (the "verify a guard can actually fire" rule —
  `docs/internals/engineering-principles.md`).

None of this is a trust gate, and the tests are not pretending otherwise: a
subagent's report is model-carried text, worth exactly nothing to the gates,
which read journals, stores, and the utterance log. These tests guard the
GUIDANCE — that we never invite a delegation the doctrine forbids.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from hpc_agent._kernel.registry.primitive import get_registry, register_primitives
from tests._paths import REPO_ROOT

if TYPE_CHECKING:
    from collections.abc import Mapping

    from hpc_agent._kernel.registry.primitive import PrimitiveMeta

_SKILLS_DIR = REPO_ROOT / "src/hpc_agent/slash_commands/skills"
_AGENT = REPO_ROOT / "src/hpc_agent/slash_commands/agents/hpc-recon.md"

#: The workflow skills D3 settles as carrying a delegation section.
_DELEGATING_SKILLS = (
    "hpc-submit",
    "hpc-status",
    "hpc-aggregate",
    "hpc-campaign",
    "hpc-notebook-audit",
)

_SECTION_RE = re.compile(r"^##\s+Delegation\s+\(hpc-recon\)\s*$", re.M)
_HEADING_RE = re.compile(r"^##\s", re.M)
#: A registry key is lowercase, hyphen-joined (`net-triage`, `append-decision`).
#: Tokens are matched maximally and then tested for registry membership — a
#: token is a verb name IFF the registry says so.
_TOKEN_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


# --------------------------------------------------------------------------
# The shared checker (module-level by design: the per-skill sweep and the
# synthetic fire-path test must exercise the SAME code, or the fire path
# proves nothing about what runs over the real skills).
# --------------------------------------------------------------------------


def _frontmatter(text: str) -> str:
    """The YAML block between the opening and closing ``---`` fences (or ``""``)."""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    return text[3:end] if end != -1 else ""


def delegation_section(text: str) -> str | None:
    """The ``## Delegation (hpc-recon)`` body, or ``None`` if the skill has none."""
    match = _SECTION_RE.search(text)
    if match is None:
        return None
    rest = text[match.end() :]
    nxt = _HEADING_RE.search(rest)
    return rest[: nxt.start()] if nxt else rest


def _bullets(section: str, label: str) -> list[str]:
    return [
        line.partition(":")[2]
        for line in section.splitlines()
        if line.lstrip().lower().startswith(f"- {label}:")
    ]


def check_delegation_section(text: str, registry: Mapping[str, PrimitiveMeta]) -> list[str]:
    """Return the delegation-boundary violations in one skill's SKILL.md text.

    Empty list == conforming. A skill with no section is vacuously clean (D3
    scopes the sections to the workflow skills; presence is pinned separately
    by :func:`test_workflow_skills_carry_a_delegation_section`). The locked set
    is resolved from ``registry`` — anything that is not ``query``/``validate``
    is locked, so a primitive decorated tomorrow is covered today.
    """
    section = delegation_section(text)
    if section is None:
        return []

    violations: list[str] = []

    delegable = _bullets(section, "delegable")
    if not delegable:
        violations.append("the Delegation section names no `- delegable:` step")
    for line in delegable:
        for token in _TOKEN_RE.findall(line):
            meta = registry.get(token)
            if meta is None:
                continue  # not a verb name — prose, a path fragment, a skill slug
            if meta.verb not in ("query", "validate"):
                violations.append(
                    f"`- delegable:` offers {token!r} (verb={meta.verb}) — only "
                    "verb=query/validate reconnaissance is delegable; every "
                    "read-and-sign and every act stays with the main session "
                    "(docs/design/agent-delegation.md, rule 2)"
                )

    locked = _bullets(section, "locked")
    if not locked:
        violations.append(
            "the Delegation section carries no `- locked:` restatement — the "
            "boundary must be stated where the delegation is offered"
        )
    elif not any("append-decision" in line for line in locked):
        violations.append(
            "the `- locked:` bullet must name `append-decision` — the "
            "read-and-sign rendezvous is the one thing a subagent never touches"
        )

    allowed = next(
        (
            line.partition(":")[2]
            for line in _frontmatter(text).splitlines()
            if line.lower().startswith("allowed-tools:")
        ),
        "",
    )
    if "Task" not in allowed.split():
        violations.append(
            "a skill that offers delegation must carry `Task` in its "
            f"frontmatter allowed-tools (found: {allowed.strip()!r})"
        )

    return violations


@pytest.fixture(scope="module")
def registry() -> dict[str, PrimitiveMeta]:
    register_primitives()
    return get_registry()


def _skill_paths() -> list[Path]:
    return sorted(_SKILLS_DIR.glob("*/SKILL.md"))


# --------------------------------------------------------------------------
# The real skills
# --------------------------------------------------------------------------


@pytest.mark.parametrize("skill", _DELEGATING_SKILLS)
def test_workflow_skills_carry_a_delegation_section(skill: str) -> None:
    """D3: each of the five workflow skills states its delegation boundary.

    Silence is the failure mode this pins — a skill with no section reads as
    "nothing here is delegable", which is not what the doctrine settled, and
    the next editor has nothing to drift FROM.
    """
    path = _SKILLS_DIR / skill / "SKILL.md"
    assert path.is_file(), f"{path} must exist"
    assert delegation_section(path.read_text(encoding="utf-8")) is not None, (
        f"{skill} must carry a `## Delegation (hpc-recon)` section "
        "(docs/design/agent-delegation.md; plan D3)"
    )


@pytest.mark.parametrize("path", _skill_paths(), ids=lambda p: p.parent.name)
def test_skill_delegation_section_conforms(path: Path, registry: dict[str, PrimitiveMeta]) -> None:
    """Every shipped skill — not just the five — is swept, so a section added
    to a sixth skill inherits the boundary without an edit here."""
    violations = check_delegation_section(path.read_text(encoding="utf-8"), registry)
    assert not violations, f"{path.parent.name}/SKILL.md: " + "; ".join(violations)


# --------------------------------------------------------------------------
# The agent definition
# --------------------------------------------------------------------------


def test_recon_agent_ships_and_is_read_only() -> None:
    """D2: `hpc-recon` is the first core-shipped agent since the worker removal.

    `agent_assets._copy_asset_tree`'s ``agents/`` walk installs whatever is in
    the directory, so the charter IS the enforcement surface: no `Write`, no
    `Edit`, read-only by construction of the frontmatter.
    """
    assert _AGENT.is_file(), f"{_AGENT} must exist — the recon agent ships with core"
    text = _AGENT.read_text(encoding="utf-8")
    front = _frontmatter(text)
    assert "name: hpc-recon" in front, "frontmatter must name the agent"

    tools = next(
        (
            line.partition(":")[2]
            for line in front.splitlines()
            if line.lower().startswith("tools:")
        ),
        None,
    )
    assert tools is not None, "the agent must declare an explicit `tools:` line"
    granted = {tool.strip(" ,`") for tool in tools.replace(",", " ").split()}
    assert not granted & {"Write", "Edit", "NotebookEdit", "MultiEdit"}, (
        f"hpc-recon is read-only by charter — write tools must not be granted: {sorted(granted)}"
    )


def test_recon_agent_body_forbids_paraphrasing_a_verbatim_render() -> None:
    """Rule 2's sharpest edge: if the output would be relayed VERBATIM to the
    human, no agent sits between the code render and the human — the agent
    returns POINTERS + COUNTS instead."""
    body = _AGENT.read_text(encoding="utf-8")
    assert "paraphrase" in body.lower(), (
        "the hpc-recon body must carry the never-paraphrase-a-verbatim-render rule"
    )
    assert "append-decision" in body, "the charter must name the undelegable sign-off verb"


# --------------------------------------------------------------------------
# Fire path — the guard is not decorative
# --------------------------------------------------------------------------


_SYNTHETIC = """---
name: synthetic-skill
allowed-tools: Bash Read Write Task
---

Body.

## Delegation (hpc-recon)

- delegable: {offer} — read the state, hand back counts.
- locked: `append-decision`, the `y`/nudge rendezvous, sign-off, verbatim relays.
"""


def test_checker_refuses_a_delegable_append_decision(
    tmp_path: Path, registry: dict[str, PrimitiveMeta]
) -> None:
    """The fire path: a synthetic skill that offers the sign-off verb as
    delegable is refused, and refused FOR THAT REASON — everything else about
    the synthetic (locked bullet, `Task`) is conforming, so the failure isolates
    the delegable-verb resolution rather than any incidental defect."""
    skill = tmp_path / "SKILL.md"
    skill.write_text(_SYNTHETIC.format(offer="`append-decision`"), encoding="utf-8")
    violations = check_delegation_section(skill.read_text(encoding="utf-8"), registry)
    assert len(violations) == 1, f"expected exactly the delegable-verb refusal, got {violations}"
    assert "append-decision" in violations[0] and "verb=mutate" in violations[0]


def test_checker_admits_a_query_only_offer(
    tmp_path: Path, registry: dict[str, PrimitiveMeta]
) -> None:
    """The control arm: the same synthetic offering `doctor` (verb=query) passes
    clean. Without this, the fire-path test above would be satisfied by a
    checker that refuses everything."""
    skill = tmp_path / "SKILL.md"
    skill.write_text(_SYNTHETIC.format(offer="`doctor`, `attention-queue`"), encoding="utf-8")
    assert check_delegation_section(skill.read_text(encoding="utf-8"), registry) == []
