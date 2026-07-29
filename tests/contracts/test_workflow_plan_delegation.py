"""Contract: the workflow-plan delegation boundary is mechanized, not prose.

`docs/design/agent-delegation.md` rule 5 lets a validated plan relay verbs the
freeform-agent surface may not touch — but only as authored `COMMANDS`
templates, never as text a model acts on freely, and never the rendezvous
verb. Three lines, all resolved against the LIVE registry
(`hpc_agent._kernel.registry.primitive::get_registry`), never a hand list:

* `append-decision` (the read-and-sign rendezvous) appears as an
  ``hpc-agent`` invocation in NO plan, in no section — the tier-3 lock has
  no plan-relay exception;
* every ``hpc-agent <verb>`` occurrence OUTSIDE a plan's ``COMMANDS`` block —
  in `PROMPTS`, in the adapter, anywhere a model could be handed it as
  instruction text — resolves to ``verb=query``/``validate``. Freeform text
  stays at rule-1 (recon) scope;
* inside ``COMMANDS``, any registry verb but the rendezvous lock is legal —
  that IS rule 5, and the checker's control arm proves `block-drive` passes
  there so the fire paths cannot be satisfied by refusing everything.

Like its sibling (`test_agent_delegation_guidance.py`) this guards GUIDANCE,
not trust: a plan-relayed verb hits the same runtime gates as an inline one
(`assert_greenlit_target` reads the journal, not the caller), so these tests
keep the plans honest about a boundary the gates already enforce blind.
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

_WORKFLOWS_DIR = REPO_ROOT / "src/hpc_agent/slash_commands/workflows"

#: The rendezvous verb: locked in every section of every plan (rule 2, no
#: rule-5 exception). Named, not resolved — the lock is about WHAT the verb
#: is (a human's typed words becoming evidence), not its VerbKind.
_RENDEZVOUS = "append-decision"

#: ``hpc-agent <verb>`` invocations; the token is then tested for registry
#: membership, so prose like "hpc-agent query verbs" (no verb token) or a
#: non-verb word after the binary name never false-positives.
_INVOCATION_RE = re.compile(r"hpc-agent\s+([a-z][a-z0-9]*(?:-[a-z0-9]+)*)")
#: A top-level plan block: ``const NAME = ...`` at column 0, running until
#: the next column-0 ``const``/section separator. The plan format is pinned
#: by the README.md beside the plans (PORTABLE PLAN section order), so this
#: line-shape parse is parsing a contract, not guessing at freeform JS.
_BLOCK_RE = re.compile(r"^const\s+(\w+)\s*=", re.M)


# --------------------------------------------------------------------------
# The shared checker (module-level by design: the real-plan sweep and the
# synthetic fire-path tests must exercise the SAME code).
# --------------------------------------------------------------------------


def _blocks(text: str) -> dict[str, tuple[int, int]]:
    """Map each top-level ``const NAME =`` block name to its (start, end) span."""
    matches = list(_BLOCK_RE.finditer(text))
    out: dict[str, tuple[int, int]] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[m.group(1)] = (m.start(), end)
    return out


def check_plan_delegation(text: str, registry: Mapping[str, PrimitiveMeta]) -> list[str]:
    """Return the rule-5 violations in one workflow plan's source text.

    Empty list == conforming. The COMMANDS block is the only span where a
    non-query/validate invocation is legal; the rendezvous verb is illegal
    everywhere. Registry membership decides what counts as a verb token.
    """
    violations: list[str] = []
    commands_start, commands_end = _blocks(text).get("COMMANDS", (0, 0))

    for match in _INVOCATION_RE.finditer(text):
        token = match.group(1)
        if token == _RENDEZVOUS:
            violations.append(
                f"invokes `hpc-agent {token}` — the read-and-sign rendezvous is "
                "locked in every plan section, with no rule-5 exception "
                "(docs/design/agent-delegation.md rule 2)"
            )
            continue
        meta = registry.get(token)
        if meta is None:
            continue  # not a verb name — prose or a coincidental token
        if meta.verb in ("query", "validate"):
            continue
        # a mutating/workflow verb: legal only inside the COMMANDS block
        if not (commands_start <= match.start() < commands_end):
            violations.append(
                f"invokes `hpc-agent {token}` (verb={meta.verb}) outside the "
                "COMMANDS block — model-facing text stays at recon scope; "
                "plan-relayed execution lives only in authored COMMANDS "
                "templates (docs/design/agent-delegation.md rule 5)"
            )
    return violations


@pytest.fixture(scope="module")
def registry() -> dict[str, PrimitiveMeta]:
    register_primitives()
    return get_registry()


def _plan_paths() -> list[Path]:
    return sorted(_WORKFLOWS_DIR.glob("*.js"))


# --------------------------------------------------------------------------
# The real plans
# --------------------------------------------------------------------------


def test_the_flagship_plans_ship() -> None:
    """The section's two levels each have a shipped exemplar: recon-scope
    (`campaign-recon`) and plan-relay-scope (`campaign-run`). An empty
    directory would make every sweep below vacuous."""
    names = {p.name for p in _plan_paths()}
    assert {"campaign-recon.js", "campaign-run.js"} <= names, (
        f"slash_commands/workflows/ must ship both flagship plans, found: {sorted(names)}"
    )


@pytest.mark.parametrize("path", _plan_paths(), ids=lambda p: p.stem)
def test_plan_conforms_to_delegation_boundary(
    path: Path, registry: dict[str, PrimitiveMeta]
) -> None:
    """Every plan in the directory is swept, so a plan added tomorrow
    inherits the boundary without an edit here."""
    violations = check_plan_delegation(path.read_text(encoding="utf-8"), registry)
    assert not violations, f"{path.name}: " + "; ".join(violations)


# --------------------------------------------------------------------------
# Fire paths — the guard is not decorative
# --------------------------------------------------------------------------


_SYNTHETIC = """const COMMANDS = {{
  probe: ({{ repo }}) => `hpc-agent {in_commands} --spec "$SPEC" --experiment-dir ${{repo}}`,
}}

const PROMPTS = {{
  step: ({{ repo }}) => `Run hpc-agent {in_prompts} and relay the result.`,
}}
"""


def test_checker_refuses_the_rendezvous_verb_even_in_commands(
    registry: dict[str, PrimitiveMeta],
) -> None:
    """The tier-3 lock: `append-decision` is refused INSIDE the one block
    where mutating verbs are otherwise legal — isolating the
    no-exception rule from the section rule below."""
    text = _SYNTHETIC.format(in_commands="append-decision", in_prompts="doctor")
    violations = check_plan_delegation(text, registry)
    assert len(violations) == 1, f"expected exactly the rendezvous refusal, got {violations}"
    assert "append-decision" in violations[0] and "rendezvous" in violations[0]


def test_checker_refuses_a_workflow_verb_in_prompts(
    registry: dict[str, PrimitiveMeta],
) -> None:
    """The section rule: `block-drive` (verb=workflow) is refused in PROMPTS —
    a model must never be handed a mutating invocation as instruction text."""
    text = _SYNTHETIC.format(in_commands="doctor", in_prompts="block-drive")
    violations = check_plan_delegation(text, registry)
    assert len(violations) == 1, f"expected exactly the PROMPTS refusal, got {violations}"
    assert "block-drive" in violations[0] and "COMMANDS" in violations[0]


def test_checker_admits_rule5_relay_in_commands(
    registry: dict[str, PrimitiveMeta],
) -> None:
    """The control arm: the same workflow verb passes clean inside COMMANDS —
    rule 5 is a grant, and a checker that refuses everything would satisfy
    both fire paths while enforcing a different (stricter) doctrine."""
    text = _SYNTHETIC.format(in_commands="block-drive", in_prompts="doctor")
    assert check_plan_delegation(text, registry) == []
