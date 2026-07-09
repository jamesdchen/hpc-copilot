"""Relay-enforcement fixture TRIPLES (K5) — loader + journal seeder.

A *triple* is ``(journal state, final message, expected verdict)``: the
scenario fixtures the capability-2 (relay enforcement / ACT) kit runs through a
harness's ``run_enforcement_point`` seam. They live as declarative data in
``fixtures/relay/triples.json`` and are SEEDED into a fixture repo here — the
journal state is Python (``append_decision`` / ``upsert_run`` /
``write_run_sidecar`` / a notebook sign-off), not a static file, so this module
turns each triple's ``seed`` block into the durable records
``verify_relay`` / ``verify_notebook_relay`` (and thus the Stop-hook ACT seam)
read back.

Design (mirrors K2's ``test_canonicalization`` vector discipline):

* the triples enumerate EVERY contradiction kind the reference detects — the
  blocking set is ``relay_audit_stop._CONTRADICTION_KINDS`` (``number`` /
  ``state`` / ``run_id``), reused verbatim for the run relay and the notebook
  relay (a wrong section status / module ``passed`` is ``state``; a mismatched
  sha-hex is ``number``) — plus the PASS cases (faithful relay, truncation,
  the ``unverifiable`` drop, a message naming nothing) and a FAIL-OPEN case (a
  torn journal record degrades to not-blocked);
* each triple pins its expected mismatch kinds; the K2-discipline unit tests in
  ``tests/conformance_kit/`` prove the pin AGREES with our own
  ``verify_relay`` — the vectors are checked against the reference, never
  hand-asserted;
* ``blocks`` is DERIVED from ``_CONTRADICTION_KINDS`` (the same constant the
  hook filters on), so the fixtures track the code rather than a hardcoded copy.

Pytest-free (stdlib + ``hpc_agent.state``); the two test modules import it. It
is a ``conformance`` sibling, so importing ``hpc_agent.state`` is inside the
D-K1 boundary (only core-OUTSIDE-conformance importing the kit is banned).
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hpc_agent._wire.queries.verify_relay import VerifyRelayResult

__all__ = [
    "CONTRADICTION_KINDS",
    "NB_SOURCE",
    "RelayTriple",
    "blocking_kinds",
    "load_triples",
    "reference_result",
    "seed_triple",
]

_FIXTURES = Path(__file__).parent / "fixtures" / "relay"

# The percent-format notebook source the notebook triples audit against — two
# audited sections, ``load-data`` and ``fit-model`` (identical to the reference
# ``tests/ops/test_verify_relay.py`` source so the shas line up).
NB_SOURCE = """# %%
# hpc-audit-section: load-data
import pandas as pd
data = pd.read_csv("in.csv")

# %%
# hpc-audit-section: fit-model
model = fit(data)
"""


def _contradiction_kinds() -> frozenset[str]:
    """The blocking mismatch kinds — REUSED from the Stop hook's own constant.

    Deriving the fixtures' notion of "blocks" from the exact set the hook
    filters on (``_CONTRADICTION_KINDS``) means an additive change to the
    blocking set can never silently desync the vectors from the code.
    """
    from hpc_agent._kernel.hooks.relay_audit_stop import _CONTRADICTION_KINDS

    return _CONTRADICTION_KINDS


CONTRADICTION_KINDS: frozenset[str] = _contradiction_kinds()


def blocking_kinds(mismatch_kinds: list[str]) -> list[str]:
    """The subset of *mismatch_kinds* that would BLOCK a stop (sorted-unique)."""
    return sorted({k for k in mismatch_kinds if k in CONTRADICTION_KINDS})


@dataclasses.dataclass(frozen=True)
class RelayTriple:
    """One ``(journal state, final message, expected verdict)`` scenario.

    ``scope`` — ``"run"`` (``verify_relay``) or ``"notebook"``
    (``verify_notebook_relay``).
    ``target_id`` — the run id / audit id the relay is audited against.
    ``final_message`` — the final agent-visible text the ACT seam judges.
    ``seed`` — the declarative journal state (see :func:`seed_triple`).
    ``mismatch_kinds`` — every kind the reference verdict carries, incl.
    ``unverifiable`` (the pin the unit tests check against ``verify_relay``).
    ``doc`` — a one-line human note (the contradiction kind / pass reason).
    """

    name: str
    scope: str
    target_id: str
    final_message: str
    seed: dict[str, Any]
    mismatch_kinds: list[str]
    doc: str

    @property
    def contradiction_kinds(self) -> list[str]:
        """The kinds that make this triple BLOCK (derived from the hook set)."""
        return blocking_kinds(self.mismatch_kinds)

    @property
    def blocks(self) -> bool:
        """Whether a conforming enforcement seam blocks this triple's relay."""
        return bool(self.contradiction_kinds)


def load_triples() -> list[RelayTriple]:
    """Every relay triple, in file order."""
    raw = json.loads((_FIXTURES / "triples.json").read_text(encoding="utf-8"))
    return [
        RelayTriple(
            name=c["name"],
            scope=c["scope"],
            target_id=c["target_id"],
            final_message=c["final_message"],
            seed=c.get("seed", {}),
            mismatch_kinds=list(c.get("mismatch_kinds", [])),
            doc=c.get("doc", ""),
        )
        for c in raw["triples"]
    ]


# ── journal seeding ─────────────────────────────────────────────────────────


def seed_triple(experiment_dir: Path, triple: RelayTriple) -> None:
    """Materialise *triple*'s ``seed`` block into *experiment_dir*'s journal.

    Run seeds honour ``journal`` (evidence_digest), ``sidecar`` (a run
    sidecar), ``record`` (a ``RunRecord`` status/job_ids) and
    ``corrupt_record`` (overwrite the record file with non-JSON bytes — the
    fail-open fixture). Notebook seeds honour ``source`` / ``template``
    (materialise the ``.py`` + interview.json) and ``sign`` (a list of section
    slugs to sign off). Everything is idempotent per fixture repo.
    """
    if triple.scope == "run":
        _seed_run(experiment_dir, triple)
    elif triple.scope == "notebook":
        _seed_notebook(experiment_dir, triple)
    else:  # pragma: no cover - guarded by the vectors' own test
        raise ValueError(f"unknown triple scope {triple.scope!r}")


def _seed_run(experiment_dir: Path, triple: RelayTriple) -> None:
    from hpc_agent.state.decision_journal import append_decision
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord
    from hpc_agent.state.runs import write_run_sidecar

    seed = triple.seed
    run_id = triple.target_id

    journal = seed.get("journal")
    if journal is not None:
        append_decision(
            experiment_dir,
            scope_kind="run",
            scope_id=run_id,
            block="submit-s1",
            response="y",
            evidence_digest=dict(journal),
        )

    sidecar = seed.get("sidecar")
    if sidecar is not None:
        write_run_sidecar(
            experiment_dir,
            run_id=run_id,
            cmd_sha="a" * 64,
            hpc_agent_version="0.0.0",
            submitted_at="2026-07-03T00:00:00+00:00",
            executor="python3 .hpc/_hpc_dispatch.py",
            result_dir_template="results/{run_id}/task_{task_id}",
            task_count=int(sidecar.get("task_count", 10)),
            tasks_py_sha="b" * 64,
        )

    record = seed.get("record")
    if record is not None:
        upsert_run(
            experiment_dir,
            RunRecord(
                run_id=run_id,
                profile="p",
                cluster="hoffman2",
                ssh_target="u@h",
                remote_path="/remote",
                job_name="j",
                job_ids=list(record.get("job_ids", ["13610902"])),
                total_tasks=10,
                submitted_at="2026-07-03T00:00:00+00:00",
                experiment_dir=str(experiment_dir),
                status=str(record["status"]),
            ),
        )

    if seed.get("corrupt_record"):
        _corrupt_run_record(experiment_dir, run_id)


def _corrupt_run_record(experiment_dir: Path, run_id: str) -> None:
    """Overwrite the run record JSON with garbage — the torn-journal fixture.

    ``load_run`` degrades a non-JSON record to ``None`` (fail-open reader), so
    the audit sees NO recorded state and every lifecycle claim becomes
    ``unverifiable`` rather than a contradiction — the enforcement seam does
    not block. The intact-record counterpart (``run_state_contradiction``)
    proves the same message DOES block when the record reads, so the guard can
    fire (engineering-principles: verify a guard can actually fire).
    """
    from hpc_agent.state.run_record import _current_homedir, repo_hash

    path = _current_homedir() / repo_hash(experiment_dir) / "runs" / f"{run_id}.json"
    if path.exists():
        path.write_text("{ this is not valid json", encoding="utf-8")


def _seed_notebook(experiment_dir: Path, triple: RelayTriple) -> None:
    from hpc_agent.state import notebook_audit as nb
    from hpc_agent.state.audit_source import parse_percent_source
    from hpc_agent.state.decision_journal import append_decision

    seed = triple.seed
    audit_id = triple.target_id

    if seed.get("source"):
        (experiment_dir / "source.py").write_text(NB_SOURCE, encoding="utf-8")
    if seed.get("template"):
        (experiment_dir / "template.py").write_text(NB_SOURCE, encoding="utf-8")
    if seed.get("interview"):
        block: dict[str, Any] = {"source": "source.py", "audit_id": audit_id}
        if seed.get("template"):
            block["template"] = "template.py"
        (experiment_dir / "interview.json").write_text(
            json.dumps({"audited_source": block}), encoding="utf-8"
        )

    sections = {s.slug: s for s in parse_percent_source(NB_SOURCE).sections}
    for slug in seed.get("sign", []):
        append_decision(
            experiment_dir,
            scope_kind="notebook",
            scope_id=audit_id,
            block=nb.SIGN_OFF_BLOCK,
            response=f"reviewed the {slug} section",
            resolved={
                "audit_id": audit_id,
                "section": slug,
                "section_sha": sections[slug].section_sha,
                "view_sha": "view-1",
            },
        )


# ── the reference verdict (the vectors are checked against this) ─────────────


def reference_result(experiment_dir: Path, triple: RelayTriple) -> VerifyRelayResult:
    """Run OUR reference audit for *triple* against its seeded journal.

    ``verify_relay`` for a run triple, ``verify_notebook_relay`` for a notebook
    one — the SAME functions the Stop-hook ACT seam drives. The unit tests
    assert this result's mismatch kinds equal the triple's pin.
    """
    if triple.scope == "run":
        from hpc_agent._wire.queries.verify_relay import VerifyRelayInput
        from hpc_agent.ops.decision.verify_relay import verify_relay

        return verify_relay(
            experiment_dir=experiment_dir,
            spec=VerifyRelayInput(run_id=triple.target_id, relay_text=triple.final_message),
        )
    from hpc_agent.ops.decision.verify_relay import verify_notebook_relay

    return verify_notebook_relay(experiment_dir, triple.target_id, triple.final_message)
