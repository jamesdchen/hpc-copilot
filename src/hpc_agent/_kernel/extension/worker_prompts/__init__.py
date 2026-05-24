"""Code-rendered worker prompt templates.

The four files in this package — ``submit.md``, ``status.md``,
``aggregate.md``, ``campaign.md`` — are not skills. They are
deterministic prompt templates that
:func:`hpc_agent.atoms.spawn_prompt._procedure_body` reads as inert text
and inlines into the ``claude -p --bare`` worker's ``cacheable_prefix``.

A headless ``claude -p --bare`` worker has no Skill tool / no skill
discovery; the procedure travels inside the prompt. The directory name
``worker_prompts/`` is what the templates actually are, replacing the
older misnomer of ``skills/hpc-{submit,status,aggregate,campaign}/`` —
see ``docs/internals/skill-policy.md`` for the forcing rule.

A plugin may overlay a procedure by exposing a ``worker_prompt_assets``
attribute (an :mod:`importlib.resources` traversable) on its
``hpc_agent.plugins`` entry point; the first plugin to provide
``<workflow>.md`` wins, then the host's bundled copy is used.

Templates are eligible for prose hardening that real skills are not —
snapshot tests on the rendered prefix bytes, token-budget lints, and
``hpc-agent <primitive>`` reference cross-checks against the operations
catalog. The deterministic prompt construction is what makes those
tests meaningful; an LLM-discovered Skill consumed via the Skill tool
is by design stochastic and tolerant.
"""

from __future__ import annotations

from importlib.resources import files

__all__ = ["read_procedure"]


def read_procedure(name: str) -> str:
    """Return the bundled worker-prompt body for *name* as a string.

    Reads ``hpc_agent/worker_prompts/<name>.md`` from package data.
    Procedures carry no frontmatter — the file is the body verbatim,
    inlined into the spawn pipeline's cacheable prefix as-is.

    Plugin overlays are resolved by
    :func:`hpc_agent.atoms.spawn_prompt._procedure_body`; this helper
    is the host-only lookup that the overlay falls back to.
    """
    return (files("hpc_agent._kernel.extension.worker_prompts") / f"{name}.md").read_text(
        encoding="utf-8"
    )
