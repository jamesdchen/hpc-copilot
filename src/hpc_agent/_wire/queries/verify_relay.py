"""Pydantic models for the ``verify-relay`` query primitive (conduct rule 10).

The mechanized counterpart to the doctrine's rule "never relay numbers/state
that don't match the journal" (``docs/design/proving-run-2-hardening.md`` §6,
row 10). The LLM only ever relays code-digested briefs — but the *relay itself*
is unguarded: a transcription error, a rounded number, a swapped run-id, or a
stale state claim ("running" when the journal already recorded "failed" —
observed in proving run #3) can still leave the durable record.

``verify-relay`` is a pure, deterministic AUDIT: code extracts the factual
CLAIMS from the agent's draft relay text and diffs each against the durable
records for the run (decision journal, run sidecar, RunRecord, and the
per-run briefs log when present). This is *code auditing the LLM against the
durable record* — the inversion of Claude Science's LLM-audits-LLM reviewer,
and the project's moat stated as a feature.

It never blocks anything itself: it returns a verdict (``clean`` plus the
itemized mismatches). Hook-level enforcement lives in the ``Stop`` hook
(:mod:`hpc_agent._kernel.hooks.relay_audit_stop`), which runs this audit over
the final assistant text at turn end.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from hpc_agent._wire._shared import RunIdStrict


class VerifyRelayInput(BaseModel):
    """What relay to audit, and against which run's durable records."""

    model_config = ConfigDict(extra="forbid", title="verify-relay input spec")

    run_id: RunIdStrict
    # The agent's draft outgoing message — the text whose factual claims are
    # audited against the run's durable records.
    relay_text: str
    # Optional block hint (e.g. ``submit-s2``) for provenance in the result;
    # the audit reads all durable sources regardless.
    block: str | None = None


class RelayMismatch(BaseModel):
    """One claim in the relay that the durable records do not support.

    ``kind``:

    * ``number`` — a numeric claim that contradicts every source number
      (there ARE source numbers, but none match, even under truncation
      tolerance). ``nearest_source_value`` carries the closest source number.
    * ``state`` — a lifecycle/verification word that contradicts the run's
      recorded state (e.g. relay says "running" while the journal says
      "failed"). ``nearest_source_value`` carries the recorded state.
    * ``run_id`` — a run-id/job-id-shaped token that matches no authoritative
      identifier for the run. ``nearest_source_value`` carries the run in scope.
    * ``unverifiable`` — a factual claim with NO source to check against at
      all (no records carry any comparable value). Flagged, never silently
      passed: the bar is useful-conservative — prefer flagging to missing.
    """

    model_config = ConfigDict(extra="forbid", title="verify-relay mismatch")

    claim: str
    kind: Literal["number", "state", "run_id", "unverifiable"]
    detail: str
    nearest_source_value: str | None = None


class VerifyRelayResult(BaseModel):
    """The audit verdict over the relay text.

    ``clean`` is False iff any mismatch was found. ``claims_checked`` counts
    only the FACTUAL claims that were evaluated (conversational numbers —
    list markers, ``~``-prefixed durations — are filtered out before the
    count). ``sources_consulted`` honestly lists only the durable records
    that were actually found and read (nothing to contradict → an empty or
    short list, not a fabricated one).
    """

    model_config = ConfigDict(extra="forbid", title="verify-relay output data")

    clean: bool
    claims_checked: int = Field(ge=0)
    mismatches: list[RelayMismatch]
    sources_consulted: list[str]
