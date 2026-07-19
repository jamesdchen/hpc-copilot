"""Hermetic pins for ``sandbox_seed`` — plan-unit U2 of
``docs/plans/sandbox-proving-run-2026-07-18.md`` (the §3 trust doctrine).

The seeder is the ONE sanctioned way a sandbox proving run plants authorship
evidence into an ephemeral journal namespace so the REAL gates fire against it
(rung 2). These tests pin the two halves of that contract, with no docker, no
SSH, and no cluster — the file deliberately carries NO ``slow`` /
``scheduler_integration`` mark so it runs in the default tier:

* the STRUCTURAL GUARD — every seeding function refuses when
  ``HPC_JOURNAL_DIR`` is unset/empty, when it (or the declared
  ``journal_home``) resolves inside the production ``~/.claude/hpc``, and when
  the declaration diverges from the env authority — so the helper is
  structurally incapable of touching a production namespace;
* the SEEDED SUBSTRATE — a seeded utterance lands at the exact locator the
  human-authorship gate reads (``state.utterances.utterances_path`` →
  ``<journal home>/<repo_hash>/utterances.jsonl``, consumed via
  ``ops/decision/journal/_shared.py::_harness_human_texts``), carries the
  frozen writer's record shape plus the additive §3 provenance stamp, and
  actually SATISFIES the real gate (while an un-uttered value still refuses);
* the U5.5 DECOY-NAMESPACE PIN — a record seeded in namespace A is invisible
  to a gate read scoped to namespace B under the SAME sandbox home (the
  namespace-coupling snag of 2026-07-18, as a permanent assertion);
* ``seed_prior_signoff`` round-trips through the REAL decision-journal write
  path (``state.decision_journal.append_decision`` — the conformance
  ``seed_triple`` precedent) and is read back by the gate's prior-record scan.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sandbox_seed import (
    SANDBOX_SEEDED_BY,
    SandboxSeedError,
    assert_sandbox_journal_home,
    seed_prior_signoff,
    seed_utterance,
)

RUN_REF = "sandbox-u2-tests"

# >=4-char word tokens (the gate's ``_HA_WORD_RE`` floor): "fit"/"the" never
# count, so the utterance and the committed value overlap on real vocabulary.
GOAL_UTTERANCE = "please fit the garch volatility model sweep with 20 seeds at 1M samples"
GOAL_VALUE = "garch volatility model sweep"
UNUTTERED_GOAL = "hurst exponent estimation over fractional noise paths"


@pytest.fixture
def sandbox_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An ephemeral journal home under tmp, named by HPC_JOURNAL_DIR.

    ``monkeypatch.setenv`` is the documented override idiom: the env var
    out-ranks the ``HPC_HOMEDIR`` attribute the session-wide autouse
    ``_isolated_journal_home`` fixture patches (tests/conftest.py).
    """
    home = tmp_path / "sandbox_journal_home"
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(home))
    return home


def _spec(scope_id: str, block: str = "submit-s1", response: str = "y"):
    """An append-decision input shaped the way the driver commits one."""
    from hpc_agent._wire.actions.decision_journal import AppendDecisionInput

    return AppendDecisionInput.model_validate(
        {"scope_kind": "run", "scope_id": scope_id, "block": block, "response": response}
    )


def _gate(experiment_dir: Path, scope_id: str, resolved: dict) -> None:
    """The REAL human-authorship gate, exactly as the ops append path calls it."""
    from hpc_agent.ops.decision.journal import _assert_human_authorship

    _assert_human_authorship(experiment_dir, _spec(scope_id), resolved)


# ── (a) the guard refuses when HPC_JOURNAL_DIR is unset ──────────────────────


def test_guard_refuses_when_env_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The autouse fixture already popped any shell-inherited HPC_JOURNAL_DIR;
    # delenv makes the precondition explicit AND order-independent. Note the
    # refusal fires even though the autouse fixture left a usable-looking
    # HPC_HOMEDIR attribute fallback — leg 1 of the guard: the quieter
    # fallbacks must never silently authorize a seed.
    monkeypatch.delenv("HPC_JOURNAL_DIR", raising=False)
    exp = tmp_path / "exp"
    with pytest.raises(SandboxSeedError, match="HPC_JOURNAL_DIR"):
        assert_sandbox_journal_home(tmp_path / "anywhere")
    with pytest.raises(SandboxSeedError, match="HPC_JOURNAL_DIR"):
        seed_utterance(tmp_path / "anywhere", exp, GOAL_UTTERANCE, run_ref=RUN_REF)
    with pytest.raises(SandboxSeedError, match="HPC_JOURNAL_DIR"):
        seed_prior_signoff(
            tmp_path / "anywhere", exp, run_ref=RUN_REF, scope_id="sandbox-run-a1", block="s1"
        )
    # The refusal preceded every write — not even the experiment dir was made.
    assert not exp.exists()


def test_guard_refuses_when_env_empty(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # kills: ``if env_val is None`` without the empty-string arm — an explicit
    # "" must degrade to "unset" (the real resolver treats it the same way).
    monkeypatch.setenv("HPC_JOURNAL_DIR", "")
    with pytest.raises(SandboxSeedError, match="HPC_JOURNAL_DIR"):
        assert_sandbox_journal_home(tmp_path / "anywhere")


# ── (b) the guard refuses any home resolving inside the real ~/.claude/hpc ───


def test_guard_refuses_production_journal_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    production = Path.home() / ".claude" / "hpc"
    exp = tmp_path / "exp"
    for doomed in (production, production / "deadbeefns"):
        monkeypatch.setenv("HPC_JOURNAL_DIR", str(doomed))
        with pytest.raises(SandboxSeedError, match="production journal home"):
            seed_utterance(doomed, exp, GOAL_UTTERANCE, run_ref=RUN_REF)
        with pytest.raises(SandboxSeedError, match="production journal home"):
            seed_prior_signoff(doomed, exp, run_ref=RUN_REF, scope_id="sandbox-run-b1", block="s1")
    # Read-only proof the guard fired BEFORE the namespace claim: no namespace
    # keyed to the experiment dir appeared under the real production home.
    from hpc_agent.state.run_record import repo_hash

    assert not (production / repo_hash(exp)).exists()
    assert not exp.exists()


def test_guard_refuses_declared_home_inside_production(sandbox_home: Path) -> None:
    # Belt-and-braces leg: even a sandbox-pointing env cannot launder a
    # production target through the journal_home parameter.
    production = Path.home() / ".claude" / "hpc"
    with pytest.raises(SandboxSeedError, match="production journal home"):
        assert_sandbox_journal_home(production)
    with pytest.raises(SandboxSeedError, match="production journal home"):
        assert_sandbox_journal_home(production / "deadbeefns")


def test_guard_refuses_divergent_declaration(sandbox_home: Path, tmp_path: Path) -> None:
    # kills: dropping the declaration/env agreement leg — writes resolve
    # through the env, so a divergent declaration is a loud bug, not a redirect.
    with pytest.raises(SandboxSeedError, match="disagrees"):
        assert_sandbox_journal_home(tmp_path / "elsewhere")


def test_guard_accepts_a_matching_sandbox_pair(sandbox_home: Path) -> None:
    assert assert_sandbox_journal_home(sandbox_home) == sandbox_home.resolve()


# ── (c) a seeded utterance lands where the authorship gate reads it ──────────


def test_seeded_utterance_lands_where_the_gate_reads(sandbox_home: Path, tmp_path: Path) -> None:
    from hpc_agent.ops.decision.journal._shared import _harness_human_texts
    from hpc_agent.state.run_record import repo_hash
    from hpc_agent.state.utterances import read_utterances, utterances_path

    exp = tmp_path / "exp_a"
    record = seed_utterance(sandbox_home, exp, GOAL_UTTERANCE, run_ref=RUN_REF)

    # The returned record: the frozen writer's shape plus the §3 stamp.
    assert record["text"] == GOAL_UTTERANCE
    assert record["sha256"] == hashlib.sha256(GOAL_UTTERANCE.encode("utf-8")).hexdigest()
    assert record["ts"]
    assert record["seeded_by"] == SANDBOX_SEEDED_BY == "sandbox-proving"
    assert record["run"] == RUN_REF

    # The exact locator the gate's reader resolves: the sandbox home, this
    # repo's hash namespace, the unsuffixed utterance log.
    path = utterances_path(exp)
    assert path == sandbox_home / repo_hash(exp) / "utterances.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    on_disk = json.loads(lines[0])
    # The stamp is ON DISK for auditors, and the frozen sorted-keys shape holds.
    assert on_disk["seeded_by"] == "sandbox-proving"
    assert on_disk["run"] == RUN_REF
    assert list(on_disk) == sorted(on_disk)

    # The gate's own read path sees the utterance (store reader, then the
    # shared gate helper built on it).
    assert [r["text"] for r in read_utterances(exp)] == [GOAL_UTTERANCE]
    assert _harness_human_texts(exp) == [GOAL_UTTERANCE]

    # The REAL gate: a value derivable from the seeded utterance commits…
    _gate(exp, "sandbox-run-c1", {"goal": GOAL_VALUE})
    # …and an un-uttered value still REFUSES — the sandbox proves the gate
    # fires; it never proves a human approved anything.
    from hpc_agent import errors

    with pytest.raises(errors.SpecInvalid, match="human-authorship gate"):
        _gate(exp, "sandbox-run-c2", {"goal": UNUTTERED_GOAL})


def test_seeded_utterance_provenance_ignored_by_gate_reader(
    sandbox_home: Path, tmp_path: Path
) -> None:
    # kills: a stamp key leaking into the evidence pool — the gate reads ONLY
    # ``text``/``ts``; the additive keys are invisible to it by construction.
    from hpc_agent.ops.decision.journal._shared import _harness_human_texts

    exp = tmp_path / "exp"
    seed_utterance(sandbox_home, exp, GOAL_UTTERANCE, run_ref=RUN_REF)
    assert _harness_human_texts(exp) == [GOAL_UTTERANCE]


# ── (d) the decoy-namespace pin (U5.5, the 2026-07-18 namespace snag) ────────


def test_decoy_namespace_does_not_unlock(sandbox_home: Path, tmp_path: Path) -> None:
    from hpc_agent import errors
    from hpc_agent.ops.decision.journal._shared import _harness_human_texts
    from hpc_agent.state.run_record import repo_hash

    exp_a = tmp_path / "exp_a"
    exp_b = tmp_path / "exp_b"
    seed_utterance(sandbox_home, exp_a, GOAL_UTTERANCE, run_ref=RUN_REF)
    # B's log exists and is NON-EMPTY, so B's gate read is on the
    # harness-captured tier (the lock) — never the friction-tier fallback. If
    # the gate leaked across namespaces, A's utterance would unlock B here.
    seed_utterance(sandbox_home, exp_b, "what should we order for lunch today", run_ref=RUN_REF)

    # One shared sandbox home, two distinct namespaces — the decoy is planted
    # beside the target, not in some other journal root.
    assert repo_hash(exp_a) != repo_hash(exp_b)
    assert (sandbox_home / repo_hash(exp_a) / "utterances.jsonl").is_file()
    assert (sandbox_home / repo_hash(exp_b) / "utterances.jsonl").is_file()

    assert _harness_human_texts(exp_a) == [GOAL_UTTERANCE]
    assert _harness_human_texts(exp_b) == ["what should we order for lunch today"]

    # Scoped to B the gate must REFUSE A's value — with the authorship marker
    # (E2), i.e. it is the authorship bar firing, not some structural refusal.
    with pytest.raises(errors.SpecInvalid, match="human-authorship gate") as exc:
        _gate(exp_b, "sandbox-run-d1", {"goal": GOAL_VALUE})
    assert getattr(exc.value, "failure_features", None) == {"authorship_evidence": "missing"}

    # …while the SAME commit passes in the namespace that actually holds the
    # utterance — the pin is scoping, not a broken gate.
    _gate(exp_a, "sandbox-run-d2", {"goal": GOAL_VALUE})


# ── seed_prior_signoff: the REAL decision-journal write path ─────────────────


def test_seed_prior_signoff_round_trips_through_the_real_reader(
    sandbox_home: Path, tmp_path: Path
) -> None:
    from hpc_agent.state.decision_journal import read_decisions

    exp = tmp_path / "exp"
    record = seed_prior_signoff(
        sandbox_home,
        exp,
        run_ref=RUN_REF,
        scope_id="sandbox-run-e1",
        block="submit-s1",
        resolved={"goal": GOAL_VALUE},
    )
    # The §3 stamp rides the record's caller-supplied provenance dict…
    assert record["provenance"] == {"seeded_by": "sandbox-proving", "run": RUN_REF}
    assert record["schema_version"] == 1
    assert record["resolved"] == {"goal": GOAL_VALUE}

    # …and the record lands where the REAL reader (and thus every gate's
    # prior-record scan) reads it: the experiment-relative decision journal,
    # NOT the journal home.
    assert (exp / ".hpc" / "runs" / "sandbox-run-e1.decisions.jsonl").is_file()
    assert read_decisions(exp, "run", "sandbox-run-e1") == [record]

    # The authorship gate's prior-record scan reads the seeded record: a field
    # already present in a prior record's ``resolved`` was gated when it was
    # committed, so re-committing it is the already-gated path — no utterance,
    # a bare 'y', and still no refusal.
    _gate(exp, "sandbox-run-e1", {"goal": GOAL_VALUE})


# ── seeder refusals: a seed is always one the real capture hook COULD write ──


def test_seed_utterance_refuses_harness_injected_text(sandbox_home: Path, tmp_path: Path) -> None:
    exp = tmp_path / "exp"
    with pytest.raises(SandboxSeedError, match="harness-injection"):
        seed_utterance(
            sandbox_home, exp, "<system-reminder>planted</system-reminder>", run_ref=RUN_REF
        )
    assert not exp.exists()


def test_seed_utterance_refuses_blank_text_and_blank_run_ref(
    sandbox_home: Path, tmp_path: Path
) -> None:
    exp = tmp_path / "exp"
    with pytest.raises(SandboxSeedError, match="non-empty"):
        seed_utterance(sandbox_home, exp, "   ", run_ref=RUN_REF)
    with pytest.raises(SandboxSeedError, match="run_ref"):
        seed_utterance(sandbox_home, exp, GOAL_UTTERANCE, run_ref="  ")
    # Refusals are loud AND side-effect-free: a seeder that does not write
    # must never leave a claimed namespace behind to make a sandbox vacuous.
    assert not exp.exists()
