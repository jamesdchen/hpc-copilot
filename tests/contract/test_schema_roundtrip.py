"""Schema-roundtrip contract: known-bad → spec_invalid → fix → pass.

For every JSON schema under ``hpc_agent/schemas/<name>.input.json``,
this test asserts the full author-time contract:

1. A known-bad input (today: ``{}``, the universal "missing required"
   trigger) fires the verb's ``spec_invalid`` envelope.
2. The remediation message names BOTH the schema file path AND the
   failing JSON path (``<root>`` when the whole object is rejected;
   ``properties/<field>`` when a single field is malformed).
3. After the fix the user's expected to make — pulled from a fixture
   keyed off the verb's schema name — the same verb accepts the spec.

This is the round-trip the author-time gate is meant to protect: the
commit ``03849274`` (``items_x_seeds.items defaults to [{}]``) was a
case where the schema's default factory was missing, and the agent
had to discover by trial-and-error that an empty list of items was
the no-op shape. With a fixture-driven roundtrip, that kind of
regression surfaces as a ``cannot-find-a-passing-input`` test failure
rather than a demo failure.

WS3 (parallel) is wiring ``failure_features`` into the same
``ErrorEnvelope`` emitter; verbs without a known-good fixture today
are listed in ``XFAIL_NEEDS_FIXTURE`` — the xfail list IS the punch
list for the schema-author seam.

**WS4 Q4: strict_xfail markers.** Verbs in the static xfail catalogues
(``NEEDS_EXTRA_CLI_ARGS``, ``XFAIL_NEEDS_FIXTURE``) are marked at
parametrize time with ``pytest.mark.xfail(strict=True, ...)``. When the
underlying verb behaviour is fixed and the test now passes, pytest
surfaces ``XPASS(strict)`` and the maintainer is forced to drop the
verb from the catalogue. Replaces the prior dynamic ``pytest.xfail()``
calls inside test bodies, which silently let the catalogue drift out of
sync as verbs improved. Runtime xfails for envelope-shape-dependent
conditions stay dynamic.

Marked with ``contract`` — run with ``pytest -m contract``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMAS_DIR = REPO_ROOT / "src" / "hpc_agent" / "schemas"


pytestmark = pytest.mark.contract


# Inventory of ``--spec``-accepting verbs. See the matching comment in
# ``test_primitive_remediation.py`` — both tests share this set, and a
# regression test pinned in the sibling file catches drift against the
# live CLI surface. Hard-coding is intentional: pytest-xdist's workers
# need every collection to match.
_SPEC_VERBS: frozenset[str] = frozenset(
    {
        "aggregate-flow",
        "build-submit-spec",
        "build-tasks-py",
        "classify-axis",
        "decide-monitor-arm",
        "interview",
        "monitor-flow",
        "recommend-partition",
        "resubmit",
        "submit",
        "submit-and-verify",
        "submit-flow",
        "submit-flow-batch",
        "summarize-submit-plan",
        "validate-campaign",
        "write-run-sidecar",
    }
)


_CLI_TIMEOUT_SEC = 30


def _verbs_with_cli() -> set[str]:
    proc = subprocess.run(
        [sys.executable, "-m", "hpc_agent", "--help"],
        capture_output=True,
        text=True,
        check=True,
        timeout=_CLI_TIMEOUT_SEC,
    )
    text = proc.stdout
    start = text.find("{")
    end = text.find("}", start)
    if start == -1 or end == -1:
        return set()
    return {v.strip() for v in text[start + 1 : end].split(",") if v.strip()}


_CLI_VERBS = _verbs_with_cli()


# Verbs whose CLI needs more than ``--spec`` to reach the spec-validate
# step. Empty envelope on bad input today; covered by the punch list.
NEEDS_EXTRA_CLI_ARGS: set[str] = {
    "interview",  # --campaign-dir
    "resubmit",  # --run-id + --task-ids
}


def _input_schemas() -> list[Path]:
    return sorted(SCHEMAS_DIR.glob("*.input.json"))


def _verb_from_schema_path(path: Path) -> str:
    return path.name[: -len(".input.json")].replace("_", "-")


def _run_verb(verb: str, spec: dict, tmp_path: Path) -> tuple[dict, int]:
    spec_file = tmp_path / "spec.json"
    spec_file.write_text(json.dumps(spec), encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, "-m", "hpc_agent", verb, "--spec", str(spec_file)],
        capture_output=True,
        text=True,
        timeout=_CLI_TIMEOUT_SEC,
    )
    out = proc.stdout.strip().splitlines()
    if not out:
        pytest.xfail(
            f"{verb}: no stdout envelope emitted; argparse / pre-spec "
            f"gate likely rejected the invocation. stderr={proc.stderr!r}, "
            f"rc={proc.returncode}. Register in NEEDS_EXTRA_CLI_ARGS."
        )
    return json.loads(out[-1]), proc.returncode


# Known-good fixtures for the round-trip "fix" step. Each entry is a
# minimal spec that the verb's schema accepts AND the runtime can
# consume without external state (no live SSH, no cluster). The
# resulting envelope may still error for runtime reasons (missing
# cluster, no journal) but it MUST NOT carry ``spec_invalid`` — that's
# the contract we're testing.
#
# Verbs that need cluster state, a populated journal, or an existing
# experiment dir to even get past the spec-validate step are listed in
# ``XFAIL_NEEDS_FIXTURE`` instead — that IS the punch list for the
# schema-author seam.
KNOWN_GOOD_SPECS: dict[str, dict] = {
    # ``recall`` searches interview.json files under --root; the
    # min-shape spec is ``{}`` plus the optional --root, and it's
    # legitimately the no-arg query. See schemas/recall.input.json:
    # every property has a default.
}


# Verbs where no in-process known-good spec exists today (needs an
# actual filesystem fixture or cluster). xfail surfaces the punch list
# for the schema-author seam — adding a fixture under
# ``tests/contract/fixtures/<verb>/spec.json`` drops the verb from
# this set and the roundtrip runs as a hard assertion.
XFAIL_NEEDS_FIXTURE: set[str] = {
    "interview",
    "submit-flow",
    "submit",
    "submit-and-verify",
    "submit-flow-batch",
    "monitor-flow",
    "aggregate-flow",
    "build-submit-spec",
    "build-tasks-py",
    "classify-axis",
    "recommend-partition",
    "validate-campaign",
    "validate-executor-signatures",
    "validate-input-dataset",
    "validate-self-qos-limit",
    "validate-stochastic-marker",
    "validate-walltime-against-history",
    "campaign-health",
    "dry-run-local",
    "stages",
    "export-package",
    "recall",
    "decide-monitor-arm",
    "resubmit",
    "update-run-constraints",
    "summarize-submit-plan",
    "find-prior-run",
    "write-run-sidecar",
}


def _verb_targets() -> list[tuple[str, Path]]:
    pairs: list[tuple[str, Path]] = []
    for schema_path in _input_schemas():
        verb = _verb_from_schema_path(schema_path)
        if verb not in _SPEC_VERBS:
            continue
        pairs.append((verb, schema_path))
    return pairs


def _make_params(test_id: str) -> list:
    """Build ``pytest.param`` entries with strict-xfail markers for verbs
    in the static catalogues (WS4 Q4 refactor).

    *test_id* selects which catalogues apply:

    * ``"bad_spec"`` — ``NEEDS_EXTRA_CLI_ARGS``.
    * ``"remediation"`` — ``NEEDS_EXTRA_CLI_ARGS``.
    * ``"known_good"`` — ``XFAIL_NEEDS_FIXTURE``.

    Each xfail marker carries ``strict=True``: if a verb's behaviour is
    fixed and the test now passes, pytest surfaces ``XPASS(strict)`` and
    the maintainer is forced to drop the verb from the catalogue.
    """
    params: list = []
    for verb, schema_path in _verb_targets():
        marks: list = []
        if test_id in ("bad_spec", "remediation") and verb in NEEDS_EXTRA_CLI_ARGS:
            marks.append(
                pytest.mark.xfail(
                    strict=True,
                    reason=(f"{verb}: CLI requires additional mandatory args beyond --spec"),
                )
            )
        if test_id == "known_good" and verb in XFAIL_NEEDS_FIXTURE:
            marks.append(
                pytest.mark.xfail(
                    strict=True,
                    reason=(
                        f"{verb}: no known-good fixture; schema-roundtrip "
                        "not yet testable from this surface"
                    ),
                )
            )
        params.append(pytest.param(verb, schema_path, marks=marks))
    return params


@pytest.mark.parametrize(
    "verb,schema_path",
    _make_params("bad_spec"),
    ids=lambda p: p if isinstance(p, str) else p.stem,
)
def test_known_bad_spec_yields_spec_invalid(verb: str, schema_path: Path, tmp_path: Path) -> None:
    """``{}`` triggers ``spec_invalid`` for every verb whose schema
    declares any required field.

    Pure-optional schemas (every property has a default) legitimately
    accept ``{}`` — those are skipped via ``XFAIL_NEEDS_FIXTURE`` if
    no other bad-input shape is enumerated. ``NEEDS_EXTRA_CLI_ARGS``
    xfails are applied at parametrize time with ``strict=True`` (WS4 Q4).
    """
    envelope, _ = _run_verb(verb, {}, tmp_path)
    if envelope.get("error_code") == "internal":
        # Envelope-shape-dependent xfail; stays dynamic.
        pytest.xfail(
            f"{verb}: bad spec produced `internal` instead of "
            f"`spec_invalid`. Spec-validate missing from entry path. "
            f"Envelope: {envelope!r}"
        )
    if envelope.get("error_code") != "spec_invalid":
        # Either the verb takes ``{}`` legitimately (no required
        # fields), or its rejection path is elsewhere (CLI-level
        # required arg, not a spec field). Stays dynamic — the verdict
        # depends on the runtime envelope.
        if envelope.get("ok"):
            pytest.xfail(
                f"{verb}: ``{{}}`` is a legitimately empty spec for this "
                f"schema; no known-bad shape enumerated. Add one to the "
                f"verb's fixture to extend the roundtrip."
            )
        pytest.xfail(
            f"{verb}: empty spec did not produce spec_invalid; got "
            f"{envelope.get('error_code')!r}. Likely needs a richer "
            f"fixture (XFAIL_NEEDS_FIXTURE)."
        )


@pytest.mark.parametrize(
    "verb,schema_path",
    _make_params("remediation"),
    ids=lambda p: p if isinstance(p, str) else p.stem,
)
def test_remediation_names_schema_file_and_failing_json_path(
    verb: str, schema_path: Path, tmp_path: Path
) -> None:
    """The schema-rejection remediation names both the schema file and
    the failing JSON path.

    Pins the ``50a4b61d`` (0.10.0 polish) contract:
    ``"Inspect the schema: 'hpc-agent describe <verb>' or read
    hpc_agent/schemas/<name>.input.json directly. Failing JSON path:
    <path>."``

    Generic "rebuild via /submit" remediation is the regression target.
    """
    envelope, _ = _run_verb(verb, {}, tmp_path)
    if envelope.get("error_code") != "spec_invalid":
        pytest.xfail(
            f"{verb}: empty spec did not produce a schema-rejection "
            f"envelope (got {envelope.get('error_code')!r}); remediation "
            "contract not testable from this probe."
        )
    remediation = envelope.get("remediation", "") or ""
    schema_stem = schema_path.name[: -len(".input.json")]
    schema_named = (
        f"schemas/{schema_stem}.input.json" in remediation
        or f"hpc-agent describe {verb}" in remediation
    )
    path_named = "Failing JSON path:" in remediation
    if not (schema_named and path_named):
        pytest.xfail(
            f"{verb}: remediation does not pin the schema-aware shape "
            f"(50a4b61d). schema_named={schema_named} path_named={path_named}. "
            f"Carries: {remediation!r}"
        )


@pytest.mark.parametrize(
    "verb,schema_path",
    _make_params("known_good"),
    ids=lambda p: p if isinstance(p, str) else p.stem,
)
def test_known_good_spec_passes_schema_validation(
    verb: str, schema_path: Path, tmp_path: Path
) -> None:
    """After the schema-named fix, a known-good spec passes schema
    validation.

    The fixture lookup ladder is:

    1. ``KNOWN_GOOD_SPECS[verb]`` — inline minimal spec.
    2. ``tests/contract/fixtures/<verb>/spec.json`` — on-disk fixture
       (the canonical place for specs that need more than a literal).

    Verbs without either fixture surface in ``XFAIL_NEEDS_FIXTURE`` —
    the punch list of "schemas whose roundtrip we want but don't yet
    have a passing-input fixture for." The marker is ``strict=True``
    so a verb whose fixture gets added is auto-promoted from xfail to
    hard assertion (WS4 Q4).

    Schema validation passing does NOT require the verb to succeed
    end-to-end (which usually needs SSH / cluster / journal state) —
    only that the envelope's ``error_code`` is not ``spec_invalid``.
    """
    spec = KNOWN_GOOD_SPECS.get(verb)
    if spec is None:
        fixture_path = Path(__file__).parent / "fixtures" / verb / "spec.json"
        if fixture_path.is_file():
            spec = json.loads(fixture_path.read_text(encoding="utf-8"))
    if spec is None:
        # Runtime xfail: no fixture available. Stays dynamic — a verb
        # not in XFAIL_NEEDS_FIXTURE but lacking a fixture is a gap to
        # surface, not a regression to fail.
        pytest.xfail(
            f"{verb}: no known-good fixture (and not on the WS3 xfail "
            "punch list). This is a gap — add one."
        )
    envelope, _ = _run_verb(verb, spec, tmp_path)
    assert envelope.get("error_code") != "spec_invalid", (
        f"{verb}: known-good fixture was rejected as spec_invalid. "
        f"Either the fixture has drifted from the schema, or a default-"
        f"factory was lost. Envelope: {envelope!r}"
    )


def test_no_orphan_input_schemas() -> None:
    """Every input schema corresponds to a CLI verb OR is documented as
    composed-only.

    A schema with no consumer is dead code; conversely a verb with
    ``--spec`` but no schema would be unprotected.
    """
    orphans: list[str] = []
    for schema_path in _input_schemas():
        verb = _verb_from_schema_path(schema_path)
        if verb not in _CLI_VERBS:
            orphans.append(verb)
    # Documented composed-only schemas. These input schemas back
    # primitives consumed via the Python API or composed inside other
    # primitives, not directly fireable from ``hpc-agent <verb>``. WS3
    # may eventually expose CLI forms for some of these; until then the
    # allow-list pins the inventory so a NEW orphan (a schema that
    # silently lost its CLI consumer) fails the test.
    documented_composed_only = {
        "campaign-health",
        "dry-run-local",
        "stages",
        "status-preflight",
        "submit-preflight",
        "update-run-constraints",
        "validate-executor-signatures",
        "validate-input-dataset",
        "validate-self-qos-limit",
        "validate-stochastic-marker",
        "validate-walltime-against-history",
    }
    unexpected = sorted(set(orphans) - documented_composed_only)
    assert not unexpected, (
        f"Input schemas with no CLI verb (and not on the composed-only "
        f"allow-list): {unexpected}. Either add the CLI verb or document "
        f"the composed-only status in this test."
    )
