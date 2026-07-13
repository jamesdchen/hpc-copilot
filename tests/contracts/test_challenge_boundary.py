"""Challenge-attestation boundary contracts (``docs/design/challenge-attestation.md``).

The enforcement rows mechanized (T9). Structured dissent is a standing, sha-bound
HUMAN record; these pins hold the load-bearing lines no lint can:

* **No agent-authored dissent** — no challenge/contest/dispute/refute verb in the
  mutate registry (Lock 1); no code path hand-writes a challenge-family block (C3).
* **``contested`` is orthogonal, never a status** — it lives in NO status
  vocabulary; a ``current`` target can be ``contested`` (C-status).
* **Route-through the ONE kernel + collectors** — the reduction binds/reduces via
  ``state/attestation.py``; the gate verifies target + citations server-side; every
  disclosure seat routes through ``standing_challenges`` (the D5 form).
* **Closed vocabularies pinned equal** — the wire status literal == the state
  reduction's five statuses; the wire ``CitationKind`` == ``CITATION_KINDS``.
* **Toy-domain fixtures only** — the challenge tests name widgets, never a real
  domain's words.

TOY VOCABULARY ONLY: widget lineage.
"""

from __future__ import annotations

import inspect
import pathlib
from typing import get_args

from hpc_agent.state import challenges as ch

# ── no agent-authored dissent (Lock 1 + C3) ───────────────────────────────────


def test_no_challenge_affordance_in_registry() -> None:
    """No mutate/workflow verb is named challenge/contest/dispute/refute — the ONLY
    write path is append-decision under the gated block (Lock 1, no-unlock-verb).
    """
    from hpc_agent._kernel.registry.primitive import get_registry, register_primitives

    register_primitives()
    banned = ("challenge", "contest", "dispute", "refute")
    for name in get_registry():
        low = name.lower()
        # ``challenge-status`` is the ONE read-only query (verb="query"); every other
        # challenge-shaped verb is forbidden. Allow only that exact read verb.
        if name == "challenge-status":
            continue
        for word in banned:
            assert word not in low, (
                f"a verb-shaped dissent affordance appeared in the registry: {name!r}"
            )


def test_no_code_path_writes_a_challenge_family_block() -> None:
    """No core writer hand-commits a challenge-family block — the blocks only ride a
    human ``append-decision`` through the gate (C3: code never files dissent).
    """
    src_root = pathlib.Path(inspect.getfile(ch)).parents[1]  # hpc_agent/
    offenders: list[str] = []
    for py in src_root.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        for block in ("challenge", "challenge-verdict", "challenge-withdraw"):
            if f'block="{block}"' in text or f"block='{block}'" in text:
                offenders.append(f"{py}: block={block}")
    assert not offenders, f"a code path mechanically writes a challenge block: {offenders}"


def test_challenge_status_query_is_read_only() -> None:
    """``challenge-status`` is verb=query, no side effects (the ONE read surface)."""
    from hpc_agent._kernel.registry.primitive import get_registry, register_primitives

    register_primitives()
    spec = get_registry()["challenge-status"]
    assert spec.verb == "query"
    assert list(spec.side_effects) == []


# ── contested is orthogonal, never a status (C-status) ─────────────────────────


def test_contested_is_no_status_vocabulary_member() -> None:
    """``contested`` joins NO status vocabulary — registration, conclusion, or the
    challenge reduction itself. A merged word would grant revocation by vocabulary.
    """
    from hpc_agent.state.evidence import STATUSES as CONCLUSION_STATUSES
    from hpc_agent.state.registration import STATUSES as REGISTRATION_STATUSES

    assert "contested" not in set(CONCLUSION_STATUSES)
    assert "contested" not in set(REGISTRATION_STATUSES)
    assert "contested" not in ch.STATUSES  # the challenge reduction's own five


def test_challenge_reduction_statuses_are_the_closed_five() -> None:
    """The reduction yields exactly ``open|upheld|dismissed|withdrawn|superseded``."""
    assert frozenset({ch.OPEN, ch.UPHELD, ch.DISMISSED, ch.WITHDRAWN, ch.SUPERSEDED}) == ch.STATUSES


# ── route-through the ONE kernel + collectors ─────────────────────────────────


def test_reduction_routes_through_the_one_kernel() -> None:
    """The per-challenge reduction routes winner-selection through
    ``state/attestation.py::reduce`` — never a re-inlined newest-first / sha-compare.
    """
    assert "attestation.reduce(" in inspect.getsource(ch.reduce_challenge)


def test_gate_verifies_target_and_citations_server_side() -> None:
    """The filing gate binds the content_sha through the ONE kernel and verifies the
    target committed + every citation live (the receipt-laundering hole closed).
    """
    from hpc_agent.ops.decision.journal import _assert_challenge_filing_full

    src = inspect.getsource(_assert_challenge_filing_full)
    assert "attestation.bind(" in src  # content_sha hash-locked via the kernel
    assert "resolve_target_existence(" in src  # target confirmed committed at the sha
    assert "resolve_citation(" in src  # every citation verified server-side at append


def test_target_and_citation_resolution_dispatch_to_evidence_table() -> None:
    """Target resolution dispatches to the evidence resolver table — never a copy."""
    src = inspect.getsource(ch.resolve_target_existence) + inspect.getsource(
        ch.resolve_target_current
    )
    assert "resolve_citation(" in src


def test_every_disclosure_seat_routes_through_standing_challenges() -> None:
    """Each seat that surfaces ``contested`` calls the ONE collector — no re-glob."""
    from hpc_agent.ops import attention_queue as aq
    from hpc_agent.ops.registration import prereqs, verify_op
    from hpc_agent.state import evidence

    for func in (
        verify_op._contested_counts,
        evidence._conclusion_contested,
        aq.collect_challenges,
        prereqs._uncontested_open_count,
    ):
        assert "standing_challenges(" in inspect.getsource(func), (
            f"{func.__qualname__} must route through standing_challenges"
        )


# ── closed vocabularies pinned equal (one definition) ─────────────────────────


def test_wire_status_literal_equals_state_reduction_five() -> None:
    """The wire ``ChallengeStatus`` literal == the state reduction's five statuses."""
    from hpc_agent._wire.queries.challenge_status import ChallengeStatus as WireStatus

    assert set(get_args(WireStatus)) == set(ch.STATUSES)


def test_wire_citation_kind_equals_citation_kinds() -> None:
    """The wire ``CitationKind`` == the closed ``CITATION_KINDS`` (one vocabulary)."""
    from hpc_agent._wire.queries.challenge_status import CitationKind
    from hpc_agent.state.evidence import CITATION_KINDS

    assert set(get_args(CitationKind)) == set(CITATION_KINDS)


# ── toy-domain fixtures only ───────────────────────────────────────────────────


def test_challenge_fixtures_use_toy_vocabulary_only() -> None:
    """No real domain word lands in a challenge FIXTURE (the domain-packs rule).

    Scans the challenge fixture builders for ``harxhar`` (the categorical banned
    token — a model name, never a fixture noun; MEMORY rule). Rule-STATEMENT lines
    ("never harxhar/quant") are excluded — they name the boundary, they don't cross
    it. The T2 wire test's deliberate adversarial opaque values (a domain word fed
    as an opaque ``ref`` to prove core never interprets it) are its own concern and
    not scanned here — that IS the opacity guarantee, not a violation of it.
    """
    banned = ("harxhar",)
    test_root = pathlib.Path(__file__).parent.parent
    files = [
        test_root / "ops" / "attention" / "test_challenge_attention.py",
        test_root / "ops" / "registration" / "test_verify_op_contested.py",
    ]
    offenders: list[str] = []
    for f in files:
        if not f.is_file():
            continue
        for lineno, line in enumerate(f.read_text(encoding="utf-8").splitlines(), start=1):
            low = line.lower()
            if "never" in low:  # a rule-statement line names the boundary, never crosses it
                continue
            for word in banned:
                if word in low:
                    offenders.append(f"{f.name}:{lineno}: {word}")
    assert not offenders, f"a real domain word landed in a challenge fixture: {offenders}"
