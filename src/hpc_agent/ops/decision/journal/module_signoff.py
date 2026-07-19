"""The notebook MODULE sign-off gate (wave-3 piece 3) — recompute lock + the
authorship floor over a ``notebook-module-sign-off`` record.

A module sign-off attests that a HUMAN reviewed a WHOLE linked source module (the
file an audited section imports under a ``source_root``) at a specific
``module_sha``. It is the signable attention UNIT that lets one re-sign clear
every dependent section instead of per-dependent noise: the graduation gate's
linked-source drift check treats a module whose CURRENT sha is human-signed as
current (``ops/notebook_gate.py`` +
``state.notebook_audit.module_sha_signed``).

It rides the SAME ``append-decision`` path + gate stack as the section sign-off
(T8) and mirrors its record discipline: the un-fakeable RECOMPUTE lock (the
asserted ``module_sha`` must equal a fresh hash of the file on disk, bound through
the ONE attestation kernel, D5 lock 2) plus a tiered authorship floor (bare acks
refused; the sign-off must NAME the module). It is deliberately THINNER than the
section gate — a module has no per-section view/tier/diff-token bar; the
load-bearing property is the recompute lock over the file the dependents rest on.
"""

# MIRROR: hpc_agent/ops/decision/journal/signoff.py::_assert_signoff_authorship (the section sign-off record discipline) pinned-by tests/ops/notebook/test_wave3_modules.py::test_module_gate_lockstep_with_section_gate  # noqa: E501
from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput

from ._shared import (
    _fresh_human_texts,
    _is_bare_ack,
    _names_slug,
    _read_interview_actors,
    _refuse_missing_authorship,
)

_MODULE_SIGNOFF_BLOCK = "notebook-module-sign-off"


def _names_module(text: str, module: str) -> bool:
    """True iff *text* names the *module* — its full relpath OR its file stem.

    A human signs off "the engine module"; they may type the full relpath
    (``src/engine.py``) or just the stem (``engine``). Either token-exact naming
    satisfies the floor (a bare ack does not), mirroring the section gate's
    slug-naming bar over the coarser module identity.
    """
    if _names_slug(text, module):
        return True
    stem = Path(module).stem
    return bool(stem) and _names_slug(text, stem)


# MIRROR: hpc_agent/ops/decision/journal/signoff.py::_assert_signoff_authorship (the three locks, over a coarser subject) pinned-by tests/ops/notebook/test_wave3_modules.py::test_module_gate_lockstep_with_section_gate  # noqa: E501
def _assert_module_signoff_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """Human-authorship + recompute gate for a NOTEBOOK MODULE sign-off (piece 3).

    A module sign-off is an ordinary ``append-decision`` whose
    ``scope_kind=="notebook"``, ``block=="notebook-module-sign-off"``, and
    ``resolved={audit_id, module, module_sha, view_sha?}``. Enforced (mirrors the
    section sign-off's three locks over the coarser module subject):

    * **Block convention (both directions)** — a ``notebook-module-sign-off`` block
      is refused for any ``scope_kind`` other than ``notebook``; every other record
      passes untouched.
    * **Lock 2 (recompute, un-fakeable)** — the module file named by
      ``resolved['module']`` is read from disk, its ``sha256_normalized`` computed,
      and the record bound through the ONE attestation kernel
      (``state.attestation.bind``): the asserted ``module_sha`` must equal the
      recomputed one or the append is refused. A missing/unreadable module is
      REFUSED loudly (a sign-off that cannot be recomputed is never skipped).
    * **Lock 3 (authorship floor, tiered)** — bare acks are refused; the sign-off
      must NAME the module (full relpath or file stem, token-exact). With a harness
      utterance log present the naming leg runs over LOGGED HUMAN UTTERANCES (the
      agent-relayed ``response`` carries no authorship weight); absent a log the
      non-bare ``response`` is the friction tier (byte-identical to a v1 typed
      sign-off).

    Raises :class:`errors.SpecInvalid` on any refusal.
    """
    is_module_block = spec.block == _MODULE_SIGNOFF_BLOCK
    if is_module_block and spec.scope_kind != "notebook":
        raise errors.SpecInvalid(
            f"block {_MODULE_SIGNOFF_BLOCK!r} is only valid for scope_kind='notebook' "
            f"(a notebook module sign-off); got scope_kind={spec.scope_kind!r}."
        )
    if not (is_module_block and spec.scope_kind == "notebook"):
        return  # not a module sign-off — nothing to gate

    if not isinstance(resolved, dict):
        raise errors.SpecInvalid(
            "notebook module sign-off gate: resolved must carry {audit_id, module, module_sha}."
        )
    module = resolved.get("module")
    module_sha = resolved.get("module_sha")
    missing = [
        name
        for name, value in (("module", module), ("module_sha", module_sha))
        if not isinstance(value, str) or not value
    ]
    if missing:
        raise errors.SpecInvalid(
            "notebook module sign-off gate: resolved must carry non-empty "
            f"{{audit_id, module, module_sha}}; missing/empty: {missing}."
        )
    assert isinstance(module, str) and isinstance(module_sha, str)

    # Read the module file up front — it anchors the temporal filter (below) AND is
    # the recompute source (Lock 2). Refuses an unresolvable module loudly.
    from hpc_agent.state import attestation
    from hpc_agent.state.audit_source import sha256_normalized
    from hpc_agent.state.notebook_audit import MODULE_SUBJECT_KIND

    path = experiment_dir / module
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise errors.SpecInvalid(
            f"notebook module sign-off gate: module {module!r} is unreadable ({exc}). A "
            "module sign-off RECOMPUTES the module hash from the file on disk — an "
            "unresolvable module is refused, never skipped."
        ) from exc

    # Lock 3 (floor) — TIERED like the section sign-off gate, and TEMPORALLY BOUND
    # (finding 10): a human cannot review module content that did not yet exist, so
    # a candidate utterance must be logged at/after the module file's mtime (the
    # module's own "what the human saw" anchor, the render-mtime analogue). This
    # routes through the ONE shared ts>=anchor filter (:func:`_fresh_human_texts`),
    # never an unbounded utterance read (the B4 route-through contract). ``None`` —
    # no log, or an unattributed >1-actor session — falls to the friction tier over
    # the agent-relayed ``response`` (honestly weaker, byte-identical to v1).
    response = str(spec.response or "")
    actor_ids, _ = _read_interview_actors(experiment_dir)
    try:
        anchor: float | None = int(path.stat().st_mtime)
    except OSError:
        anchor = None
    harness_texts = _fresh_human_texts(experiment_dir, actor_ids=actor_ids, anchor=anchor)
    if harness_texts is None:
        if _is_bare_ack(response) or not _names_module(response, module):
            _refuse_missing_authorship(
                "notebook module sign-off gate: signing off a source module is a HUMAN "
                f"act — a bare ack cannot sign it. Name the module ({module!r}) and "
                "state that you reviewed it."
            )
    else:
        if not any(not _is_bare_ack(t) and _names_module(t, module) for t in harness_texts):
            _refuse_missing_authorship(
                "notebook module sign-off gate: no logged human utterance (fresh since the "
                f"module was written) NAMES the module {module!r} (its relpath or file "
                "stem). The human types the module sign-off in their own words; an "
                "agent-relayed response carries no authorship weight here."
            )

    attestation.bind(
        {
            "attestor": "human",
            "subject_kind": MODULE_SUBJECT_KIND,
            "subject_id": module,
            "content_sha": module_sha,
        },
        recompute=sha256_normalized(text),
    )
