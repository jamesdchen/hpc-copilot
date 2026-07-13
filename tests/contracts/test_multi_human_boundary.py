"""Multi-human boundary contracts (``docs/design/multi-human.md``).

The enforcement rows mechanized (MT10). Multi-human makes today's IMPLICIT
single-actor identity EXPLICIT — an opaque, harness-asserted slug core COMPARES,
never verifies — and every load-bearing line no lint can hold lives here:

* **Attributed ≠ verified** — no actor-touching module reaches an
  identity-verification path (``getpass`` / ``pwd`` / ``crypt`` / ``ssl`` /
  signature-verify). The tier is harness-asserted, stated, never overclaimed.
* **The actor is never caller-suppliable** — no gated INPUT spec carries an
  ``actor`` / ``attestor_id`` field; the session actor is server-resolved only,
  and the ONE env seam is ``infra/env_flags.py::env_actor`` (the sole reader of
  ``HPC_ACTOR``).
* **The utterance write API stays frozen per file** — attributed capture adds a
  LOCATOR, never a record field: every ``utterances[.<actor>].jsonl`` line is
  exactly ``{ts, sha256, text}``.
* **``attestor_id`` is additive + kernel-validated** — optional on the ONE kernel;
  ``reduce`` never keys on it (drift is identity-of-subject, not -of-attestor).
* **Byte-identical single-actor** — every comparison / policy read / stamp is
  guarded by the ``len(ids) > 1`` census (source-level pin); zero declared actors
  is today's system.
* **No role vocabulary** — actor slugs are opaque; no wire field / fixture names a
  role (``pi`` / ``advisor`` / ``postdoc`` / ``student`` / ``reviewer`` …); toy
  fixtures use ``alice`` / ``bob``.
* **Route-through the ONE kernel** — draft records + the reviewer≠author reduction
  bind/reduce via ``state/attestation.py``, never a re-inlined newest-first.

TOY VOCABULARY ONLY: alice / bob.
"""

from __future__ import annotations

import inspect
import pathlib
import re

# ── the actor-touching module set (one list, reused by the AST/source scans) ──


def _hpc_src_root() -> pathlib.Path:
    import hpc_agent

    return pathlib.Path(inspect.getfile(hpc_agent)).parent


_ACTOR_MODULES = (
    "infra/env_flags.py",
    "ops/decision/journal.py",
    "ops/notebook/draft_op.py",
    "state/utterances.py",
    "state/notebook_audit.py",
    "state/attestation.py",
    "_wire/actions/interview.py",
    "_wire/actions/notebook_draft.py",
    "_kernel/hooks/utterance_capture.py",
    "_kernel/hooks/answer_capture.py",
)


def _read(rel: str) -> str:
    return (_hpc_src_root() / rel).read_text(encoding="utf-8")


# ── attributed ≠ verified: no identity-verification path ──────────────────────


def test_no_auth_verification_import_in_actor_modules() -> None:
    """No actor-touching module imports an OS-identity / credential / signature
    verifier — the attribution is harness-asserted, NEVER verified (the honesty
    pin). ``hashlib`` (content hashing) is explicitly permitted; identity is not
    a thing core checks.
    """
    banned_imports = ("getpass", "pwd", "spwd", "crypt", "ssl", "cryptography", "nacl", "hmac")
    offenders: list[str] = []
    for rel in _ACTOR_MODULES:
        for lineno, line in enumerate(_read(rel).splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                for mod in banned_imports:
                    if re.search(rf"\b{mod}\b", stripped):
                        offenders.append(f"{rel}:{lineno}: {stripped}")
    assert not offenders, f"an identity-verification import reached core: {offenders}"


def test_no_signature_verify_call_in_actor_modules() -> None:
    """No actor-touching module calls a signature/credential VERIFIER — attribution
    is compared by opaque identity, never cryptographically checked."""
    banned_calls = ("verify_signature", ".verify(", "check_password", "authenticate(")
    offenders: list[str] = []
    for rel in _ACTOR_MODULES:
        text = _read(rel)
        for call in banned_calls:
            if call in text:
                offenders.append(f"{rel}: {call}")
    assert not offenders, f"a verification call reached core: {offenders}"


# ── the actor is never caller-suppliable (server-resolved only) ───────────────


def test_no_actor_field_on_gated_input_specs() -> None:
    """No gated INPUT spec exposes an ``actor`` / ``attestor_id`` field — the model
    must not choose its identity. The append-decision + notebook-draft specs are
    the mutate surfaces; the session actor is resolved SERVER-SIDE.
    """
    from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
    from hpc_agent._wire.actions.notebook_draft import NotebookDraftSpec

    banned = {"actor", "attestor_id", "attestor", "actor_id", "signer"}
    for model in (AppendDecisionInput, NotebookDraftSpec):
        fields = set(model.model_fields)
        leak = fields & banned
        assert not leak, f"{model.__name__} exposes a caller-suppliable actor field: {leak}"


def test_attestor_id_is_output_only_on_decision_record() -> None:
    """``attestor_id`` rides the OUTPUT record (server-computed), never the input."""
    from hpc_agent._wire.actions.decision_journal import AppendDecisionInput, DecisionRecord

    assert "attestor_id" in DecisionRecord.model_fields  # surfaced on the read-back
    assert "attestor_id" not in AppendDecisionInput.model_fields  # never an input


def test_gate_binds_attestor_id_to_the_server_resolved_actor() -> None:
    """The ONE append site stamps ``attestor_id`` from the server-resolved
    ``_session_actor`` result, and the gate never reads an actor OFF the spec."""
    from hpc_agent.ops.decision import journal

    src = inspect.getsource(journal.append_decision)
    # The stamp is the resolved variable, bound from _session_actor.
    assert "_session_actor(" in src
    assert "attestor_id=attestor_id" in src
    # The gate never trusts a spec-side actor field (there is none to read).
    module_src = inspect.getsource(journal)
    assert "spec.attestor_id" not in module_src
    assert "spec.actor" not in module_src


def test_env_actor_is_the_sole_reader_of_hpc_actor() -> None:
    """``HPC_ACTOR`` is read from the process env in exactly ONE place —
    ``infra/env_flags.py::env_actor`` — so the out-of-model channel has one seam;
    every other consumer routes through ``env_actor()`` (docstrings/messages
    naming the var are not env reads)."""
    root = _hpc_src_root()
    offenders: list[str] = []
    for py in root.rglob("*.py"):
        rel = py.relative_to(root).as_posix()
        if rel == "infra/env_flags.py":
            continue
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), start=1):
            if "os.environ" in line and "HPC_ACTOR" in line:
                offenders.append(f"{rel}:{lineno}")
    assert not offenders, f"HPC_ACTOR read outside env_actor: {offenders}"
    # And env_actor genuinely reads it.
    assert "os.environ.get(var" in inspect.getsource(
        __import__("hpc_agent.infra.env_flags", fromlist=["env_actor"]).env_actor
    )


# ── the utterance write API stays frozen per file (locator, not field) ────────


def test_suffixed_utterance_file_holds_the_frozen_three_fields(tmp_path: object) -> None:
    """An actor-suffixed utterance log carries the SAME frozen ``{ts, sha256,
    text}`` record — attribution rides the LOCATOR, never a fourth field."""
    import json
    from pathlib import Path

    from hpc_agent.state.run_record import journal_dir
    from hpc_agent.state.utterances import append_utterance, utterances_path

    exp = Path(str(tmp_path))
    journal_dir(exp)  # scaffold the namespace (the no-scaffold rule)
    append_utterance(exp, "alice typed this", actor="alice")
    path = utterances_path(exp, "alice")
    assert path.name == "utterances.alice.jsonl"
    (line,) = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln]
    assert set(json.loads(line)) == {"ts", "sha256", "text"}


def test_actor_scoped_read_excludes_the_anonymous_log(tmp_path: object) -> None:
    """An actor-scoped ``read_utterances`` NEVER returns unsuffixed-log records —
    anonymous text must not satisfy an actor-specific evidence check."""
    from pathlib import Path

    from hpc_agent.state.run_record import journal_dir
    from hpc_agent.state.utterances import append_utterance, read_utterances

    exp = Path(str(tmp_path))
    journal_dir(exp)
    append_utterance(exp, "anonymous text", actor=None)
    append_utterance(exp, "alice text", actor="alice")
    scoped = read_utterances(exp, actor="alice")
    texts = {r.get("text") for r in scoped}
    assert texts == {"alice text"}
    assert "anonymous text" not in texts


# ── attestor_id: additive + kernel-validated, reduce never keys on it ─────────


def test_attestor_id_optional_and_empty_refused() -> None:
    """``attestor_id`` is optional (absent → today's single-actor record) and,
    when present, validated like ``view_sha`` (a present-but-empty is refused)."""
    from hpc_agent import errors
    from hpc_agent.state import attestation

    base = {"attestor": "human", "subject_kind": "k", "subject_id": "s", "content_sha": "a" * 8}
    assert attestation.validate(base).attestor_id is None  # absent → byte-compatible
    assert attestation.validate({**base, "attestor_id": "alice"}).attestor_id == "alice"  # opaque
    import pytest

    with pytest.raises((errors.SpecInvalid, ValueError)):
        attestation.validate({**base, "attestor_id": ""})  # present-but-empty refused


def test_reduce_never_keys_on_attestor_id() -> None:
    """``reduce`` is drift-of-SUBJECT, never -of-attestor — it must not branch on
    ``attestor_id`` (a re-bound attestor revokes nothing)."""
    from hpc_agent.state import attestation

    assert "attestor_id" not in inspect.getsource(attestation.reduce)


# ── byte-identical single-actor: the census guard (source pin) ────────────────


def test_every_actor_comparison_is_census_guarded() -> None:
    """Every multi-human comparison / policy read / evidence-scoping helper in the
    gate module is guarded by the ``len(ids) > 1`` (or ``<= 1``) census — zero/one
    declared actor is byte-identical to today (no refusal, no stamp, no read)."""
    from hpc_agent.ops.decision import journal

    for func in (
        journal._assert_signoff_reviewer_not_author,
        journal._assert_actor_policy,
        journal._actor_scoped_human_texts,
        journal._assert_challenge_verdict_authorship,
    ):
        src = inspect.getsource(func)
        assert "len(ids)" in src, f"{func.__name__} lacks the len(ids) census guard"


def test_gate_never_hardcodes_a_named_actor() -> None:
    """No fixture actor slug (``alice`` / ``bob``) is hardcoded in the core gate —
    identity comparisons are variable-to-variable over opaque slugs, never a
    named-actor special case (a named branch would be a vocabulary)."""
    from hpc_agent.ops.decision import journal

    module_src = inspect.getsource(journal)
    for slug in ("alice", "bob", "carol"):
        assert f'"{slug}"' not in module_src and f"'{slug}'" not in module_src, (
            f"a named actor {slug!r} is hardcoded in the gate module"
        )


# ── no role vocabulary (wire + fixtures) ──────────────────────────────────────

_ROLE_WORDS = ("advisor", "supervisor", "postdoc", "professor", "student", "reviewer_role")


def test_no_role_vocabulary_in_actor_wire_model() -> None:
    """The ``actors`` wire block names NO role — slugs are opaque; ``ids`` /
    ``policy`` are the only fields, and no role word is a field name."""
    from hpc_agent._wire.actions.interview import ActorsBlock

    fields = set(ActorsBlock.model_fields)
    assert fields == {"ids", "policy"}
    for role in _ROLE_WORDS:
        assert not any(role in f for f in fields)


def test_no_role_vocabulary_in_multi_human_fixtures() -> None:
    """The MT7 gate fixtures use toy slugs only — no role word as a fixture token
    (rule-statement lines that NAME the boundary are exempt)."""
    test_root = pathlib.Path(__file__).parent.parent
    fixture = test_root / "ops" / "decision" / "test_multi_human_gate.py"
    offenders: list[str] = []
    for lineno, line in enumerate(fixture.read_text(encoding="utf-8").splitlines(), start=1):
        low = line.lower()
        if "never" in low or "role" in low:  # a boundary-naming line, not a crossing
            continue
        for role in _ROLE_WORDS:
            if role in low:
                offenders.append(f"{fixture.name}:{lineno}: {role}")
    assert not offenders, f"a role word landed in a multi-human fixture: {offenders}"


# ── route-through the ONE kernel (draft records + reviewer≠author reduction) ──


def test_draft_records_route_through_the_kernel() -> None:
    """The draft attestation binds via ``attestation.bind`` and the author is read
    via ``attestation.reduce`` (the ONE reducer) — never a re-inlined newest-first
    or a bare ``content_sha ==`` compare (the accruing-member rule)."""
    from hpc_agent.state import notebook_audit as nb

    assert "attestation.bind(" in inspect.getsource(nb.record_draft)
    read_src = inspect.getsource(nb.read_draft_author)
    assert "reduce(" in read_src and "_newest_valid(" in read_src


def test_notebook_draft_writes_no_utterance_file() -> None:
    """The notebook-draft verb writes a JOURNAL record, never an utterance file —
    the LLM never gains an utterance-writing affordance (the suffixed files
    included)."""
    from hpc_agent.ops.notebook import draft_op

    src = inspect.getsource(draft_op)
    assert "append_utterance" not in src
    assert "utterances_path" not in src
