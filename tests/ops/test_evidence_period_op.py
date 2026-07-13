"""T6 tests — ``ops/evidence_period_op.py``: the ``evidence-period`` window verb.

Toy WIDGET vocabulary only (never harxhar/quant — the domain-packs toy-fixture
rule). Crafted journals/sidecars exercise: window inclusion/exclusion at BOTH
bounds, an open ``until``, the unconcluded list populated + dated, a ``tags``
filter within the window, cache honesty (miss→hit) + the disabled opt-out, fleet
+ skipped accounting, the non-creating pin (no ``.hpc`` under a fresh namespace),
and the ``render_period`` seam stubbed (asserting the WINDOWED collection reaches
the render).

Expected-red until regen: importing the op registers the ``evidence-period``
primitive; the registry-count / schema-ref / operations.json checks are a
separate regen wave (dev_regen_list). These tests exercise the verb's behavior
directly, not the CLI registry, so they pass ahead of regen.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pytest

from hpc_agent._wire.queries.evidence import EvidencePeriodSpec
from hpc_agent.ops import evidence_period_op  # type: ignore[attr-defined]

# --- tiny toy-store writers (NON-CREATING globs read these back) -------------


def _append_jsonl(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")


def _cite(kind: str = "run", ref: str = "widget-run-1", sha: str = "deadbeefcafef00d") -> dict:
    return {"kind": kind, "ref": ref, "sha": sha}


def _write_conclusion(
    exp: Path,
    cid: str,
    *,
    ts: str,
    tags: list[str] | None = None,
    concludes: list[dict] | None = None,
    finding: str = "widget finding",
) -> None:
    record = {
        "ts": ts,
        "scope_kind": "conclusion",
        "scope_id": cid,
        "block": "conclusion",
        "response": "y",
        "resolved": {
            "conclusion_id": cid,
            "tags": tags or [],
            "concludes": concludes or [],
            "citations": [_cite()],
            "finding": finding,
        },
    }
    _append_jsonl(exp / ".hpc" / "conclusions" / f"{cid}.decisions.jsonl", record)


def _write_terminal_campaign(exp: Path, campaign_id: str, *, ts: str) -> None:
    _append_jsonl(
        exp / ".hpc" / "campaigns" / campaign_id / "decisions.jsonl",
        {"ts": ts, "block": "complete", "resolved": {}},
    )


# --- the render seam stub -----------------------------------------------------


@pytest.fixture
def stub_render(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Inject a fake ``hpc_agent.ops.evidence_render`` module; capture its input.

    The op imports the render seam LATE via ``importlib.import_module`` (the
    parallel Wave-B file may not exist yet), so a ``sys.modules`` injection is the
    clean stub. Returns a dict the render fills with the collection it received.
    """
    captured: dict = {}

    def _render_period(collection, *, since, until, computed_at):  # noqa: ANN001, ANN202
        captured["collection"] = collection
        captured["since"] = since
        captured["until"] = until
        return "STUB-RENDER"

    mod = types.ModuleType("hpc_agent.ops.evidence_render")
    mod.render_period = _render_period  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hpc_agent.ops.evidence_render", mod)
    return captured


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the journal home (cache + fleet discovery) to a scratch dir."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path / "home"))
    monkeypatch.delenv("HPC_NO_EVIDENCE_CACHE", raising=False)
    return tmp_path


def _run(exp: Path, **spec_kwargs) -> object:
    return evidence_period_op.evidence_period(
        experiment_dir=exp, spec=EvidencePeriodSpec(**spec_kwargs)
    )


# --- window inclusion / exclusion at both bounds ------------------------------


def test_window_excludes_below_since_and_above_until(
    tmp_path: Path, home: Path, stub_render: dict
) -> None:
    exp = tmp_path / "exp"
    _write_conclusion(exp, "widget-old", ts="2025-01-01T00:00:00Z")  # < since
    _write_conclusion(exp, "widget-mid", ts="2025-04-01T00:00:00Z")  # in window
    _write_conclusion(exp, "widget-future", ts="2025-09-01T00:00:00Z")  # > until

    result = _run(exp, since="2025-03-01T00:00:00Z", until="2025-06-01T00:00:00Z")
    ids = [c.conclusion_id for c in result.conclusions]  # type: ignore[attr-defined]
    assert ids == ["widget-mid"]


def test_window_bounds_are_inclusive_at_both_ends(
    tmp_path: Path, home: Path, stub_render: dict
) -> None:
    exp = tmp_path / "exp"
    _write_conclusion(exp, "widget-at-since", ts="2025-03-01T00:00:00Z")  # == since
    _write_conclusion(exp, "widget-at-until", ts="2025-06-01T00:00:00Z")  # == until

    result = _run(exp, since="2025-03-01T00:00:00Z", until="2025-06-01T00:00:00Z")
    ids = sorted(c.conclusion_id for c in result.conclusions)  # type: ignore[attr-defined]
    assert ids == ["widget-at-since", "widget-at-until"]


def test_open_until_includes_everything_at_or_after_since(
    tmp_path: Path, home: Path, stub_render: dict
) -> None:
    exp = tmp_path / "exp"
    _write_conclusion(exp, "widget-before", ts="2025-01-01T00:00:00Z")  # < since
    _write_conclusion(exp, "widget-a", ts="2025-04-01T00:00:00Z")
    _write_conclusion(exp, "widget-b", ts="2999-01-01T00:00:00Z")  # far future, no until

    result = _run(exp, since="2025-03-01T00:00:00Z")  # until defaults open
    ids = sorted(c.conclusion_id for c in result.conclusions)  # type: ignore[attr-defined]
    assert ids == ["widget-a", "widget-b"]
    assert result.as_of is None  # type: ignore[attr-defined]


# --- the unconcluded list: populated + dated ---------------------------------


def test_unconcluded_populated_and_dated(tmp_path: Path, home: Path, stub_render: dict) -> None:
    exp = tmp_path / "exp"
    _write_terminal_campaign(exp, "widget-camp-1", ts="2025-04-15T00:00:00Z")

    result = _run(exp, since="2025-03-01T00:00:00Z", until="2025-06-01T00:00:00Z")
    unconcluded = result.unconcluded  # type: ignore[attr-defined]
    assert len(unconcluded) == 1
    assert unconcluded[0].scope_kind == "campaign"
    assert unconcluded[0].scope_id == "widget-camp-1"
    assert unconcluded[0].completed_at == "2025-04-15T00:00:00Z"


def test_concluded_campaign_drops_off_unconcluded(
    tmp_path: Path, home: Path, stub_render: dict
) -> None:
    exp = tmp_path / "exp"
    _write_terminal_campaign(exp, "widget-camp-1", ts="2025-04-15T00:00:00Z")
    _write_conclusion(
        exp,
        "widget-conc",
        ts="2025-05-01T00:00:00Z",
        concludes=[{"scope_kind": "campaign", "scope_id": "widget-camp-1"}],
    )

    result = _run(exp, since="2025-03-01T00:00:00Z", until="2025-06-01T00:00:00Z")
    assert result.unconcluded == []  # type: ignore[attr-defined]


def test_unconcluded_campaign_before_since_is_excluded(
    tmp_path: Path, home: Path, stub_render: dict
) -> None:
    exp = tmp_path / "exp"
    _write_terminal_campaign(exp, "widget-old-camp", ts="2025-01-01T00:00:00Z")  # < since
    result = _run(exp, since="2025-03-01T00:00:00Z", until="2025-06-01T00:00:00Z")
    assert result.unconcluded == []  # type: ignore[attr-defined]


# --- tags filter within the window -------------------------------------------


def test_tags_filter_within_window(tmp_path: Path, home: Path, stub_render: dict) -> None:
    exp = tmp_path / "exp"
    _write_conclusion(exp, "widget-conc", ts="2025-04-01T00:00:00Z", tags=["widget"])
    _write_conclusion(exp, "gadget-conc", ts="2025-04-02T00:00:00Z", tags=["gadget"])

    result = _run(exp, since="2025-03-01T00:00:00Z", until="2025-06-01T00:00:00Z", tags=["widget"])
    ids = [c.conclusion_id for c in result.conclusions]  # type: ignore[attr-defined]
    assert ids == ["widget-conc"]


# --- cache honesty + disabled -------------------------------------------------


def test_cache_miss_then_hit(tmp_path: Path, home: Path, stub_render: dict) -> None:
    exp = tmp_path / "exp"
    _write_conclusion(exp, "widget-conc", ts="2025-04-01T00:00:00Z")

    first = _run(exp, since="2025-03-01T00:00:00Z", until="2025-06-01T00:00:00Z")
    assert first.cache == "miss"  # type: ignore[attr-defined]
    second = _run(exp, since="2025-03-01T00:00:00Z", until="2025-06-01T00:00:00Z")
    assert second.cache == "hit"  # type: ignore[attr-defined]
    # a hit reproduces the same projection (deleting/serving the cache is a no-op)
    assert [c.conclusion_id for c in second.conclusions] == [  # type: ignore[attr-defined]
        c.conclusion_id
        for c in first.conclusions  # type: ignore[attr-defined]
    ]


def test_cache_disabled_opt_out(
    tmp_path: Path, home: Path, stub_render: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HPC_NO_EVIDENCE_CACHE", "1")
    exp = tmp_path / "exp"
    _write_conclusion(exp, "widget-conc", ts="2025-04-01T00:00:00Z")

    first = _run(exp, since="2025-03-01T00:00:00Z", until="2025-06-01T00:00:00Z")
    second = _run(exp, since="2025-03-01T00:00:00Z", until="2025-06-01T00:00:00Z")
    assert first.cache == "disabled"  # type: ignore[attr-defined]
    assert second.cache == "disabled"  # type: ignore[attr-defined]


# --- fleet + skipped ----------------------------------------------------------


def _write_repo_json(home_dir: Path, namespace: str, experiment_dir: Path | None) -> None:
    p = home_dir / namespace / "repo.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    if experiment_dir is None:
        p.write_text("{ this is torn json", encoding="utf-8")  # unreadable
    else:
        p.write_text(json.dumps({"experiment_dir": str(experiment_dir)}), encoding="utf-8")


def test_fleet_collects_and_accounts_skipped(tmp_path: Path, home: Path, stub_render: dict) -> None:
    home_dir = tmp_path / "home"
    exp_a = tmp_path / "exp_a"
    _write_conclusion(exp_a, "widget-a", ts="2025-04-01T00:00:00Z")
    _write_repo_json(home_dir, "hashA", exp_a)
    _write_repo_json(home_dir, "hashB", None)  # torn → skipped

    result = _run(tmp_path / "unused", since="2025-03-01T00:00:00Z", fleet=True)
    ids = [c.conclusion_id for c in result.conclusions]  # type: ignore[attr-defined]
    assert ids == ["widget-a"]
    refs = {s.ref for s in result.skipped}  # type: ignore[attr-defined]
    assert "hashB" in refs


# --- non-creating -------------------------------------------------------------


def test_non_creating_on_fresh_namespace(tmp_path: Path, home: Path, stub_render: dict) -> None:
    exp = tmp_path / "fresh"
    exp.mkdir()
    result = _run(exp, since="2025-03-01T00:00:00Z", until="2025-06-01T00:00:00Z")
    assert result.conclusions == []  # type: ignore[attr-defined]
    assert result.unconcluded == []  # type: ignore[attr-defined]
    # the collector created no store directory under the experiment namespace
    assert not (exp / ".hpc").exists()
    assert list(exp.iterdir()) == []


# --- render seam stubbed: the WINDOWED collection reaches the render ----------


def test_render_seam_receives_windowed_collection(
    tmp_path: Path, home: Path, stub_render: dict
) -> None:
    exp = tmp_path / "exp"
    _write_conclusion(exp, "widget-old", ts="2025-01-01T00:00:00Z")  # < since, filtered out
    _write_conclusion(exp, "widget-mid", ts="2025-04-01T00:00:00Z")  # in window

    result = _run(exp, since="2025-03-01T00:00:00Z", until="2025-06-01T00:00:00Z")
    assert result.render == "STUB-RENDER"  # type: ignore[attr-defined]
    collection = stub_render["collection"]
    # the render sees the WINDOWED collection, not the raw one
    seen = [c.conclusion_id for c in collection.conclusions]
    assert seen == ["widget-mid"]
    assert stub_render["since"] == "2025-03-01T00:00:00Z"
    assert stub_render["until"] == "2025-06-01T00:00:00Z"
