"""Boundary contract for the domain-pack substrate: core BINDS pack content as
data, ECHOES an opaque ``{pack, version, sha}`` identity, and GATES on named
receipts — and never learns, ships, imports, or interprets a line of what a pack
MEANS.

``docs/design/domain-packs.md`` is the design; the enforcement-rows table at its
foot is what this suite mechanizes. Every pin here holds one line of the Q1
boundary (``docs/internals/engineering-principles.md``, "substrate, not
semantics"), extended to the domain layer:

* **the seam vocabulary is CLOSED and shape-only** — ``SEAM_NAMES`` equals the
  agreed set exactly, and the loaders validate STRUCTURE, never a value's meaning
  (a behaviour pin: nonsense-but-well-formed vocab is accepted — there is no
  allowlist of "recognized" readers/patterns/tolerances).
* **core ships NO default pack and NO pack vocabulary** — no manifest, seam file,
  or inlined reader/pattern/axis-hint vocabulary under ``src/hpc_agent/`` (a
  package-data scan + a no-literal-vocab AST pin; the clusters.yaml package-data
  leak is the cautionary precedent).
* **core never imports/executes pack content** — no ``importlib`` /
  ``entry_points`` / ``exec`` / ``eval`` / ``__import__`` anywhere in the pack
  modules (DP3 distribution-invisible; DP2 code-never-runs — the
  ``test_bundler_copies_bytes_and_never_parses_content`` form).
* **pack attestations route through the ONE kernel** — bind, receipt, and
  reduction reach ``state/attestation.py::bind``/``reduce`` and never re-inline a
  recompute-and-compare or a newest-first drift (getsource pins).
* **receipt shas are server-computed** — ``PackRecordReceiptSpec`` carries no
  caller-suppliable sha (a wire-schema pin; the fire test that a mid-flight
  content change is refused lives in ``tests/ops/pack/test_record_receipt_op.py``,
  referenced here).
* **a CODE receipt never satisfies a HUMAN tier** — the pack blocks and the
  ``"pack"`` scope kind are absent from every human-authorship block/scope set in
  ``ops/decision/journal.py`` (the no-affordance pin; the sign-off authorship
  fire-test family stays untouched).
* **no pack-domain vocabulary on the wire** — every pack Pydantic model exposes a
  property NAME from the mechanism set only, never a domain word (the
  ``_schema_property_names`` recursive walk, mirrored from
  ``test_dossier_boundary.py``).

House style: mirrors ``test_dossier_boundary.py`` (AST + closed authoritative sets
kept inline so drift surfaces here) and ``test_pack_wire.py`` (the wire-side pins
this suite is the contracts-level home of).
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "hpc_agent"

# --- the pack modules this suite pins ---------------------------------------

# The core files that make up the pack substrate. The AST pins (no-import,
# no-literal-vocab) sweep exactly these; the getsource pins reach into the
# record/reduce ones. DERIVED by glob, not hand-listed: the auto-remedy wave
# added ``ops/pack/refresh_op.py`` + ``state/pack_sweep.py`` and a hand-listed
# tuple silently missed both — a new pack module must be pinned the day it
# lands, not when someone remembers to append it here.
_PACK_STATE_FILES = tuple(sorted((_SRC / "state").glob("pack*.py")))
_PACK_OPS_FILES = tuple(sorted((_SRC / "ops" / "pack").glob("*.py"))) + (
    _SRC / "ops" / "pack_gate.py",
)
_ALL_PACK_FILES = _PACK_STATE_FILES + _PACK_OPS_FILES


def test_pack_file_lists_cover_the_known_substrate() -> None:
    """The derived scan set covers every known pack module (glob sanity pin)."""
    names = {p.name for p in _ALL_PACK_FILES}
    assert {
        "pack.py",
        "pack_declarations.py",
        "pack_receipts.py",
        "pack_sweep.py",
        "bind_op.py",
        "record_receipt_op.py",
        "status_op.py",
        "refresh_op.py",
        "pack_gate.py",
    } <= names, f"pack boundary scan set lost a known module: {sorted(names)}"


# --- authoritative closed sets (kept inline; drift surfaces here) -----------

# The closed seam vocabulary. Equality (not subset) against the live
# ``SEAM_NAMES`` — adding a seam is a reviewed vocabulary change that lands HERE.
# ``actor_policy`` (multi-human MH8) is the ONE reserved future member; it enters
# via the doc's reviewed process when multi-human lands, not before.
_EXPECTED_SEAMS = frozenset(
    {
        "reader_calls",
        "failure_patterns",
        "axis_hints",
        "audit_template",
        "tolerances",
        "registration_fields",
    }
)

# Domain-semantics vocabulary core must never NAME on the wire (field names only —
# prose/descriptions are fine). Mirrors
# ``test_dossier_boundary.py::_FORBIDDEN_FIELD_NAMES`` and
# ``test_pack_wire.py``: the pack echo is ``{pack, version, sha}`` and nothing
# meaning-bearing; a reader/pattern/holdout/metric field is the leak.
_FORBIDDEN_FIELD_NAMES = frozenset(
    {
        "control",
        "controls",
        "unit",
        "units",
        "metric",
        "metrics",
        "holdout",
        "treatment",
        "baseline",
        "significance",
        "placebo",
        "anchor",
        "reader",
        "readers",
        "widget",
        "widgets",
    }
)

# The import/execute names a pack module must never reach for over pack content
# (DP2/DP3). ``__import__`` included; ``entry_points`` is the plugin-lane seam
# packs must NOT touch (packs are the TRUST lane, content-addressed).
_FORBIDDEN_IMPORT_NAMES = frozenset(
    {"importlib", "import_module", "exec", "eval", "__import__", "entry_points"}
)


# --- helpers ----------------------------------------------------------------


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _docstring_node_ids(tree: ast.Module) -> set[int]:
    """The id() of every DOCSTRING constant (module/class/def leading string).

    Docstrings are prose ABOUT the mechanism — they legitimately say "reader",
    "widget", "metric". The no-literal-vocab pin targets inlined DATA VALUES (a
    hardcoded reader-name list, a pattern string), so it excludes docstrings; a
    real vocabulary value would land as a non-docstring literal and trip.
    """
    out: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            out.add(id(first.value))
    return out


def _data_string_constants(tree: ast.Module) -> list[str]:
    """Every string-literal constant EXCEPT docstrings — the inlined data values."""
    doc_ids = _docstring_node_ids(tree)
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in doc_ids
    ]


def _schema_property_names(schema: dict[str, Any]) -> set[str]:
    """Every property NAME anywhere in a JSON schema, recursively (names only).

    Copied from ``test_dossier_boundary.py`` — walks ``properties`` keys through
    nested ``$defs``/``items``; descriptions and titles are never walked, so a
    domain word in prose never trips the forbidden-vocabulary test.
    """
    names: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                names.update(k for k in props if isinstance(k, str))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return names


def _pack_wire_models() -> list[type[BaseModel]]:
    """Every pack Pydantic wire model — the closed surface the vocab walk covers."""
    from hpc_agent._wire.actions import pack_bind, pack_record_receipt, pack_status

    models: list[type[BaseModel]] = []
    for mod in (pack_bind, pack_record_receipt, pack_status):
        for obj in vars(mod).values():
            if isinstance(obj, type) and issubclass(obj, BaseModel) and obj is not BaseModel:
                models.append(obj)
    return models


# --- (a) the seam vocabulary is CLOSED and shape-only -----------------------


def test_seam_names_equal_the_closed_set() -> None:
    """``state/pack.py::SEAM_NAMES`` equals the agreed seam set — exactly.

    Equality, not subset: a new seam cannot be added without landing here as a
    reviewed vocabulary change (the ``DOSSIER_SOURCES`` equality-pin pattern).
    """
    from hpc_agent.state.pack import SEAM_NAMES

    assert frozenset(SEAM_NAMES) == _EXPECTED_SEAMS, (
        "SEAM_NAMES drifted from the closed seam vocabulary. "
        f"expected {sorted(_EXPECTED_SEAMS)}, found {sorted(SEAM_NAMES)}. Adding a "
        "seam is a reviewed change (update this pin and the design doc together)."
    )


def test_axis_literals_are_derived_never_inlined() -> None:
    """``AXIS_LITERALS`` is DERIVED from core's ``DataAxis`` union, not hand-listed.

    A pack's ``axis_hints`` may only name a core axis literal (identity against an
    EXISTING closed vocabulary — never a new axis kind). If core hand-inlined the
    axis names it would own a second, drift-prone axis vocabulary; deriving them
    from ``DataAxis`` proves it does not. Pin: the source computes the set via
    ``typing.get_args(DataAxis)``, and the value equals that derivation.
    """
    import typing

    from hpc_agent.experiment_kit.axis import DataAxis
    from hpc_agent.state import pack

    src = (_SRC / "state" / "pack.py").read_text(encoding="utf-8")
    assert "get_args(DataAxis)" in src, (
        "AXIS_LITERALS must be derived from DataAxis via typing.get_args, not "
        "hand-inlined — core never owns a second axis vocabulary."
    )
    assert frozenset(pack.AXIS_LITERALS) == frozenset(
        t.__name__ for t in typing.get_args(DataAxis)
    ), "AXIS_LITERALS no longer equals the DataAxis-derived set."


def test_seam_loaders_are_shape_only_no_value_allowlist() -> None:
    """The seam loaders accept ARBITRARY well-formed vocab — no meaning check.

    A behaviour pin (the strongest form of "shape only, never meaning"): feed each
    loader deliberately nonsense-but-well-shaped values and assert acceptance. If a
    loader grew an allowlist of "recognized" readers / a privileged pattern id / a
    per-metric tolerance rule, one of these made-up values would be rejected and
    this test would fire. Core matches by IDENTITY and counts; it never asks what a
    value MEANS.
    """
    from hpc_agent.state import pack

    # S1: any dotted callable name is accepted (no reader allowlist).
    assert pack.load_reader_calls(["zz.made_up_reader", "nonsense.qqq"], source="s") == [
        "zz.made_up_reader",
        "nonsense.qqq",
    ]

    # S2: any slug id → any compiling regex (no privileged pattern id).
    assert pack.load_failure_patterns({"zzz-made-up": r"\d+ nonsense"}, source="s") == {
        "zzz-made-up": r"\d+ nonsense"
    }

    # S3: any regex + any CORE axis literal (identity against DataAxis only).
    some_axis = sorted(pack.AXIS_LITERALS)[0]
    assert pack.load_axis_hints([{"pattern": r"^zqx", "axis": some_axis}], source="s") == [
        {"pattern": r"^zqx", "axis": some_axis}
    ]

    # S5: any slug id → any number (no per-metric semantic).
    assert pack.load_tolerances({"zz-made-up": 3.14}, source="s") == {"zz-made-up": 3.14}

    # S6: any slug field (reserved; counted, never interpreted).
    assert pack.load_registration_fields(["zz-made-up-field"], source="s") == ["zz-made-up-field"]


def test_axis_hint_rejects_non_core_axis() -> None:
    """The ONE identity check a loader DOES make is against core's OWN vocabulary.

    Shape-only never means "accept anything": an ``axis_hints`` ``axis`` must be a
    core ``DataAxis`` literal (identity against an existing closed set, not a new
    vocabulary). A made-up axis kind is refused — proving a pack can never mint a
    new axis. This is identity against CORE's set, never a domain-meaning check.
    """
    from hpc_agent import errors
    from hpc_agent.state import pack

    with pytest.raises(errors.SpecInvalid):
        pack.load_axis_hints([{"pattern": "x", "axis": "TotallyNewAxisKind"}], source="s")


# --- (b) no default pack, no inlined pack vocabulary ------------------------


def test_core_ships_no_pack_manifest_or_seam_data() -> None:
    """No pack manifest / seam-data file ships under ``src/hpc_agent/``.

    The package-data scan (the clusters.yaml leak is the cautionary precedent): a
    pack manifest is a JSON object carrying ``seams`` + ``files`` + ``fills_slots``;
    a ``packs/`` data dir under core is equally forbidden. Core content-addresses a
    CALLER-referenced pack; it never bundles one in the wheel. The toy pack lives
    under ``examples/`` and ``tests/`` — never here.
    """
    import json

    # No ``packs/`` data directory anywhere under core.
    pack_dirs = [p for p in _SRC.rglob("packs") if p.is_dir()]
    assert not pack_dirs, (
        f"a 'packs/' directory ships under src/hpc_agent/: {pack_dirs}. Core bundles "
        "no pack — a pack is caller-referenced and content-addressed (DP3)."
    )

    # No JSON file under core is shaped like a pack manifest.
    offenders: list[str] = []
    for jf in _SRC.rglob("*.json"):
        try:
            doc = json.loads(jf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and "seams" in doc and "files" in doc and "fills_slots" in doc:
            offenders.append(str(jf.relative_to(_REPO_ROOT)))
    assert not offenders, (
        f"pack-manifest-shaped JSON ships under src/hpc_agent/: {offenders}. Core "
        "ships no default pack (no bundled manifest, ever)."
    )


def test_no_inlined_reader_pattern_or_axis_vocabulary() -> None:
    """No forbidden domain word appears as a string LITERAL in a pack module.

    The no-literal-vocab AST pin: core's pack modules are LOADERS + resolvers +
    gates — they carry mechanism nouns (``pack``, ``slot``, ``manifest``,
    ``content_sha``, seam NAMES) but never a domain value (a reader name, a pattern
    string, an axis-hint word). A forbidden domain word landing as a literal here
    is a vocabulary smuggled into core. Names only: the seam-name keys (e.g.
    ``reader_calls``) are mechanism identifiers, not the forbidden word ``reader``
    standing alone, so they never trip this.
    """
    offenders: list[tuple[str, str]] = []
    for path in _ALL_PACK_FILES:
        for text in _data_string_constants(_tree(path)):
            # Tokenize so an inlined value's words are checked individually; a
            # forbidden domain word only trips as a standalone token.
            leaked = _split_tokens(text) & _FORBIDDEN_FIELD_NAMES
            if leaked:
                offenders.append((path.name, ", ".join(sorted(leaked))))
    assert not offenders, (
        "a domain-vocabulary word appears as a string literal in a core pack "
        f"module (an inlined reader/pattern/axis vocabulary): {offenders}. Core "
        "loaders carry mechanism nouns only; the domain words live in the pack's "
        "own files, which core reads as opaque bytes."
    )


def _split_tokens(text: str) -> set[str]:
    import re

    return {t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text)}


# --- (c) core never imports/executes pack content ---------------------------


def test_no_import_or_execute_of_pack_content() -> None:
    """No ``importlib``/``entry_points``/``exec``/``eval``/``__import__`` in a pack module.

    DP3 (distribution invisible) + DP2 (code never runs in core): the pack modules
    read bytes and hash them; they never turn a pack-named file into code, never
    look at pip metadata or entry points. Pinned by AST over every Name/Attribute —
    the ``test_bundler_copies_bytes_and_never_parses_content`` form, one layer up.
    """
    offenders: list[tuple[str, str, int]] = []
    for path in _ALL_PACK_FILES:
        for node in ast.walk(_tree(path)):
            hit: str | None = None
            if isinstance(node, ast.Name) and node.id in _FORBIDDEN_IMPORT_NAMES:
                hit = node.id
            elif isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_IMPORT_NAMES:
                hit = node.attr
            if hit is not None:
                offenders.append((path.name, hit, getattr(node, "lineno", -1)))
    assert not offenders, (
        "a pack module reaches for an import/execute name over content: "
        f"{offenders}. Core never imports, executes, or interprets a pack file "
        "(DP2/DP3); it reads bytes and hashes them."
    )


# --- (d) pack attestations route through the ONE kernel ---------------------


def test_bind_and_receipt_route_through_the_kernel() -> None:
    """The bind + receipt record paths call ``attestation.bind`` (never re-inlined).

    getsource pins (the accruing-member rule on the attestation "one kernel" row):
    a bind or a receipt is a CODE attestation created ONLY via the kernel's ``bind``
    — so a sha cannot be asserted into existence outside the recompute lock.
    """
    from hpc_agent.ops.pack import bind_op, record_receipt_op

    bind_src = inspect.getsource(bind_op.pack_bind)
    assert "attestation.bind(" in bind_src, (
        "pack_bind must create its CODE attestation through attestation.bind — the "
        "recompute lock (a bind can no more assert a sha than a sign-off can)."
    )
    receipt_src = inspect.getsource(record_receipt_op.pack_record_receipt)
    assert "attestation.bind(" in receipt_src, (
        "pack_record_receipt must bind its receipt through attestation.bind — the "
        "server-side recompute is the lock, never a re-inlined write."
    )


def test_currency_reductions_route_through_the_kernel() -> None:
    """``current_bind`` + ``slot_status`` decide drift via ``attestation.reduce``.

    The currency verdict (current / stale / missing) is the ONE kernel's job; the
    read side never re-inlines a newest-first scan or a bare ``content_sha ==``
    compare to reach a verdict. getsource pins over both readers.
    """
    from hpc_agent.state import pack_receipts

    for fn in (pack_receipts.current_bind, pack_receipts.slot_status):
        src = inspect.getsource(fn)
        assert "attestation.reduce(" in src, (
            f"{fn.__name__} must route its drift verdict through attestation.reduce "
            "— the 'one kernel' row; a re-inlined currency compare is the leak."
        )


# --- (e) receipt shas are server-computed -----------------------------------


def test_record_receipt_spec_carries_no_caller_sha() -> None:
    """``PackRecordReceiptSpec`` exposes NO sha field — the exact sha-free set.

    Every sha (``content_sha``, ``manifest_sha``, a per-file sha) is server-
    recomputed from disk at record time (the parse IS the recompute). A caller
    cannot assert a receipt for content not on disk — the v1 receipt-laundering
    hole closed one layer up. The full field-set equality (stronger than "no sha
    substring") makes any NEW field land here as a deliberate review. The fire test
    — an entry whose on-disk content changed between caller-read and record is
    refused/re-hashed — lives in
    ``tests/ops/pack/test_record_receipt_op.py::test_recorded_sha_reflects_disk_at_record_time``
    and ``::test_receipt_reads_stale_after_checked_file_drifts``.
    """
    from hpc_agent._wire.actions.pack_bind import PackBindSpec
    from hpc_agent._wire.actions.pack_record_receipt import PackRecordReceiptSpec

    fields = set(PackRecordReceiptSpec.model_fields)
    assert fields == {"pack", "slot", "checked", "passed", "evidence"}, (
        f"PackRecordReceiptSpec field set drifted to {sorted(fields)} — receipt "
        "shas are server-computed; the spec carries no sha field."
    )
    # The input specs (what a CALLER supplies) carry no sha of any name.
    for spec in (PackRecordReceiptSpec, PackBindSpec):
        offenders = [n for n in spec.model_fields if "sha" in n.lower()]
        assert not offenders, (
            f"{spec.__name__} exposes a caller-suppliable sha field {offenders} — a "
            "sha is server-recomputed, never caller-asserted (the enforcement row)."
        )

    # Tripwire: the fire-test file exists and asserts the record-time recompute.
    fire = _REPO_ROOT / "tests" / "ops" / "pack" / "test_record_receipt_op.py"
    assert fire.is_file(), "the pack-record-receipt fire-test file is missing"
    fire_src = fire.read_text(encoding="utf-8")
    assert "test_recorded_sha_reflects_disk_at_record_time" in fire_src, (
        "the record-time server-recompute fire test disappeared — the "
        "receipt-laundering closure is no longer demonstrated."
    )


# --- (f) a CODE receipt never satisfies a human tier ------------------------


def test_pack_records_are_absent_from_every_human_tier() -> None:
    """The pack blocks + the ``"pack"`` scope kind touch NO human-authorship gate.

    A pack bind/receipt is a CODE attestation. It can satisfy a code-receipt slot;
    it can never clear a HUMAN sign-off, a scope unlock, or a registration — the
    maximal human ceremonies. The no-affordance pin: the pack blocks
    (``pack-bind``/``pack-receipt``) and the ``"pack"`` scope kind are disjoint from
    every human-tier block set and human-tier scope kind. The human-authorship gate
    dispatches ONLY on those blocks × scope kinds, so a ``"pack"`` record can never
    reach ``_assert_signoff_authorship`` / ``_assert_unlock_authorship`` /
    ``_assert_registration_authorship``. The sign-off authorship fire-test family is
    untouched (this suite adds no path into it).
    """
    from hpc_agent.ops.decision.journal import _SCOPE_UNLOCK_BLOCK, _SIGNOFF_BLOCK
    from hpc_agent.state.pack_receipts import (
        PACK_BIND_BLOCK,
        PACK_RECEIPT_BLOCK,
        PACK_SUBJECT_KIND,
    )
    from hpc_agent.state.registration import REGISTRATION_BLOCK_FAMILY

    human_tier_blocks = {_SIGNOFF_BLOCK, _SCOPE_UNLOCK_BLOCK} | set(REGISTRATION_BLOCK_FAMILY)
    human_tier_scope_kinds = {"notebook", "scope", "registration"}

    pack_blocks = {PACK_BIND_BLOCK, PACK_RECEIPT_BLOCK}
    assert not (pack_blocks & human_tier_blocks), (
        f"a pack block collides with a human-tier block: {pack_blocks & human_tier_blocks}. "
        "A CODE receipt can never share a block with a HUMAN sign-off/unlock/"
        "registration — that is the softening channel this pin forbids."
    )
    assert PACK_SUBJECT_KIND not in human_tier_scope_kinds, (
        f"the pack scope kind {PACK_SUBJECT_KIND!r} is a human-authorship scope kind — "
        "a pack record must never route through a human-authorship gate."
    )


def test_signoff_authorship_gate_never_names_pack_blocks() -> None:
    """The human-authorship gate source never keys on a pack block/scope.

    A source-level backstop to the constant-disjointness pin: the human-authorship
    dispatcher in ``ops/decision/journal.py`` must not mention ``pack-bind`` /
    ``pack-receipt`` / a ``"pack"`` scope branch — so no future edit can wire a CODE
    receipt into a human tier without tripping here.
    """
    # ``journal`` is a PACKAGE now — concatenate every submodule so the scan
    # covers the whole gate surface, not just the __init__ facade.
    pkg = _SRC / "ops" / "decision" / "journal"
    src = "\n\n".join(py.read_text(encoding="utf-8") for py in sorted(pkg.rglob("*.py")))
    for needle in ("pack-bind", "pack-receipt", 'scope_kind == "pack"', "scope_kind=='pack'"):
        assert needle not in src, (
            f"the decision-journal authorship module mentions {needle!r} — a pack "
            "CODE record must never be recognised by a human-authorship gate."
        )


# --- (g) no pack-domain vocabulary on the wire ------------------------------


def test_pack_wire_models_expose_no_domain_vocabulary() -> None:
    """No pack wire model has a field NAME drawn from domain semantics.

    Walks ``model_json_schema()`` property names recursively (nested models
    included) for EVERY pack Pydantic model. The pack echo is ``{pack, version,
    sha}`` and the specs carry mechanism nouns only (``manifest``, ``slot``,
    ``checked``, ``passed``, ``evidence``); a field named for a reader, a metric, a
    holdout — a caller-owned MEANING — is the substrate-vs-semantics leak.
    """
    models = _pack_wire_models()
    assert models, "found no pack wire models — the pack wire surface vanished"
    for model in models:
        names = _schema_property_names(model.model_json_schema())
        leaked = names & _FORBIDDEN_FIELD_NAMES
        assert not leaked, (
            f"{model.__name__} exposes domain-semantics field name(s) {sorted(leaked)}. "
            "The pack wire describes a bind/receipt by mechanism identity only; a "
            "field named for a caller-owned role is the Q1 leak."
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
