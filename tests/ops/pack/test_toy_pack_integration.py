"""The toy-widgets pack driven through EVERY seam, end to end (T12, the F10 proof).

One test walks the whole domain-pack lifecycle against the SHIPPED example pack
(``examples/packs/toy-widgets/``, materialized verbatim by
``tests/fixtures/toy_pack``) — so a green run also proves the committed example is
self-consistent:

    bind  ->  resolve every seam (S1-S6)  ->  lint-with-vocab (reader_calls
    resolved via state/pack_declarations)  ->  record a receipt  ->  the gate
    passes at BOTH synchronous seats  ->  edit one pack file  ->  the gate REFUSES
    (drift-revocation live)  ->  rebuild manifest + re-bind + re-receipt  ->  the
    gate passes again.

Toy-domain vocabulary only (``widgets.load_widget``, ``widget-jam``,
``widget-audit``) — never a real domain's words.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import hpc_agent.state.pack_declarations as pd
from hpc_agent import errors
from hpc_agent._wire.actions.notebook_lint import NotebookLintInput
from hpc_agent._wire.actions.pack_bind import PackBindSpec
from hpc_agent._wire.actions.pack_record_receipt import PackRecordReceiptSpec
from hpc_agent.ops.notebook.lint import notebook_lint
from hpc_agent.ops.pack.bind_op import pack_bind
from hpc_agent.ops.pack.record_receipt_op import pack_record_receipt
from hpc_agent.ops.pack_gate import assert_pack_receipts_current
from tests.fixtures.toy_pack import (
    PACK_NAME,
    SLOT,
    TEMPLATE_RELPATH,
    build_toy_pack,
    rebuild_manifest,
)

if TYPE_CHECKING:
    from pathlib import Path

_TEMPLATE_CHECKED = f"packs/{PACK_NAME}/{TEMPLATE_RELPATH}"

# A minimal 4-section audit template the notebook-lint drive needs (structural
# rule wants a template to subsequence against). Toy slugs only.
_LINT_TEMPLATE = """\
# %%
# hpc-audit-section: load-data
DF = None

# %%
# hpc-audit-section: report
R = None
"""


def _write_interview(experiment_dir: Path, manifest_rel: str) -> None:
    """Opt in: a ``packs`` block binding the caller-authored ``widget-audit`` slot."""
    doc = {
        "packs": [
            {
                "pack": PACK_NAME,
                "manifest": manifest_rel,
                "receipt_bindings": [{"slot": SLOT, "pack": PACK_NAME}],
            }
        ]
    }
    (experiment_dir / "interview.json").write_text(json.dumps(doc), encoding="utf-8")


def _record_receipt(experiment_dir: Path) -> None:
    pack_record_receipt(
        experiment_dir=experiment_dir,
        spec=PackRecordReceiptSpec(
            pack=PACK_NAME,
            slot=SLOT,
            checked=[_TEMPLATE_CHECKED],
            passed=True,
            evidence={"checker": "check_widgets"},
        ),
    )


def test_toy_pack_drives_every_seam_and_drift_revokes(tmp_path: Path) -> None:
    # ── setup: materialize the shipped example pack + opt in ──────────────────
    manifest_rel = build_toy_pack(tmp_path)  # packs/toy-widgets/manifest.json
    _write_interview(tmp_path, manifest_rel)

    # ── bind: the committed example binds clean (proves it is self-consistent) ─
    bind_result = pack_bind(experiment_dir=tmp_path, spec=PackBindSpec(manifest=manifest_rel))
    assert bind_result.pack == PACK_NAME
    original_sha = bind_result.manifest_sha
    assert sorted(bind_result.seams) == [
        "audit_template",
        "axis_hints",
        "failure_patterns",
        "reader_calls",
        "registration_fields",
        "tolerances",
    ]

    # ── resolve EVERY loadable seam (S1/S2/S3/S5/S6), each echo == the bind ────
    decls = pd.resolve_declarations(tmp_path)
    assert [d.names for d in decls.reader_calls] == [("widgets.load_widget", "widgets.load_frame")]
    assert decls.failure_patterns[0].patterns == {"widget-jam": r"widget jam at \d+"}
    assert decls.axis_hints[0].hints == ({"pattern": "^widget_seed", "axis": "Independent"},)
    assert decls.tolerances[0].tolerances == {"widget-rmse": 0.01}
    assert decls.registration_fields[0].fields == ("widget-owner",)
    for group in (
        decls.reader_calls,
        decls.failure_patterns,
        decls.axis_hints,
        decls.tolerances,
        decls.registration_fields,
    ):
        assert group[0].echo.pack == PACK_NAME
        assert group[0].echo.sha == original_sha

    # ── S4: the audit-template pack echo resolves by file identity ────────────
    tmpl_echo = pd.resolve_template_pack_echo(tmp_path, _TEMPLATE_CHECKED)
    assert tmpl_echo == {"pack": PACK_NAME, "version": "1.0.0", "sha": original_sha}

    # ── lint-with-vocab: reader_calls resolved via the pack flow into the lint ─
    reader_decl = pd.resolve_reader_calls(tmp_path)[0]
    (tmp_path / "source.py").write_text(
        '# %%\n# hpc-audit-section: load-data\nW = widgets.load_widget("widget_a")\n',
        encoding="utf-8",
    )
    (tmp_path / "template.py").write_text(_LINT_TEMPLATE, encoding="utf-8")
    lint = notebook_lint(
        experiment_dir=tmp_path,
        spec=NotebookLintInput(
            source="source.py",
            template="template.py",
            input_roots=["inputs"],
            reader_calls=list(reader_decl.names),
            reader_calls_echo=reader_decl.echo.as_dict(),
        ),
    )
    live = [f for f in lint.findings if f.rule == "executes_live"]
    assert len(live) == 1
    assert live[0].evidence["reader_call"] == "widgets.load_widget"
    # The pack echo the reader vocabulary came from rides the surfaced result.
    assert lint.reader_call_echo == reader_decl.echo.as_dict()

    # ── receipt + gate PASS at the ONE definition both submit seats call ───────
    _record_receipt(tmp_path)
    assert_pack_receipts_current(tmp_path)  # opted-in, current+passed -> no raise

    # ── edit one pack file -> the gate REFUSES (drift-revocation live) ─────────
    template_on_disk = tmp_path / "packs" / PACK_NAME / TEMPLATE_RELPATH
    template_on_disk.write_text(
        template_on_disk.read_text(encoding="utf-8") + "\n# edited standard\n",
        encoding="utf-8",
    )
    with pytest.raises(errors.SpecInvalid):
        # The edited file is a listed manifest file, so its on-disk sha no longer
        # matches the bind's recorded sha — the gate reads drift and refuses before
        # any submit work (hashes moved -> every clearance under the old sha is void).
        assert_pack_receipts_current(tmp_path)

    # ── rebuild manifest + re-bind + re-receipt -> the gate PASSES again ───────
    rebuild_manifest(tmp_path / "packs" / PACK_NAME)  # new file sha -> new manifest bytes
    rebind = pack_bind(experiment_dir=tmp_path, spec=PackBindSpec(manifest=manifest_rel))
    assert rebind.manifest_sha != original_sha  # the standards moved
    _record_receipt(tmp_path)  # re-check under the new bind
    assert_pack_receipts_current(tmp_path)  # cleared again -> no raise


def test_both_submit_seats_wire_the_one_gate() -> None:
    """The two synchronous seats both route through ``assert_pack_receipts_current``.

    The gate is ONE definition (``ops/pack_gate.py``) called at the pre-sidecar and
    pre-staging seats — the same defense-in-depth the notebook gate wires. A source
    check keeps the "both seats" claim of the integration walk honest without
    standing up a full submit flow.
    """
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    for rel in ("src/hpc_agent/ops/resolve_submit_inputs.py", "src/hpc_agent/ops/submit_flow.py"):
        text = (repo_root / rel).read_text(encoding="utf-8")
        assert "assert_pack_receipts_current(experiment_dir)" in text, (
            f"{rel} no longer calls the ONE pack gate at its submit seat — the "
            "pre-sidecar / pre-staging defense-in-depth is broken."
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
