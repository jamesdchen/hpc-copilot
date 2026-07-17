"""Conformance kit — capability 5 (the Stop-hook append channel).

Asserts a harness's turn-final APPEND channel (``run_stop_hook_append``) lets
deterministic code APPEND what it holds (an owed terminal verdict, a rule-10
correction) to the human via a hook ``systemMessage`` and PROCEED, instead of
bouncing the model into re-relaying it. The D1 two-shape probe
(``docs/design/stop-hook-completer.md`` D1; ``docs/internals/harness-contract.md``,
"Capability 5 — the Stop-hook append channel") — a conforming append channel MUST
display BOTH output shapes, since display may differ between them:

* **shape A** — a BARE ``systemMessage`` on a PROCEEDING stop (an owed omission
  code-appended, the stop proceeds);
* **shape B** — a ``systemMessage`` COMBINED with ``decision:"block"`` (a poisoned
  decision bounces AND the omission rides the same systemMessage — the D2
  discharge-gated-on-confirmed-display path).

The seam is outcome-shaped (:class:`~hpc_agent.conformance.adapter.StopAppendOutcome`:
the appended ``system_message`` + whether the stop ALSO ``blocked``), never
mechanism-shaped — a Stop hook and any other turn-final interceptor certify through
the same seam (the D-K3 outcome-not-mechanism rule).

**HONEST STATUS (T10).** This is the BEHAVED leg only. There is NO passive install
seam — a hook ``systemMessage`` leaves zero evidence in ``settings.json`` — so
``harness-capabilities`` reports ``stop_hook_append`` tri-state, ENV-declared
(``"unknown"`` until a probe confirms it), never a passive ``true``. This module
closes "declared == behaved" for the REFERENCE relay-audit completer core; a FOREIGN
append-channel provider's proof, and a passive detection seam, remain owed.

Standalone / reference (the K2 pattern): with no ``--harness-adapter`` — OR an
adapter that does not declare capability 5 — the built-in REFERENCE completer,
hpc-agent's own ``relay_audit_stop.build_hook_output`` core driven IN-PROCESS with
the append channel ACTIVATED (scoped env, always restored — never leaked into a
sibling probe), is the candidate. When an adapter DECLARES capability 5, its
``run_stop_hook_append`` is the candidate. It never SKIPs: capability 5 is not part
of the three-capability ``conforming: harness contract v1`` verdict.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from hpc_agent.conformance.adapter import (
    CAP_STOP_HOOK_APPEND,
    StopAppendOutcome,
    declared_capabilities,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_RUN_ID = "kit-stop-append-run"
_AUDIT_ID = "kit-stop-append-audit"
_SHA12 = "abcdef012345"
_APPEND_ENV = "HPC_STOP_HOOK_APPEND"
_APPEND_ON_BLOCK_ENV = "HPC_STOP_HOOK_APPEND_ON_BLOCK"


# --- journal seeding (a live owed / poisoned stop, reproduced) ---------------


def seed_owed_marker(experiment_dir: Path) -> None:
    """Journal ONE undischarged relay-due marker (a terminal ``passed`` verdict).

    Creates the notebook-audit journal too (so the audit is discoverable). A final
    message that does NOT carry the state word / sha12 leaves it OWED — the omission
    the completer appends via ``systemMessage``.
    """
    from hpc_agent.state import notebook_audit as nb

    record = nb.record_relay_due(
        experiment_dir, audit_id=_AUDIT_ID, state="passed", module_sha=_SHA12 + "0" * 52
    )
    assert record is not None


def seed_poison(experiment_dir: Path) -> None:
    """Seed a journaled run (``failed``) + a still-PENDING brief the relay contradicts.

    The brief's far-future ``ts`` has no subsequent committed ``y``, so it stays
    pending; a relay that calls the ``failed`` run "running" both contradicts the
    record (rule-10) AND intersects the pending brief's content — a poisoned
    decision, the surviving bounce.
    """
    from hpc_agent.state.decision_briefs import append_brief
    from hpc_agent.state.decision_journal import append_decision
    from hpc_agent.state.journal import upsert_run
    from hpc_agent.state.run_record import RunRecord

    upsert_run(
        experiment_dir,
        RunRecord(
            run_id=_RUN_ID,
            profile="p",
            cluster="hoffman2",
            ssh_target="u@h",
            remote_path="/remote",
            job_name="j",
            job_ids=["13610902"],
            total_tasks=10,
            submitted_at="2026-07-03T00:00:00+00:00",
            experiment_dir=str(experiment_dir),
            status="failed",
        ),
    )
    append_decision(
        experiment_dir,
        scope_kind="run",
        scope_id=_RUN_ID,
        block="submit-s1",
        response="y",
        evidence_digest={"canary": "green", "core_hours": 128},
    )
    append_brief(
        experiment_dir,
        run_id=_RUN_ID,
        block="s2",
        ts="2099-01-01T00:00:00+00:00",
        brief={"proposal": "resume: the run is running"},
    )


#: The final relay text that contradicts the ``failed`` run + poisons the pending
#: brief (shape B). It never carries the owed marker's tokens, so the omission
#: stays owed and rides the systemMessage alongside the bounce.
POISON_RELAY = f"Run {_RUN_ID} is running; ending."
#: A clean final message that names nothing owed (shape A) — the owed omission is
#: the only thing to append, and it appends without a bounce.
CLEAN_RELAY = "All wrapped up here; ending the turn."


# --- the append-channel candidate seam ---------------------------------------


@dataclass(frozen=True)
class StopAppendCandidate:
    """A Stop-hook append channel under test — the reference core or an adapter."""

    name: str
    run: Callable[..., StopAppendOutcome]


@contextlib.contextmanager
def _appender_active(*, on_block: bool) -> Iterator[None]:
    """ACTIVATE the append channel via the env markers, restoring prior values.

    Scoped and always restored (a ``finally``): the reference completer reads the
    channel from ``os.environ`` (``detect_stop_hook_append``), and leaking the marker
    into a sibling capability probe is the exact local trap this closes — the
    capability-7 kit deliberately scrubs these markers, so this one must never leave
    them set.
    """
    prior = {k: os.environ.get(k) for k in (_APPEND_ENV, _APPEND_ON_BLOCK_ENV)}
    os.environ[_APPEND_ENV] = "1"
    if on_block:
        os.environ[_APPEND_ON_BLOCK_ENV] = "1"
    else:
        os.environ.pop(_APPEND_ON_BLOCK_ENV, None)
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _builtin_reference() -> StopAppendCandidate:
    """hpc-agent's own relay-audit completer core driven in-process (the reference).

    Writes *final_message* as the trailing assistant entry of a transcript, builds
    the Stop payload, ACTIVATES the append channel (scoped env), and maps
    ``relay_audit_stop.build_hook_output`` onto a
    :class:`~hpc_agent.conformance.adapter.StopAppendOutcome`: ``systemMessage`` is
    the appended text, ``decision == "block"`` is the mixed-class bounce.
    """
    from hpc_agent._kernel.hooks.relay_audit_stop import build_hook_output

    def run(
        experiment_dir: Path, *, final_message: str, on_block: bool = False
    ) -> StopAppendOutcome:
        transcript = experiment_dir / "_kit_stop_transcript.jsonl"
        content = [{"type": "text", "text": final_message}]
        line = json.dumps(
            {"type": "assistant", "message": {"role": "assistant", "content": content}}
        )
        transcript.write_text(line + "\n", encoding="utf-8")
        payload = {"cwd": str(experiment_dir), "transcript_path": str(transcript)}
        with _appender_active(on_block=on_block):
            out = build_hook_output(payload)
        out = out or {}
        return StopAppendOutcome(
            system_message=out.get("systemMessage"),
            blocked=out.get("decision") == "block",
        )

    return StopAppendCandidate(name="hpc-agent (relay_audit_stop completer)", run=run)


@pytest.fixture
def stop_hook_append_candidate(request: pytest.FixtureRequest) -> StopAppendCandidate:
    """The append-channel seam to certify — the adapter's when declared, else reference.

    With ``--harness-adapter`` AND a declared capability 5, the adapter's
    ``run_stop_hook_append`` is the candidate. Otherwise the built-in reference core
    runs (no SKIP — capability 5 is not a ``conforming: harness contract v1`` verdict
    capability; a FOREIGN proof is the follow-on).
    """
    spec = request.config.getoption("--harness-adapter", default=None)
    if spec:
        adapter = request.getfixturevalue("harness_adapter")
        if CAP_STOP_HOOK_APPEND in declared_capabilities(adapter):
            return StopAppendCandidate(
                name=getattr(adapter, "name", "<adapter>"), run=adapter.run_stop_hook_append
            )
    return _builtin_reference()


# --- assertions (mirror-drivable: first arg is the candidate, second the repo) --
#
# Each check seeds a FRESH state into its own repo, so callers pass a distinct
# claimed repo per check (the pytest ``fixture_repo`` is per-test; the mirror
# claims one per call).


def check_displays_systemmessage_on_proceed(candidate: StopAppendCandidate, repo: Path) -> None:
    """Shape A: an owed omission is code-appended via ``systemMessage``, no bounce.

    A channel that displays NOTHING (the rejector degrade — ``system_message`` None)
    is FAILED here (guard-can-fire): it cannot complete, so it is not the append
    channel.
    """
    seed_owed_marker(repo)
    outcome = candidate.run(repo, final_message=CLEAN_RELAY, on_block=False)
    assert outcome.system_message, (
        f"[{candidate.name}] shape A: an owed verdict MUST be code-appended via a bare "
        "systemMessage on a proceeding stop (not bounced, not swallowed)"
    )
    assert outcome.blocked is False, (
        f"[{candidate.name}] shape A: an omission-only completion PROCEEDS — it must not block"
    )


def check_displays_systemmessage_with_block(candidate: StopAppendCandidate, repo: Path) -> None:
    """Shape B: a poisoned bounce AND the omission ride ONE output (systemMessage + block).

    A channel that SWALLOWS the systemMessage on a blocked stop (``system_message``
    None while blocked) is FAILED here — the D2 confirmed-display shape must carry
    both.
    """
    seed_poison(repo)
    seed_owed_marker(repo)  # an omission that rides the blocked stop's systemMessage
    outcome = candidate.run(repo, final_message=POISON_RELAY, on_block=True)
    assert outcome.blocked is True, (
        f"[{candidate.name}] shape B: a poisoned decision MUST bounce (decision:block)"
    )
    assert outcome.system_message, (
        f"[{candidate.name}] shape B: the append channel MUST display a systemMessage COMBINED "
        "with the block (D1's second shape) — a swallowed blocked-stop message is not conforming"
    )


def test_stop_hook_append_displays_on_proceed(
    stop_hook_append_candidate: StopAppendCandidate, fixture_repo: Path
) -> None:
    """Capability 5 behaved leg (D1 shape A): a bare systemMessage on a proceeding stop."""
    check_displays_systemmessage_on_proceed(stop_hook_append_candidate, fixture_repo)


def test_stop_hook_append_displays_with_block(
    stop_hook_append_candidate: StopAppendCandidate, fixture_repo: Path
) -> None:
    """Capability 5 behaved leg (D1 shape B): a systemMessage combined with decision:block."""
    check_displays_systemmessage_with_block(stop_hook_append_candidate, fixture_repo)
