"""Tests for ``suggest-prelude-action`` — the prelude priority ladder (P1.b).

Coverage strategy: ONE test per rung, each seeded to the minimal substrate state
that rung is supposed to own, plus the properties the ladder promises as a whole:

* **totality** — every seeded state resolves to exactly one action, and the
  bare-dir / fully-settled ends of the range land on their catch-all rungs;
* **precedence** — a lower rung pre-empts a higher one even when both would fire
  (the pack integrity mismatch beats an unsigned audit);
* **the pack integrity pair** — the 2026-07-30 live fumble: a pack with a
  ``bound`` record but no interview.json ``packs`` entry is a NAMED remedy, and
  the reverse direction scaffolds ``pack-bind``;
* **never crashes** — every corrupt-substrate shape (invalid ``axes.yaml``,
  unparseable ``interview.json``, a journal of nothing but bad lines, an
  ``audited_source`` naming an unparseable ``.py``) becomes the disclosed
  ``doctor`` rung rather than an exception;
* **determinism** — the same substrate state produces a byte-identical result.
"""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from hpc_agent.cli.prelude_actions import suggest_prelude_action
from hpc_agent.state import notebook_audit as nb
from hpc_agent.state.audit_source import parse_percent_source
from hpc_agent.state.decision_journal import append_decision

if TYPE_CHECKING:
    from pathlib import Path

_AUDIT = "har_base_sweep"

_TEMPLATE = """\
# %%
# hpc-audit-section: load-data

# %%
# hpc-audit-section: fit-model
"""

_SOURCE = """\
# %%
# hpc-audit-section: load-data
df = load()

# %%
# hpc-audit-section: fit-model
model = fit(df)
"""

_RUN_SRC = """\
from __future__ import annotations
from hpc_agent.experiment_kit import register_run


@register_run
def train(seed: int, lr: float) -> dict:
    return {"seed": seed, "lr": lr}
"""


# ── seeding helpers ───────────────────────────────────────────────────────────


def _write_audit_py(d: Path) -> None:
    (d / "source.py").write_text(_SOURCE, encoding="utf-8")
    (d / "template.py").write_text(_TEMPLATE, encoding="utf-8")


def _write_interview(d: Path, **blocks: Any) -> None:
    (d / "interview.json").write_text(json.dumps(blocks), encoding="utf-8")


def _audited_source_block(audit_id: str = _AUDIT) -> dict[str, Any]:
    return {"audit_id": audit_id, "source": "source.py", "template": "template.py"}


def _open_audit(d: Path, audit_id: str = _AUDIT) -> None:
    """Journal the audit-open config seat (the standalone-audit path)."""
    nb.record_audit_config(d, audit_id=audit_id, input_roots=[], source_roots=[])


def _sign_off(d: Path, slug: str, section_sha: str, audit_id: str = _AUDIT) -> None:
    append_decision(
        d,
        scope_kind="notebook",
        scope_id=audit_id,
        block=nb.SIGN_OFF_BLOCK,
        response="y",
        resolved={
            "audit_id": audit_id,
            "section": slug,
            "section_sha": section_sha,
            "view_sha": "v",
        },
    )


def _sign_every_section(d: Path, audit_id: str = _AUDIT) -> None:
    for section in parse_percent_source(_SOURCE).sections:
        _sign_off(d, section.slug, section.section_sha, audit_id)


def _bind_pack(d: Path, name: str = "rv") -> str:
    """Bind a real minimal pack through ``pack-bind``; return its manifest relpath."""
    from hpc_agent._wire.actions.pack_bind import PackBindSpec
    from hpc_agent.ops.pack.bind_op import pack_bind

    pack_dir = d / "packs" / name
    pack_dir.mkdir(parents=True, exist_ok=True)
    seam = pack_dir / "readers.json"
    seam.write_text('["widgets.load_widget"]', encoding="utf-8")
    seam_sha = hashlib.sha256(seam.read_bytes()).hexdigest()
    manifest_rel = f"packs/{name}/pack.json"
    (d / manifest_rel).write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "files": [{"path": "readers.json", "sha256": seam_sha}],
                "seams": {"reader_calls": "readers.json"},
                "fills_slots": [],
            }
        ),
        encoding="utf-8",
    )
    pack_bind(experiment_dir=d, spec=PackBindSpec(manifest=manifest_rel))
    return manifest_rel


def _materialized(run_name: str = "train") -> dict[str, Any]:
    return {
        "at": "2026-07-30T00:00:00+00:00",
        "entry_point": {"kind": "register_run", "run_name": run_name},
    }


def _current_signature(d: Path, run_name: str = "train") -> str:
    from hpc_agent.state.discover import discover_runs

    return next(i.run_signature_sha for i in discover_runs(d) if i.name == run_name)


def _write_axes(d: Path, run_name: str, signature_sha: str) -> None:
    from hpc_agent.state.axes import upsert_executor

    upsert_executor(
        d,
        run_name,
        executor_entry={
            "run_signature_sha": signature_sha,
            "data_axis": {"kind": "independent"},
            "classified_by": "agent",
            "classified_at": "2026-07-30T00:00:00+00:00",
        },
    )


# ── rung 8: cold start ────────────────────────────────────────────────────────


def test_rung_8_cold_start_scaffolds_the_template(tmp_path: Path) -> None:
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 8
    assert out.action == "notebook-scaffold-template"
    assert out.scaffold.verb == "notebook-scaffold-template"
    assert out.scaffold.unresolved_fields == ["slugs"]
    assert out.substrates.audit_ids == []
    assert out.substrates.interview_json is False


# ── rung 2: an open audit with no config seat ─────────────────────────────────


def test_rung_2_journal_without_config_seat(tmp_path: Path) -> None:
    _write_audit_py(tmp_path)
    # A sign-off landed but the audit-open seat was never recorded.
    _sign_off(tmp_path, "load-data", parse_percent_source(_SOURCE).sections[0].section_sha)
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 2
    assert out.action == "notebook-record-config"
    assert out.scaffold.verb == "notebook-record"
    assert out.scaffold.spec == {
        "kind": "config",
        "audit_id": _AUDIT,
        "input_roots": [],
        "source_roots": [],
    }
    assert out.substrates.audit_id == _AUDIT
    assert out.substrates.audit_config_recorded is False


# ── rung 3: a standalone audit whose source/template live with the caller ─────


def test_rung_3_standalone_audit_hands_the_predicate_back(tmp_path: Path) -> None:
    _write_audit_py(tmp_path)
    _open_audit(tmp_path)  # journal seat only — no interview.json audited_source
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 3
    assert out.action == "notebook-status"
    # The predicate is NOT approximated here: source/template come back as
    # placeholders the caller fills.
    assert out.scaffold.unresolved_fields == ["source", "template"]
    assert out.scaffold.spec is not None
    assert out.scaffold.spec["audit_id"] == _AUDIT
    assert out.substrates.audit_config_recorded is True
    assert out.substrates.audited_source_seat is False
    assert out.substrates.notebook_passed is None


# ── rung 4: sections awaiting sign-off ────────────────────────────────────────


def test_rung_4_sections_awaiting_sign_off_relays_the_view(tmp_path: Path) -> None:
    _write_audit_py(tmp_path)
    _write_interview(tmp_path, audited_source=_audited_source_block())
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 4
    assert out.action == "notebook-audit-view"
    assert out.substrates.notebook_passed is False
    assert out.substrates.sections_awaiting == 2
    assert f"audit {_AUDIT} has 2 section(s) awaiting sign-off" in out.why
    assert out.scaffold.spec == {
        "audit_id": _AUDIT,
        "source": "source.py",
        "template": "template.py",
    }


def test_rung_4_counts_only_the_sections_still_unsigned(tmp_path: Path) -> None:
    _write_audit_py(tmp_path)
    _write_interview(tmp_path, audited_source=_audited_source_block())
    _sign_off(tmp_path, "load-data", parse_percent_source(_SOURCE).sections[0].section_sha)
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 4
    assert out.substrates.sections_awaiting == 1


# ── rung 5: a passed audit with no interview intent ───────────────────────────


def test_rung_5_passed_audit_without_intent_runs_audit_handoff(tmp_path: Path) -> None:
    _write_audit_py(tmp_path)
    _write_interview(tmp_path, audited_source=_audited_source_block())
    _sign_every_section(tmp_path)
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 5
    assert out.action == "audit-handoff"
    assert out.substrates.notebook_passed is True
    assert out.substrates.sections_awaiting == 0
    assert out.scaffold.spec == {
        "audit_id": _AUDIT,
        "source": "source.py",
        "template": "template.py",
    }


# ── rung 6: drafted intent, nothing materialized ──────────────────────────────


def test_rung_6_intent_without_materialization_runs_the_interview(tmp_path: Path) -> None:
    _write_audit_py(tmp_path)
    _write_interview(tmp_path, audited_source=_audited_source_block(), goal="sweep the halo window")
    _sign_every_section(tmp_path)
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 6
    assert out.action == "interview"
    # The InterviewSpec is composed by scaffold-spec, never hand-divined.
    assert out.scaffold.verb == "scaffold-spec"
    assert out.scaffold.cli is not None
    assert "--verb interview" in out.scaffold.cli


def test_rung_6_fires_without_any_audit(tmp_path: Path) -> None:
    _write_interview(tmp_path, goal="a bare interview shell")
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 6
    assert out.substrates.audit_ids == []


# ── rung 7: axes.yaml absent / stale ──────────────────────────────────────────


def test_rung_7_axes_absent_after_materialization(tmp_path: Path) -> None:
    _write_interview(tmp_path, goal="g", _materialized=_materialized())
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 7
    assert out.action == "classify-axis"
    assert ".hpc/axes.yaml is absent" in out.why
    assert out.substrates.entry_point_run_name == "train"
    assert out.scaffold.spec is not None
    assert out.scaffold.spec["run_name"] == "train"
    # The classification itself is a judgment point — never auto-resolved.
    assert "data_axis" in out.scaffold.unresolved_fields
    assert out.scaffold.spec["data_axis"] == {"kind": "sequential"}


def test_rung_7_axes_present_but_no_entry_for_the_run(tmp_path: Path) -> None:
    (tmp_path / "train.py").write_text(_RUN_SRC, encoding="utf-8")
    _write_interview(tmp_path, goal="g", _materialized=_materialized())
    _write_axes(tmp_path, "other_run", "deadbeef")
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 7
    assert "no classified axis for run 'train'" in out.why
    # The signature IS derivable from the AST scan, so it is filled, not flagged.
    assert out.scaffold.unresolved_fields == ["data_axis"]
    assert out.scaffold.spec is not None
    assert out.scaffold.spec["run_signature_sha"] == _current_signature(tmp_path)


def test_rung_7_stale_when_the_recorded_signature_moved(tmp_path: Path) -> None:
    (tmp_path / "train.py").write_text(_RUN_SRC, encoding="utf-8")
    _write_interview(tmp_path, goal="g", _materialized=_materialized())
    _write_axes(tmp_path, "train", "a" * 64)  # classified at a signature that moved
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 7
    assert "(stale)" in out.why


# ── rung 9: everything settled ────────────────────────────────────────────────


def test_rung_9_settled_prelude_starts_the_submit_chain(tmp_path: Path) -> None:
    (tmp_path / "train.py").write_text(_RUN_SRC, encoding="utf-8")
    _write_audit_py(tmp_path)
    _write_interview(
        tmp_path,
        audited_source=_audited_source_block(),
        goal="g",
        _materialized=_materialized(),
    )
    _sign_every_section(tmp_path)
    _write_axes(tmp_path, "train", _current_signature(tmp_path))
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 9
    assert out.action == "submit-s1"
    assert out.scaffold.verb == "block-drive"
    assert out.scaffold.spec == {"workflow": "submit"}
    assert out.substrates.notebook_passed is None  # no notebook rung fired
    assert out.substrates.materialized is True


# ── rung 1: the pack bind / opt-in integrity pair ─────────────────────────────


def test_rung_1_bound_but_not_opted_in_is_a_named_remedy(tmp_path: Path) -> None:
    """The 2026-07-30 live fumble: a bind with no ``packs`` entry.

    ``pack-status`` iterates the OPT-IN list, so it never reports this pack at
    all and every pack gate silently passes. The ladder names it.
    """
    _bind_pack(tmp_path, "rv")
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 1
    assert out.action == "pack-optin-repair"
    assert "bound but not opted in" in out.why
    assert out.substrates.packs_bound == ["rv"]
    assert out.substrates.packs_opted_in == []
    # The remedy is a FILE EDIT, not a verb call.
    assert out.scaffold.verb == "interview.json"
    assert out.scaffold.cli is None
    assert out.scaffold.spec is not None
    assert out.scaffold.spec["packs"][0]["pack"] == "rv"
    # The manifest relpath is DERIVED (name + bind-sha identity), so the fragment
    # is copy-pasteable and nothing is left unresolved.
    assert out.scaffold.spec["packs"][0]["manifest"] == "packs/rv/pack.json"
    assert out.scaffold.unresolved_fields == []
    # And the named remedy rides the finding too.
    remedies = [f.remedy for f in out.findings if f.substrate == "pack"]
    assert any(r and "bound but not opted in — add the packs entry" in r for r in remedies)


def test_bound_not_opted_in_manifest_placeholder_names_the_pack(tmp_path: Path) -> None:
    """No resolvable manifest → a placeholder that still SUBSTITUTES the pack name.

    A guess stays disclosed in ``unresolved_fields``, but it is a useful guess:
    the conventional location with the real pack name, not a literal ``<pack>``.
    """
    _bind_pack(tmp_path, "rv")
    (tmp_path / "packs" / "rv" / "pack.json").unlink()  # the bind outlives the file
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 1
    assert out.scaffold.spec is not None
    assert out.scaffold.spec["packs"][0]["manifest"] == "PLACEHOLDER-packs/rv/pack.json"
    assert out.scaffold.unresolved_fields == ["packs[0].manifest"]


def test_bound_not_opted_in_manifest_resolves_by_bind_sha_not_by_name(tmp_path: Path) -> None:
    """Two manifests declare the same pack; the BOUND one wins on sha identity.

    A lab-vs-upstream copy differs by sha but not by name, so a name-only match
    would emit the wrong relpath half the time. The bind's ``manifest_sha`` makes
    the match an identity.
    """
    _bind_pack(tmp_path, "rv")  # binds packs/rv/pack.json
    decoy = tmp_path / "vendor" / "rv"
    decoy.mkdir(parents=True)
    seam = decoy / "readers.json"
    seam.write_text('["widgets.other_reader"]', encoding="utf-8")
    decoy_seam_sha = hashlib.sha256(seam.read_bytes()).hexdigest()
    (decoy / "pack.json").write_text(
        json.dumps(
            {
                "name": "rv",  # SAME name…
                "version": "9.9.9",  # …different bytes → different sha
                "files": [{"path": "readers.json", "sha256": decoy_seam_sha}],
                "seams": {"reader_calls": "readers.json"},
                "fills_slots": [],
            }
        ),
        encoding="utf-8",
    )
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 1
    assert out.scaffold.spec is not None
    assert out.scaffold.spec["packs"][0]["manifest"] == "packs/rv/pack.json"
    assert out.scaffold.unresolved_fields == []


def test_ambiguous_manifests_stay_a_disclosed_placeholder(tmp_path: Path) -> None:
    """Two byte-identical manifests: the sha cannot single one out → disclose.

    Picking one would be a coin flip that binds a possibly-wrong pack root, so the
    ladder discloses the ambiguity and keeps the placeholder.
    """
    _bind_pack(tmp_path, "rv")
    twin = tmp_path / "vendor" / "rv"
    twin.mkdir(parents=True)
    for name in ("pack.json", "readers.json"):
        (twin / name).write_bytes((tmp_path / "packs" / "rv" / name).read_bytes())
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 1
    assert out.scaffold.spec is not None
    assert out.scaffold.spec["packs"][0]["manifest"] == "PLACEHOLDER-packs/rv/pack.json"
    assert out.scaffold.unresolved_fields == ["packs[0].manifest"]
    assert any("manifests declare pack 'rv'" in d for d in out.disclosures)


def test_rung_1_opted_in_but_not_bound_scaffolds_pack_bind(tmp_path: Path) -> None:
    _write_interview(
        tmp_path,
        packs=[{"pack": "rv", "manifest": "packs/rv/pack.json", "receipt_bindings": []}],
    )
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 1
    assert "no current bind" in out.why
    assert out.substrates.packs_opted_in == ["rv"]
    assert out.substrates.packs_bound == []
    assert out.scaffold.verb == "pack-bind"
    assert out.scaffold.spec == {"manifest": "packs/rv/pack.json", "pack": "rv"}
    assert out.scaffold.unresolved_fields == []


def test_rung_1_prefers_the_silent_bound_not_opted_in_direction(tmp_path: Path) -> None:
    _bind_pack(tmp_path, "rv")
    _write_interview(
        tmp_path,
        packs=[{"pack": "other", "manifest": "packs/other/pack.json", "receipt_bindings": []}],
    )
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 1
    assert "'rv' bound but not opted in" in out.why
    # BOTH directions are still reported as findings — nothing is dropped.
    details = " | ".join(f.detail for f in out.findings)
    assert "'rv'" in details and "'other'" in details


def test_pack_integrity_pre_empts_an_unsigned_audit(tmp_path: Path) -> None:
    """Precedence: a lower rung wins even when a higher one would also fire."""
    _write_audit_py(tmp_path)
    _write_interview(tmp_path, audited_source=_audited_source_block())
    _bind_pack(tmp_path, "rv")
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 1
    # The pre-empted audit state is still disclosed on the evidence vector.
    assert out.substrates.audit_ids == [_AUDIT]


def test_consistent_pack_pair_does_not_fire(tmp_path: Path) -> None:
    manifest_rel = _bind_pack(tmp_path, "rv")
    _write_interview(
        tmp_path, packs=[{"pack": "rv", "manifest": manifest_rel, "receipt_bindings": []}]
    )
    out = suggest_prelude_action(tmp_path)
    assert out.rung != 1
    assert out.substrates.packs_bound == ["rv"]
    assert out.substrates.packs_opted_in == ["rv"]


# ── rung 0: corrupt / unreadable substrates ───────────────────────────────────


def test_rung_0_invalid_axes_yaml(tmp_path: Path) -> None:
    (tmp_path / ".hpc").mkdir()
    (tmp_path / ".hpc" / "axes.yaml").write_text("axes_schema_version: nope\n", encoding="utf-8")
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 0
    assert out.action == "doctor"
    assert out.scaffold.verb == "doctor"
    assert any(f.substrate == "axes" for f in out.findings)


def test_rung_0_unparseable_interview_json(tmp_path: Path) -> None:
    (tmp_path / "interview.json").write_text("{not json", encoding="utf-8")
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 0
    assert out.action == "doctor"
    # Presence is still reported honestly — "absent" and "unreadable" differ.
    assert out.substrates.interview_json is True
    assert any("not parseable JSON" in f.detail for f in out.findings)


def test_rung_0_interview_json_that_is_not_an_object(tmp_path: Path) -> None:
    (tmp_path / "interview.json").write_text("[1, 2, 3]", encoding="utf-8")
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 0
    assert any("not a JSON object" in f.detail for f in out.findings)


def test_rung_0_journal_of_nothing_but_corrupt_lines(tmp_path: Path) -> None:
    """A non-empty journal that yields zero records must not read as "no audit".

    ``read_decisions`` skips individually bad lines by design, so this state is
    silently empty to every other reader — exactly why it is surfaced here.
    """
    journal = tmp_path / ".hpc" / "notebooks"
    journal.mkdir(parents=True)
    (journal / f"{_AUDIT}.decisions.jsonl").write_text("not json at all\n", encoding="utf-8")
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 0
    assert any("yields no readable records" in f.detail for f in out.findings)


def test_rung_0_malformed_packs_block(tmp_path: Path) -> None:
    _write_interview(tmp_path, packs={"rv": "not a list"})
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 0
    assert any("'packs' opt-in block is not a list" in f.detail for f in out.findings)


def test_rung_0_packs_entry_that_is_not_an_object(tmp_path: Path) -> None:
    """A malformed ENTRY is the same failure class as a malformed block.

    It silently opts in nothing, so every gate over that pack passes — the exact
    fumble class this verb exists to surface. A bare skip would recreate it.
    """
    _write_interview(tmp_path, packs=["rv"])
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 0
    assert out.action == "doctor"
    assert any("'packs'[0] is a str, not an object" in f.detail for f in out.findings)


def test_rung_0_packs_entry_without_a_string_pack_name(tmp_path: Path) -> None:
    _write_interview(tmp_path, packs=[{"pack": 5, "manifest": "packs/rv/pack.json"}])
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 0
    assert any("declares no string 'pack' name (got 5)" in f.detail for f in out.findings)
    assert out.substrates.packs_opted_in == []


def test_doctor_pre_empts_the_pack_repair(tmp_path: Path) -> None:
    """Rung 0 MUST out-rank rung 1, and the ordering is load-bearing.

    Mutation this test kills: move ``_rule_doctor`` below ``_rule_pack_optin``.
    With a bound pack AND a malformed (dict-shaped) ``packs`` block, the mutant
    answers "bound but not opted in — add the packs entry", which is actively
    wrong: a ``packs`` block DOES exist, it is malformed, and following the advice
    would author a duplicate. Deciding over unknown state must always lose to
    reporting that the state is unknown.
    """
    _bind_pack(tmp_path, "rv")
    _write_interview(tmp_path, packs={"rv": "packs/rv/pack.json"})  # a dict, not a list
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 0
    assert out.action == "doctor"
    assert "add the packs entry" not in out.why
    # BOTH facts are still disclosed — the pre-empted repair is never dropped.
    details = " | ".join(f.detail for f in out.findings)
    assert "'packs' opt-in block is not a list" in details
    assert "has a current bind but no interview.json 'packs' entry" in details


def test_rung_0_audited_source_naming_an_unparseable_module(tmp_path: Path) -> None:
    # A misplaced section marker: the percent parser refuses loudly.
    (tmp_path / "source.py").write_text(
        "# %%\nx = 1\n# hpc-audit-section: load-data\n", encoding="utf-8"
    )
    (tmp_path / "template.py").write_text(_TEMPLATE, encoding="utf-8")
    _write_interview(tmp_path, audited_source=_audited_source_block())
    _open_audit(tmp_path)
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 0
    assert any("could not be read or parsed" in f.detail for f in out.findings)


def test_rung_0_audited_source_naming_a_missing_file(tmp_path: Path) -> None:
    _write_interview(tmp_path, audited_source=_audited_source_block())
    _open_audit(tmp_path)
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 0
    assert out.action == "doctor"


# ── whole-ladder properties ───────────────────────────────────────────────────


def test_ladder_is_deterministic(tmp_path: Path) -> None:
    _write_audit_py(tmp_path)
    _write_interview(tmp_path, audited_source=_audited_source_block())
    first = suggest_prelude_action(tmp_path).model_dump()
    second = suggest_prelude_action(tmp_path).model_dump()
    assert first == second


def test_ladder_never_raises_on_a_thoroughly_broken_repo(tmp_path: Path) -> None:
    """Several substrates broken at once still yields ONE disclosed suggestion."""
    (tmp_path / "interview.json").write_text("{{{", encoding="utf-8")
    (tmp_path / ".hpc").mkdir(exist_ok=True)
    (tmp_path / ".hpc" / "axes.yaml").write_text(": : :\n", encoding="utf-8")
    notebooks = tmp_path / ".hpc" / "notebooks"
    notebooks.mkdir(parents=True, exist_ok=True)
    (notebooks / "a.decisions.jsonl").write_text("}\n", encoding="utf-8")
    out = suggest_prelude_action(tmp_path)
    assert out.rung == 0
    assert out.action == "doctor"
    assert len(out.findings) >= 3


def test_every_action_is_reachable_from_some_seeded_state(tmp_path: Path) -> None:
    """The ladder's action vocabulary carries no dead members.

    ``verify a guard can actually fire``: a rung whose condition no state can
    satisfy is inertia, not design. Every member of the closed vocabulary must be
    produced by some state this suite seeds — this test asserts the set covered by
    the per-rung tests above equals the declared vocabulary.
    """
    import typing

    from hpc_agent._wire.queries.suggest_prelude_action import PreludeAction

    declared = set(typing.get_args(PreludeAction))
    seen: set[str] = set()

    def _fresh(name: str) -> Path:
        d = tmp_path / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    d = _fresh("cold")
    seen.add(suggest_prelude_action(d).action)

    d = _fresh("doctor")
    (d / "interview.json").write_text("{{{", encoding="utf-8")
    seen.add(suggest_prelude_action(d).action)

    d = _fresh("pack")
    _bind_pack(d, "rv")
    seen.add(suggest_prelude_action(d).action)

    d = _fresh("noconfig")
    _write_audit_py(d)
    _sign_off(d, "load-data", parse_percent_source(_SOURCE).sections[0].section_sha)
    seen.add(suggest_prelude_action(d).action)

    d = _fresh("standalone")
    _write_audit_py(d)
    _open_audit(d)
    seen.add(suggest_prelude_action(d).action)

    d = _fresh("unsigned")
    _write_audit_py(d)
    _write_interview(d, audited_source=_audited_source_block())
    seen.add(suggest_prelude_action(d).action)

    d = _fresh("passed")
    _write_audit_py(d)
    _write_interview(d, audited_source=_audited_source_block())
    _sign_every_section(d)
    seen.add(suggest_prelude_action(d).action)

    d = _fresh("intent")
    _write_interview(d, goal="g")
    seen.add(suggest_prelude_action(d).action)

    d = _fresh("axes")
    _write_interview(d, goal="g", _materialized=_materialized())
    seen.add(suggest_prelude_action(d).action)

    d = _fresh("settled")
    (d / "train.py").write_text(_RUN_SRC, encoding="utf-8")
    _write_interview(d, goal="g", _materialized=_materialized())
    _write_axes(d, "train", _current_signature(d))
    seen.add(suggest_prelude_action(d).action)

    assert seen == declared, f"unreachable actions: {sorted(declared - seen)}"
