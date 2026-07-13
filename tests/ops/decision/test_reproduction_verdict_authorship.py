"""T12 — the reproduction-verdict admission gate (``ops/decision/journal.py``).

Fires each lock of ``_assert_reproduction_verdict_authorship`` on a synthetic
violation and drives the happy accept round-trip end to end: the accepted record
carries the FULL canonicalized ``content_sha``, and the store-layer admission join
(:func:`compute_admitted_flags`) then admits the sample.

TOY VOCABULARY ONLY (the plan's fixture rule): widget metrics, never a real
domain's words.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from hpc_agent import errors
from hpc_agent._wire.actions.decision_journal import AppendDecisionInput
from hpc_agent.ops.decision.journal import append_decision
from hpc_agent.state.fingerprint_store import (
    SUBJECT_KIND,
    append_sample,
    compute_admitted_flags,
    content_sha_over_payloads,
    read_samples,
)
from hpc_agent.state.runs import write_run_sidecar

# ── toy fixtures ────────────────────────────────────────────────────────────

_REPRO_RUN = "repro-widget-1"
_ORIG_RUN = "orig-widget-1"
_CMD_SHA = "widgetcmd0123456789abcdef"  # >16 chars: exercises the ledger [:16] key
_IDENTITY = {
    "cmd_sha": _CMD_SHA,
    "tasks_py_sha": "tasks-sha-aaa",
    "executor": "widget_executor.py",
    "data_sha": "data-sha-111",
}

# Two DIFFERENT widget-metrics payloads → a real (non-exact) comparison that a
# human must adjudicate: the mismatch/needs_verdict sample the gate admits.
_PAYLOAD_A = {"grid0": {"throughput": 100.0, "defects": 3}}
_PAYLOAD_B = {"grid0": {"throughput": 101.5, "defects": 4}}


def _write_payloads(tmp_path: Path) -> tuple[Path, Path]:
    pa = tmp_path / "orig_metrics.json"
    pb = tmp_path / "repro_metrics.json"
    pa.write_text(json.dumps(_PAYLOAD_A), encoding="utf-8")
    pb.write_text(json.dumps(_PAYLOAD_B), encoding="utf-8")
    return pa, pb


def _seed_ledger(
    experiment_dir: Path,
    *,
    verdict: str = "needs_verdict",
    repro_run: str = _REPRO_RUN,
    content_sha: str | None = None,
) -> str:
    """Append ONE inadmissible sample to the ledger and return its content_sha.

    The sample is bound to two on-disk payloads (the store's un-fakeable append),
    so its ``content_sha`` is the real canonical hash over the pair.
    """
    pa, pb = _write_payloads(experiment_dir)
    csha = (
        content_sha
        if content_sha is not None
        else content_sha_over_payloads(_PAYLOAD_A, _PAYLOAD_B)
    )
    record: dict[str, Any] = {
        "attestor": "code",
        "subject_kind": SUBJECT_KIND,
        "subject_id": _CMD_SHA,
        "content_sha": csha,
        "identity": dict(_IDENTITY),
        "source": "verify-reproduction",
        "run_ids": [_ORIG_RUN, repro_run],
        "cluster": "widgetlab",
        "scale": "main",
        "verdict": verdict,
        "same_submission": False,
        "partial": False,
        "task_indices": None,
        "per_key": [],
    }
    append_sample(experiment_dir, record=record, artifact_a=pa, artifact_b=pb)
    return csha


def _seed_sidecar(
    experiment_dir: Path, *, run_id: str = _REPRO_RUN, cmd_sha: str = _CMD_SHA
) -> None:
    write_run_sidecar(
        experiment_dir,
        run_id=run_id,
        cmd_sha=cmd_sha,
        hpc_agent_version="0.0.0-test",
        submitted_at="2026-07-08T00:00:00Z",
        executor="widget_executor.py",
        result_dir_template="results/{task}",
        task_count=1,
        tasks_py_sha="tasks-sha-aaa",
    )


def _spec(
    *,
    block: str = "reproduction-verdict",
    scope_kind: str = "run",
    scope_id: str = _REPRO_RUN,
    response: str,
    resolved: dict[str, Any],
) -> AppendDecisionInput:
    return AppendDecisionInput(
        scope_kind=scope_kind,  # type: ignore[arg-type]
        scope_id=scope_id,
        block=block,
        response=response,
        resolved=resolved,
    )


# ── the happy path: accept → FULL sha → store join admits ────────────────────


def test_accept_canonicalizes_to_full_sha_and_store_join_admits(tmp_path: Path) -> None:
    csha = _seed_ledger(tmp_path)
    _seed_sidecar(tmp_path)
    prefix = csha[:8]

    result = append_decision(
        experiment_dir=tmp_path,
        spec=_spec(
            response=f"accept — sample {prefix} is an env-sensitivity finding, not drift",
            resolved={"accept": True, "content_sha": prefix},
        ),
    )

    # The record persisted carries the FULL canonicalized sha (not the prefix).
    assert result.record.resolved is not None
    assert result.record.resolved["content_sha"] == csha

    # The store-layer admission join now admits the sample.
    samples, _ = read_samples(tmp_path, _CMD_SHA)
    flags, excluded = compute_admitted_flags(tmp_path, samples)
    assert flags == [True]
    assert excluded == 0


def test_accept_without_prefilled_content_sha_still_canonicalizes(tmp_path: Path) -> None:
    """The response names the prefix; resolved carries no content_sha at all."""
    csha = _seed_ledger(tmp_path)
    _seed_sidecar(tmp_path)

    result = append_decision(
        experiment_dir=tmp_path,
        spec=_spec(
            response=f"accept sample {csha[:10]} — reviewed, environment sensitivity",
            resolved={"accept": True},
        ),
    )
    assert result.record.resolved is not None
    assert result.record.resolved["content_sha"] == csha


# ── the authorship bar (E2-marked refusals) ──────────────────────────────────


def test_bare_ack_acceptance_refused_and_marked(tmp_path: Path) -> None:
    _seed_ledger(tmp_path)
    _seed_sidecar(tmp_path)
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(response="y", resolved={"accept": True}),
        )
    assert getattr(exc.value, "failure_features", None) == {"authorship_evidence": "missing"}


def test_acceptance_naming_no_prefix_refused_and_marked(tmp_path: Path) -> None:
    _seed_ledger(tmp_path)
    _seed_sidecar(tmp_path)
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                response="accept this one, it looks fine to me",  # no hex prefix
                resolved={"accept": True},
            ),
        )
    assert getattr(exc.value, "failure_features", None) == {"authorship_evidence": "missing"}


def test_prefix_matching_no_sample_refused_and_marked(tmp_path: Path) -> None:
    _seed_ledger(tmp_path)
    _seed_sidecar(tmp_path)
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                response="accept sample deadbeef01 — reviewed",  # 8 hex, no such sample
                resolved={"accept": True},
            ),
        )
    assert getattr(exc.value, "failure_features", None) == {"authorship_evidence": "missing"}


def test_ambiguous_prefix_refused_and_marked(tmp_path: Path) -> None:
    """Two ledger samples sharing an 8-hex prefix → the named prefix is ambiguous."""
    shared = "abcdef01"
    csha1 = shared + "1" + "0" * 55
    csha2 = shared + "2" + "0" * 55
    # Two samples for the SAME repro run, both prefixed ``abcdef01``.
    pa, pb = _write_payloads(tmp_path)
    for csha in (csha1, csha2):
        record: dict[str, Any] = {
            "attestor": "code",
            "subject_kind": SUBJECT_KIND,
            "subject_id": _CMD_SHA,
            "content_sha": csha,
            "identity": dict(_IDENTITY),
            "source": "verify-reproduction",
            "run_ids": [_ORIG_RUN, _REPRO_RUN],
            "cluster": "widgetlab",
            "scale": "main",
            "verdict": "needs_verdict",
            "same_submission": False,
            "partial": False,
            "task_indices": None,
            "per_key": [],
        }
        # Bind is content-addressed over the payloads, so bypass it by writing the
        # ledger line directly through the same append helper the store uses.
        from hpc_agent.infra.io import append_jsonl_line
        from hpc_agent.state.fingerprint_store import fingerprint_path

        record.setdefault("schema_version", 1)
        record.setdefault("ts", "2026-07-08T00:00:00Z")
        append_jsonl_line(fingerprint_path(tmp_path, _CMD_SHA), record)
    _seed_sidecar(tmp_path)

    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                response=f"accept sample {shared} — reviewed",
                resolved={"accept": True},
            ),
        )
    assert getattr(exc.value, "failure_features", None) == {"authorship_evidence": "missing"}
    assert "ambiguous" in str(exc.value)


def test_reject_faces_the_same_bar(tmp_path: Path) -> None:
    """A rejection (accept: false) with a bare ack is refused just like an accept."""
    _seed_ledger(tmp_path)
    _seed_sidecar(tmp_path)
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(response="y", resolved={"accept": False}),
        )
    assert getattr(exc.value, "failure_features", None) == {"authorship_evidence": "missing"}


def test_reject_with_named_prefix_succeeds_but_does_not_admit(tmp_path: Path) -> None:
    csha = _seed_ledger(tmp_path)
    _seed_sidecar(tmp_path)
    append_decision(
        experiment_dir=tmp_path,
        spec=_spec(
            response=f"reject sample {csha[:8]} — this is real drift, not environment",
            resolved={"accept": False},
        ),
    )
    samples, _ = read_samples(tmp_path, _CMD_SHA)
    flags, excluded = compute_admitted_flags(tmp_path, samples)
    assert flags == [False]  # accept:false never admits
    assert excluded == 1


# ── structural refusals (UNMARKED — a re-elicit cannot fix them) ─────────────


def test_wrong_scope_kind_refused_unmarked(tmp_path: Path) -> None:
    """block reproduction-verdict on a non-run scope → structural refusal."""
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                scope_kind="campaign",
                scope_id="camp-1",
                response="accept abcdef01",
                resolved={"accept": True},
            ),
        )
    assert not hasattr(exc.value, "failure_features")
    assert "scope_kind='run'" in str(exc.value)


def test_block_only_fires_for_its_own_block(tmp_path: Path) -> None:
    """The mirror direction: a run scope carries other blocks untouched by this gate.

    A plain non-verdict block on a run scope must NOT be dragged through the
    reproduction-verdict recompute leg (nothing else claims the block).
    """
    _seed_sidecar(tmp_path)
    # No ledger, no content_sha, a bare ack — would be refused IF the gate fired.
    result = append_decision(
        experiment_dir=tmp_path,
        spec=_spec(block="some-other-block", response="y", resolved={"note": "hi"}),
    )
    assert result.record.block == "some-other-block"


def test_non_bool_accept_refused_unmarked(tmp_path: Path) -> None:
    _seed_ledger(tmp_path)
    _seed_sidecar(tmp_path)
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                response="accept abcdef01",
                resolved={"accept": "yes"},  # not a bool
            ),
        )
    assert not hasattr(exc.value, "failure_features")
    assert "must be a bool" in str(exc.value)


def test_missing_sidecar_refused_unmarked(tmp_path: Path) -> None:
    csha = _seed_ledger(tmp_path)  # ledger present, but NO sidecar written
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                response=f"accept {csha[:8]}",
                resolved={"accept": True},
            ),
        )
    assert not hasattr(exc.value, "failure_features")
    assert "sidecar" in str(exc.value)


def test_contradicting_prefilled_content_sha_refused_unmarked(tmp_path: Path) -> None:
    """A pre-filled content_sha that disagrees with the named sample → structural."""
    csha = _seed_ledger(tmp_path)
    _seed_sidecar(tmp_path)
    with pytest.raises(errors.SpecInvalid) as exc:
        append_decision(
            experiment_dir=tmp_path,
            spec=_spec(
                response=f"accept {csha[:8]} — env sensitivity",
                resolved={"accept": True, "content_sha": "f" * 64},  # not this sample
            ),
        )
    assert not hasattr(exc.value, "failure_features")
    assert "does not extend" in str(exc.value)
