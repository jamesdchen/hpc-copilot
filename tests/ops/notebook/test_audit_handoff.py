"""Tests for the ``audit-handoff`` projection + the audit-open intent seat.

Coverage:

* the audit-open journaling round-trips — ``record_audit_config`` with
  ``goal`` / ``task_axes`` → ``read_audit_intent`` → the projection reads them;
* the ``$HPC_RESULT_DIR`` write scanner finds the DECLARED forms
  (``os.path.join`` / ``Path`` ``/`` / f-string, inline + aliased base) and is
  HONEST about a computed tail (unverifiable) and an uncovered form (a safe miss);
* ``@register_run`` entry-point detection — one fills, zero/several disclose;
* non-derivable fields are PLACEHOLDERS, never guessed;
* the projection is deterministic (same records → byte-identical draft).
"""

from __future__ import annotations

import ast
from typing import TYPE_CHECKING

from hpc_agent._wire.queries.audit_handoff import AuditHandoffSpec
from hpc_agent.ops.notebook.audit_handoff_op import (
    _scan_register_run,
    _scan_result_writes,
    audit_handoff,
)
from hpc_agent.state import notebook_audit

if TYPE_CHECKING:
    from pathlib import Path

_TEMPLATE = """\
# %%
# hpc-audit-section: load

# %%
# hpc-audit-section: report
"""


def _write_source(tmp_path: Path, body: str, name: str = "analysis.py") -> str:
    (tmp_path / name).write_text(body, encoding="utf-8")
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    return name


def _spec(source: str = "analysis.py") -> AuditHandoffSpec:
    return AuditHandoffSpec(audit_id="aud-1", source=source, template="template.py")


# ── scanner: $HPC_RESULT_DIR writes ───────────────────────────────────────────


def _scan(body: str) -> tuple[list[str], list[str]]:
    return _scan_result_writes(ast.parse(body))


def test_scan_os_path_join_aliased_base() -> None:
    body = "import os\nrd = os.environ['HPC_RESULT_DIR']\np = os.path.join(rd, 'summary.json')\n"
    candidates, unverifiable = _scan(body)
    assert candidates == ["summary.json"]
    assert unverifiable == []


def test_scan_join_inline_env_and_multi_segment() -> None:
    body = "import os\np = os.path.join(os.environ['HPC_RESULT_DIR'], 'sub', 'out.csv')\n"
    candidates, _ = _scan(body)
    assert candidates == ["sub/out.csv"]


def test_scan_pathlib_div_operator() -> None:
    body = (
        "import os\n"
        "from pathlib import Path\n"
        "base = Path(os.environ['HPC_RESULT_DIR'])\n"
        "out = base / 'metrics' / 'run.json'\n"
    )
    candidates, unverifiable = _scan(body)
    assert candidates == ["metrics/run.json"]
    assert unverifiable == []


def test_scan_fstring_literal_tail() -> None:
    body = "import os\nrd = os.getenv('HPC_RESULT_DIR')\np = f'{rd}/summary.json'\n"
    candidates, _ = _scan(body)
    assert candidates == ["summary.json"]


def test_scan_result_dir_alias_env_key() -> None:
    body = "import os\nrd = os.environ.get('RESULT_DIR')\np = os.path.join(rd, 'r.json')\n"
    candidates, _ = _scan(body)
    assert candidates == ["r.json"]


def test_scan_computed_tail_is_unverifiable_not_dropped() -> None:
    body = (
        "import os\n"
        "rd = os.environ['HPC_RESULT_DIR']\n"
        "name = 'x'\n"
        "p = os.path.join(rd, name)\n"
        "q = f'{rd}/{name}.json'\n"
    )
    candidates, unverifiable = _scan(body)
    assert candidates == []
    # Both computed forms are disclosed, never silently dropped.
    assert len(unverifiable) == 2


def test_scan_dedup_and_sorted() -> None:
    body = (
        "import os\n"
        "rd = os.environ['HPC_RESULT_DIR']\n"
        "a = os.path.join(rd, 'b.json')\n"
        "b = os.path.join(rd, 'b.json')\n"
        "c = os.path.join(rd, 'a.json')\n"
    )
    candidates, _ = _scan(body)
    assert candidates == ["a.json", "b.json"]


def test_scan_declared_noncoverage_is_a_safe_miss() -> None:
    # `+` concatenation and str.format are NOT covered — the scanner honestly
    # misses them (no false journaled fact), reporting neither candidate nor gap.
    body = (
        "import os\n"
        "rd = os.environ['HPC_RESULT_DIR']\n"
        "p = rd + '/summary.json'\n"
        "q = '{}/out.json'.format(rd)\n"
    )
    candidates, unverifiable = _scan(body)
    assert candidates == []
    assert unverifiable == []


def test_scan_ignores_non_result_paths() -> None:
    body = "import os\nrd = os.environ['OTHER_DIR']\np = os.path.join(rd, 'x.json')\n"
    candidates, unverifiable = _scan(body)
    assert candidates == []
    assert unverifiable == []


# ── scanner: @register_run ────────────────────────────────────────────────────


def test_register_run_bare_and_called_and_dotted() -> None:
    body = (
        "from hpc_agent import register_run\n"
        "@register_run\n"
        "def train():\n    pass\n"
        "@register_run()\n"
        "def evaluate():\n    pass\n"
        "@hpc_agent.register_run\n"
        "def score():\n    pass\n"
    )
    assert _scan_register_run(ast.parse(body)) == ["evaluate", "score", "train"]


def test_register_run_absent() -> None:
    assert _scan_register_run(ast.parse("def main():\n    pass\n")) == []


# ── the projection ────────────────────────────────────────────────────────────


def test_projection_reads_journaled_intent_and_writes(tmp_path: Path) -> None:
    body = (
        "import os\n"
        "from hpc_agent import register_run\n"
        "@register_run\n"
        "def forecast(bucket, chunk):\n"
        "    rd = os.environ['HPC_RESULT_DIR']\n"
        "    with open(os.path.join(rd, 'summary.json'), 'w') as f:\n"
        "        f.write('{}')\n"
    )
    _write_source(tmp_path, body)
    notebook_audit.record_audit_config(
        tmp_path,
        audit_id="aud-1",
        input_roots=["data"],
        source_roots=["src"],
        output_roots=["results"],
        goal="forecast returns across buckets and chunks",
        task_axes=["bucket", "chunk"],
    )

    result = audit_handoff(experiment_dir=tmp_path, spec=_spec())

    assert result.goal == "forecast returns across buckets and chunks"
    assert result.task_axes == ["bucket", "chunk"]
    assert result.entry_point == {"kind": "register_run", "run_name": "forecast"}
    assert result.entry_point_candidates == ["forecast"]
    assert result.summary_artifact_candidates == ["summary.json"]
    assert result.audited_source["input_roots"] == ["data"]
    assert result.audited_source["source_roots"] == ["src"]
    assert result.audited_source["output_roots"] == ["results"]
    assert result.audited_source["audit_id"] == "aud-1"
    # task_generator / task_count / produced_by are ALWAYS placeholders.
    placeholder_fields = {p.field for p in result.placeholders}
    assert {"task_generator", "task_count", "produced_by"} <= placeholder_fields
    assert "goal" not in placeholder_fields  # goal was journaled → not a placeholder


def test_intent_round_trips_through_read_audit_intent(tmp_path: Path) -> None:
    notebook_audit.record_audit_config(
        tmp_path,
        audit_id="aud-2",
        input_roots=[],
        source_roots=[],
        goal="a goal",
        task_axes=["seed"],
    )
    goal, axes = notebook_audit.read_audit_intent(tmp_path, "aud-2")
    assert goal == "a goal"
    assert axes == ["seed"]


def test_config_only_record_has_no_intent(tmp_path: Path) -> None:
    # A config record written WITHOUT intent reads (None, []) — byte-identical
    # posture, and audit-handoff emits a goal placeholder rather than guessing.
    notebook_audit.record_audit_config(
        tmp_path, audit_id="aud-3", input_roots=["d"], source_roots=["s"]
    )
    goal, axes = notebook_audit.read_audit_intent(tmp_path, "aud-3")
    assert goal is None
    assert axes == []


def test_missing_goal_is_placeholder_not_guessed(tmp_path: Path) -> None:
    _write_source(tmp_path, "def main():\n    pass\n")
    # No config/intent recorded at all.
    result = audit_handoff(experiment_dir=tmp_path, spec=_spec())
    assert result.goal is None
    assert "goal" in {p.field for p in result.placeholders}
    assert any("no goal" in d.lower() for d in result.disclosures)


def test_ambiguous_entry_point_is_disclosed_not_picked(tmp_path: Path) -> None:
    body = (
        "from hpc_agent import register_run\n"
        "@register_run\n"
        "def a():\n    pass\n"
        "@register_run\n"
        "def b():\n    pass\n"
    )
    _write_source(tmp_path, body)
    result = audit_handoff(experiment_dir=tmp_path, spec=_spec())
    assert result.entry_point is None  # never picks across candidates
    assert result.entry_point_candidates == ["a", "b"]
    assert "entry_point" in {p.field for p in result.placeholders}


def test_multiple_summary_candidates_disclosed(tmp_path: Path) -> None:
    body = (
        "import os\n"
        "rd = os.environ['HPC_RESULT_DIR']\n"
        "a = os.path.join(rd, 'metrics.json')\n"
        "b = os.path.join(rd, 'trace.csv')\n"
    )
    _write_source(tmp_path, body)
    result = audit_handoff(experiment_dir=tmp_path, spec=_spec())
    assert result.summary_artifact_candidates == ["metrics.json", "trace.csv"]
    assert any("candidate" in d.lower() for d in result.disclosures)


def test_projection_is_deterministic(tmp_path: Path) -> None:
    body = (
        "import os\n"
        "from hpc_agent import register_run\n"
        "@register_run\n"
        "def forecast():\n"
        "    rd = os.environ['HPC_RESULT_DIR']\n"
        "    open(os.path.join(rd, 'summary.json'), 'w')\n"
    )
    _write_source(tmp_path, body)
    notebook_audit.record_audit_config(
        tmp_path,
        audit_id="aud-1",
        input_roots=["data"],
        source_roots=["src"],
        goal="g",
        task_axes=["x"],
    )
    first = audit_handoff(experiment_dir=tmp_path, spec=_spec())
    second = audit_handoff(experiment_dir=tmp_path, spec=_spec())
    assert first.model_dump_json() == second.model_dump_json()
