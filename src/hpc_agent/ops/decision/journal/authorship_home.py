"""The authorship-home grant gate (docs/design/dev-mode-authorship.md, leg b).

The dev-mode cross-repo opt-in is a JOURNALED, human-authored decision record
in the SECOND repo — never a silent config flag (a trust delegation with no
journal record is invisible consent, and every config surface is
agent-writable, so a flag would be self-minted trust): ``scope_kind="scope"``,
``scope_id="authorship-home"`` (a code-reserved slug), the code-owned block
``authorship-home``, ``resolved.action ∈ {"grant", "revoke"}``. The journal is
repo-local and append-only; state resolution is newest-wins (the scope-lock
state machine verbatim). A grant declares "trust the utterance log of named
home repo H" and widens the value-derivation gate's evidence pool
(:func:`hpc_agent.ops.decision.journal._shared._authorship_evidence_texts`).
Revocation is the safe direction (it only narrows the pool) and carries no
bar — the lock/unlock asymmetry.

A grant append carries no REQUIRED_CALLER field, so the value-derivation gate
never fires on it — it gets its own authorship leg, the BOOTSTRAP rule (the
naming-gate family: ``_names_target_sha_prefix`` / scope-unlock precedent):

* the human's grant utterance — non-bare, naming the 12-hex
  ``home_repo_hash`` as a whole token — must exist in the HOME namespace's
  utterance log. Naming the hash proves engagement with the home's presented
  identity (the vocabulary-impossibility class the sha-prefix FILING gates
  rely on); requiring it in the HOME log proves presence in the namespace
  being delegated — a second-repo utterance cannot show it.
* harness tier ONLY: a journal ``response`` carries NO weight for a grant —
  an agent-relayed "the human says trust <hash>" is self-minted trust (the
  overnight-consent bound ruling's tightening, 2026-07-12).
* the agent CAN compute the hash into ``resolved`` — fine; it cannot WRITE
  the home-log utterance (there is deliberately no utterance-writing verb,
  pinned by the contracts suite), so the bootstrap cannot be self-satisfied.

Every refusal here is a PLAIN :class:`errors.SpecInvalid` — NEVER the E2
``authorship_evidence: missing`` marker. This is the one place the marker
family deliberately breaks the convention, and the reason is load-bearing:
the marker drives the MCP elicitation retry, and a re-elicited utterance is
captured into the CURRENT session's namespace (the second repo) — the wrong
log, a guaranteed-failing round-trip, exactly what
``_refuse_missing_authorship``'s docstring reserves the marker against. The
refusal message instead directs the human: "in a session whose cwd is the
home repo, state: trust this repo's utterance log <hash>".
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput

from ._shared import (
    _AUTHORSHIP_HOME_ACTIONS,
    _AUTHORSHIP_HOME_BLOCK,
    _AUTHORSHIP_HOME_SCOPE,
    _actor_scoped_human_texts,
    _is_bare_ack,
    _names_repo_hash,
    _read_interview_actors,
)


def _assert_authorship_home_grant(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """Human-authorship gate for an authorship-home GRANT (dev-mode leg b).

    Block convention, enforced both directions (the scope-unlock precedent):

    * an ``authorship-home`` block is refused for any ``scope_kind`` other
      than ``scope``, and
    * a grant/revoke action in the ``authorship-home`` scope MUST ride the
      code-owned ``authorship-home`` block — a laundered record cannot hide
      under another block.

    A REVOKE (or any non-grant action) never reaches the bar: revocation only
    narrows the evidence pool, the safe direction.

    The GRANT bar, in order — every refusal a plain ``SpecInvalid`` with NO E2
    marker (see the module docstring):

    1. **structural** — ``resolved.home_experiment_dir`` must be a path that
       resolves to an existing directory, ``resolved.home_repo_hash`` must
       recompute from it (:func:`hpc_agent.state.run_record.repo_hash`), and
       the home journal namespace must already exist (the home must already
       be an hpc-agent repo — the store's no-scaffold posture, mirrored).
    2. **bootstrap naming** — a non-bare logged human utterance naming the
       12-hex ``home_repo_hash`` as a whole token must exist in the HOME
       namespace's utterance log, read with the SAME actor scoping as the
       match-time home read (under >1 declared actors, the session actor's
       suffixed home log only; an unattributed session reads nothing and the
       naming check fails). Journal ``response`` text carries no weight.

    Raises :class:`errors.SpecInvalid`.
    """
    is_home_block = spec.block == _AUTHORSHIP_HOME_BLOCK
    action = resolved.get("action") if isinstance(resolved, dict) else None

    # The authorship-home block is a scope-only convention.
    if is_home_block and spec.scope_kind != "scope":
        raise errors.SpecInvalid(
            f"block {_AUTHORSHIP_HOME_BLOCK!r} is only valid for scope_kind='scope' "
            f"(the dev-mode authorship-home grant journal); got scope_kind="
            f"{spec.scope_kind!r}."
        )
    if spec.scope_kind != "scope" or spec.scope_id != _AUTHORSHIP_HOME_SCOPE:
        return  # another scope's journal — nothing to gate
    if action in _AUTHORSHIP_HOME_ACTIONS and not is_home_block:
        raise errors.SpecInvalid(
            "authorship-home grant gate: an authorship-home grant/revoke "
            f"(resolved.action={action!r}) must be journaled with block="
            f"{_AUTHORSHIP_HOME_BLOCK!r}, not {spec.block!r} — the distinct "
            "code-owned block is how the reader recognises the record (the "
            "scope-lock convention)."
        )
    if action != "grant":
        return  # revocation is the safe direction — no authorship bar
    if not isinstance(resolved, dict):  # unreachable (action came from it) — mypy
        return

    # ── structural legs: the home path must resolve, the hash must recompute,
    # and the home namespace must exist (dev-mode-authorship.md: a wrong
    # path/hash is SpecInvalid WITHOUT the E2 marker).
    from hpc_agent.state.run_record import journal_root_if_exists, repo_hash

    home_raw = resolved.get("home_experiment_dir")
    home_hash = resolved.get("home_repo_hash")
    if not isinstance(home_raw, str) or not home_raw:
        raise errors.SpecInvalid(
            "authorship-home grant gate: a grant requires "
            "resolved.home_experiment_dir naming the home repo's directory "
            "(the repo whose utterance log this repo opts into trusting)."
        )
    home_path = Path(home_raw)
    if not home_path.is_dir():
        raise errors.SpecInvalid(
            f"authorship-home grant gate: home_experiment_dir {home_raw!r} does "
            "not resolve to an existing directory — the grant is structural: "
            "the home must already be an hpc-agent repo (the store's "
            "no-scaffold posture, mirrored)."
        )
    if not isinstance(home_hash, str) or repo_hash(home_path) != home_hash:
        raise errors.SpecInvalid(
            "authorship-home grant gate: resolved.home_repo_hash "
            f"{home_hash!r} does not recompute from home_experiment_dir "
            f"{home_raw!r} (repo_hash gives {repo_hash(home_path)!r}) — the "
            "hash is computed from the resolved home path, never hand-set."
        )
    if not journal_root_if_exists(home_path).is_dir():
        raise errors.SpecInvalid(
            f"authorship-home grant gate: the home namespace {home_hash} does "
            "not exist in the journal home — the home must already be an "
            "hpc-agent repo (the store's no-scaffold posture, mirrored: an "
            "utterance log lands only in an EXISTING namespace)."
        )

    # ── the bootstrap naming leg (harness tier ONLY): a non-bare utterance
    # naming the 12-hex home_repo_hash as a whole token, in the HOME log —
    # the one place the model has no write path. A journal response naming
    # the hash does NOT grant: presence in the home is what attests.
    _actor_ids, _ = _read_interview_actors(experiment_dir)
    home_texts = _actor_scoped_human_texts(home_path, _actor_ids) or []
    if not any(not _is_bare_ack(text) and _names_repo_hash(text, home_hash) for text in home_texts):
        raise errors.SpecInvalid(
            "authorship-home grant gate (dev-mode bootstrap): a grant must be "
            "authored from the HOME log — no logged human utterance in home "
            f"namespace {home_hash} names that hash as a whole token, and a "
            "journal response carries no weight for a grant (an agent-relayed "
            "'the human says trust <hash>' is self-minted trust; harness tier "
            "only). In a session whose cwd is the home repo, state: trust "
            f"this repo's utterance log {home_hash}"
        )
