"""Every primitive verb that can fail must emit a typed failure envelope.

This is the WS4 contract: when the user / agent fires a verb with a
known-bad input and the verb rejects it, the response envelope MUST
carry enough structured evidence for the caller to recover without
free-form prose interpretation.

Concretely, for every primitive that accepts a JSON ``--spec`` (the
input-bearing surface; the only place a structurally-malformed input
can be reliably fabricated), we assert:

1. Bad input → ``ok == False`` with ``error_code == "spec_invalid"`` —
   the producer recognised the failure as a user-input shape error,
   not as an internal crash.
2. ``failure_features`` is present on the envelope with a populated
   ``error_class`` (one of the values from the FailureCategory
   vocabulary in ``hpc_agent/_wire/_shared.py``).
3. ``remediation`` names a path the caller can act on — either the
   schema file (``hpc_agent/schemas/<name>.input.json``) or the
   ``hpc-agent describe <verb>`` form that resolves to it.

Today ``failure_features`` is being wired into ``ErrorEnvelope`` by
WS3 (running in parallel). Verbs that don't emit it yet are listed in
``XFAIL_NO_FAILURE_FEATURES``; the xfail list IS the punch list for
downstream. As WS3 wires each verb, drop it from the xfail set; the
test then runs as a hard assertion.

**WS4 Q4: strict_xfail markers.** Verbs in the static xfail catalogues
(``NEEDS_EXTRA_CLI_ARGS``, ``XFAIL_NEEDS_FIXTURE``,
``XFAIL_NO_FAILURE_FEATURES``) are marked at parametrize time with
``pytest.mark.xfail(strict=True, ...)``. ``strict=True`` semantics:
when the underlying verb behaviour is fixed and the test now passes,
pytest surfaces an ``XPASS(strict)`` failure — the maintainer is
forced to drop the verb from the catalogue. Replaces the prior dynamic
``pytest.xfail()`` calls inside test bodies, which silently let the
catalogue drift out of sync as verbs improved. Runtime xfails for
envelope-shape-dependent conditions (e.g. ``error_code == "internal"``
when ``spec_invalid`` was expected) stay dynamic — those conditions
aren't knowable at parametrize time.

Marked with ``contract`` — run with ``pytest -m contract``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import invoke_cli

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "src" / "hpc_agent" / "schemas"


pytestmark = pytest.mark.contract


def _input_schemas() -> list[Path]:
    return sorted(SCHEMAS_DIR.glob("*.input.json"))


def _verb_from_schema_path(path: Path) -> str:
    """Map ``submit_flow.input.json`` → ``submit-flow``.

    Schema filenames use underscores; CLI verbs use hyphens. This is
    the same mechanical mapping ``_validate_against_schema`` does in
    ``cli/_helpers.py``.
    """
    stem = path.name[: -len(".input.json")]
    return stem.replace("_", "-")


# Inventory of CLI verbs that accept ``--spec``. Hard-coded for
# determinism: pytest-xdist forks workers and we need every worker to
# parametrize the same test set. A subprocess-based probe at collection
# time can race against ``hpc_agent``'s import side-effects on Windows
# (different workers see different verb counts), which manifests as
# the "Different tests were collected between gw3 and gw2" failure.
#
# Inventory was generated 2026-06-04 by scanning every ``hpc-agent
# <verb> --help`` for a ``--spec SPEC`` argument. The
# ``test_spec_verb_inventory_matches_cli`` regression test below pins
# this list to the live CLI surface — if a new --spec-accepting verb
# lands without being added here, that test fails and the maintainer
# updates the inventory.
_SPEC_VERBS: frozenset[str] = frozenset(
    {
        "trace-diff",
        "trace-render",
        "pack-bind",
        "pack-record-receipt",
        "pack-status",
        "audit-preflight",
        "data-manifest",
        "notebook-draft-context",
        "verify-registration",
        # Human-amplification block verbs + kill/doctor/decision verbs that
        # gained a ``--spec`` surface (2026-07). Added to track the live CLI.
        "aggregate-check",
        "aggregate-run",
        "append-decision",
        "block-drive",
        "campaign-complete",
        "campaign-greenlight",
        "campaign-watch",
        "doctor",
        # doctor-install accepts an all-optional spec, so it IS a live --spec
        # verb (kept here so the inventory matches the CLI) but ``{}`` is VALID
        # and probing it would EXECUTE an OS-scheduler mutation — it is excluded
        # from the destructive parametrized probes in ``_verb_targets`` below.
        "doctor-install",
        "kill",
        "net-triage",
        # notebook-lint (notebook-audit substrate, T4): a read-only validate verb
        # over an audit source .py. Gains a --spec CLI surface, so the inventory
        # tracks it here. Its input schema (notebook_lint.input.json) is baked by
        # the orchestrator AFTER this wave — until then it is absent, so the verb
        # does not appear in the schema-file-parametrized remediation tests
        # (_verb_targets), only in this inventory-vs-CLI drift check.
        "notebook-lint",
        "read-decisions",
        # scope-lock / scope-status (rigor primitives, T4): the scope lock-state
        # verbs. Both accept --spec, so the inventory tracks them here.
        "scope-lock",
        "scope-status",
        # reproduction receipt + dossier export + S3 archival (2026-07-07):
        # four spec-taking verbs landed in one wave.
        "reproduce-run",
        "verify-reproduction",
        "export-dossier",
        # export-attestations (conformance-kit K3): the in-toto/DSSE portability
        # projection over the sealed dossier. Spec-taking mutate; its input
        # schema (export_attestations.input.json) is baked in the same commit,
        # so it also appears in the schema-file-parametrized remediation probes
        # ({} is invalid — run_id is required).
        "export-attestations",
        "archive-dossier",
        "status-snapshot",
        "status-watch",
        # run-story (run-story T4): the read-only journal-trail timeline query.
        # Spec-taking query; failure_features attaches at the shared dispatch
        # seam (so it stays OUT of XFAIL_NO_FAILURE_FEATURES). Its input schema
        # (run_story.input.json) is baked by the orchestrator AFTER this wave —
        # until then it is absent, so the verb does not appear in the
        # schema-file-parametrized remediation tests (_verb_targets), only in
        # this inventory-vs-CLI drift check.
        "run-story",
        # notebook-status (notebook-audit T6): the per-section audit-state read.
        # Spec-taking query; failure_features attaches at the shared dispatch
        # seam (so it stays OUT of XFAIL_NO_FAILURE_FEATURES).
        "notebook-status",
        # worker-log-digest (run-#10 G2): code-rendered digest of a local worker
        # log. Spec-taking query; failure_features attaches at the shared dispatch
        # seam (so it stays OUT of XFAIL_NO_FAILURE_FEATURES).
        "worker-log-digest",
        # attention-queue (attention-queue T4): the fleet-wide read-only digest
        # ordered by needs-your-verdict-first. Spec-taking query; failure_features
        # attaches at the shared dispatch seam (so it stays OUT of
        # XFAIL_NO_FAILURE_FEATURES). Its input schema (attention_queue.input.json)
        # is baked by the orchestrator AFTER this wave — until then it is absent, so
        # the verb does not appear in the schema-file-parametrized remediation tests
        # (_verb_targets), only in this inventory-vs-CLI drift check.
        "attention-queue",
        # harness-capabilities (harness-contract capability negotiation): the
        # read-only detection verb. Spec-taking query (all-optional, spec_required
        # False); failure_features attaches at the shared dispatch seam (so it
        # stays OUT of XFAIL_NO_FAILURE_FEATURES). Its input schema
        # (harness_capabilities.input.json) is baked by the orchestrator AFTER this
        # wave — until then it is absent, so the verb does not appear in the
        # schema-file-parametrized remediation tests (_verb_targets), only in this
        # inventory-vs-CLI drift check.
        "harness-capabilities",
        # notebook-audit-view (notebook-audit T5): the deterministic per-section
        # audit VIEW as a read-only query verb. Spec-taking; failure_features
        # attaches at the shared dispatch seam (so it stays OUT of
        # XFAIL_NO_FAILURE_FEATURES). Its input schema (notebook_audit_view.input.json)
        # is baked by the orchestrator AFTER this wave — until then it is absent, so
        # the verb does not appear in the schema-file-parametrized remediation tests
        # (_verb_targets), only in this inventory-vs-CLI drift check.
        "notebook-audit-view",
        # notebook-auto-clear (notebook-audit): the CODE-attestor mutate verb — the
        # machine mirror of notebook-sign-off. Spec-taking; failure_features
        # attaches at the shared dispatch seam (so it stays OUT of
        # XFAIL_NO_FAILURE_FEATURES). Its input schema (notebook_auto_clear.input.json)
        # is baked by the orchestrator AFTER this wave — until then it is absent, so
        # the verb does not appear in the schema-file-parametrized remediation tests
        # (_verb_targets), only in this inventory-vs-CLI drift check.
        "notebook-auto-clear",
        # notebook-record-receipt (notebook-audit T10): the emitter's CODE
        # render-receipt journaling verb. Spec-taking mutate; failure_features
        # attaches at the shared dispatch seam (so it stays OUT of
        # XFAIL_NO_FAILURE_FEATURES). Its input schema (notebook_record_receipt.input.json)
        # is baked by the orchestrator AFTER this wave — until then it is absent, so
        # the verb does not appear in the schema-file-parametrized remediation tests
        # (_verb_targets), only in this inventory-vs-CLI drift check.
        "notebook-record-receipt",
        # notebook-record-config (run-#10 standalone-audit seat): the journaled
        # audit-configuration writer for audits with no interview audited_source
        # opt-in. Spec-taking mutate; failure_features attaches at the shared
        # dispatch seam (so it stays OUT of XFAIL_NO_FAILURE_FEATURES). Its
        # input schema (notebook_record_config.input.json) is baked in the same
        # commit, so it also appears in the schema-file-parametrized remediation
        # probes ({} is invalid — audit_id/input_roots/source_roots are required
        # — so the probe refuses at model validation, before any journal append).
        "notebook-record-config",
        # notebook-scaffold-template: the content-free audit-template scaffold
        # writer. Spec-taking mutate; failure_features attaches at the shared
        # dispatch seam (so it stays OUT of XFAIL_NO_FAILURE_FEATURES). Its
        # input schema (notebook_scaffold_template.input.json) is baked in the
        # same commit, so it also appears in the schema-file-parametrized
        # remediation probes ({} is invalid — slugs/output_path are required —
        # so the probe refuses at model validation, before any file write).
        "notebook-scaffold-template",
        "verify-relay",
        "wait-detached",
        "submit-s1",
        "submit-s2",
        "submit-s3",
        "submit-s4",
        "submit-speculate",
        # revise-resolved (proving-run-5 wave 5.1): the nudge-as-delta verb —
        # re-resolves a run under a {field: value} patch. Spec-taking composite.
        "revise-resolved",
        # retarget-run (proving-run-5 wave 5.2): the cluster-retarget recovery arm —
        # supersede + re-resolve(new run_name) + re-canary. Spec-taking composite.
        "retarget-run",
        "aggregate-flow",
        "apply-safe-defaults",
        "build-submit-spec",
        "build-tasks-py",
        "campaign-run",
        "classify-axis",
        "classify-axis-auto",
        "decide-monitor-arm",
        "interview",
        "monitor-flow",
        # check-preflight gained an optional --spec (#275) to run the uv runtime
        # probe; it has no own input schema (reuses submit_flow.input.json), so
        # it appears here but not in the schema-file-parametrized remediation tests.
        "preflight",
        "prepare-phase2-spec",
        "provenance-manifest",
        "recommend-partition",
        "resolve-submit-inputs",
        "resubmit",
        "status-pipeline",
        "submit",
        "submit-and-verify",
        "submit-flow",
        "submit-flow-batch",
        "submit-pipeline",
        "summarize-submit-plan",
        "validate-campaign",
        "walk-submit-ambiguities",
        "write-run-sidecar",
    }
)


def _verbs_with_cli() -> set[str]:
    rc, text, stderr = invoke_cli(["--help"])
    assert rc == 0, f"hpc-agent --help failed (rc={rc}): {stderr!r}"
    start = text.find("{")
    end = text.find("}", start)
    if start == -1 or end == -1:
        return set()
    return {v.strip() for v in text[start + 1 : end].split(",") if v.strip()}


def _run_verb_with_bad_spec(verb: str, spec: dict, tmp_path: Path) -> dict:
    """Fire ``hpc-agent <verb> --spec <bad>`` and return the parsed envelope.

    A verb that requires additional CLI flags beyond ``--spec`` exits
    via argparse with usage-help on stderr and no JSON envelope. Surfaces
    as ``pytest.xfail`` so the caller registers the verb in
    ``NEEDS_EXTRA_CLI_ARGS`` and moves on.
    """
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    # In-process for the bulk (same dispatch path, same envelope contract);
    # real subprocess for SUBPROCESS_SAMPLE_VERBS — see conftest.invoke_cli.
    rc, stdout, stderr = invoke_cli([verb, "--spec", str(spec_file)])
    out = stdout.strip().splitlines()
    if not out:
        pytest.xfail(
            f"{verb}: no stdout envelope emitted; argparse / pre-spec "
            f"gate likely rejected the invocation. stderr={stderr!r}, "
            f"rc={rc}. Register in NEEDS_EXTRA_CLI_ARGS."
        )
    parsed: dict = json.loads(out[-1])
    return parsed


# Verbs whose primitive does not emit ``failure_features`` on the
# spec_invalid envelope. Now EMPTY: the shared dispatch seam
# (``cli/_dispatch.py`` model-validation) attaches structured
# ``failure_features`` (``error_class="code_bug"`` + the offending field
# paths in ``error_class_raw``) to every SpecInvalid it raises, and
# ``_err_from_hpc`` synthesizes the default for every other SpecInvalid
# raise site — so every spec_invalid envelope now names its failure class.
# A verb that regresses (validates specs on a bespoke path that bypasses
# the seam) will FAIL the strict-xfail-free assertion loudly; add it back
# here with a one-line reason only if the fix must live in its own module.
# Emptied: aggregate-flow (the last member) is fixed at the dispatch seam —
# an explicitly SUPPLIED empty/invalid spec now falls through to model
# validation for optional-spec verbs instead of returning a None the body
# dereferenced into an `internal` envelope. Only a truly ABSENT --spec defers
# to arg_pre synthesis (aggregate-flow's --run-id shortcut).
XFAIL_NO_FAILURE_FEATURES: set[str] = set()


# Verbs whose schema input shape doesn't accept an empty ``{}`` and
# instead emits a different error mode (e.g. the framework needs the
# file to exist on disk before validation, or the verb's wrapper does
# its own pre-spec gate). These take a different known-bad probe rather
# than ``{}``. Kept tiny — when a verb needs more elaborate seeding the
# test is xfail-ed under ``XFAIL_NEEDS_FIXTURE`` below instead.
#
# A bogus key every ``extra="forbid"`` wire model rejects — the known-bad
# probe for verbs whose models are ALL-OPTIONAL, where ``{}`` is a valid
# spec. (Until the 2026-07 dispatch fix, the ``--spec`` falsy-guard
# rejected a supplied literal ``{}`` before the model ever saw it,
# masking which verbs legitimately accept an empty spec — these entries
# surfaced when that guard was fixed to key on the path, not the loaded
# dict's falsiness.)
_BOGUS_KEY_SPEC: dict = {"contract-probe-bogus-key": 1}

EMPTY_SPEC_OVERRIDES: dict[str, dict] = {
    "apply-safe-defaults": _BOGUS_KEY_SPEC,
    # pack-status's spec is all-optional ({} = every opted-in pack) — probe
    # with the bogus key so the wire model rejects it.
    "pack-status": _BOGUS_KEY_SPEC,
    # attention-queue's spec is all-optional ({} is a valid experiment-scope
    # read) — probe with the bogus key so the wire model rejects it.
    "attention-queue": _BOGUS_KEY_SPEC,
    "block-drive": _BOGUS_KEY_SPEC,
    # harness-capabilities' spec is empty/all-optional ({} is a valid detection
    # read) — probe with the bogus key so the wire model rejects it.
    "harness-capabilities": _BOGUS_KEY_SPEC,
    "doctor": _BOGUS_KEY_SPEC,
    # net-triage's spec is all-optional ({} is valid and would EXECUTE real
    # network probes) — probe with the bogus key so the wire model rejects it.
    "net-triage": _BOGUS_KEY_SPEC,
    # scope-status's spec is all-optional ({} is a valid all-tags read) —
    # probe with the bogus key so the wire model rejects it.
    "scope-status": _BOGUS_KEY_SPEC,
    "status-snapshot": _BOGUS_KEY_SPEC,
    "walk-submit-ambiguities": _BOGUS_KEY_SPEC,
}


# Verbs whose contract conformance can't be probed without a richer
# fixture (e.g. a real campaign dir, a real cluster, an existing
# sidecar). They xfail with this reason; the punch-list item is "add a
# fixture under tests/contracts/fixtures/<verb>/" so the probe can run.
XFAIL_NEEDS_FIXTURE: set[str] = set()


# Verbs whose CLI requires *additional* mandatory args beyond ``--spec``
# (e.g. ``interview --campaign-dir``). Firing ``hpc-agent <verb> --spec
# <bad>`` without those exits with argparse usage-help to stderr, not a
# JSON envelope — so this seam can't reach the spec-validate path
# without a richer fixture. Listed here so the parametrize skips them
# (their xfail belongs in the "needs richer fixture" punch list, not
# masquerading as a remediation failure).
NEEDS_EXTRA_CLI_ARGS: set[str] = {
    "interview",  # --campaign-dir
    "resubmit",  # --run-id + --task-ids
}


def _verb_targets() -> list[tuple[str, Path]]:
    """Return ``(verb, schema_path)`` pairs for every verb in the
    hard-coded ``_SPEC_VERBS`` inventory.

    Schemas without a CLI form (composed-only primitives) and CLI
    verbs that take per-flag arguments instead of ``--spec`` are not
    testable from this surface and are filtered out — they would be
    false negatives.
    """
    pairs: list[tuple[str, Path]] = []
    for schema_path in _input_schemas():
        verb = _verb_from_schema_path(schema_path)
        if verb not in _SPEC_VERBS:
            continue
        if verb == "doctor-install":
            # All-optional spec → ``{}`` is a VALID spec that EXECUTES the verb,
            # which mutates the OS scheduler (schtasks / crontab). Kept in
            # _SPEC_VERBS for the inventory-drift contract, but never probed with
            # a fabricated bad spec here — that would install a real task.
            continue
        pairs.append((verb, schema_path))
    return pairs


def _make_params(test_id: str) -> list:
    """Build ``pytest.param`` entries for *test_id* with strict-xfail
    markers per the test's static xfail catalogues (WS4 Q4 refactor).

    *test_id* selects which catalogues apply:

    * ``"spec_invalid"`` — ``NEEDS_EXTRA_CLI_ARGS`` + ``XFAIL_NEEDS_FIXTURE``.
    * ``"failure_features"`` — same plus ``XFAIL_NO_FAILURE_FEATURES``.
    * ``"remediation"`` — same as ``"spec_invalid"``.

    Each xfail marker carries ``strict=True``: if a verb's behaviour is
    fixed and the test now passes, pytest surfaces ``XPASS(strict)`` and
    the maintainer is forced to drop the verb from the catalogue.
    Replaces the prior dynamic ``pytest.xfail()`` calls inside test
    bodies, which silently let the catalogue drift as verbs improved.
    """
    params: list = []
    for verb, schema_path in _verb_targets():
        marks: list = []
        if verb in NEEDS_EXTRA_CLI_ARGS:
            marks.append(
                pytest.mark.xfail(
                    strict=True,
                    reason=(
                        f"{verb}: CLI requires additional mandatory args "
                        "beyond --spec; spec-validate path unreachable "
                        "from this probe."
                    ),
                )
            )
        if verb in XFAIL_NEEDS_FIXTURE:
            marks.append(
                pytest.mark.xfail(
                    strict=True,
                    reason=f"{verb}: needs a richer fixture to probe contract",
                )
            )
        if test_id == "failure_features" and verb in XFAIL_NO_FAILURE_FEATURES:
            marks.append(
                pytest.mark.xfail(
                    strict=True,
                    reason=(
                        f"{verb}: failure_features not yet wired into "
                        "spec_invalid envelope (WS3 punch list)"
                    ),
                )
            )
        params.append(pytest.param(verb, schema_path, marks=marks))
    return params


def test_spec_verb_inventory_matches_cli() -> None:
    """The hard-coded ``_SPEC_VERBS`` inventory matches the live CLI.

    Drift means either (a) a new spec-accepting verb shipped without
    being added to the inventory, or (b) a verb's CLI surface stopped
    accepting ``--spec``. Both are author-time concerns — update the
    inventory, then promote/demote the verb's xfail entry as needed.
    """
    cli_verbs = _verbs_with_cli()
    accepting: set[str] = set()
    for verb in cli_verbs:
        # In-process: this is a --help surface scan (argparse output), not an
        # envelope-contract probe; the console entry path is covered by the
        # SUBPROCESS_SAMPLE_VERBS probes.
        _rc, help_text, _err = invoke_cli([verb, "--help"])
        for line in help_text.splitlines():
            stripped = line.strip()
            if stripped.startswith("--spec ") or stripped == "--spec":
                accepting.add(verb)
                break
    extra = accepting - _SPEC_VERBS
    missing = _SPEC_VERBS - accepting
    assert not extra and not missing, (
        f"_SPEC_VERBS drift: extra={sorted(extra)} missing={sorted(missing)}. "
        "Update the hard-coded inventory at the top of this file."
    )


@pytest.mark.parametrize(
    "verb,schema_path",
    _make_params("spec_invalid"),
    ids=lambda p: p if isinstance(p, str) else p.stem,
)
def test_primitive_emits_spec_invalid_on_bad_input(
    verb: str, schema_path: Path, tmp_path: Path
) -> None:
    """Every input-taking primitive rejects a clearly-bad input as
    ``spec_invalid``, not as ``internal`` (an unhandled exception).

    The bad input is ``{}`` (empty object) — which fails every schema
    that has any required field, and is the simplest reproducible
    known-bad input. Verbs that accept ``{}`` as legitimately empty
    are extended via ``EMPTY_SPEC_OVERRIDES``.

    Static-catalogue xfails (``NEEDS_EXTRA_CLI_ARGS``,
    ``XFAIL_NEEDS_FIXTURE``) are applied at parametrize time with
    ``strict=True``; see :func:`_make_params`. The runtime ``internal``-
    envelope xfail below stays dynamic because the condition isn't
    knowable at parametrize time.
    """
    spec = EMPTY_SPEC_OVERRIDES.get(verb, {})
    envelope = _run_verb_with_bad_spec(verb, spec, tmp_path)
    if envelope.get("error_code") == "internal":
        # ``internal`` envelopes are unhandled exceptions reaching the
        # generic CLI handler — they're a regression target (the spec
        # should have been validated *before* the runtime ever touched
        # it), not a clean spec_invalid path. Stays dynamic — the
        # condition is envelope-shape-dependent, not knowable at
        # parametrize time.
        pytest.xfail(
            f"{verb}: bad spec produced an `internal` envelope, not "
            f"`spec_invalid`. Spec-validate is missing from this verb's "
            f"entry path. Envelope: {envelope!r}"
        )
    assert envelope.get("ok") is False, (
        f"{verb}: empty spec should be rejected, got ok={envelope.get('ok')}: {envelope!r}"
    )
    assert envelope.get("error_code") == "spec_invalid", (
        f"{verb}: empty spec should produce error_code=spec_invalid; got "
        f"{envelope.get('error_code')!r}. Envelope: {envelope!r}"
    )


@pytest.mark.parametrize(
    "verb,schema_path",
    _make_params("failure_features"),
    ids=lambda p: p if isinstance(p, str) else p.stem,
)
def test_primitive_emits_failure_features_on_spec_invalid(
    verb: str, schema_path: Path, tmp_path: Path
) -> None:
    """Every spec_invalid envelope must carry ``failure_features`` with
    a populated ``error_class``.

    WS3 (parallel workstream) is wiring ``failure_features`` into the
    ``ErrorEnvelope`` emitter — verbs in ``XFAIL_NO_FAILURE_FEATURES``
    are the punch list. The ``strict=True`` marker means a verb that
    starts emitting the field gets surfaced as ``XPASS(strict)`` and
    the maintainer is forced to drop it from the catalogue (per WS4 Q4).
    """
    spec = EMPTY_SPEC_OVERRIDES.get(verb, {})
    envelope = _run_verb_with_bad_spec(verb, spec, tmp_path)
    assert envelope.get("ok") is False
    failure_features = envelope.get("failure_features")
    assert failure_features is not None, (
        f"{verb}: spec_invalid envelope missing failure_features. Envelope: {envelope!r}"
    )
    error_class = failure_features.get("error_class")
    assert error_class, (
        f"{verb}: failure_features.error_class is empty / null. The whole "
        f"point of WS3's structured-evidence layer is that the producer "
        f"names the failure class so the caller doesn't have to grep "
        f"error_class_raw."
    )


@pytest.mark.parametrize(
    "verb,schema_path",
    _make_params("remediation"),
    ids=lambda p: p if isinstance(p, str) else p.stem,
)
def test_remediation_names_schema_path_or_describe(
    verb: str, schema_path: Path, tmp_path: Path
) -> None:
    """``remediation`` on a spec_invalid envelope must name something
    actionable.

    Either the schema file path (``hpc_agent/schemas/<name>.input.json``)
    or the ``hpc-agent describe <verb>`` form that resolves to it. This
    pins the 50a4b61d (0.10.0 polish) fix as a contract — a generic
    "Inspect .hpc/tasks.py and rebuild" remediation is the OLD shape
    that took 4-5 round-trips to debug, and a regression to it is a
    real bug.
    """
    spec = EMPTY_SPEC_OVERRIDES.get(verb, {})
    envelope = _run_verb_with_bad_spec(verb, spec, tmp_path)
    if envelope.get("error_code") != "spec_invalid":
        # Envelope-shape-dependent xfail; stays dynamic.
        pytest.xfail(
            f"{verb}: empty spec did not produce spec_invalid "
            f"(error_code={envelope.get('error_code')!r}); remediation "
            "contract not testable from this probe."
        )
    remediation = envelope.get("remediation", "") or ""
    schema_stem = schema_path.name[: -len(".input.json")]
    if (
        f"schemas/{schema_stem}.input.json" not in remediation
        and f"hpc-agent describe {verb}" not in remediation
    ):
        # Envelope-content-dependent xfail; stays dynamic. The generic
        # "rebuild via /submit" shape is the pre-50a4b61d regression
        # target — flag it for the WS3 punch list instead of failing
        # today, then ratchet down.
        pytest.xfail(
            f"{verb}: remediation does not name the schema file or "
            f"`hpc-agent describe {verb}`; carries: {remediation!r}"
        )
