"""The reproduction-verdict admission gate (T12) — a fingerprint sample joins the
determinism envelope only through a human-authored ``reproduction-verdict``."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput

from ._shared import (
    _HEX_RUN_RE,
    _is_bare_ack,
    _refuse_missing_authorship,
    _registration_authored_text,
)

# ── reproduction-verdict authorship gate (D-consume admission, T12) ───────────


def _match_ledger_sha_prefix(
    authored: str, candidate_shas: set[str]
) -> tuple[str | None, str | None]:
    """Match the 8+ hex prefixes in *authored* against *candidate_shas*.

    Returns ``(full_sha, ambiguity)``:

    * a UNIQUE match → ``(full_sha, None)`` (the full bind-locked sha the store
      join keys on);
    * NO match → ``(None, None)`` — the caller distinguishes "no prefix named at
      all" from "a prefix matched nothing" by re-testing :data:`_HEX_RUN_RE`;
    * an AMBIGUOUS match → ``(None, reason)`` naming the COUNT, never the shas: the
      count is the disclosure a human needs to narrow the prefix; printing the
      colliding shas would hand back the very evidence the naming bar demands they
      quote.

    Reuses the R6 sha-prefix vocabulary (:data:`_HEX_RUN_RE`, 8+ hex) — an 8-hex
    prefix exists nowhere in a human's prior vocabulary and can only derive from
    the presented reproduction evidence.
    """
    runs = [m.group(0).lower() for m in _HEX_RUN_RE.finditer(authored or "")]
    lowered = {s.lower(): s for s in candidate_shas}
    matched: set[str] = set()
    for run in runs:
        hits = {orig for low, orig in lowered.items() if low.startswith(run)}
        if len(hits) > 1:
            return None, (
                f"the named 8-hex prefix {run!r} matches {len(hits)} distinct ledger "
                "samples (ambiguous) — quote a LONGER prefix that names exactly one sample"
            )
        matched |= hits
    if not matched:
        return None, None
    if len(matched) > 1:
        return None, (
            f"the response names {len(matched)} distinct ledger samples by prefix — a "
            "reproduction verdict resolves exactly ONE sample; name a single content_sha"
        )
    return matched.pop(), None


def _assert_reproduction_verdict_authorship(
    experiment_dir: Path, spec: AppendDecisionInput, resolved: dict[str, Any] | None
) -> None:
    """D-consume admission gate for a fingerprint-sample acceptance/rejection (T12).

    A ``needs_verdict`` / ``mismatch`` fingerprint sample joins the determinism
    envelope ONLY when the reproduction run's decision journal carries a
    ``reproduction-verdict`` record whose ``resolved`` names the sample's
    ``content_sha`` TOKEN-EXACT with ``accept: true`` (the store-layer admission
    join, ``state/fingerprint_store.py::_is_admitted``). Without a gate an AGENT
    could append that acceptance and launder a mismatch into the envelope — the
    accumulation attack the D-consume admission rule exists to close. This gate is
    that lock, beside :func:`_assert_signoff_authorship` /
    :func:`_assert_registration_authorship`, same three-lock structure.

    Block convention (one direction — the run scope legitimately carries MANY
    blocks, so it is not made exclusive like ``registration``): the
    ``reproduction-verdict`` block is refused for any ``scope_kind`` other than
    ``"run"`` (it rides the reproduction run's journal); nothing else claims the
    block, so every other record passes untouched.

    ``resolved['accept']`` must be a real bool — an acceptance (``true``, the join
    predicate the store reads) AND a rejection (``false``) both face the full bar
    (a reject is a human judgment too, no cheaper path).

    Authorship bar, tiered exactly like the registration sha-prefix leg
    (:func:`_registration_authored_text` — the harness utterance log LOCK when
    present, the agent-relayed ``response`` FRICTION tier otherwise; NO waiver /
    auto-clear tier):

    * a bare ack (:func:`_is_bare_ack`) cannot resolve a verdict, and
    * the authored text must NAME the accepted sample's ``content_sha`` by an 8+
      hex prefix (the R6 form).

    Recompute leg: the gate re-reads THIS run's fingerprint ledger
    (``state/fingerprint_store.py::read_samples`` for the ``cmd_sha`` resolved from
    the run sidecar — the journal ``scope_id`` IS the reproduction run id) and
    refuses a prefix that matches nothing (``acceptance naming no sample``) or that
    matches ambiguously (refused naming the COUNT). Candidate shas are the samples
    whose SECOND ``run_ids`` member is this run — exactly the samples this verdict
    can admit.

    Prefix canonicalization (the store-join enabler): on a unique match the gate
    REWRITES ``resolved['content_sha']`` to the FULL matched sha before append, so
    the store-layer join (``resolved.content_sha == sample.content_sha``, token-exact
    on the full sha) admits. A pre-filled ``resolved['content_sha']`` that does not
    extend the named sample is a structural inconsistency, refused.

    Marking (the E2 scoping): the authorship-bar refusals (bare ack, no/ambiguous/
    unmatched prefix) carry the elicitation marker via
    :func:`_refuse_missing_authorship` — a freshly typed human utterance naming the
    right prefix resolves them. The STRUCTURAL refusals (wrong scope kind,
    non-bool ``accept``, an unresolvable sidecar / cmd_sha, a contradicting
    pre-filled ``content_sha``) raise plain :class:`errors.SpecInvalid` UNMARKED —
    a re-elicit cannot fix them.
    """
    from hpc_agent.state.fingerprint_store import REPRODUCTION_VERDICT_BLOCK, read_samples
    from hpc_agent.state.runs import read_run_sidecar

    if spec.block != REPRODUCTION_VERDICT_BLOCK:
        return  # nothing else claims this block

    # Block↔scope convention: the verdict rides the reproduction RUN's journal.
    if spec.scope_kind != "run":
        raise errors.SpecInvalid(
            f"block {REPRODUCTION_VERDICT_BLOCK!r} is only valid for scope_kind='run' "
            f"(it rides the reproduction run's decision journal); got "
            f"scope_kind={spec.scope_kind!r}."
        )

    if not isinstance(resolved, dict):
        raise errors.SpecInvalid(
            "reproduction-verdict gate: resolved must be a mapping carrying "
            "{accept: bool, content_sha}."
        )

    # accept is the store's join predicate (`accept is True`) — it MUST be a real
    # bool. A rejection (false) faces the same authorship bar as an acceptance.
    accept = resolved.get("accept")
    if not isinstance(accept, bool):
        raise errors.SpecInvalid(
            "reproduction-verdict gate: resolved['accept'] must be a bool (true admits "
            "the sample into the determinism envelope, false records the rejection); "
            f"got {accept!r}."
        )

    # Authorship floor: a bare ack cannot resolve a needs_verdict / mismatch — the
    # admission is deliberately effortful (the D-attention rarity-plus-typing bet).
    response = str(spec.response or "")
    if _is_bare_ack(response):
        _refuse_missing_authorship(
            "reproduction-verdict gate: admitting (or rejecting) a reproduction sample "
            f"is a HUMAN act — a bare {spec.response!r} (a 'y' / click) cannot resolve it. "
            "Name the sample's content_sha (an 8+ hex prefix) and state your verdict."
        )

    # Recompute leg: re-read THIS run's fingerprint ledger. cmd_sha comes from the
    # run sidecar (the scope_id IS the reproduction run id); an unresolvable sidecar
    # / cmd_sha is a STRUCTURAL refusal (a re-elicit cannot conjure the ledger).
    try:
        sidecar = read_run_sidecar(experiment_dir, spec.scope_id)
    except (FileNotFoundError, OSError, ValueError) as exc:
        raise errors.SpecInvalid(
            "reproduction-verdict gate: could not read the reproduction run's sidecar "
            f"for run {spec.scope_id!r} ({exc}) — the gate resolves cmd_sha from it to "
            "re-read the fingerprint ledger. An unresolvable run is refused, never skipped."
        ) from exc
    cmd_sha = str(sidecar.get("cmd_sha") or "")
    if not cmd_sha:
        raise errors.SpecInvalid(
            "reproduction-verdict gate: the run sidecar for "
            f"{spec.scope_id!r} carries no cmd_sha, so the fingerprint ledger cannot be "
            "located to verify the named sample."
        )

    samples, _skipped = read_samples(experiment_dir, cmd_sha)
    candidate_shas: set[str] = set()
    for sample in samples:
        run_ids = sample.get("run_ids")
        if not isinstance(run_ids, (list, tuple)) or len(run_ids) < 2:
            continue
        if run_ids[1] != spec.scope_id:  # only samples THIS verdict can admit
            continue
        content_sha = sample.get("content_sha")
        if isinstance(content_sha, str) and content_sha:
            candidate_shas.add(content_sha)

    # Tiered evidence source (utterance-log LOCK > journal-response FRICTION; NO
    # waiver tier) — the shared registration sha-prefix tiering.
    authored = _registration_authored_text(experiment_dir, response)
    full_sha, ambiguity = _match_ledger_sha_prefix(authored, candidate_shas)
    if ambiguity is not None:
        _refuse_missing_authorship("reproduction-verdict gate: " + ambiguity + ".")
    if full_sha is None:
        if _HEX_RUN_RE.search(authored):
            _refuse_missing_authorship(
                "reproduction-verdict gate: the response names an 8+ hex prefix that "
                f"matches NO sample in run {spec.scope_id!r}'s fingerprint ledger. A "
                "verdict must name the content_sha of a sample that EXISTS on record — "
                "quote the prefix from the reproduction receipt / evidence brief."
            )
        _refuse_missing_authorship(
            "reproduction-verdict gate: the response must NAME the accepted sample's "
            "content_sha by an 8+ hex-character prefix (the R6 form — a token that can "
            "only derive from the presented evidence). Quote the sample's content_sha "
            "prefix from the reproduction receipt."
        )

    # Prefix canonicalization: rewrite resolved['content_sha'] to the FULL matched
    # sha so the store-layer admission join (resolved.content_sha == sample.content_sha,
    # token-exact on the full bind-locked sha) admits. A pre-filled content_sha that
    # does not extend the named sample is a structural inconsistency, refused.
    existing = resolved.get("content_sha")
    if isinstance(existing, str) and existing and not full_sha.lower().startswith(existing.lower()):
        raise errors.SpecInvalid(
            "reproduction-verdict gate: resolved['content_sha'] "
            f"({existing!r}) does not extend the sample named by the response "
            f"(content_sha {full_sha!r}). Do not hand-commit a content_sha that "
            "disagrees with the named prefix; name the prefix and let the gate "
            "canonicalize it to the full sha."
        )
    resolved["content_sha"] = full_sha
