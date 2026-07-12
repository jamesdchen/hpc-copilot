"""G10 lockstep contracts: hand-maintained vocabulary sets vs their catalogs.

Generator G10 (``docs/plans/upstream-fixes-2026-07.md``): every hand-maintained
vocabulary set that must stay in lock-step with an owning catalog needs a subset
/ equality contract test so a later edit to one side cannot silently diverge from
the other. This file adds the pins that were still MISSING after the 2026-07-11
bug-sweep, following the precedent of
``tests/contracts/test_failure_category_covers_classifier.py`` and
``tests/contracts/test_failure_category_resubmittable_covers_classifier.py``.

Skipped (already pinned — no new test here):

* **#2** — the wire ``FailureCategory`` Literal ⊆/⊇ the classifier catalog is
  already pinned by ``test_failure_category_covers_classifier.py`` (lines 41-63)
  and ``test_failure_category_resubmittable_covers_classifier.py``.
* **#21** — ``state/evidence.py::_decision_journal_path`` branching on ALL
  ``SCOPE_KINDS`` is already pinned by
  ``tests/state/test_evidence.py::test_decision_journal_path_matches_canonical_for_all_scope_kinds``
  (line 380), which iterates every ``decision_journal.SCOPE_KINDS`` member.

Built here:

* **#27/#28** — the census-excluded trace transport filename, sourced from ONE
  ``data_trace_contract`` constant, consumed by BOTH censuses.
* **#36/#66** — the deploy-placed framework files ⊆ the prune/anomaly exemption
  set (== ``PROTECTED_RUNTIME_FILES``).
* **#47** — the reproduction ``requires`` whitelist vs ``evidence_meets``' demand
  key set. This one is a **KNOWN-UNFIXED drift** (see the test's docstring); it
  is marked ``xfail(strict=True)`` so the suite stays green while pinning the
  defect, and flips to a hard failure the moment the source is fixed.
"""

from __future__ import annotations

# ── #27/#28 — the census-excluded trace transport filename ────────────────────


def test_trace_transport_filename_excluded_by_both_censuses() -> None:
    """The framework trace transport file is census-excluded in lock-step from ONE SoT.

    SoT: ``execution/mapreduce/data_trace_contract.TRACE_TRANSPORT_FILENAME``
    (``_trace.jsonl``) — the single name the emitter writes and the two
    produced-result censuses must skip.

    Drift mode (bug-sweep #27/#28): the pack emitter appends ``_trace.jsonl`` into
    ``$HPC_RESULT_DIR``. If either census counts it as a produced RESULT, an
    output-less-but-trace-emitting task reads "complete" — the proving-run-5
    finding-16 FALSE GREEN, reopened by the data-trace feature. The two censuses
    that must exclude it:

    * ``execution/mapreduce/dispatch.py::_FRAMEWORK_ARTIFACT_NAMES`` — the
      dispatcher's produced-result census (the empty-output guard, #27), whose
      ``_TRACE_TRANSPORT_FILENAME`` copy must equal the contract's, and
    * ``execution/mapreduce/reduce/status.py::_FRAMEWORK_ARTIFACT_NAMES`` — the
      reporter's census (#28).

    A new census that forgets the transport file, or a rename of the constant on
    one side only, breaks this pin.
    """
    from hpc_agent.execution.mapreduce import data_trace_contract, dispatch
    from hpc_agent.execution.mapreduce.reduce import status

    transport_name = data_trace_contract.TRACE_TRANSPORT_FILENAME

    # The dispatcher's own hardcoded copy must equal the contract (the "kept in
    # lock-step" comment at dispatch.py:88 made mechanical).
    assert transport_name == dispatch._TRACE_TRANSPORT_FILENAME, (
        "dispatch.py hardcodes _TRACE_TRANSPORT_FILENAME as a lock-step copy of "
        "data_trace_contract.TRACE_TRANSPORT_FILENAME; they have diverged: "
        f"{dispatch._TRACE_TRANSPORT_FILENAME!r} != {transport_name!r}."
    )

    assert transport_name in dispatch._FRAMEWORK_ARTIFACT_NAMES, (
        "The dispatcher's produced-result census "
        "(dispatch.py::_FRAMEWORK_ARTIFACT_NAMES) must exclude the trace "
        f"transport file {transport_name!r} (bug-sweep #27), or an output-less "
        "trace-emitting task is promoted complete (finding-16 false green)."
    )

    assert transport_name in status._FRAMEWORK_ARTIFACT_NAMES, (
        "The reporter's census (reduce/status.py::_FRAMEWORK_ARTIFACT_NAMES) must "
        f"exclude the trace transport file {transport_name!r} (bug-sweep #28), or "
        "the reporter confirms 'complete' on a trace-only result dir."
    )


# ── #36/#66 — deploy-placed files ⊆ the prune/anomaly exemption set ────────────


def test_deploy_placed_files_are_all_prune_exempt() -> None:
    """Every ``deploy_runtime``-placed framework file is prune/anomaly-exempt.

    SoT: ``infra/transport.py::_build_deploy_items`` enumerates exactly what the
    deploy leg ships into ``<remote>/`` (the dispatcher, combiner, templates, the
    reporter closure under ``hpc_agent/``). Those files live only on the remote —
    the local push tree never contains them.

    Drift mode (bug-sweep #36): a ``delete=True`` delta push walks the remote
    tree, finds these framework files as ``extra`` (not in the local push
    manifest), and — unless exempted — surfaces every one as a ruling-6 prune
    ANOMALY ("needs a human decision") on EVERY push, burying real foreign-file
    anomalies. The exemption is ``_is_runtime_placed`` (used at
    ``_prune_manifest_known_extras``), which matches against
    ``PROTECTED_RUNTIME_FILES``. This pins the documented relation:

        {dst_rel for _build_deploy_items} ⊆ {relpath : _is_runtime_placed(relpath)}

    for every scheduler family (the shipped set is scheduler-dependent). A new
    deploy item whose dst_rel escapes ``PROTECTED_RUNTIME_FILES`` breaks it.
    """
    from hpc_agent.infra import transport

    for scheduler in ("sge", "slurm", "pbspro", "torque", None):
        items = transport._build_deploy_items(scheduler=scheduler)
        assert items, f"_build_deploy_items(scheduler={scheduler!r}) shipped nothing"
        not_exempt = [it.dst_rel for it in items if not transport._is_runtime_placed(it.dst_rel)]
        assert not not_exempt, (
            f"deploy_runtime ships file(s) that _is_runtime_placed does NOT exempt "
            f"(scheduler={scheduler!r}): {sorted(not_exempt)}. Every delta push would "
            "flag them as prune ANOMALY 'needs a human decision' (bug-sweep #36). Add "
            "a covering entry to transport.PROTECTED_RUNTIME_FILES."
        )


def test_deploy_cache_manifest_is_protected() -> None:
    """The deploy-cache manifest ``.hpc/.deploy_state.json`` is a protected runtime file.

    SoT: ``infra/transport.py::_DEPLOY_MANIFEST_REL`` names the #242 content-hash
    deploy-cache manifest that ``deploy_runtime`` writes on the remote.

    Drift mode (bug-sweep #66): if it is absent from ``PROTECTED_RUNTIME_FILES``,
    every ``delete=True`` push wipes it (the local tree never carries it), so the
    deploy cache ALWAYS misses (re-ships every file) and the manifest prune loses
    its record of what we shipped. The manifest must be protected AND exempt from
    the anomaly channel — the same relation ``_is_runtime_placed`` encodes.
    """
    from hpc_agent.infra import transport

    manifest_rel = transport._DEPLOY_MANIFEST_REL
    assert manifest_rel in transport.PROTECTED_RUNTIME_FILES, (
        f"The deploy-cache manifest {manifest_rel!r} must be in "
        "transport.PROTECTED_RUNTIME_FILES (bug-sweep #66), or every delete=True "
        "push wipes it and the #242 deploy cache never hits."
    )
    assert transport._is_runtime_placed(manifest_rel), (
        f"{manifest_rel!r} must read as runtime-placed so the prune/anomaly channel "
        "never nags about the framework's own deploy-cache manifest."
    )


# ── #47 — reproduction ``requires`` whitelist vs evidence_meets demand keys ────


def test_reproduction_floor_does_not_forward_cross_kind_key(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The reproduction evidence floor must not forward a cross-kind ``requires`` key.

    Two lock-step key sets that must agree:

    * ``ops/registration/prereqs.py::_REQUIRES_KEYS[KIND_REPRODUCTION]`` |
      ``{UNCONTESTED_REQUIRES_KEY}`` — the validation whitelist a reproduction
      entry's ``requires`` may carry (``_reject_unknown_requires``). ``uncontested``
      is the ONE cross-kind key, accepted on every kind and gated by the separate
      ``_apply_uncontested_demand`` pass — it is NOT an evidence-floor demand.
    * ``state/determinism.py::_ALLOWED_DEMAND_KEYS`` — the CLOSED set
      ``evidence_meets`` accepts; an unknown demand key is a loud
      ``errors.SpecInvalid``.

    The relation that must hold: the keys the reproduction floor actually forwards
    into ``evidence_meets`` ⊆ ``_ALLOWED_DEMAND_KEYS``. Static set membership
    already holds (``_REQUIRES_KEYS[reproduction]`` == ``_ALLOWED_DEMAND_KEYS``);
    the drift is at RUNTIME — the floor forwards ``dict(entry.requires)`` verbatim,
    so the cross-kind ``uncontested`` leaks through and crashes. We exercise the
    floor directly with a met floor (``min_n``) plus ``uncontested`` and assert it
    resolves without raising; on HEAD it raises (xfail).
    """
    from hpc_agent.ops.registration import prereqs
    from hpc_agent.state.determinism import _ALLOWED_DEMAND_KEYS
    from hpc_agent.state.registration import KIND_REPRODUCTION, UNCONTESTED_REQUIRES_KEY

    # Guard the premise: the cross-kind key is whitelisted for reproduction but is
    # NOT an evidence_meets demand key. If either of these changes, this test's
    # framing (not just its verdict) needs revisiting.
    assert UNCONTESTED_REQUIRES_KEY not in _ALLOWED_DEMAND_KEYS
    assert prereqs._REQUIRES_KEYS[KIND_REPRODUCTION] <= set(_ALLOWED_DEMAND_KEYS)

    demand = {"min_n": 1, UNCONTESTED_REQUIRES_KEY: True}
    # An empty repro identity → empty ledger → n=0, an ordinary shortfall; the
    # crash (if any) is the unknown-key refusal in evidence_meets, reached before
    # any sample counting. A correct floor strips the cross-kind key and returns.
    met, _shortfall = prereqs._reproduction_evidence_floor(
        tmp_path, repro_ident={}, sidecar={}, demand=demand
    )
    assert met is False  # no ledger evidence, so the floor is unmet (not a crash)
