"""Boundary contract for ``run-story``: identity/ordering/counting over opaque
records — never a metric, never a role, never an LLM in the render path.

The run story is the decision journal's INTERFACE sibling
(``docs/design/run-story.md``): one code-rendered timeline of typed, gated
records. The whole feature lives or dies on one line of the boundary test in
``docs/internals/engineering-principles.md`` (Q1, "substrate, not semantics"):
core knows *which store* an event came from and *who* authored it — and NOTHING
about what any record MEANS. The moment an event grows a ``role`` / ``metric`` /
``verdict-quality`` field, or a metric VALUE reaches the rendered line, or an LLM
touches the render path, the story has crossed from IDENTITY+ORDERING+COUNTING
into narrating the caller's semantics — the exact leak the four-question test
forbids.

Six cheap pins hold that line (the T1/T2/T5 tests are the normative copy in
``engineering-principles.md``):

* **event shape** — every event-dict construction carries EXACTLY the D3 key set
  (the dossier entry-shape AST-scan precedent). A fifth, meaning-bearing key is
  the leak.
* **stream vocabulary** — the closed :data:`STREAMS` set equals the dossier's
  source stores MINUS the two opaque ones (``aggregated`` / ``sidecar``); the
  render path never parses the aggregated tree.
* **forbidden vocabulary** — no wire model exposes a field NAME drawn from the
  domain-semantics set.
* **no LLM / no wire in the render path** — the projection + render modules
  import nothing LLM-adjacent and nothing from ``_wire``, and the render entry
  point takes no free-prose input parameter.
* **counts-only rule** — a metric VALUE crafted into an event's evidence is
  DROPPED from the rendered line; a COUNT / pointer renders.
* **one ordering** — the assembly op routes through the single ``merge_events``
  definition and NEVER re-sorts the timeline (a consumer re-sorting has forked
  it — the boundary-drift flag).

House style: mirrors ``test_dossier_boundary.py`` (AST + a closed authoritative
set kept inline so drift surfaces here).
"""

from __future__ import annotations

import ast
from pathlib import Path

from tests._paths import SRC_DIR

# --- module anchors ---------------------------------------------------------

_STATE_FILE = SRC_DIR / "hpc_agent" / "state" / "run_story.py"
_RENDER_FILE = SRC_DIR / "hpc_agent" / "ops" / "story_render.py"
_OP_FILE = SRC_DIR / "hpc_agent" / "ops" / "run_story.py"

#: The render PATH — the projection + the deterministic renderer. These two must
#: stay LLM-free and wire-free (the ``relay_render.py`` posture, D4).
_RENDER_PATH_FILES = (_STATE_FILE, _RENDER_FILE)

# --- authoritative closed sets (kept inline; drift surfaces here) -----------

# The exact D3 event key set. Every event-dict construction carries these and
# nothing else; a fifth, meaning-bearing key ("role", "metric", "quality") is
# the boundary leak.
_EVENT_KEYS = frozenset({"ts", "stream", "actor", "kind", "subject_id", "evidence", "text"})

# The closed stream vocabulary — the dossier's source stores MINUS the two opaque
# ones the story excludes (D1): ``aggregated`` (opaque bytes; parsing it names
# metrics) and ``sidecar`` (identity, not events; feeds the header). Equality (not
# subset) so a new stream noun lands here as a reviewed vocabulary change.
_EXPECTED_STREAMS = frozenset(
    {
        "decision-journal",
        "briefs",
        "block-terminal",
        "journal-record",
        "scope-journal",
        "look-ledger",
        "notebook-journal",
    }
)

# Domain-semantics vocabulary the wire must never NAME (field names only; prose
# and store nouns are fine). Mirrors the dossier boundary test's set.
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
        "accuracy",
        "loss",
    }
)

# Substrings that betray an LLM/prose-generation import in the render path. A
# render module reaching for any of these would be generating narrative rather
# than deterministically formatting records.
_LLM_IMPORT_MARKERS = ("anthropic", "openai", "llm", "prompt", "claude_", "generat")


# --- helpers ----------------------------------------------------------------


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _dict_key_sets_with(tree: ast.Module, marker: str) -> list[frozenset[str]]:
    """Key sets of every dict literal / ``dict(...)`` carrying *marker* as a key."""
    out: list[frozenset[str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = frozenset(
                k.value
                for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            )
            if marker in keys:
                out.append(keys)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "dict"
        ):
            keys = frozenset(kw.arg for kw in node.keywords if kw.arg is not None)
            if marker in keys:
                out.append(keys)
    return out


def _imported_modules(tree: ast.Module) -> set[str]:
    """Every module name reached by ``import x`` / ``from x import ...``."""
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


# --- (a) event-shape pin ----------------------------------------------------


def test_event_dict_construction_is_exactly_the_d3_key_set() -> None:
    """Every event-dict construction carries EXACTLY the D3 key set.

    The distinctive key is ``subject_id`` (only the event projection builds it).
    Pinned by AST so it holds regardless of which helper constructs the dict — a
    fifth, meaning-bearing key ("role", "metric", "quality") is the boundary leak.
    """
    from hpc_agent.ops.story_render import EVENT_KEYS

    assert frozenset(EVENT_KEYS) == _EVENT_KEYS, (
        f"EVENT_KEYS drifted from the D3 shape: expected {sorted(_EVENT_KEYS)}, "
        f"found {sorted(EVENT_KEYS)}."
    )
    key_sets = _dict_key_sets_with(_tree(_RENDER_FILE), "subject_id")
    assert key_sets, (
        "found no event-dict construction in story_render.py (no dict carrying a "
        "'subject_id' key). The event-shape pin cannot see the construction — "
        "update it deliberately if the event payload moved."
    )
    for keys in key_sets:
        assert keys == _EVENT_KEYS, (
            "an event dict's key set drifted from the D3 shape. expected "
            f"{sorted(_EVENT_KEYS)}, found {sorted(keys)}. An event is IDENTITY + "
            "ORDERING + COUNTING over an opaque record — a fifth, meaning-bearing "
            "key is the substrate-vs-semantics leak (engineering-principles Q1)."
        )


# --- (b) stream-vocabulary + no-aggregated-parse pin ------------------------


def test_stream_vocabulary_is_dossier_sources_minus_the_opaque_stores() -> None:
    """``STREAMS`` equals the dossier's stores MINUS ``aggregated`` / ``sidecar``.

    The story's sources can never disagree with the dossier's sealed stores about
    what a run's trail IS, and the two opaque stores are excluded BY CONSTRUCTION:
    ``aggregated`` (parsing it names metrics) and ``sidecar`` (identity, not
    events). Equality so a new stream is a reviewed vocabulary change.
    """
    from hpc_agent.state.run_story import STREAMS

    assert frozenset(STREAMS) == _EXPECTED_STREAMS, (
        "STREAMS drifted from the closed vocabulary (dossier sources minus the "
        f"opaque aggregated/sidecar stores). expected {sorted(_EXPECTED_STREAMS)}, "
        f"found {sorted(STREAMS)}."
    )
    assert "aggregated" not in STREAMS and "sidecar" not in STREAMS


def test_render_path_never_reads_the_aggregated_tree() -> None:
    """No render-path module references the ``_aggregated`` tree.

    ``aggregated`` is opaque bytes by the dossier's no-parse pin; the story merges
    records, never harvested aggregates. If ``_aggregated`` never appears, the
    metric-naming leak the exclusion exists to prevent cannot happen.
    """
    for path in (_STATE_FILE, _RENDER_FILE, _OP_FILE):
        text = path.read_text(encoding="utf-8")
        assert "_aggregated" not in text, (
            f"{path.name} references the _aggregated tree — the story excludes the "
            "aggregated store (opaque bytes); parsing it would name the caller's "
            "metrics (D1)."
        )


# --- (c) forbidden-vocabulary pin -------------------------------------------


def test_wire_models_expose_no_domain_vocabulary() -> None:
    """No wire model has a field NAME drawn from domain semantics.

    Walks ``model_json_schema()`` property names recursively for the spec, the
    event, and the result. Names only — descriptions may mention counts/metrics in
    prose. A field named for a caller-owned role is the substrate-vs-semantics
    leak.
    """
    from hpc_agent._wire.queries.run_story import (
        RunStoryEvent,
        RunStoryResult,
        RunStorySpec,
    )

    def _property_names(schema: dict) -> set[str]:
        names: set[str] = set()

        def walk(node: object) -> None:
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

    for model in (RunStorySpec, RunStoryEvent, RunStoryResult):
        names = _property_names(model.model_json_schema())
        leaked = names & _FORBIDDEN_FIELD_NAMES
        assert not leaked, (
            f"{model.__name__} exposes domain-semantics field name(s) {sorted(leaked)}. "
            "The run-story wire describes a timeline by IDENTITY + ORDERING + "
            "COUNTING; a field named for a caller-owned role is the "
            "substrate-vs-semantics leak (engineering-principles Q1)."
        )


# --- (d) no LLM / no wire in the render path --------------------------------


def test_render_path_imports_nothing_llm_adjacent_and_no_wire() -> None:
    """The projection + render modules import nothing LLM-adjacent and no ``_wire``.

    The render path is deterministic string work (the ``relay_render.py`` posture,
    D4): it formats records, it does not generate narrative, and it never reaches
    the wire boundary (the ``ops`` op owns the Pydantic seam). Any LLM/prose import
    or a ``_wire`` import here is the leak.
    """
    for path in _RENDER_PATH_FILES:
        mods = _imported_modules(_tree(path))
        for mod in mods:
            low = mod.lower()
            assert not any(marker in low for marker in _LLM_IMPORT_MARKERS), (
                f"{path.name} imports {mod!r} — the render path must not reach for "
                "LLM/prose generation; it deterministically formats records."
            )
            assert not low.startswith("hpc_agent._wire"), (
                f"{path.name} imports {mod!r} from _wire — the render path is "
                "wire-free (the ops op owns the Pydantic boundary, D4)."
            )


def test_render_entry_point_takes_no_free_prose_parameter() -> None:
    """``render_story`` accepts no free-prose input parameter.

    Its inputs are the header mapping, the ordered events, the honesty counts, and
    a ``markdown`` bool — no ``prose`` / ``summary`` / ``narrative`` / ``text``
    parameter through which an LLM's words could enter the render as timeline
    narrative.
    """
    import inspect

    from hpc_agent.ops.story_render import render_story

    params = set(inspect.signature(render_story).parameters)
    forbidden = {"prose", "summary", "narrative", "text", "note", "commentary"}
    leaked = params & forbidden
    assert not leaked, (
        f"render_story exposes a free-prose parameter {sorted(leaked)} — the render "
        "path must not accept generated narrative (D4 / the boundary-drift flag)."
    )


# --- (e) counts-only rule ---------------------------------------------------


def test_counts_only_metric_value_never_reaches_the_line() -> None:
    """A metric VALUE crafted into an event's evidence is DROPPED from the line.

    The behavioral form of the counts-only rule (D3): a fabricated ``accuracy``
    reaches ``evidence`` but the render whitelist keeps only sha pointers + counts
    + identity literals, so the value can never render. A COUNT and a pointer DO
    render.
    """
    from hpc_agent.ops.story_render import render_story
    from hpc_agent.state.run_story import StoryEvent

    crafted = StoryEvent(
        ts="2026-07-08T12:00:00+00:00",
        stream="briefs",
        actor="code",
        kind="s4",
        subject_id="r1",
        evidence={"accuracy": 0.9731, "row_count": 20, "cmd_sha": "csha"},
    )
    render = render_story({"run_ids": ["r1"]}, [crafted], total_events=1, omitted_count=0)
    assert "0.9731" not in render.markdown, "a metric VALUE reached the rendered line"
    assert "accuracy" not in render.markdown
    assert "row_count=20" in render.markdown  # a COUNT renders
    assert "cmd_sha=csha" in render.markdown  # a pointer renders


# --- (f) one-ordering pin ---------------------------------------------------


def test_assembly_op_never_re_sorts_the_timeline() -> None:
    """``ops/run_story.py`` calls no ``sorted(...)`` / ``.sort(...)``.

    The timeline order is the ONE ``merge_events`` definition (D2), applied inside
    ``build_story``. The assembly op windows (slices) and renders that order; it
    must NEVER re-sort, because a second ordering re-derivation forks the timeline
    (the boundary-drift flag). Pinned by AST over the op module.
    """
    offenders: list[int] = []
    for node in ast.walk(_tree(_OP_FILE)):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Name) and func.id == "sorted") or (
                isinstance(func, ast.Attribute) and func.attr == "sort"
            ):
                offenders.append(node.lineno)
    assert not offenders, (
        "ops/run_story.py re-sorts events at line(s) "
        f"{offenders} — the timeline order is the single merge_events definition "
        "(D2); a second re-sort forks the timeline (the boundary-drift flag). "
        "Route ordering through build_story; window by slicing, never re-sort."
    )


def test_state_module_orders_events_only_in_merge_events() -> None:
    """The ONE event-ordering ``sorted(...)`` lives in ``merge_events`` — nowhere else.

    The single ordering definition (D2) lives once, in ``merge_events``. A second
    ``sorted`` over EVENTS anywhere in the module would be a forked timeline. A
    ``sorted(...)`` over a filesystem enumeration (``glob``/``rglob``/``iterdir``)
    is NOT an event re-sort — it makes a directory read deterministic — so it is
    explicitly allowed outside ``merge_events``. (``sort_keys=True`` inside
    ``json.dumps`` is a kwarg, not a ``sorted`` call, so it never counts.)
    """
    tree = _tree(_STATE_FILE)
    merge_fn = next(
        (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "merge_events"),
        None,
    )
    assert merge_fn is not None, "state/run_story.py must define merge_events (the one merge)"

    def _sorted_calls(scope: ast.AST) -> list[ast.Call]:
        return [
            n
            for n in ast.walk(scope)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "sorted"
        ]

    _FS_ENUM = {"glob", "rglob", "iterdir", "scandir", "listdir"}

    def _is_fs_enum_sort(call: ast.Call) -> bool:
        """True when ``sorted(...)``'s first arg is a filesystem enumeration call."""
        if not call.args:
            return False
        arg = call.args[0]
        return (
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Attribute)
            and arg.func.attr in _FS_ENUM
        )

    in_merge = _sorted_calls(merge_fn)
    assert len(in_merge) == 1, (
        f"merge_events has {len(in_merge)} sorted() calls — the ONE ordering "
        "definition (D2) is exactly one sorted() over the events."
    )

    merge_linenos = {c.lineno for c in in_merge}
    for call in _sorted_calls(tree):
        if call.lineno in merge_linenos:
            continue
        assert _is_fs_enum_sort(call), (
            f"state/run_story.py sorts at line {call.lineno} OUTSIDE merge_events "
            "and it is not a filesystem enumeration sort — a second sorted() over "
            "events forks the timeline (the boundary-drift flag). The one ordering "
            "definition is merge_events."
        )
