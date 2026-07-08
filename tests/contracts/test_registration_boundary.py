"""Boundary contract for the registration kernel: the deployment-boundary
attestation ships MECHANISM only — a closed kind vocabulary, no default
template, no domain vocabulary on the wire, and a human-ONLY attestor.

The registration kernel (``docs/design/registration-kernel.md``) makes a
promotion "one more attestation" (R1) over the strongest subject the system
seals (the dossier, R2). The whole feature lives or dies on the same line every
rigor primitive holds (``docs/internals/engineering-principles.md`` Q1,
"substrate, not semantics"): core knows which STORE / MECHANISM a prerequisite
routes through and NOTHING about what a field slug, a subject_id, or "ready to
deploy" means in any domain. The moment core ships a default template, defaults
a field slug, names a domain word on the wire, or lets a mechanical writer
author a ``registration`` block, it has crossed from IDENTITY / ORDERING /
COMPARISON / COUNTING over opaque caller content into naming the caller's
semantics — the exact leak the four-question test forbids.

Seven pins hold that line, one per row of the plan's enforcement table (R6/R7,
"Agnosticism by FIVE mechanisms"). House style mirrors
``test_dossier_boundary.py`` (AST + closed authoritative sets kept inline so
drift surfaces here) and the run-story boundary suite (the
``_schema_property_names`` recursive walk).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "hpc_agent"

# The core registration source the no-literal-vocab pins police. These modules
# are the whole of the registration MECHANISM in core; T10's consumer is a TEST
# fixture, not core, so it is deliberately NOT scanned here.
_STATE_REGISTRATION = _SRC / "state" / "registration.py"
_OPS_REGISTRATION_DIR = _SRC / "ops" / "registration"
_REGISTRATION_VIEW_FACADE = _SRC / "ops" / "registration_view.py"


# --- authoritative closed sets (kept inline; drift surfaces here) -----------

# The closed prerequisite-kind vocabulary (R3). Every value is a core MECHANISM
# noun (a store / one-definition checker), never a domain word. Mirrors
# ``state/registration.py::PREREQUISITE_KINDS`` and the wire ``PrerequisiteKind``
# literal — the equality tests below fail on any drift, so adding a kind is a
# reviewed vocabulary change that lands HERE.
_EXPECTED_KINDS = frozenset(
    {
        "notebook-audit",
        "reproduction",
        "scope-budget",
        "pack-receipt",
        "attestation",
    }
)

# The registration status vocabulary (R7) — the other core-owned set. Mechanism
# words, never domain words.
_EXPECTED_STATUSES = frozenset({"current", "stale", "revoked", "superseded", "absent"})

# Per-kind, the ONE existing definition its checker must route through (R3 table).
# A checker that stops naming its route-through symbol has re-inlined a member's
# currency logic — the "one kernel" row's fire path.
_KIND_ROUTE_THROUGH = {
    "notebook-audit": ("audit_module(", "_linked_source_drift(", "sha256_normalized("),
    "reproduction": ("detect_code_drift(",),
    "scope-budget": ("count_prior_looks(", "is_scope_locked("),
    "pack-receipt": (),  # reserved: a loud not-yet-available refusal, no checker body yet
    "attestation": ("attestation.reduce(",),
}

# Domain-semantics vocabulary core must never name — field NAMES only (prose and
# store filenames are fine, copied as opaque bytes). Mirrors the dossier suite's
# set: a wire field named for a caller role is the substrate-vs-semantics leak.
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
    }
)

# The toy-domain fixture slugs (T10). They belong ONLY in tests/fixtures — if one
# appears as a string literal in core, a default field slug has been hardcoded.
_TOY_FIELD_SLUGS = frozenset({"widget-owner", "jam-threshold"})

# The toy-domain fixture rule mechanized (R4 mechanism #4): registration
# fixtures register something deliberately DUMB (the widget lineage). A real
# domain word (the harxhar quant model, its vocabulary) in a fixture smuggles a
# vocabulary into the tree that greps and future maintainers mistake for core
# knowledge. Built from the plan's named rule ("never harxhar/quant"), extended
# to the unmistakable real-quant nouns that rule exists to keep out.
_TOY_TOKEN_DENYLIST = frozenset({"harxhar", "quant"})


# --- helpers ----------------------------------------------------------------


def _string_constants(path: Path) -> list[str]:
    """Every string-literal constant in *path*'s AST (docstrings included)."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _core_registration_files() -> list[Path]:
    """The core registration modules the no-literal-vocab pins scan."""
    files = [_STATE_REGISTRATION, _REGISTRATION_VIEW_FACADE]
    files += sorted(_OPS_REGISTRATION_DIR.glob("*.py"))
    return [p for p in files if p.is_file()]


def _schema_property_names(schema: dict[str, Any]) -> set[str]:
    """Every property NAME anywhere in a JSON schema, recursively.

    Walks the whole schema object (top-level ``properties`` plus every nested
    model under ``$defs``/``items``/etc.); collects the keys of any dict found
    under a ``properties`` key. Names only — descriptions/titles are not walked,
    so domain words in prose never trip the forbidden-vocabulary test. Mirrored
    verbatim from ``test_dossier_boundary.py``.
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


def _registration_test_and_fixture_files() -> list[Path]:
    """Every registration TEST / FIXTURE / EXAMPLE file the toy denylist scans.

    This suite's OWN file is excluded: it defines the denylist literally (and
    names the banned words in prose), so it is not fixture vocabulary.
    """
    tests_dir = _REPO_ROOT / "tests"
    out: set[Path] = set()
    # Named test modules across the registration slice.
    out.update(tests_dir.glob("state/test_registration.py"))
    out.update(tests_dir.glob("ops/registration/*.py"))
    out.update(tests_dir.glob("ops/decision/test_registration_authorship.py"))
    out.update(tests_dir.glob("ops/attention/test_registration_attention.py"))
    # The toy fixture substrate + the toy consumer example (T10).
    out.update((tests_dir / "fixtures" / "toy_registration").rglob("*"))
    out.update((_REPO_ROOT / "examples" / "toy_registration").rglob("*"))
    out.discard(Path(__file__).resolve())
    return sorted(p for p in out if p.is_file())


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """The id()s of the docstring Constant nodes (module/class/def) in *tree*.

    A docstring that STATES the toy-domain rule ("never harxhar/quant") is not
    fixture vocabulary, so the denylist scan excludes docstrings and reads only
    fixture DATA (non-docstring string literals) + code identifiers.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def _fixture_tokens(path: Path) -> set[str]:
    """The fixture-vocabulary tokens in *path*, lowercased.

    For a ``.py`` file: every code identifier + every NON-docstring string
    literal, split into word tokens (docstrings excluded — see
    :func:`_docstring_nodes`). For any other file: every word token in the raw
    text (fixture JSON, example scripts).
    """
    import re

    text = path.read_text(encoding="utf-8")
    if path.suffix != ".py":
        return {t.lower() for t in re.findall(r"[A-Za-z]+", text)}
    tree = ast.parse(text, filename=str(path))
    docstrings = _docstring_nodes(tree)
    tokens: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            tokens.add(node.id.lower())
        elif isinstance(node, ast.Attribute):
            tokens.add(node.attr.lower())
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            tokens.update(t.lower() for t in re.findall(r"[A-Za-z]+", node.value))
    return tokens


# --- (a) no-affordance registry scan ----------------------------------------


def test_no_registration_write_affordance_in_the_registry() -> None:
    """No mutate verb named register/registration; ``verify-registration`` is a
    side-effect-free query (R1/R6 lock 1 — the no-unlock-verb doctrine).

    A registration is written ONLY via ``append-decision`` under a gated block:
    no registration verb, no chain, no next_block, no skill. This pins the
    no-sign-off-verb form: scan the whole primitive registry for any mutating
    verb whose name says register/registration, and confirm the one registration
    surface that DOES exist (``verify-registration``) is a read-only reporter.
    """
    from hpc_agent._kernel.registry.primitive import get_registry, register_primitives

    register_primitives()
    registry = get_registry()

    offenders = [
        name
        for name, meta in registry.items()
        if ("register" in name or "registration" in name)
        and name != "verify-registration"
        and meta.verb in {"mutate", "submit", "workflow"}
    ]
    assert not offenders, (
        f"a registration WRITE affordance appeared in the registry: {offenders}. A "
        "registration rides append-decision under a gated block — there is NO "
        "registration verb / chain / next_block / skill (R6 lock 1). Remove the verb."
    )

    verify = registry.get("verify-registration")
    assert verify is not None, "verify-registration must be registered (the R8 consumer seat)."
    assert verify.verb == "query", (
        f"verify-registration must be verb='query' (a read-only reporter, R8); got {verify.verb!r}."
        " The deploy refusal is wired caller-side against status — core never blocks here."
    )
    assert tuple(verify.side_effects) == (), (
        "verify-registration declared side effect(s) "
        f"{[se.kind for se in verify.side_effects]} — it REPORTS, it must never write."
    )


# --- (b) PREREQUISITE_KINDS closed + route-through + closed requires ---------


def test_prerequisite_kinds_is_the_closed_mechanism_noun_set() -> None:
    """``PREREQUISITE_KINDS`` equals the closed set — exactly (the ``DOSSIER_SOURCES``
    equality-pin pattern), and the wire ``PrerequisiteKind`` literal matches it.

    Equality (not subset) so a new kind cannot be added without landing here as a
    reviewed vocabulary change; the wire mirror is pinned equal so the two never
    drift out of lockstep.
    """
    from hpc_agent._wire.actions.verify_registration import PrerequisiteKind
    from hpc_agent.state.registration import PREREQUISITE_KINDS, STATUSES

    assert frozenset(STATUSES) == _EXPECTED_STATUSES, (
        "the registration STATUSES set drifted from the closed status vocabulary. "
        f"expected {sorted(_EXPECTED_STATUSES)}, found {sorted(STATUSES)}."
    )
    assert frozenset(PREREQUISITE_KINDS) == _EXPECTED_KINDS, (
        "PREREQUISITE_KINDS drifted from the closed mechanism-noun set. "
        f"expected {sorted(_EXPECTED_KINDS)}, found {sorted(PREREQUISITE_KINDS)}. "
        "Adding a kind is a reviewed vocabulary change; a domain member is forbidden."
    )
    # The wire Literal's arms are its type args.
    wire_kinds = frozenset(getattr(PrerequisiteKind, "__args__", ()))
    assert wire_kinds == _EXPECTED_KINDS, (
        "the wire PrerequisiteKind Literal drifted from PREREQUISITE_KINDS. "
        f"expected {sorted(_EXPECTED_KINDS)}, found {sorted(wire_kinds)}. Keep the two equal."
    )
    # No forbidden domain word may masquerade as a kind.
    assert not (_EXPECTED_KINDS & _FORBIDDEN_FIELD_NAMES), (
        "a PREREQUISITE_KINDS value collides with domain-semantics vocabulary — a kind names a "
        "core mechanism, never what content means."
    )


def test_every_kind_dispatches_to_a_named_checker_that_routes_through() -> None:
    """Each kind has ONE checker, and each checker routes through its ONE existing
    definition (R3) — the composer never re-inlines a member's currency logic.

    Pins the ``_DISPATCH`` keys equal to ``PREREQUISITE_KINDS`` (no kind left
    uncheckable, no orphan checker) and asserts each checker's source names its
    route-through symbol — the ``test_layers_share_one_drift_predicate`` form.
    """
    import inspect

    from hpc_agent.ops.registration import prereqs
    from hpc_agent.state.registration import PREREQUISITE_KINDS

    assert set(prereqs._DISPATCH) == frozenset(PREREQUISITE_KINDS), (
        "prereqs._DISPATCH keys drifted from PREREQUISITE_KINDS — every kind must dispatch to "
        f"one checker. dispatch={sorted(prereqs._DISPATCH)}, kinds={sorted(PREREQUISITE_KINDS)}."
    )
    for kind, tokens in _KIND_ROUTE_THROUGH.items():
        checker = prereqs._DISPATCH[kind]
        src = inspect.getsource(checker)
        for token in tokens:
            assert token in src, (
                f"the {kind!r} checker ({checker.__name__}) no longer routes through {token!r} — "
                "it must call its ONE existing definition, never re-inline the currency logic (R3)."
            )


def test_requires_keys_are_closed_per_kind_and_attestation_accepts_none() -> None:
    """The ``requires`` keys are a CLOSED set per kind, and the generic
    ``attestation`` kind accepts NONE (R3/R4 — the dangling-reference posture).

    An unknown ``requires`` key for a kind is a loud refusal, never a silent
    pass, so the allowed-key table must be pinned closed and cover every kind.
    """
    from hpc_agent.ops.registration.prereqs import _REQUIRES_KEYS
    from hpc_agent.state.registration import _KINDS_WITHOUT_REQUIRES, PREREQUISITE_KINDS

    assert set(_REQUIRES_KEYS) == frozenset(PREREQUISITE_KINDS), (
        "_REQUIRES_KEYS must name every kind (each kind's allowed requires-key set is closed). "
        f"keys={sorted(_REQUIRES_KEYS)}, kinds={sorted(PREREQUISITE_KINDS)}."
    )
    assert _REQUIRES_KEYS["attestation"] == frozenset(), (
        "the generic attestation kind must accept NO requires keys — it carries no evidence-tier "
        "vocabulary core could interpret (R3)."
    )
    assert "attestation" in _KINDS_WITHOUT_REQUIRES, (
        "state/registration.py must also forbid a requires payload on the attestation kind (the "
        "loader's takes-none rule, R3)."
    )


# --- (c) no default template + no registration vocabulary in core -----------


def test_core_ships_no_default_registration_template() -> None:
    """No template file ships in the package (R5 — the fabrication class).

    Core ships NO default template, ever: a template is caller-referenced data.
    Scan every ``.json`` under ``src/hpc_agent`` for the registration-template
    shape (a list-of-slugs ``fields`` + a ``prerequisites`` key) and assert none
    exists — a bundled default would let a registration resolve a template core
    authored.
    """
    offenders: list[str] = []
    for path in _SRC.rglob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            isinstance(data, dict)
            and isinstance(data.get("fields"), list)
            and all(isinstance(f, str) for f in data["fields"])
            and "prerequisites" in data
        ):
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, (
        f"a registration template shipped in package data: {offenders}. Core ships NO default "
        "template (R5) — a registration must reference a caller-authored file."
    )


def test_core_source_inlines_no_registration_field_vocabulary() -> None:
    """No field-slug / domain vocabulary is hardcoded in core (R5 — no invented
    defaults), via a no-literal-vocab AST pin over the registration modules.

    Fires if core defaults a field/slot/id: a domain-semantics word, a toy field
    slug leaking from a fixture, or harxhar/quant vocabulary appearing as a string
    literal in ``ops/registration/`` or ``state/registration.py``.
    """
    banned = _FORBIDDEN_FIELD_NAMES | _TOY_FIELD_SLUGS | _TOY_TOKEN_DENYLIST
    offenders: dict[str, list[str]] = {}
    for path in _core_registration_files():
        hits = sorted({c for c in _string_constants(path) if c.lower() in banned})
        if hits:
            offenders[str(path.relative_to(_REPO_ROOT))] = hits
    assert not offenders, (
        f"core registration source inlined field/domain vocabulary: {offenders}. Core ships NO "
        "default field slug and never names a domain word — field slugs are opaque caller data "
        "(R5); a hardcoded one is the no-invented-defaults leak."
    )


# --- (d) no domain vocabulary on the wire -----------------------------------


def test_wire_models_expose_no_domain_vocabulary() -> None:
    """No verify-registration schema exposes a ``_FORBIDDEN_FIELD_NAMES`` member.

    The ``_schema_property_names`` recursive walk (mirrored from the dossier
    suite) over every verify-registration wire model: every field name must be a
    MECHANISM noun (a store, a leg, a sha, a count), never a caller role.
    """
    from pydantic import BaseModel

    from hpc_agent._wire.actions import verify_registration as wire

    models: list[type[BaseModel]] = [
        wire.VerifyRegistrationSpec,
        wire.VerifyRegistrationResult,
        wire.DossierLeg,
        wire.TemplateLeg,
        wire.PrerequisiteLeg,
        wire.FieldsBlock,
        wire.ChainEntry,
        wire.PrerequisiteRequires,
    ]
    for model in models:
        names = _schema_property_names(model.model_json_schema())
        leaked = names & _FORBIDDEN_FIELD_NAMES
        assert not leaked, (
            f"{model.__name__} exposes domain-semantics field name(s) {sorted(leaked)}. The "
            "registration wire describes a promotion by MECHANISM only (which stores, which "
            "prerequisites, what shas); a field named for a caller role is the Q1 leak."
        )


# --- (e) the attestor is ALWAYS human ---------------------------------------


def test_registration_attestor_is_always_human_never_a_code_writer() -> None:
    """No code-writer block set contains a registration block, and the gate binds
    the literal human attestor with no waiver tier (R6 — the one instance where
    D-attention's answer is "always human-required by construction").

    Two legs: (1) the notebook module's CODE-writer attestor map never names a
    registration block (a mechanical writer must never author a ``registration``
    record); (2) the gate source binds ``attestor="human"`` and carries no
    auto-clear / redundant / waiver vocabulary (no tier ever waives).
    """
    import inspect

    from hpc_agent.ops.decision import journal as decision_journal
    from hpc_agent.state import notebook_audit
    from hpc_agent.state.registration import REGISTRATION_BLOCK, REVOKE_BLOCK

    reg_blocks = {REGISTRATION_BLOCK, REVOKE_BLOCK}

    # (1) The one existing block→attestor map that a CODE writer keys on.
    code_writer_blocks = {
        block for block, attestor in notebook_audit._BLOCK_ATTESTOR.items() if attestor == "code"
    }
    assert not (code_writer_blocks & reg_blocks), (
        f"a registration block appears in a CODE-writer attestor set {sorted(code_writer_blocks)}."
        " The registration attestor is ALWAYS human; no mechanical writer authors the record (R6)."
    )

    # (2) The gate binds the literal human attestor and grows no waiver tier.
    # Scan the CODE only — the docstrings legitimately STATE the rule ("NO
    # auto-clear tier"), which is not a waiver path.
    def _code_no_docstring(fn: Any) -> str:
        import textwrap

        fdef = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
        assert isinstance(fdef, ast.FunctionDef)
        if ast.get_docstring(fdef) is not None:
            fdef.body = fdef.body[1:]
        return ast.unparse(fdef)

    gate_src = _code_no_docstring(decision_journal._assert_registration_full)
    gate_src += "\n" + _code_no_docstring(decision_journal._assert_revoke_floor)
    # ast.unparse normalizes string literals to single quotes.
    assert "'human'" in gate_src, (
        "the registration gate must bind the literal 'human' attestor through the kernel — the "
        "attestor is never code at this seat (R6)."
    )
    assert "'code'" not in gate_src, (
        "the registration gate names the 'code' attestor — the registration attestor is ALWAYS "
        "human; a code attestation can fill a CHAIN slot but never BE the registration (R6)."
    )
    _waivers = ("auto_clear", "auto_cleared", "auto-clear", "redundant", "waive", "safe_default")
    for waiver in _waivers:
        assert waiver not in gate_src, (
            f"the registration gate source contains waiver vocabulary {waiver!r} — there is NO "
            "auto-clear / redundant / waived tier at this gate; the bar never waives (R6)."
        )


# --- (f) toy-domain fixtures only -------------------------------------------


def test_registration_fixtures_carry_no_real_domain_vocabulary() -> None:
    """No harxhar/quant vocabulary in the registration tests/fixtures/examples
    (R4 mechanism #4 — the toy-domain fixture rule mechanized).

    A token denylist scan over every registration test / fixture / example file:
    real domain words in a fixture would smuggle a vocabulary into the tree that
    greps and future maintainers mistake for core knowledge. Whole-token,
    case-insensitive.
    """
    offenders: dict[str, list[str]] = {}
    for path in _registration_test_and_fixture_files():
        try:
            tokens = _fixture_tokens(path)
        except (OSError, UnicodeDecodeError, SyntaxError):
            continue
        hits = sorted(tokens & _TOY_TOKEN_DENYLIST)
        if hits:
            offenders[str(path.relative_to(_REPO_ROOT))] = hits
    assert not offenders, (
        f"real domain vocabulary leaked into a registration fixture: {offenders}. Registration "
        "fixtures register something deliberately DUMB (the widget lineage) — never harxhar/quant "
        "(R4 mechanism #4); a real domain word reads as core knowledge to the next maintainer."
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
