"""Sandbox authorship seeding — plan-unit U2 of docs/plans/sandbox-proving-run-2026-07-18.md.

A sandbox proving run (rung 2 of the jurisdiction ladder) exercises the REAL
gates — never bypasses them — against a seeded, namespace-isolated substrate
(plan §3, the trust doctrine that must never bend). This module is the ONE
sanctioned seeder for that substrate, mirroring the conformance kit's fixture
posture (:mod:`hpc_agent.conformance.relay_fixtures` ``seed_triple`` /
:mod:`hpc_agent.conformance.fixture_repo` ``claim_fixture_repo``):

* :func:`seed_utterance` writes a human-utterance record into the SANDBOX
  utterance log — the exact locator the human-authorship gate reads
  (``state.utterances.utterances_path`` → ``<journal home>/<repo_hash>/
  utterances.jsonl``, consumed by the gate via
  ``ops/decision/journal/_shared.py::_harness_human_texts`` →
  ``read_utterances``). It is what lets the sandbox prove the authorship gate
  ACCEPTS a human-shaped utterance — and, paired with an un-seeded control,
  that it still REFUSES a fabricated value (a sandbox run proves the gates
  fire correctly; it never proves a human approved anything).
* :func:`seed_prior_signoff` appends a prior decision record through the REAL
  write path (``state.decision_journal.append_decision`` — the same call the
  conformance seeder uses), for scenarios that need an existing journal thread
  (e.g. the authorship gate's first-commit scan: a field already present in a
  prior record's ``resolved`` was gated when it was committed).

Provenance (plan §3): every seeded record carries
``{"seeded_by": "sandbox-proving", "run": <sandbox_run_ref>}``. The stamp is
ADDITIVE — for the utterance log it rides as two extra top-level keys (the
store's readers tolerate unknown keys by contract; the gate reads only
``text``/``ts``), for the decision journal it lands in the record's existing
caller-supplied ``provenance`` dict. No gate reads either; auditors do.

The structural guard (plan §3)
------------------------------
Every public function routes through :func:`assert_sandbox_journal_home`,
which REFUSES (raises :class:`SandboxSeedError`) when:

1. ``HPC_JOURNAL_DIR`` is unset or empty — the env var is the one explicit,
   visible sandbox pointer; the quieter fallbacks (the ``HPC_HOMEDIR`` module
   attribute, the ``~/.claude/hpc`` default) must never silently authorize a
   seed;
2. the env value resolves to — or anywhere inside — the production journal
   home ``~/.claude/hpc``;
3. the caller-declared *journal_home* resolves to — or inside — the
   production home (belt-and-braces: even a divergent env cannot launder a
   production target through the parameter);
4. *journal_home* and the env value disagree — the write resolves through
   ``state.run_record.current_homedir()`` (env is the authority), so a
   mismatch means the caller's declaration diverges from where the record
   will actually land: a loud bug, never a silent redirect.

That makes the helper structurally incapable of touching a production
namespace. It lives under ``tests/`` and is NOT shipped in the wheel.

Why ``seed_utterance`` does not call ``state.utterances.append_utterance``:
the frozen harness writer has no provenance channel (deliberately — the LLM
must never gain a sanctioned utterance write). The seeder therefore mirrors
that writer byte-for-byte (same locator, same ``{ts, sha256, text}`` schema,
sha256 over the FULL raw text, the same codepoint-boundary cap, sorted-keys
JSON, one record per appended line) plus the two additive provenance keys,
and refuses text the real writer would drop (empty, harness-injected) so a
seeded record is always one the capture hook COULD have written. The §3 guard
above stands in for the harness's out-of-band-ness.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from hpc_agent.infra.time import utcnow_iso
from hpc_agent.state.utterances import (
    MAX_UTTERANCE_BYTES,
    is_harness_injected,
    utterances_path,
)

__all__ = [
    "SANDBOX_SEEDED_BY",
    "SandboxSeedError",
    "assert_sandbox_journal_home",
    "seed_prior_signoff",
    "seed_utterance",
]

# The provenance discriminator every seeded record carries (plan §3). Auditors
# grep for it; no gate reads it.
SANDBOX_SEEDED_BY = "sandbox-proving"


class SandboxSeedError(RuntimeError):
    """A sandbox-seed guard refusal or an invalid seed request.

    Deliberately NOT :class:`hpc_agent.errors.SpecInvalid`: this helper is
    tests-tree fixture machinery, not a primitive — its refusals are sandbox
    setup bugs, never gate verdicts an envelope should carry.
    """


def _production_home() -> Path:
    """The production journal home the guard exists to protect."""
    return Path.home() / ".claude" / "hpc"


def _within(child: Path, parent: Path) -> bool:
    """True when resolved *child* equals — or lives anywhere under — *parent*.

    ``PurePath`` comparison is case-normalizing on Windows and case-sensitive
    on POSIX, so this matches each platform's own filesystem semantics.
    """
    resolved_child = child.resolve()
    resolved_parent = parent.resolve()
    return resolved_child == resolved_parent or resolved_parent in resolved_child.parents


def assert_sandbox_journal_home(journal_home: str | Path) -> Path:
    """The §3 structural guard: return the resolved sandbox home, or raise.

    Refuses (see the module docstring for the four conditions) so no seed can
    ever land in — or be redirected into — the production journal namespace.
    """
    env_val = os.environ.get("HPC_JOURNAL_DIR")
    if env_val is None or env_val == "":
        raise SandboxSeedError(
            "sandbox seeding requires HPC_JOURNAL_DIR to be set to the sandbox "
            "journal home; unset, state writes resolve through the HPC_HOMEDIR "
            "attribute or the ~/.claude/hpc production default — the seeder "
            "refuses to guess (plan §3)"
        )
    env_home = Path(env_val)
    production = _production_home()
    if _within(env_home, production):
        raise SandboxSeedError(
            f"HPC_JOURNAL_DIR resolves inside the production journal home "
            f"({production}): {env_home} — the seeder is structurally barred "
            "from a production namespace (plan §3)"
        )
    declared = Path(journal_home)
    if _within(declared, production):
        raise SandboxSeedError(
            f"journal_home resolves inside the production journal home "
            f"({production}): {declared} — refused (plan §3)"
        )
    if declared.resolve() != env_home.resolve():
        raise SandboxSeedError(
            f"journal_home ({declared.resolve()}) disagrees with "
            f"HPC_JOURNAL_DIR ({env_home.resolve()}): seeded records resolve "
            "through the env var, so the declaration must name the same "
            "sandbox home the write will actually use"
        )
    return env_home.resolve()


def _truncate_utf8(text: str, max_bytes: int) -> str:
    """Byte-for-byte mirror of the frozen writer's codepoint-boundary cap."""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _provenance(run_ref: str) -> dict[str, str]:
    """The additive §3 provenance stamp every seeded record carries."""
    return {"seeded_by": SANDBOX_SEEDED_BY, "run": run_ref}


def _require_run_ref(run_ref: str) -> None:
    """Refuse a blank sandbox run reference — the §3 stamp's audit trail needs it."""
    if not isinstance(run_ref, str) or not run_ref.strip():
        raise SandboxSeedError(
            "run_ref must be a non-empty string (the sandbox run reference the "
            "§3 provenance stamp records; auditors read it)"
        )


def seed_utterance(
    journal_home: str | Path,
    experiment_dir: str | Path,
    text: str,
    *,
    run_ref: str,
) -> dict[str, Any]:
    """Seed one human utterance into the SANDBOX namespace the gate reads.

    Claims *experiment_dir*'s journal namespace under the guarded sandbox home
    (``state.run_record.journal_dir`` — the REAL claim, the conformance
    ``claim_fixture_repo`` idiom), then appends one record to
    ``<sandbox home>/<repo_hash>/utterances.jsonl`` — the exact file
    ``read_utterances(experiment_dir)`` (and thus the human-authorship gate's
    harness-captured tier) reads. The record mirrors the frozen harness writer
    (``{ts, sha256, text}``, sha256 over the FULL raw text, text capped at
    ``MAX_UTTERANCE_BYTES``) plus the additive §3 provenance keys
    ``seeded_by`` / ``run``.

    Refuses (never silently no-ops — a seeder that doesn't write would make
    the sandbox vacuous): the §3 guard, an empty/blank *text* or *run_ref*,
    and harness-injected *text* (``is_harness_injected`` — a record the real
    capture hook would have dropped is not a human utterance).

    Returns the record written.
    """
    assert_sandbox_journal_home(journal_home)
    _require_run_ref(run_ref)
    if not isinstance(text, str) or not text.strip():
        raise SandboxSeedError("text must be a non-empty human utterance")
    if is_harness_injected(text):
        raise SandboxSeedError(
            "text opens with a harness-injection tag: the real capture hook "
            "would drop it, so it is not a human utterance the sandbox may seed"
        )

    exp = Path(experiment_dir)
    exp.mkdir(parents=True, exist_ok=True)

    from hpc_agent.state.run_record import journal_dir

    journal_dir(exp)  # the REAL namespace claim: mkdir + repo.json + runs/

    record: dict[str, Any] = {
        "ts": utcnow_iso(),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text": _truncate_utf8(text, MAX_UTTERANCE_BYTES),
        **_provenance(run_ref),
    }
    path = utterances_path(exp)  # the ONE locator — never a re-derived hash
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    return record


def seed_prior_signoff(
    journal_home: str | Path,
    experiment_dir: str | Path,
    *,
    run_ref: str,
    scope_id: str,
    block: str,
    scope_kind: str = "run",
    response: str = "y",
    resolved: dict[str, Any] | None = None,
    evidence_digest: str | dict[str, Any] | None = None,
    proposal: str | list[Any] | dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Seed a prior decision record through the REAL decision-journal write.

    Appends via ``state.decision_journal.append_decision`` — the same state
    write the conformance ``seed_triple`` uses — so the seeded record is
    byte-identical in shape to anything the ops layer commits (schema_version,
    auto-stamped ts, the full §2 field set), and the authorship gate's
    prior-record scan reads it exactly like a real one. The §3 provenance
    stamp lands in the record's caller-supplied ``provenance`` dict.

    Note the decision journal is EXPERIMENT-DIR-relative
    (``<experiment_dir>/.hpc/...``), not journal-home-relative — the guard
    still runs: it pins the sandbox posture every sandbox state write
    requires (HPC_JOURNAL_DIR set and provably non-production).

    The ops-layer gates are deliberately NOT invoked here — seeding is
    substrate. The gates fire for real on the sandbox run's OWN subsequent
    appends; that is the point of rung 2.

    Returns the record written.
    """
    assert_sandbox_journal_home(journal_home)
    _require_run_ref(run_ref)

    from hpc_agent.state.decision_journal import append_decision

    return append_decision(
        Path(experiment_dir),
        scope_kind=scope_kind,
        scope_id=scope_id,
        block=block,
        response=response,
        evidence_digest=evidence_digest,
        proposal=proposal,
        resolved=resolved,
        provenance=_provenance(run_ref),
    )
