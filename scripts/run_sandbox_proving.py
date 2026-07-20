"""U3 — the sandbox block-loop driver (rung-2 proving, plan §4-U3).

WHY THIS EXISTS (``docs/plans/sandbox-proving-run-2026-07-18.md``)
------------------------------------------------------------------
A live proving run today adjudicates TWO different things at once: contract
kinks (spec shapes, block-chain sequencing, gate provenance, journal
namespaces — discoverable with no cluster at all) and cluster-environment
truth (login-shell PATH, MaxStartups throttles — discoverable ONLY live).
Five of the six snags in the 2026-07-18 drill were class 1 and each burned a
human round-trip. This driver eats them autonomously: it drives the FULL
block chain the way a harness does, against the throwaway dockerized Slurm
cluster (``ci/slurm/``), asserting every brief's envelope shape and every
gate's real behavior:

    block-drive bare fresh-start  -> the actionable skip (never a crash)
    submit-s1 walk                -> the recorded-resolution booleans honored
    submit-s1 walk+resolve        -> run_id minted, placeholders overridden
    block-drive fused --approve   -> S1 greenlight COMMITS (brief-shaped
                                     ``resolved`` passes the provenance gate;
                                     the seeded utterance passes the
                                     authorship gate)
    submit-s2 (detached)          -> canary verified, est. core-hours
    fused approve (thin)          -> S2 greenlight commits
    submit-s3 (detached)          -> watching_terminal, lifecycle complete
    fused approve (thin)          -> S3 greenlight commits
    submit-s4 (detached)          -> harvested, results table non-empty

Output is a machine-readable evidence JSON mirroring the run-15 §2.3 table
(``| Step | Where | Mechanical check | Pass |``) plus a human markdown
render. Exit 0 iff every evidence row passed.

TRUST DOCTRINE (plan §3 — the part that must never bend)
--------------------------------------------------------
Gates are never bypassed here — they fire for real against a seeded,
namespace-isolated substrate:

* ``HPC_JOURNAL_DIR`` is REQUIRED-EPHEMERAL at driver start: the driver
  REFUSES to run when it is unset or resolves inside ``~/.claude/hpc``.
* The human-authorship utterance is SEEDED into that ephemeral namespace by
  the sibling ``tests/integration/scheduler/sandbox_seed.py`` helper, which
  carries the same guard and stamps ``seeded_by: sandbox-proving``
  provenance. A sandbox run proves the gates FIRE correctly (including
  refusals); it never proves a human approved anything.
* Rung-2 jurisdiction (plan §1): this run adjudicates the harness contract.
  It can NEVER certify a default flip, a "validated live" claim, or any
  cluster-environment truth — rung 3 keeps that monopoly.

SIBLING CONTRACTS (U1/U2 landed; consumed BY PATH, never as packages)
---------------------------------------------------------------------
* ``tests/integration/scheduler/sandbox_fixture.py`` ::
  ``build_sandbox_experiment(experiment_dir, *, run_name=..., seeds=...,
  n_samples=..., executor_variant=..., cluster=..., goal=...)`` — materializes
  the REAL interview outputs (``interview.json`` / ``.hpc/axes.yaml`` /
  ``.hpc/tasks.py``) for a pi-shape ``@register_run`` executor and returns a
  ``SandboxExperiment`` carrying ``experiment_dir`` / ``run_name`` /
  ``run_id`` / ``cmd_sha`` / ``total_tasks`` / ``seeds`` / ``n_samples`` /
  ``goal``. The driver passes its own ``--run-name`` / cluster / goal so the
  fixture's pre-computed identity is exactly the one the resolve leg mints
  (asserted as evidence).
* ``tests/integration/scheduler/sandbox_seed.py`` ::
  ``seed_utterance(journal_home, experiment_dir, text, *, run_ref=...)`` —
  writes the utterance log into the sandbox namespace only, with the §3
  guards + provenance stamp.

Both are loaded BY PATH with defensive getattr: a sibling that lands a
different name produces a clear driver error, not an AttributeError.

HOW THE CHAIN IS DRIVEN (the run-15 harness pattern, journal-proven)
--------------------------------------------------------------------
Greenlights are committed through the fused ``block-drive --approve`` (the
ONE append-decision definition — every gate fires identically), with the
brief-shaped ``resolved`` for S1 and thin ``{"next_block": ...}`` resolveds
for S2/S3. The blocks themselves are invoked DIRECTLY (``submit-s2`` /
``submit-s3`` / ``submit-s4`` with ``detach: true``) — the greenlight gate
reads the committed ``y`` from the journal; detached workers park their own
terminals (``<run_id>.<verb>.terminal.json``) and materialize successor specs
(``.hpc/specs/next/<run_id>.<verb>.json``), which this driver prefers over
its own inline composition.

One tick behavior the driver must OBSERVE, never fight: the fused S2
greenlight's own advance leg CONSUMES the parked, sha-verified S3 spec (R3)
and launches submit-s3 itself — the single-lease then refuses a second
launch. So before invoking any detached block the driver probes
``poll-detached``: ``no_lease`` → invoke directly (and assert the detached
handle envelope); anything else → record the auto-advance as evidence and go
straight to the wait. The S1/S3 greenlight ticks are the harmless junk-span
case (no parked marker → the thin/brief-shaped resolved is not the
successor's spec shape → CLI validation refuses before any SSH/journal
write); the driver asserts the COMMIT via the decision journal either way
and records the tick outcome as evidence detail, never as a step failure.

U4/U6 CONSUMPTION SURFACE
-------------------------
The kill drill (U4) and anomaly arms (U6) import this module's public
helpers: the §3 guard (:func:`require_ephemeral_journal_home`), the fixture /
seed bridges (:func:`build_fixture_experiment` /
:func:`seed_authorship_utterance`), the CLI runner (:func:`run_cli`), the
detached-worker probes (:func:`poll_detached_state`,
:func:`wait_for_detached_terminal`, :func:`detached_lease_path`,
:func:`read_detached_lease`), the spec composers (:func:`compose_s2_spec` /
:func:`compose_s3_spec` / :func:`compose_s4_spec`), the journal readers
(:func:`decision_journal_path` / :func:`find_greenlight` /
:func:`terminal_record_path` / :func:`materialized_successor_path`), and the
evidence builders (:func:`build_evidence` / :func:`render_markdown` /
:class:`ChainContext` / :class:`ChainState` / :func:`run_chain`). The driver
is import-safe (no side effects at import).

USAGE
-----
::

    # CI lane / any dockerless host that already has ci_clusters.yaml:
    HPC_JOURNAL_DIR=$(mktemp -d)/journal \\
        python scripts/run_sandbox_proving.py --clusters-config ci_clusters.yaml

    # Docker-capable dev machine: stand the container up, run, tear down:
    HPC_JOURNAL_DIR=$(mktemp -d)/journal \\
        python scripts/run_sandbox_proving.py --local

    # Native Windows without docker: the sanctioned binding (plan §7/U7) is
    #     gh workflow run scheduler-integration.yml
    # --local errors with that guidance when docker is absent.

Note: dev tooling — lives in ``scripts/``, never shipped in the wheel.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
# The shared journal-home guard (single canonical definition — the fixture +
# seed siblings delegate to it too; a driver-LOCAL copy here would re-open the
# Windows alias-spelling bypass the guard's red-team corpus pins closed).
_SANDBOX_GUARD_PATH = REPO_ROOT / "tests" / "integration" / "scheduler" / "sandbox_guard.py"

# ── Sibling contract locations (loaded BY PATH — never imported as packages,
# so a partially-landed sibling produces a clear driver error) ──────────────
FIXTURE_MODULE_PATH = REPO_ROOT / "tests" / "integration" / "scheduler" / "sandbox_fixture.py"
SEED_MODULE_PATH = REPO_ROOT / "tests" / "integration" / "scheduler" / "sandbox_seed.py"
FIXTURE_ENTRYPOINT = "build_sandbox_experiment"
SEED_UTTERANCE_ENTRYPOINT = "seed_utterance"

# Placeholders for the resolve spec: schema-VALID shapes that compute-run-id
# overrides (run_id slug + 8-hex cmd_sha). The all-caps "PLACEHOLDER" literal
# is the run-15 refusal shape (U5.3 pin) — never use it.
PLACEHOLDER_RUN_ID = "placeholder-run"
PLACEHOLDER_CMD_SHA = "00000000"

DEFAULT_GOAL = "sandbox-prove the submit block loop end to end on the container cluster"
DEFAULT_RUN_NAME = "sandbox-pi"
DEFAULT_WALLTIME_SEC = 900
RESULT_DIR_TEMPLATE = "results/{run_id}/task_{task_id}"

# ── The n_samples → task-walltime mapping (the fixture generator's one knob) ──
# The U1 fixture's pi executor costs ~1.6µs/sample on the slurmci container
# (measured 2026-07-19 from scheduler-integration run 29709733724 forensics:
# ~120k samples ≈ 160ms per task). sacct is DISABLED there, so a completed
# array vanishes from squeue instantly and the array's squeue-visibility
# window IS its task walltime: ~120k-sample tasks gave a 0.9–1.4s window that
# parked inside the kill drill's old fixed 2s poll gap — the deterministic
# 3/3 "never entered the scheduler" misread. 4M samples puts one fixture task
# at ~6.4s on the container (the 5–10s band): far above the drill's new
# sub-second jittered poll, far below the 900s walltime ask.
FIXTURE_SAMPLES_PER_SEC = 625_000  # ≈ 1 / 1.6µs on the slurmci container (measured)
DEFAULT_FIXTURE_N_SAMPLES = 4_000_000  # → ~5–10s per fixture task on the container

# The array-script path deploy_runtime ships, per backend family (mirrors
# tests/integration/scheduler/test_scheduler_smoke.py's _FAMILIES).
_SCRIPT_BY_BACKEND = {
    "slurm": ".hpc/templates/cpu_array.slurm",
    "sge": ".hpc/templates/cpu_array.sh",
    "pbs": ".hpc/templates/cpu_array.sh",
}

# The ci_clusters.yaml --local writes for the container it stands up. The
# container deliberately has NO module/conda system: the wheel is
# pip-installed into the system python3 and the SSH login shell's python IS
# the intended interpreter (the same contract the smoke lane pins). That is
# DECLARED via login_shell_activation so the #281 Activation guard
# (infra/clusters.py) admits the all-empty activation as an explicit stanza
# fact — resolve_activation strips whitespace, so the old `modules: [" "]`
# cosmetic still read as empty and was refused (the 2026-07-19 CI failure).
_LOCAL_CLUSTERS_YAML = """\
slurmci:
  host: slurmci
  user: hpcuser
  scheduler: slurm
  scratch: /home/hpcuser/scratch
  # No module/conda system in the container — the login shell's python3
  # carries hpc_agent. Declared, not hacked: the Activation guard reads this
  # stanza key and admits the all-empty activation ONLY because it is stated.
  modules: []
  login_shell_activation: true
  max_walltime_sec: 3600
  constraints:
    max_array_size: 100
    max_walltime: "1:00:00"
    max_concurrent_jobs: 4
    est_spin_up: "10s"
"""

_CLI_TIMEOUT_SEC = 600
_ENV_POLL_INTERVAL = "5"  # HPC_STATUS_POLL_INTERVAL_SEC (CI lane parity)

# SubmitBlockResult / BlockDriveResult keys every block envelope must carry
# (the U3 contract assertion — a missing key is a contract kink, surfaced as
# evidence, never patched around).
_BLOCK_RESULT_KEYS = ("block", "stage_reached", "needs_decision", "reason", "run_id", "brief")
_BLOCK_DRIVE_KEYS = ("action", "run_id", "workflow", "current_verb", "next_verb", "reason")

# Keys exempt from the driver-side provenance self-check (mirror of the
# gate's meta-key exemption — ``block_drive._META_KEYS``; the gate's own copy
# is authoritative, this is evidence, not enforcement).
_PROVENANCE_META_KEYS = {"next_block"}

_RUN_ID_RE = r"^{name}-[0-9a-f]{{8}}$"

# poll-detached states (PollDetachedResult.state Literals) meaning "a worker
# for this (run_id, block) already exists" — the fused tick's R3 advance
# launched it, so the driver observes instead of re-invoking (single-lease).
_WORKER_PRESENT_STATES = frozenset({"running", "exited_recorded", "exited_unrecorded"})


class SandboxRefusal(RuntimeError):
    """A clean, one-line driver refusal (guard fired, sibling missing, CLI red).

    The driver never tracebacks for an expected refusal class — the evidence
    JSON still records the failing step when a chain is in flight, and the
    CLI prints the reason verbatim.
    """


# ────────────────────────────────────────────────────────────────────────────
# Pure helpers (hermetically testable — no cluster, no docker, no subprocess)
# ────────────────────────────────────────────────────────────────────────────


def require_ephemeral_journal_home(env: Mapping[str, str]) -> Path:
    """The §3 guard: HPC_JOURNAL_DIR must be set AND outside ``~/.claude/hpc``.

    Delegates to the SHARED sandbox guard (``_SANDBOX_GUARD_PATH`` — the same
    single guard object ``sandbox_fixture.py`` / ``sandbox_seed.py`` use), so
    no ALIAS spelling of the production home (``\\\\?\\`` extended prefix,
    ``\\\\localhost\\C$``, ``\\\\127.0.0.1\\C$``, 8.3 short names, junctions,
    case variants — the guard's red-team corpus) can slip past a plain
    ``resolve()`` comparison. Seeding + gates read this namespace; a driver
    that silently used the production journal home would let a sandbox
    greenlight masquerade as a human decision. Refuse loudly instead.
    """
    raw = env.get("HPC_JOURNAL_DIR", "").strip()
    if not raw:
        raise SandboxRefusal(
            "HPC_JOURNAL_DIR is unset — the sandbox journal home must be an "
            "EPHEMERAL directory (plan §3). Export a tmpdir, e.g. "
            "HPC_JOURNAL_DIR=$(mktemp -d)/hpc-journal."
        )
    guard = load_sibling_module(_SANDBOX_GUARD_PATH, label="sandbox_guard")
    if guard.is_within_production_home(raw):
        raise SandboxRefusal(
            f"HPC_JOURNAL_DIR={raw} canonicalizes into the production journal "
            f"home ({guard.canonical_journal_path(raw)}) — refused (plan §3: "
            "structurally incapable of touching a production namespace)."
        )
    return Path(guard.canonical_journal_path(raw))


def write_spec(scratch_dir: Path, name: str, spec: Mapping[str, Any]) -> Path:
    """Write one CLI spec JSON to *scratch_dir*, returning its path.

    The CLI takes file paths ONLY (``--spec`` refuses inline JSON), so every
    verb invocation goes through here. ``name`` is the file stem (must be
    filesystem-safe: it becomes a path segment).
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise SandboxRefusal(f"spec name {name!r} is not filesystem-safe")
    scratch_dir.mkdir(parents=True, exist_ok=True)
    path = scratch_dir / f"{name}.json"
    payload = json.dumps(spec, indent=2, sort_keys=True, default=str) + "\n"
    path.write_text(payload, encoding="utf-8")
    return path


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Parse a JSONL file into records, skipping blank lines (fail-soft on the
    last torn line: a record that doesn't parse is skipped, never fatal — the
    evidence assertion over the surviving records is the honest signal)."""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


def _dict_or_empty(value: Any) -> dict[str, Any]:
    """``value`` when it is a dict, else ``{}`` (envelope/brief navigation)."""
    return value if isinstance(value, dict) else {}


def assert_block_envelope(data: Any, *, verb: str) -> list[str]:
    """Problems with a SubmitBlockResult-shaped envelope ``data`` (empty == pass).

    The U3 contract pin: every block returns ``block`` / ``stage_reached`` /
    ``needs_decision`` / ``reason`` / ``run_id`` / ``brief`` (and
    ``next_block`` may be null/absent on terminal stages, so it is only
    type-checked when present).
    """
    problems: list[str] = []
    if not isinstance(data, dict):
        return [f"{verb}: envelope data is {type(data).__name__}, not an object"]
    for key in _BLOCK_RESULT_KEYS:
        if key not in data:
            problems.append(f"{verb}: envelope missing key {key!r}")
    if problems:
        return problems
    if not isinstance(data.get("stage_reached"), str) or not data["stage_reached"]:
        problems.append(f"{verb}: stage_reached must be a non-empty string")
    if not isinstance(data.get("needs_decision"), bool):
        problems.append(f"{verb}: needs_decision must be a boolean")
    if not isinstance(data.get("brief"), dict):
        problems.append(f"{verb}: brief must be an object")
    if (
        "next_block" in data
        and data["next_block"] is not None
        and not isinstance(data["next_block"], dict)
    ):
        problems.append(f"{verb}: next_block must be an object or null")
    return problems


def assert_block_drive_envelope(data: Any) -> list[str]:
    """Problems with a BlockDriveResult-shaped envelope ``data`` (empty == pass)."""
    problems: list[str] = []
    if not isinstance(data, dict):
        return [f"block-drive: envelope data is {type(data).__name__}, not an object"]
    for key in _BLOCK_DRIVE_KEYS:
        if key not in data:
            problems.append(f"block-drive: envelope missing key {key!r}")
    if problems:
        return problems
    if not isinstance(data.get("action"), str) or not data["action"]:
        problems.append("block-drive: action must be a non-empty string")
    return problems


def assert_run_id_minted(run_id: Any, run_name: str) -> list[str]:
    """The minted run_id must be ``<run_name>-<8 lowercase hex>`` (compute-run-id)."""
    if not isinstance(run_id, str) or not run_id:
        return [f"run_id {run_id!r} is not a non-empty string"]
    pattern = _RUN_ID_RE.format(name=re.escape(run_name))
    if not re.match(pattern, run_id):
        return [f"run_id {run_id!r} does not match {pattern} (compute-run-id shape)"]
    return []


def build_s1_greenlight_resolved(s1_brief: Mapping[str, Any]) -> dict[str, Any]:
    """The brief-shaped S1 greenlight ``resolved`` (the run-15 rec-2 shape).

    Every field is copied VERBATIM from the S1 brief's own ``resolved`` block —
    by construction each key is one the persisted brief recommended, which is
    exactly what the provenance gate diffs. ``next_block`` (the routing token)
    is meta-exempt and added here.
    """
    resolved = s1_brief.get("resolved")
    if not isinstance(resolved, dict) or not resolved:
        raise SandboxRefusal(
            "S1 brief carries no 'resolved' block — cannot build the brief-shaped "
            "greenlight (did the walk+resolve invocation reach stage 'resolved'?)."
        )
    return {**resolved, "next_block": "submit-s2"}


def collect_brief_names(value: Any, names: set[str] | None = None) -> set[str]:
    """Collect every dict key + whole string scalar in *value* (recursive).

    Mirrors the provenance gate's name pool (``brief_provenance.
    _collect_brief_names``: dict keys or whole string scalars in the persisted
    brief). Driver-side evidence helper ONLY — the gate's own copy is the
    enforcement; this powers the pre-commit self-check row so the evidence can
    say "the resolved we are about to commit is brief-shaped".
    """
    if names is None:
        names = set()
    if isinstance(value, dict):
        for key, item in value.items():
            names.add(str(key))
            collect_brief_names(item, names)
    elif isinstance(value, (list, tuple)):
        for item in value:
            collect_brief_names(item, names)
    elif isinstance(value, str):
        names.add(value)
    return names


def provenance_shape_problems(resolved: Mapping[str, Any], brief: Mapping[str, Any]) -> list[str]:
    """Self-check mirroring the provenance gate: every non-meta key of
    *resolved* must appear in *brief*'s name pool. Empty == brief-shaped."""
    names = collect_brief_names(brief)
    return sorted(
        key for key in resolved if key not in _PROVENANCE_META_KEYS and str(key) not in names
    )


def compose_s2_spec(s1_brief: Mapping[str, Any], *, detach: bool = True) -> dict[str, Any]:
    """S1→S2 spec, mirroring ``block_chain._compose_submit_s2_spec``: reuse the
    submit-flow spec the resolve leg BUILT (``brief["resolve"]["submit_spec"]``)
    — never re-author it (run-14 #4)."""
    resolve = s1_brief.get("resolve")
    submit_flow = resolve.get("submit_spec") if isinstance(resolve, dict) else None
    if not isinstance(submit_flow, dict) or not submit_flow:
        raise SandboxRefusal(
            "S1 brief carries no resolve.submit_spec — cannot compose the S2 spec."
        )
    return {"submit": {"submit": dict(submit_flow)}, "detach": detach}


def compose_s3_spec(
    s2_spec: Mapping[str, Any],
    run_id: str,
    s2_brief: Mapping[str, Any] | None = None,
    *,
    detach: bool = True,
) -> dict[str, Any]:
    """S2→S3 spec, mirroring ``block_chain._compose_submit_s3_spec``: S2's own
    ``submit`` sub-spec verbatim + the monitor identity shape + the canary ids
    off the S2 brief when present."""
    submit = s2_spec.get("submit")
    if not isinstance(submit, dict) or not submit:
        raise SandboxRefusal("S2 spec carries no 'submit' sub-spec — cannot compose S3.")
    composed: dict[str, Any] = {
        "submit": dict(submit),
        "monitor": {"run_id": run_id},
        "detach": detach,
    }
    brief = s2_brief or {}
    for key in ("canary_run_id", "canary_job_ids"):
        value = brief.get(key)
        if value is not None:
            composed[key] = value
    return composed


def compose_s4_spec(run_id: str, *, detach: bool = True) -> dict[str, Any]:
    """S3→S4 spec, mirroring ``block_chain._compose_submit_s4_spec``: the
    aggregate identity shape — run_id is all the harvest needs."""
    return {"aggregate": {"run_id": run_id}, "detach": detach}


def materialized_successor_path(experiment_dir: Path, run_id: str, successor: str) -> Path:
    """The well-known run-14 #4 materialization path:
    ``<experiment>/.hpc/specs/next/<run_id>.<successor>.json``."""
    return experiment_dir / ".hpc" / "specs" / "next" / f"{run_id}.{successor}.json"


def decision_journal_path(experiment_dir: Path, run_id: str) -> Path:
    """The run-scope decision journal: ``<experiment>/.hpc/runs/<run_id>.decisions.jsonl``."""
    return experiment_dir / ".hpc" / "runs" / f"{run_id}.decisions.jsonl"


def terminal_record_path(experiment_dir: Path, run_id: str, verb: str) -> Path:
    """A detached block's terminal record: ``<experiment>/.hpc/runs/<run_id>.<verb>.terminal.json``.

    ``state.block_terminal.terminal_block_key`` canonicalizes the submit blocks
    to their VERB key ("submit-s2"...), which is what the detached worker
    records — pass the verb, never the short literal.
    """
    return experiment_dir / ".hpc" / "runs" / f"{run_id}.{verb}.terminal.json"


def detached_lease_path(journal_home: Path, run_id: str, verb: str) -> Path:
    """The detached worker's lease: ``<journal_home>/_detached/<verb>-<run_id>.lease.json``.

    U4 reads the lease pid from here to kill the S3 dispatch process inside
    the submit-once kill window.
    """
    return journal_home / "_detached" / f"{verb}-{run_id}.lease.json"


def read_detached_lease(journal_home: Path, run_id: str, verb: str) -> dict[str, Any] | None:
    """Read the lease payload ({pid, create_time, ...}) or None when absent/corrupt."""
    path = detached_lease_path(journal_home, run_id, verb)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return payload if isinstance(payload, dict) else None


def find_greenlight(
    records: Sequence[Mapping[str, Any]], *, block: str, next_block: str | None = None
) -> Mapping[str, Any] | None:
    """The newest committed greenlight (``response == "y"``) for *block* whose
    ``resolved.next_block`` matches *next_block* (when given). This is the same
    shape ``assert_greenlit_target`` scans for — the driver's commit assertion
    reads the journal the gate itself reads."""
    for record in reversed(list(records)):
        if record.get("response") != "y" or record.get("block") != block:
            continue
        resolved = record.get("resolved")
        if next_block is not None and (
            not isinstance(resolved, dict) or resolved.get("next_block") != next_block
        ):
            continue
        return record
    return None


def _iter_numbers(value: Any) -> list[float | int]:
    """Every numeric leaf in *value* (authorship-utterance coverage)."""
    found: list[float | int] = []
    if isinstance(value, bool):
        return found
    if isinstance(value, (int, float)):
        found.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_iter_numbers(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_iter_numbers(item))
    return found


def _iter_string_values(value: Any) -> list[str]:
    """Every string VALUE leaf (keys are schema vocabulary, excluded — mirror
    of the authorship gate's collector)."""
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_iter_string_values(item))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_iter_string_values(item))
    return found


def build_utterance_text(goal: str, task_generator: Mapping[str, Any]) -> str:
    """The seeded human-utterance text that unlocks the authorship gate.

    The gate (``ops/decision/journal/human_authorship.py``) requires every
    number in ``task_generator`` and the goal's words to derive from a logged
    human utterance. Stating the goal VERBATIM plus every number VERBATIM is
    the strongest derivable form — no magnitude/range/contiguous-run rule is
    leaned on, so the text is robust to gate-evolution. String values (a
    categorical param, if the sweep ever grows one) are stated verbatim too.
    """
    numbers = _iter_numbers(task_generator)
    strings = [s for s in _iter_string_values(task_generator) if s]
    parts = [
        "Sandbox proving run — human greenlight statement (seeded; plan §3).",
        f"Goal: {goal}",
    ]
    if numbers:
        rendered = ", ".join(repr(n) for n in numbers)
        parts.append(f"Sweep values I chose: {rendered}.")
    if strings:
        parts.append(f"Named choices: {', '.join(strings)}.")
    if numbers:
        ints = sorted({n for n in numbers if isinstance(n, int) and n >= 0})
        if ints:
            parts.append(f"Counts: {len(ints)} seeds; values {ints[0]} through {ints[-1]}.")
    return " ".join(parts)


def build_evidence_row(
    step: str, where: str, check: str, passed: bool, detail: str = ""
) -> dict[str, Any]:
    """One run-15 §2.3 evidence row (| Step | Where | Mechanical check | Pass |)."""
    return {
        "step": step,
        "where": where,
        "check": check,
        "pass": bool(passed),
        "detail": detail,
    }


def build_evidence(meta: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """The full evidence document: meta block + rows + the computed verdict."""
    failures = [r for r in rows if not r.get("pass")]
    return {
        "schema_version": 1,
        "kind": "sandbox-proving-evidence",
        "meta": dict(meta),
        "rows": list(rows),
        "verdict": "pass" if not failures else "fail",
        "failed_steps": [r.get("step") for r in failures],
    }


def render_markdown(evidence: Mapping[str, Any]) -> str:
    """The human render: run-15 §2.3 table shape + meta + the rung-2 disclaimer."""
    meta = evidence.get("meta", {})
    rows = evidence.get("rows", [])
    lines = [
        "# Sandbox proving run evidence (U3 — rung 2)",
        "",
        f"- run_ref: `{meta.get('run_ref', '?')}`",
        f"- run_id: `{meta.get('run_id', '?')}`",
        f"- cluster: `{meta.get('cluster', '?')}` (container; submit-once="
        f"`{meta.get('submit_once', '?')}`)",
        f"- journal_home (ephemeral): `{meta.get('journal_home', '?')}`",
        f"- started: {meta.get('started_utc', '?')}  duration: {meta.get('duration_sec', '?')}s",
        "",
        "| Step | Where | Mechanical check | Pass |",
        "|---|---|---|---|",
    ]
    for row in rows:
        mark = "yes" if row.get("pass") else "**NO**"
        detail = row.get("detail") or ""
        check = row.get("check", "")
        if detail:
            check = f"{check} — {detail}"
        lines.append(f"| {row.get('step', '?')} | {row.get('where', '?')} | {check} | {mark} |")
    lines += [
        "",
        f"**Verdict: {evidence.get('verdict', '?')}**",
        "",
        "> Rung-2 jurisdiction (plan §1): this evidence adjudicates the harness",
        "> contract only. It can never certify a default flip, a live-validation",
        "> claim, or any cluster-environment truth — rung 3 keeps that monopoly.",
        "",
    ]
    return "\n".join(lines)


# ── Sibling loading (defensive — the contract, never their files) ───────────


def load_sibling_module(path: Path, *, label: str) -> Any:
    """Import a sibling module BY PATH. A missing/partial sibling is a clear
    driver refusal naming the contract, never an ImportError traceback."""
    if not path.is_file():
        raise SandboxRefusal(
            f"sibling {label} not found at {path} — it is built concurrently "
            "(contract: see this driver's module docstring). Integration cannot "
            "run until it lands; hermetic tests do not need it."
        )
    spec = importlib.util.spec_from_file_location(f"_sandbox_sibling_{label}", path)
    if spec is None or spec.loader is None:
        raise SandboxRefusal(f"cannot import sibling {label} from {path}")
    module = importlib.util.module_from_spec(spec)
    # Importlib contract: register in sys.modules BEFORE exec_module. A
    # dataclass-bearing sibling (@dataclass introspects its module via
    # sys.modules[module.__name__]) AttributeErrors mid-decoration when exec
    # runs unregistered — the SandboxRefusal wrapper below masked exactly
    # this bug on sandbox_fixture.py.
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 — surfaced verbatim, as a refusal
        raise SandboxRefusal(f"sibling {label} at {path} failed to import: {exc}") from exc
    return module


def sibling_entrypoint(module: Any, name: str, *, label: str) -> Any:
    """getattr with a contract-naming error (the sibling may land a different
    name — the message says EXACTLY what was expected so integration reconciles)."""
    entry = getattr(module, name, None)
    if not callable(entry):
        raise SandboxRefusal(
            f"sibling {label} does not export a callable {name!r} — the U3 "
            f"contract expects it (see module docstring). Found: "
            f"{sorted(n for n in dir(module) if not n.startswith('_'))}"
        )
    return entry


# ── Cluster config ───────────────────────────────────────────────────────────


def load_cluster_config(path: Path) -> dict[str, Any]:
    """Parse a ci_clusters.yaml ({cluster_name: stanza})."""
    if not path.is_file():
        raise SandboxRefusal(f"clusters config not found: {path}")
    try:
        import yaml  # pyyaml — a project dependency (clusters.yaml parsing)
    except ImportError as exc:  # pragma: no cover — dev-env guard
        raise SandboxRefusal("pyyaml is required to read the clusters config") from exc
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise SandboxRefusal(f"clusters config {path} did not parse: {exc}") from exc
    if not isinstance(loaded, dict) or not loaded:
        raise SandboxRefusal(f"clusters config {path} is empty or not a mapping")
    return loaded


def select_cluster(config: Mapping[str, Any], name: str | None) -> tuple[str, dict[str, Any]]:
    """Pick the cluster stanza: *name* when given, else the single configured one."""
    if name is not None:
        stanza = config.get(name)
        if not isinstance(stanza, dict):
            raise SandboxRefusal(f"cluster {name!r} not in the config (have: {sorted(config)})")
        return name, stanza
    if len(config) != 1:
        raise SandboxRefusal(
            f"the config names {len(config)} clusters {sorted(config)} — pass --cluster."
        )
    only = next(iter(config))
    stanza = config[only]
    if not isinstance(stanza, dict):
        raise SandboxRefusal(f"cluster stanza {only!r} is not a mapping")
    return only, stanza


def stanza_ssh_target(stanza: Mapping[str, Any]) -> str:
    user, host = stanza.get("user"), stanza.get("host")
    if not user or not host:
        raise SandboxRefusal("cluster stanza needs 'user' and 'host'")
    return f"{user}@{host}"


def stanza_backend(stanza: Mapping[str, Any]) -> str:
    scheduler = stanza.get("scheduler")
    if not scheduler:
        raise SandboxRefusal("cluster stanza needs 'scheduler' (slurm/sge/pbs)")
    return str(scheduler)


def stanza_remote_path(stanza: Mapping[str, Any], experiment_dir: Path) -> str:
    scratch = stanza.get("scratch")
    if not scratch:
        raise SandboxRefusal("cluster stanza needs 'scratch' (the remote base path)")
    return f"{str(scratch).rstrip('/')}/{experiment_dir.name}"


# ── Spec builders (walk / resolve — the S1 inputs) ───────────────────────────


def compute_recorded_resolutions(experiment_dir: Path, executor_run_name: str) -> dict[str, bool]:
    """Reflect the on-disk resolution state into the walk's ``*_resolved``
    booleans — the same reflection the SKILL performs for a live harness.
    The S1-walk step then asserts the brief HONORS them (U3 / U5.2).

    *executor_run_name* is the ``@register_run`` function name (the key
    ``classify-axis`` records under ``executors.`` in axes.yaml) — NOT the
    run's minting name.
    """
    interview = experiment_dir / "interview.json"
    tasks_py = experiment_dir / ".hpc" / "tasks.py"
    axes_candidates = [experiment_dir / "axes.yaml", experiment_dir / ".hpc" / "axes.yaml"]
    data_axis = False
    homogeneous = False
    for axes_path in axes_candidates:
        if not axes_path.is_file():
            continue
        try:
            import yaml

            axes = yaml.safe_load(axes_path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 — an unreadable axes.yaml reads as unresolved
            continue
        if not isinstance(axes, dict):
            continue
        executors = axes.get("executors")
        if isinstance(executors, dict) and executor_run_name in executors:
            data_axis = True
        if axes.get("homogeneous_axes") is not None:
            homogeneous = True
    return {
        "entry_point_resolved": interview.is_file(),
        "data_axis_resolved": data_axis,
        "homogeneous_axes_resolved": homogeneous,
        "tasks_py_present": tasks_py.is_file(),
    }


def build_walk_spec(
    *,
    cluster: str,
    configured_clusters: Sequence[str],
    goal: str,
    task_generator: Mapping[str, Any],
    profile: str,
    executor_run_name: str,
    walltime_sec: int,
    experiment_dir: Path,
    recorded: Mapping[str, bool],
) -> dict[str, Any]:
    """The WalkSubmitAmbiguitiesInput with every caller-owned field supplied
    (nothing left to ambiguity) and the recorded-resolution booleans reflected
    from disk."""
    return {
        "cluster": cluster,
        "configured_clusters": sorted(configured_clusters),
        "goal": goal,
        "task_generator": dict(task_generator),
        "profile": profile,
        "executor_run_name": executor_run_name,
        "walltime_sec": walltime_sec,
        "experiment_dir": str(experiment_dir),
        "uncovered_required_params": [],
        "uncovered_param_defaults": {},
        "entry_point_resolved": bool(recorded.get("entry_point_resolved")),
        "data_axis_resolved": bool(recorded.get("data_axis_resolved")),
        "homogeneous_axes_resolved": bool(recorded.get("homogeneous_axes_resolved")),
        "tasks_py_present": bool(recorded.get("tasks_py_present")),
    }


def build_resolve_spec(
    *,
    run_name: str,
    profile: str,
    cluster: str,
    ssh_target: str,
    remote_path: str,
    backend: str,
    total_tasks: int,
    executor_cmd: str,
    walltime_sec: int,
) -> dict[str, Any]:
    """The ResolveSubmitInputsSpec (run_name + submit + sidecar).

    run_id / cmd_sha are schema-valid PLACEHOLDERS — compute-run-id overrides
    them (the U5.3 placeholder pin: valid shapes pass, and the minted run_id
    replaces them).
    """
    script = _SCRIPT_BY_BACKEND.get(backend, ".hpc/templates/cpu_array.sh")
    return {
        "run_name": run_name,
        "submit": {
            "profile": profile,
            "cluster": cluster,
            "ssh_target": ssh_target,
            "remote_path": remote_path,
            "run_id": PLACEHOLDER_RUN_ID,
            "cmd_sha": PLACEHOLDER_CMD_SHA,
            "total_tasks": total_tasks,
            "backend": backend,
            "job_name": run_name,
            "script": script,
            "canary": True,
            "walltime_sec": walltime_sec,
            "is_gpu": False,
            "result_dir_template": RESULT_DIR_TEMPLATE,
        },
        "sidecar": {
            "run_id": PLACEHOLDER_RUN_ID,
            "cmd_sha": PLACEHOLDER_CMD_SHA,
            "executor": executor_cmd,
            "result_dir_template": RESULT_DIR_TEMPLATE,
            "task_count": total_tasks,
            "cluster": cluster,
            "profile": profile,
            "remote_path": remote_path,
            "resources": {"walltime_sec": walltime_sec, "cpus": 1},
        },
    }


# ── CLI subprocess runner ────────────────────────────────────────────────────


@dataclass
class CliOutcome:
    """One ``hpc-agent`` CLI invocation's parsed result."""

    verb: str
    rc: int
    envelope: dict[str, Any] | None
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0 and isinstance(self.envelope, dict) and bool(self.envelope.get("ok"))

    @property
    def data(self) -> dict[str, Any]:
        envelope = self.envelope
        if isinstance(envelope, dict):
            data = envelope.get("data")
            if isinstance(data, dict):
                return data
        return {}

    def describe_failure(self) -> str:
        code = self.envelope.get("error_code") if isinstance(self.envelope, dict) else None
        message = self.envelope.get("message") if isinstance(self.envelope, dict) else None
        parts = [f"rc={self.rc}"]
        if code:
            parts.append(f"error_code={code}")
        if message:
            parts.append(str(message)[:300])
        if not self.envelope:
            tail = (self.stdout.strip().splitlines() or [""])[-1][:200]
            err_tail = (self.stderr.strip().splitlines() or [""])[-1][:200]
            parts.append(f"stdout_tail={tail!r} stderr_tail={err_tail!r}")
        return " ".join(parts)


def parse_envelope(stdout: str) -> dict[str, Any] | None:
    """The CLI prints ONE single-line JSON envelope on stdout; tolerate leading
    noise lines (log bleed) by scanning from the LAST line backwards."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "ok" in obj:
            return obj
    return None


def run_cli(
    verb: str,
    spec_path: Path,
    *,
    experiment_dir: Path | None,
    env: Mapping[str, str],
    timeout_sec: int = _CLI_TIMEOUT_SEC,
) -> CliOutcome:
    """Invoke ``python -m hpc_agent <verb> --spec <file> [--experiment-dir <dir>]``.

    ``wait-detached`` takes NO --experiment-dir (it locates leases under the
    journal home — its CliShape has no ``experiment_dir_arg``), so
    *experiment_dir* is None there. Raises SandboxRefusal on a red invocation —
    the chain caller decides whether to record-and-abort.
    """
    argv = [sys.executable, "-m", "hpc_agent", verb, "--spec", str(spec_path)]
    if experiment_dir is not None:
        argv += ["--experiment-dir", str(experiment_dir)]
    try:
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_sec,
            env=dict(env),
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxRefusal(f"{verb}: CLI invocation timed out after {timeout_sec}s") from exc
    outcome = CliOutcome(
        verb=verb,
        rc=proc.returncode,
        envelope=parse_envelope(proc.stdout or ""),
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )
    if not outcome.ok:
        raise SandboxRefusal(f"{verb}: CLI invocation failed ({outcome.describe_failure()})")
    return outcome


# ── Evidence-row accumulator (record-and-abort chain semantics) ─────────────


@dataclass
class ChainState:
    """Mutable chain state: evidence rows + the first failure (the chain ABORTS
    on a failed row — later steps depend on earlier ones — but every row up to
    the failure lands in the evidence document)."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    failed_step: str | None = None

    def record(self, step: str, where: str, check: str, passed: bool, detail: str = "") -> None:
        self.rows.append(build_evidence_row(step, where, check, passed, detail))
        if not passed and self.failed_step is None:
            self.failed_step = step

    @property
    def aborted(self) -> bool:
        return self.failed_step is not None


# ── Fixture / seed invocation (sibling contracts) ────────────────────────────


def fixture_kwargs_from_sweep(sweep: Mapping[str, Any]) -> dict[str, Any]:
    """Map the driver-side ``--sweep`` JSON onto fixture kwargs.

    Recognized keys: ``seeds`` (a non-empty list of ints) and ``n_samples``
    (an int >= 1) — the U1 fixture's two freshness knobs. Absent keys fall to
    the fixture's own defaults (8 seeds, 100k samples). Anything else is a
    loud refusal: a silently-ignored sweep key would re-mint a STALE run_id
    and dedup against the prior sandbox run (the determinism lesson).
    """
    unknown = sorted(set(sweep) - {"seeds", "n_samples"})
    if unknown:
        raise SandboxRefusal(
            f"--sweep keys {unknown} are not fixture knobs (have: seeds, n_samples) — "
            "a silently-ignored sweep key would re-mint a stale run_id."
        )
    kwargs: dict[str, Any] = {}
    if "seeds" in sweep:
        seeds = sweep["seeds"]
        if not isinstance(seeds, (list, tuple)) or not seeds:
            raise SandboxRefusal("--sweep 'seeds' must be a non-empty list of ints")
        try:
            kwargs["seeds"] = tuple(int(s) for s in seeds)
        except (TypeError, ValueError) as exc:
            raise SandboxRefusal(f"--sweep 'seeds' must be ints: {exc}") from exc
    if "n_samples" in sweep:
        try:
            n_samples = int(sweep["n_samples"])
        except (TypeError, ValueError) as exc:
            raise SandboxRefusal(f"--sweep 'n_samples' must be an int: {exc}") from exc
        if n_samples < 1:
            raise SandboxRefusal("--sweep 'n_samples' must be >= 1")
        kwargs["n_samples"] = n_samples
    return kwargs


def build_fixture_experiment(
    root: Path,
    sweep: Mapping[str, Any],
    run_ref: str,
    *,
    run_name: str,
    cluster: str,
    goal: str,
) -> Any:
    """Build the scratch experiment via the U1 sibling. Returns the sibling's
    handle (a ``SandboxExperiment`` — the driver reads ``experiment_dir`` /
    ``run_name`` / ``run_id`` / ``cmd_sha`` / ``total_tasks`` / ``seeds`` /
    ``n_samples`` / ``goal`` off it, defensively, so a sibling that lands a
    mapping instead still integrates).

    The driver's run_name / cluster / goal are passed THROUGH so the fixture's
    pre-computed identity is exactly the one the resolve leg mints, and the
    interview's recorded goal is the one the greenlight commits.
    """
    module = load_sibling_module(FIXTURE_MODULE_PATH, label="sandbox_fixture")
    build = sibling_entrypoint(module, FIXTURE_ENTRYPOINT, label="sandbox_fixture")
    kwargs = fixture_kwargs_from_sweep(sweep)
    try:
        handle = build(
            root,
            run_name=run_name,
            executor_variant="pi",
            cluster=cluster,
            goal=goal,
            **kwargs,
        )
    except TypeError as exc:
        raise SandboxRefusal(
            f"{FIXTURE_ENTRYPOINT} rejected the call (root, run_name={run_name!r}, "
            f"executor_variant='pi', cluster={cluster!r}, goal=<str>, {kwargs}): {exc} "
            "— reconcile the U1 contract signature."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise SandboxRefusal(f"{FIXTURE_ENTRYPOINT} failed: {exc}") from exc
    experiment_dir = fixture_handle_value(handle, "experiment_dir")
    if experiment_dir is None:
        # Contract fallback: the fixture materialized into root itself.
        experiment_dir = root
    experiment_dir = Path(str(experiment_dir)).resolve()
    if not (experiment_dir / "interview.json").is_file():
        raise SandboxRefusal(
            f"fixture experiment at {experiment_dir} carries no interview.json — "
            f"the U1 contract says the REAL interview outputs are materialized "
            f"(run_ref={run_ref})."
        )
    return handle


def fixture_handle_value(handle: Any, name: str) -> Any:
    """Read *name* off the fixture handle — attribute, mapping key, or a bare
    path for ``experiment_dir`` (the defensive integration seam)."""
    if name == "experiment_dir" and isinstance(handle, (str, Path)):
        return handle
    if isinstance(handle, Mapping):
        return handle.get(name)
    return getattr(handle, name, None)


def seed_authorship_utterance(
    journal_home: Path, experiment_dir: Path, text: str, *, run_ref: str
) -> None:
    """Seed the human-authorship utterance via the U2 sibling (which carries
    its own §3 guard + the seeded_by provenance stamp)."""
    module = load_sibling_module(SEED_MODULE_PATH, label="sandbox_seed")
    seed = sibling_entrypoint(module, SEED_UTTERANCE_ENTRYPOINT, label="sandbox_seed")
    try:
        seed(journal_home, experiment_dir, text, run_ref=run_ref)
    except TypeError as exc:
        raise SandboxRefusal(
            f"{SEED_UTTERANCE_ENTRYPOINT} rejected (journal_home, experiment_dir, "
            f"text, run_ref={run_ref!r}): {exc} — reconcile the U2 contract signature."
        ) from exc
    except Exception as exc:  # noqa: BLE001 — the sibling's guard refusals land here
        raise SandboxRefusal(f"{SEED_UTTERANCE_ENTRYPOINT} refused/failed: {exc}") from exc


# ── --local container bring-up (mirrors .github/workflows/scheduler-integration.yml) ──

_U7_GUIDANCE = (
    "no docker on this host — the sanctioned binding (plan U7) is: "
    "`gh workflow run scheduler-integration.yml` + `gh run watch`."
)


def _sh(cmd: Sequence[str], *, what: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        list(cmd), capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=cwd
    )
    if proc.returncode != 0:
        tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-400:]
        raise SandboxRefusal(f"--local: {what} failed (rc={proc.returncode}): {tail}")
    return (proc.stdout or "").strip()


def _write_ssh_shims(shim_dir: Path, ssh_config: Path) -> dict[str, str]:
    """Write ssh/scp shims that inject ``-F <scratch config>`` and return the
    env overrides (HPC_SSH_BINARY / HPC_SCP_BINARY). rsync's remote shell is
    pinned to the resolved ssh binary by the framework's ``_rsync_rsh_env``,
    so the ssh shim covers rsync too. Never touches ``~/.ssh/config``."""
    shim_dir.mkdir(parents=True, exist_ok=True)
    env: dict[str, str] = {}
    if os.name == "nt":
        real_ssh = shutil.which("ssh") or r"C:\Windows\System32\OpenSSH\ssh.exe"
        real_scp = shutil.which("scp") or r"C:\Windows\System32\OpenSSH\scp.exe"
        for name, real in (("ssh", real_ssh), ("scp", real_scp)):
            shim = shim_dir / f"{name}.cmd"
            shim.write_text(f'@"{real}" -F "{ssh_config}" %*\r\n', encoding="utf-8")
            env[f"HPC_{name.upper()}_BINARY"] = str(shim)
    else:
        for name in ("ssh", "scp"):
            real_bin = shutil.which(name)
            if not real_bin:
                raise SandboxRefusal(f"--local: {name} not on PATH; {_U7_GUIDANCE}")
            shim = shim_dir / name
            shim.write_text(
                f'#!/bin/sh\nexec "{real_bin}" -F "{ssh_config}" "$@"\n', encoding="utf-8"
            )
            shim.chmod(0o700)
            env[f"HPC_{name.upper()}_BINARY"] = str(shim)
    return env


def ensure_local_cluster(workdir: Path, *, keep_container: bool) -> tuple[Path, dict[str, str]]:
    """Stand the ci/slurm container up, returning (clusters_yaml_path, extra_env).

    Mirrors the workflow's bring-up step-for-step. Tears down on failure;
    leaves the container running on success (the driver tears it down at the
    end unless --keep-container).
    """
    if not shutil.which("docker"):
        raise SandboxRefusal(f"--local: {_U7_GUIDANCE}")
    dockerfile = REPO_ROOT / "ci" / "slurm" / "Dockerfile"
    if not dockerfile.is_file():
        raise SandboxRefusal(f"--local: {dockerfile} missing — the ci/slurm lane is required.")

    _sh(
        ["docker", "build", "-t", "hpc-agent-slurm-ci:latest", "-f", str(dockerfile), "."],
        what="docker build",
        cwd=REPO_ROOT,
    )

    key = workdir / "ci_key"
    _sh(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-f", str(key), "-C", "sandbox-proving"],
        what="ssh-keygen",
    )

    # Build a wheel of the CURRENT tree and install it into the container
    # python (the cluster-side dispatcher imports hpc_agent).
    dist = workdir / "dist"
    _sh(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "-w", str(dist), str(REPO_ROOT)],
        what="pip wheel",
    )
    wheels = list(dist.glob("*.whl"))
    if not wheels:
        raise SandboxRefusal("--local: pip wheel produced no wheel")

    subprocess.run(["docker", "rm", "-f", "slurmci"], capture_output=True)  # stale OK
    _sh(
        [
            "docker",
            "run",
            "-d",
            "--name",
            "slurmci",
            "-p",
            "2222:22",
            "-v",
            f"{key}.pub:/pubkey:ro",
            "hpc-agent-slurm-ci:latest",
        ],
        what="docker run",
    )
    try:
        _sh(["docker", "cp", str(wheels[0]), "slurmci:/tmp/"], what="docker cp wheel")
        _sh(
            ["docker", "exec", "slurmci", "bash", "-lc", "pip3 install /tmp/*.whl"],
            what="container pip install",
        )
        for _ in range(30):
            state = _sh(
                ["docker", "exec", "slurmci", "sinfo", "-h", "-o", "%T"],
                what="sinfo probe",
            ).splitlines()
            if state and state[0].startswith("idle"):
                break
            time.sleep(2)
        else:
            raise SandboxRefusal("--local: slurm node never went idle (60s budget)")
    except Exception:
        if not keep_container:
            subprocess.run(["docker", "rm", "-f", "slurmci"], capture_output=True)
        raise

    ssh_config = workdir / "ssh_config"
    ssh_config.write_text(
        "Host slurmci\n"
        "  HostName 127.0.0.1\n"
        "  Port 2222\n"
        "  User hpcuser\n"
        f"  IdentityFile {key}\n"
        "  IdentitiesOnly yes\n"
        "  StrictHostKeyChecking no\n"
        "  UserKnownHostsFile /dev/null\n",
        encoding="utf-8",
    )
    shim_env = _write_ssh_shims(workdir / "shims", ssh_config)

    clusters_yaml = workdir / "ci_clusters.yaml"
    clusters_yaml.write_text(_LOCAL_CLUSTERS_YAML, encoding="utf-8")
    return clusters_yaml, shim_env


def teardown_local_container() -> None:
    if shutil.which("docker"):
        subprocess.run(["docker", "rm", "-f", "slurmci"], capture_output=True)


# ── The chain ────────────────────────────────────────────────────────────────


@dataclass
class ChainContext:
    """Everything the chain steps share."""

    env: dict[str, str]
    journal_home: Path
    workdir: Path
    scratch: Path
    experiment_dir: Path | None
    cluster: str
    configured_clusters: list[str]
    ssh_target: str
    backend: str
    remote_path_stanza: Mapping[str, Any]
    goal: str
    run_name: str
    run_ref: str
    wait_timeout: int
    poll_interval: int
    run_preflight: bool
    walltime_sec: int
    run_id: str | None = None


def _step_cli(
    state: ChainState,
    ctx: ChainContext,
    *,
    step: str,
    verb: str,
    spec: Mapping[str, Any],
    experiment_dir: Path | None,
    timeout_sec: int = _CLI_TIMEOUT_SEC,
) -> CliOutcome | None:
    """Write the spec, run the verb, record red invocations as a failing row.

    Returns the outcome on success; records + returns None (chain aborts) on
    a SandboxRefusal.
    """
    spec_path = write_spec(ctx.scratch, f"{ctx.run_ref}.{step}.{verb}", spec)
    try:
        return run_cli(
            verb, spec_path, experiment_dir=experiment_dir, env=ctx.env, timeout_sec=timeout_sec
        )
    except SandboxRefusal as exc:
        state.record(step, f"{verb} CLI", "CLI invocation ok", False, str(exc))
        return None


def _assert_step(
    state: ChainState, *, step: str, where: str, check: str, problems: Sequence[str]
) -> bool:
    """Record one assertion row (problems empty == pass). Returns the pass."""
    passed = not problems
    state.record(step, where, check, passed, "; ".join(problems))
    return passed


def poll_detached_state(ctx: ChainContext, run_id: str, block: str) -> str | None:
    """One non-blocking ``poll-detached`` snapshot: ``running`` /
    ``exited_recorded`` / ``exited_unrecorded`` / ``no_lease`` (or None on a
    red invocation — the caller records the probe outcome as evidence, never
    patches around it)."""
    spec_path = write_spec(
        ctx.scratch, f"{ctx.run_ref}.poll.{block}", {"run_id": run_id, "block": block}
    )
    try:
        outcome = run_cli(
            "poll-detached", spec_path, experiment_dir=ctx.experiment_dir, env=ctx.env
        )
    except SandboxRefusal:
        return None
    state = outcome.data.get("state")
    return str(state) if state is not None else None


def wait_for_detached_terminal(
    state: ChainState, ctx: ChainContext, *, step: str, verb: str, run_id: str
) -> dict[str, Any] | None:
    """wait-detached + terminal-record read. Returns the block's terminal
    result dict (SubmitBlockResult dump) or None (row recorded, chain aborts).

    ``wait-detached`` outcomes (``ops.monitor.wait_detached``):
    ``worker_exited`` / ``no_live_worker`` are both fine — the terminal record
    is the payload either way (a fast worker can exit before the wait
    attaches); ``timeout`` is the only failing outcome.
    """
    wait_spec = {
        "run_id": run_id,
        "block": verb,
        "timeout_sec": ctx.wait_timeout,
        "poll_interval_sec": ctx.poll_interval,
    }
    outcome = _step_cli(
        state,
        ctx,
        step=step,
        verb="wait-detached",
        spec=wait_spec,
        experiment_dir=None,  # wait-detached resolves the lease via the journal home
        timeout_sec=ctx.wait_timeout + 120,
    )
    if outcome is None:
        return None
    wait_outcome = outcome.data.get("outcome")
    if wait_outcome == "timeout":
        state.record(
            step,
            "wait-detached",
            f"detached {verb} worker exits within {ctx.wait_timeout}s",
            False,
            "outcome=timeout (worker still alive; cluster jobs may run on)",
        )
        return None
    record_path = terminal_record_path(ctx.experiment_dir, run_id, verb)  # type: ignore[arg-type]
    if not record_path.is_file():
        state.record(
            step,
            "terminal record",
            f"{verb} terminal record exists at .hpc/runs/{run_id}.{verb}.terminal.json",
            False,
            f"absent (wait outcome={wait_outcome})",
        )
        return None
    record = read_json(record_path)
    result = record.get("result") if isinstance(record, dict) else None
    if not isinstance(result, dict):
        state.record(
            step,
            "terminal record",
            "terminal record carries result",
            False,
            f"unparseable record at {record_path}",
        )
        return None
    state.record(
        step,
        "wait-detached",
        f"detached {verb} worker exits within {ctx.wait_timeout}s",
        True,
        f"outcome={wait_outcome}",
    )
    return result


# Sentinel return of _launch_block_detached: the fused tick already launched
# the block — no direct invocation happened, the terminal is awaited as usual.
_AUTO_ADVANCED = "auto-advanced"


def _launch_block_detached(
    state: ChainState,
    ctx: ChainContext,
    *,
    step: str,
    verb: str,
    run_id: str,
    spec: Mapping[str, Any],
) -> CliOutcome | str | None:
    """Invoke a detached block verb — but OBSERVE a fused-tick auto-advance first.

    The fused S2 greenlight's own advance leg consumes the parked,
    sha-verified successor spec (R3) and launches submit-s3 itself; the
    single-lease then refuses a second launch. So: probe ``poll-detached``
    first — a worker already present means the tick advanced (record it as
    evidence and skip the invocation; the wait reads the terminal either
    way). ``no_lease`` → invoke directly and assert the detached handle.
    Returns the invocation outcome, the ``_AUTO_ADVANCED`` sentinel when the
    tick launched the block (no invocation happened), or None when the chain
    must abort (a failing evidence row was recorded).
    """
    snap = poll_detached_state(ctx, run_id, verb)
    if snap in _WORKER_PRESENT_STATES:
        live = snap != "exited_unrecorded"
        state.record(
            step,
            "poll-detached",
            f"{verb} already launched by the fused tick's advance (R3) — observed, "
            "not re-invoked (single-lease)",
            live,
            f"state={snap}" + ("" if live else " (worker exited with no terminal record)"),
        )
        return _AUTO_ADVANCED if live else None
    outcome = _step_cli(
        state, ctx, step=step, verb=verb, spec=spec, experiment_dir=ctx.experiment_dir
    )
    if outcome is None:
        return None
    problems = assert_block_envelope(outcome.data, verb=verb)
    if outcome.data.get("stage_reached") != "detached":
        problems.append(
            f"stage_reached={outcome.data.get('stage_reached')!r} (expected 'detached' "
            f"with detach=true): {outcome.data.get('reason', '')}"
        )
    if not _assert_step(
        state,
        step=step,
        where=f"{verb} envelope",
        check=f"{verb} detaches (durable worker owns the cluster wait)",
        problems=problems,
    ):
        return None
    return outcome


def _commit_greenlight(
    state: ChainState,
    ctx: ChainContext,
    *,
    step: str,
    run_id: str,
    block: str,
    resolved: Mapping[str, Any],
    next_block: str,
    proposal: str,
    evidence_digest: Mapping[str, Any],
) -> bool:
    """The fused block-drive --approve: commit the greenlight through the ONE
    append-decision definition (every gate fires), then assert the COMMIT
    landed in the run's decision journal. The tick's own advance leg is
    recorded as detail, never a failure: on a thin/brief-shaped resolved that
    is not the successor's spec shape it junk-spans harmlessly (CLI
    validation refuses before any SSH/journal write); at the S2 boundary it
    consumes the parked R3 spec and launches S3 itself — either way the
    COMMIT is what the driver asserts."""
    approve_spec = {
        "run_id": run_id,
        "approve": {
            "scope_kind": "run",
            "scope_id": run_id,
            "block": block,
            "response": "y",
            "resolved": dict(resolved),
            "proposal": proposal,
            "evidence_digest": dict(evidence_digest),
            "provenance": {},
        },
    }
    outcome = _step_cli(
        state,
        ctx,
        step=step,
        verb="block-drive",
        spec=approve_spec,
        experiment_dir=ctx.experiment_dir,
    )
    if outcome is None:
        return False
    records = read_jsonl(decision_journal_path(ctx.experiment_dir, run_id))  # type: ignore[arg-type]
    committed = find_greenlight(records, block=block, next_block=next_block)
    tick = f"tick action={outcome.data.get('action')!r} reason={outcome.data.get('reason', '')!r}"
    problems: list[str] = []
    if committed is None:
        problems.append(
            f"no committed 'y' for block={block!r} with resolved.next_block={next_block!r} "
            "in the run's decision journal — a gate refused the greenlight"
        )
    passed = _assert_step(
        state,
        step=step,
        where="decision journal",
        check=f"{block} greenlight commits (provenance + authorship gates accept)",
        problems=problems,
    )
    if passed:
        state.rows[-1]["detail"] = tick
    return passed


def run_chain(ctx: ChainContext, *, sweep: Mapping[str, Any]) -> ChainState:
    """Drive the full block chain, accumulating evidence rows. Record-and-abort:
    the first failing row stops the chain (later steps depend on it)."""
    state = ChainState()

    # ── Step: fixture build (U1 sibling) ────────────────────────────────────
    handle: Any = None
    if ctx.experiment_dir is None:
        try:
            handle = build_fixture_experiment(
                ctx.workdir / "experiment",
                sweep,
                ctx.run_ref,
                run_name=ctx.run_name,
                cluster=ctx.cluster,
                goal=ctx.goal,
            )
            ctx.experiment_dir = Path(str(fixture_handle_value(handle, "experiment_dir"))).resolve()
        except SandboxRefusal as exc:
            state.record("fixture", "sandbox_fixture", "fixture experiment builds", False, str(exc))
            return state
        state.record(
            "fixture",
            "sandbox_fixture",
            "fixture experiment builds with interview.json materialized",
            True,
            f"experiment_dir={ctx.experiment_dir}",
        )
    experiment_dir = ctx.experiment_dir

    # ── Step: read the interview outputs (canonical on-disk contract) ────────
    try:
        interview = read_json(experiment_dir / "interview.json")
        entry_point = interview.get("entry_point") or {}
        executor_run_name = str(entry_point.get("run_name") or "run")
        materialized = interview.get("_materialized") or {}
        executor_cmd = str((materialized.get("entry_point") or {}).get("executor_cmd") or "")
        task_generator = interview.get("task_generator") or {}
        total_tasks = int(materialized.get("total_tasks") or interview.get("task_count") or 0)
        profile = str((interview.get("cluster_target") or {}).get("profile") or "cpu")
        if not executor_cmd:
            raise SandboxRefusal("interview.json carries no _materialized.entry_point.executor_cmd")
        if not task_generator:
            raise SandboxRefusal("interview.json carries no task_generator")
        if total_tasks < 1:
            raise SandboxRefusal("interview.json carries no usable task_count")
    except (SandboxRefusal, json.JSONDecodeError, OSError) as exc:
        state.record("interview", "interview.json", "interview outputs readable", False, str(exc))
        return state
    # The MINTING name is the fixture's run_name (compute-run-id keys on it) —
    # never the executor's function name (interview entry_point.run_name is the
    # @register_run function, used for the walk's executor_run_name + the
    # axes.yaml `executors.` lookup).
    run_name = str(fixture_handle_value(handle, "run_name") or ctx.run_name)
    expected_run_id = fixture_handle_value(handle, "run_id")
    state.record(
        "interview",
        "interview.json",
        "interview outputs readable (executor_cmd / task_generator / task_count)",
        True,
        f"run_name={run_name} executor={executor_run_name} total_tasks={total_tasks} "
        f"profile={profile}",
    )

    # ── Step: seed the authorship utterance (U2 sibling, §3) ─────────────────
    utterance = build_utterance_text(ctx.goal, task_generator)
    try:
        seed_authorship_utterance(ctx.journal_home, experiment_dir, utterance, run_ref=ctx.run_ref)
    except SandboxRefusal as exc:
        state.record("seed", "sandbox_seed", "authorship utterance seeded", False, str(exc))
        return state
    state.record(
        "seed",
        "sandbox_seed",
        "authorship utterance seeded into the ephemeral journal home",
        True,
        "seeded_by=sandbox-proving (the sibling stamps provenance)",
    )

    # ── Step 1: bare block-drive fresh-start → the actionable skip (U5.1) ────
    outcome = _step_cli(
        state,
        ctx,
        step="s1.block-drive-bare",
        verb="block-drive",
        spec={"workflow": "submit"},
        experiment_dir=experiment_dir,
    )
    if outcome is None:
        return state
    problems = assert_block_drive_envelope(outcome.data)
    if not problems:
        if outcome.data.get("action") != "skip":
            problems.append(f"action={outcome.data.get('action')!r}, expected 'skip'")
        if outcome.data.get("next_verb") != "submit-s1":
            problems.append(f"next_verb={outcome.data.get('next_verb')!r}, expected 'submit-s1'")
        reason = str(outcome.data.get("reason", ""))
        if "cannot fresh-start submit-s1" not in reason:
            problems.append(f"reason is not the actionable skip: {reason!r}")
    if not _assert_step(
        state,
        step="s1.block-drive-bare",
        where="block-drive",
        check="bare fresh-start returns the actionable skip (never a crash)",
        problems=problems,
    ):
        return state

    # ── Step 2: S1 walk-only → recorded-resolution booleans honored (U5.2) ───
    recorded = compute_recorded_resolutions(experiment_dir, executor_run_name)
    walk = build_walk_spec(
        cluster=ctx.cluster,
        configured_clusters=ctx.configured_clusters,
        goal=ctx.goal,
        task_generator=task_generator,
        profile=profile,
        executor_run_name=executor_run_name,
        walltime_sec=ctx.walltime_sec,
        experiment_dir=experiment_dir,
        recorded=recorded,
    )
    outcome = _step_cli(
        state,
        ctx,
        step="s1.walk",
        verb="submit-s1",
        spec={"walk": walk, "run_preflight": ctx.run_preflight},
        experiment_dir=experiment_dir,
    )
    if outcome is None:
        return state
    s1_data = outcome.data
    problems = assert_block_envelope(s1_data, verb="submit-s1")
    brief = _dict_or_empty(s1_data.get("brief"))
    provenance = _dict_or_empty(brief.get("provenance"))
    for field_name, flag in (
        ("entry_point", "entry_point_resolved"),
        ("data_axis", "data_axis_resolved"),
        ("homogeneous_axes", "homogeneous_axes_resolved"),
    ):
        if recorded.get(flag) and provenance.get(field_name) != "resolved_on_disk":
            problems.append(
                f"{flag}=True on disk but brief.provenance.{field_name}="
                f"{provenance.get(field_name)!r} (expected 'resolved_on_disk')"
            )
        if not isinstance(recorded.get(flag), bool):
            problems.append(f"{flag} is not a boolean: {recorded.get(flag)!r}")
    ambiguities = brief.get("ambiguities")
    if ambiguities:
        problems.append(f"walk surfaced ambiguities despite all fields supplied: {ambiguities}")
    # A CLEAN walk with no resolve leg lands at stage 'resolved' with
    # needs_decision=True — the PRE-RESOLVE boundary (run_id unminted).
    # 'needs_resolution' is the AMBIGUOUS stage, which the ambiguities check
    # above already fails on.
    if s1_data.get("stage_reached") != "resolved":
        problems.append(
            f"walk-only stage_reached={s1_data.get('stage_reached')!r} "
            "(expected 'resolved' — the clean PRE-RESOLVE boundary)"
        )
    if s1_data.get("needs_decision") is not True:
        problems.append("walk-only needs_decision is not true (the y/nudge boundary)")
    if not _assert_step(
        state,
        step="s1.walk",
        where="submit-s1 brief",
        check="recorded-resolution booleans honored; clean walk lands at the "
        "PRE-RESOLVE 'resolved' boundary",
        problems=problems,
    ):
        return state

    # ── Step 3: S1 walk+resolve → run_id minted, placeholders overridden ─────
    resolve = build_resolve_spec(
        run_name=run_name,
        profile=profile,
        cluster=ctx.cluster,
        ssh_target=ctx.ssh_target,
        remote_path=stanza_remote_path(ctx.remote_path_stanza, experiment_dir),
        backend=ctx.backend,
        total_tasks=total_tasks,
        executor_cmd=executor_cmd,
        walltime_sec=ctx.walltime_sec,
    )
    outcome = _step_cli(
        state,
        ctx,
        step="s1.resolve",
        verb="submit-s1",
        spec={"walk": walk, "run_preflight": ctx.run_preflight, "resolve": resolve},
        experiment_dir=experiment_dir,
    )
    if outcome is None:
        return state
    s1_data = outcome.data
    problems = assert_block_envelope(s1_data, verb="submit-s1")
    brief = _dict_or_empty(s1_data.get("brief"))
    resolve_brief = _dict_or_empty(brief.get("resolve"))
    run_id = s1_data.get("run_id") or resolve_brief.get("run_id")
    if s1_data.get("stage_reached") != "resolved":
        problems.append(
            f"stage_reached={s1_data.get('stage_reached')!r} (expected 'resolved'): "
            f"{s1_data.get('reason', '')}"
        )
    problems += assert_run_id_minted(run_id, run_name)
    if run_id and isinstance(run_id, str):
        if run_id == PLACEHOLDER_RUN_ID:
            problems.append("run_id is still the placeholder — compute-run-id did not override")
        if expected_run_id and str(expected_run_id) != run_id:
            problems.append(
                f"minted run_id {run_id!r} != the fixture's pre-computed identity "
                f"{str(expected_run_id)!r} — the same compute-run-id must mint both"
            )
        if not (experiment_dir / ".hpc" / "runs" / f"{run_id}.json").is_file():
            problems.append(f"run sidecar .hpc/runs/{run_id}.json not written")
    if not isinstance(resolve_brief.get("submit_spec"), dict):
        problems.append("brief.resolve.submit_spec missing — the S2 hand-off cannot compose")
    if not _assert_step(
        state,
        step="s1.resolve",
        where="submit-s1 brief",
        check="stage 'resolved'; run_id minted as <run_name>-<8hex>; placeholders overridden",
        problems=problems,
    ):
        return state
    ctx.run_id = str(run_id)
    run_id = ctx.run_id

    # ── Step 4: fused approve — S1 greenlight commits (brief-shaped resolved) ─
    greenlight_resolved = build_s1_greenlight_resolved(brief)
    shape_problems = provenance_shape_problems(greenlight_resolved, brief)
    state.record(
        "s1.greenlight",
        "driver self-check",
        "greenlight resolved is brief-shaped (pre-commit provenance mirror)",
        not shape_problems,
        "; ".join(shape_problems),
    )
    if shape_problems:
        return state
    if not _commit_greenlight(
        state,
        ctx,
        step="s1.greenlight",
        run_id=run_id,
        block="submit-s1",
        resolved=greenlight_resolved,
        next_block="submit-s2",
        proposal=(
            f"S1 resolved: {run_id} on {ctx.cluster}, {total_tasks} tasks; "
            "sandbox greenlight (seeded utterance); stage+canary next"
        ),
        evidence_digest={
            "preflight": brief.get("preflight"),
            "provenance": brief.get("provenance"),
            "resolved": brief.get("resolved"),
        },
    ):
        return state

    # ── Step 5: S2 stage+canary (detached) → canary verified ─────────────────
    s2_spec = compose_s2_spec(brief)
    launched = _launch_block_detached(
        state, ctx, step="s2.stage", verb="submit-s2", run_id=run_id, spec=s2_spec
    )
    if launched is None:
        return state  # a failing evidence row was recorded (red CLI / bad envelope / dead worker)
    s2_result = wait_for_detached_terminal(
        state, ctx, step="s2.canary", verb="submit-s2", run_id=run_id
    )
    if s2_result is None:
        return state
    s2_brief = _dict_or_empty(s2_result.get("brief"))
    problems = []
    if s2_result.get("stage_reached") != "canary_verified":
        problems.append(
            f"terminal stage_reached={s2_result.get('stage_reached')!r} "
            f"(expected 'canary_verified'): {s2_result.get('reason', '')}"
        )
    if s2_brief.get("verified") is not True:
        problems.append(f"brief.verified={s2_brief.get('verified')!r} (expected true)")
    if not _assert_step(
        state,
        step="s2.canary",
        where="submit-s2 terminal",
        check="canary verified; est. core-hours attached",
        problems=problems,
    ):
        return state

    # ── Step 6: fused approve — S2 greenlight (thin next_block) ──────────────
    if not _commit_greenlight(
        state,
        ctx,
        step="s2.greenlight",
        run_id=run_id,
        block="submit-s2",
        resolved={"next_block": "submit-s3"},
        next_block="submit-s3",
        proposal=(
            f"canary green (est. {s2_brief.get('est_core_hours', '?')} core-hours); "
            "submit main array under HPC_SUBMIT_ONCE=1 and watch"
        ),
        evidence_digest=s2_brief,
    ):
        return state

    # ── Step 7: S3 submit+watch (detached) → watching_terminal ───────────────
    materialized_s3 = materialized_successor_path(experiment_dir, run_id, "submit-s3")
    if materialized_s3.is_file():
        s3_spec = read_json(materialized_s3)
        s3_source = f"materialized {materialized_s3.name} (code-composed at S2 park)"
        if "detach" not in s3_spec:
            s3_spec = {**s3_spec, "detach": True}
    else:
        s3_spec = compose_s3_spec(s2_spec, run_id, s2_brief)
        s3_source = "driver-composed (no materialized spec parked)"
    state.record("s3.spec", ".hpc/specs/next", "S3 spec source", True, s3_source)
    launched = _launch_block_detached(
        state, ctx, step="s3.submit", verb="submit-s3", run_id=run_id, spec=s3_spec
    )
    if launched is None:
        return state  # a failing evidence row was recorded (red CLI / bad envelope / dead worker)
    s3_result = wait_for_detached_terminal(
        state, ctx, step="s3.watch", verb="submit-s3", run_id=run_id
    )
    if s3_result is None:
        return state
    s3_brief = _dict_or_empty(s3_result.get("brief"))
    problems = []
    if s3_result.get("stage_reached") != "watching_terminal":
        problems.append(
            f"terminal stage_reached={s3_result.get('stage_reached')!r} "
            f"(expected 'watching_terminal'): {s3_result.get('reason', '')}"
        )
    lifecycle = s3_brief.get("lifecycle_state")
    terminal = s3_result.get("stage_reached") == "watching_terminal"
    if terminal and lifecycle not in ("complete", None):
        problems.append(f"brief lifecycle_state={lifecycle!r} (expected 'complete')")
    if not _assert_step(
        state,
        step="s3.watch",
        where="submit-s3 terminal",
        check="main array terminal, lifecycle complete (submit-once: one array)",
        problems=problems,
    ):
        return state

    # ── Step 8: fused approve — S3 greenlight (thin next_block) ──────────────
    if not _commit_greenlight(
        state,
        ctx,
        step="s3.greenlight",
        run_id=run_id,
        block="submit-s3",
        resolved={"next_block": "submit-s4"},
        next_block="submit-s4",
        proposal="main array terminal (complete); harvest via submit-s4",
        evidence_digest={
            "job_ids": s3_brief.get("main_job_ids"),
            "status": s3_brief.get("lifecycle_state"),
        },
    ):
        return state

    # ── Step 9: S4 harvest (detached) → harvested, results table ─────────────
    materialized_s4 = materialized_successor_path(experiment_dir, run_id, "submit-s4")
    if materialized_s4.is_file():
        s4_spec = read_json(materialized_s4)
        s4_source = f"materialized {materialized_s4.name}"
        if "detach" not in s4_spec:
            s4_spec = {**s4_spec, "detach": True}
    else:
        s4_spec = compose_s4_spec(run_id)
        s4_source = "driver-composed (no materialized spec parked)"
    state.record("s4.spec", ".hpc/specs/next", "S4 spec source", True, s4_source)
    launched = _launch_block_detached(
        state, ctx, step="s4.harvest", verb="submit-s4", run_id=run_id, spec=s4_spec
    )
    if launched is None:
        return state  # a failing evidence row was recorded (red CLI / bad envelope / dead worker)
    s4_result = wait_for_detached_terminal(
        state, ctx, step="s4.table", verb="submit-s4", run_id=run_id
    )
    if s4_result is None:
        return state
    s4_brief = _dict_or_empty(s4_result.get("brief"))
    problems = []
    if s4_result.get("stage_reached") not in ("harvested", "harvest_partial"):
        problems.append(
            f"terminal stage_reached={s4_result.get('stage_reached')!r} "
            f"(expected 'harvested'/'harvest_partial'): {s4_result.get('reason', '')}"
        )
    results_table = s4_brief.get("results_table")
    if not results_table:
        problems.append("brief.results_table is empty — nothing harvested")
    if not _assert_step(
        state,
        step="s4.table",
        where="submit-s4 terminal",
        check="harvested; results table non-empty",
        problems=problems,
    ):
        return state

    return state


# ── CLI entry ────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_sandbox_proving.py",
        description=(
            "U3 sandbox block-loop driver (rung-2 proving): drive the full "
            "block chain against the container cluster, asserting every brief "
            "envelope + gate commit, emitting run-15-§2.3 evidence."
        ),
    )
    parser.add_argument(
        "--clusters-config",
        type=Path,
        default=None,
        help="path to a ci_clusters.yaml (the container lane generates one).",
    )
    parser.add_argument(
        "--cluster",
        default=None,
        help="cluster name inside the config (required when it names several).",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="stand the ci/slurm container up itself (docker required; on "
        "dockerless hosts — e.g. native Windows — this errors with the U7 "
        "guidance: gh workflow run scheduler-integration.yml).",
    )
    parser.add_argument(
        "--keep-container",
        action="store_true",
        help="--local: leave the slurmci container running after the chain.",
    )
    parser.add_argument(
        "--workdir",
        type=Path,
        default=None,
        help="scratch root for specs/evidence/the fixture experiment (default: a fresh tmpdir).",
    )
    parser.add_argument(
        "--experiment-dir",
        type=Path,
        default=None,
        help="use an already-fixtured experiment dir (skip the U1 build; for "
        "driver iteration). Must carry interview.json.",
    )
    parser.add_argument(
        "--sweep",
        default=None,
        help='JSON object of the fixture freshness knobs ("seeds": [ints] '
        'and/or "n_samples": int) — vary it so each run mints a new run_id '
        "(the determinism lesson). Default: a fresh n_samples.",
    )
    parser.add_argument("--run-name", default=DEFAULT_RUN_NAME, help="run name for compute-run-id.")
    parser.add_argument("--goal", default=DEFAULT_GOAL, help="the (seeded) human goal text.")
    parser.add_argument(
        "--walltime-sec",
        type=int,
        default=DEFAULT_WALLTIME_SEC,
        help="per-task walltime ask.",
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=3600,
        help="per-block detached-wait budget (seconds).",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=5,
        help="wait-detached poll interval (seconds).",
    )
    parser.add_argument(
        "--no-preflight",
        action="store_true",
        help="pass run_preflight=false to S1 (skip submit-preflight; debugging).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="evidence JSON path (default: <workdir>/evidence.json).",
    )
    parser.add_argument(
        "--markdown",
        type=Path,
        default=None,
        help="evidence markdown path (default: <workdir>/evidence.md).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    started = time.time()
    started_utc = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started))
    run_ref = f"sandbox-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(started))}-{os.getpid()}"

    # The §3 guard fires before ANY work (fixture/seed/cluster) happens.
    try:
        journal_home = require_ephemeral_journal_home(os.environ)
    except SandboxRefusal as exc:
        print(f"sandbox-proving: REFUSED — {exc}", file=sys.stderr)
        return 2
    # Normalize the driver's OWN env to the resolved home: the in-process
    # sibling guards (fixture + seed) read os.environ and compare it against
    # the declared home — the resolved form keeps both sides byte-identical.
    os.environ["HPC_JOURNAL_DIR"] = str(journal_home)

    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="hpc-sandbox-"))
    workdir = workdir.resolve()
    scratch = workdir / "specs"
    scratch.mkdir(parents=True, exist_ok=True)
    out_path = args.out or (workdir / "evidence.json")
    md_path = args.markdown or (workdir / "evidence.md")

    sweep: dict[str, Any]
    if args.sweep:
        try:
            sweep = json.loads(args.sweep)
        except json.JSONDecodeError as exc:
            print(f"sandbox-proving: --sweep is not valid JSON: {exc}", file=sys.stderr)
            return 2
        if not isinstance(sweep, dict):
            print("sandbox-proving: --sweep must be a JSON object", file=sys.stderr)
            return 2
    else:
        # A fresh n_samples per run so successive runs mint fresh run_ids
        # (the determinism lesson from the 2026-07-18 drill). The base rides
        # the fixture-walltime band (~5–10s/task, see FIXTURE_SAMPLES_PER_SEC)
        # so the main array's squeue-visibility window clears the kill drill's
        # sub-second poll — sacct is disabled, so a completed array vanishes
        # from squeue instantly.
        sweep = {"n_samples": DEFAULT_FIXTURE_N_SAMPLES + (os.getpid() % 50_000)}
    try:
        fixture_kwargs_from_sweep(sweep)  # validate before any cluster work
    except SandboxRefusal as exc:
        print(f"sandbox-proving: {exc}", file=sys.stderr)
        return 2

    env = dict(os.environ)
    env["HPC_JOURNAL_DIR"] = str(journal_home)
    env["HPC_SUBMIT_ONCE"] = "1"
    env["HPC_STATUS_POLL_INTERVAL_SEC"] = _ENV_POLL_INTERVAL

    ctx: ChainContext | None = None
    local_container = False
    try:
        if args.local:
            clusters_path, shim_env = ensure_local_cluster(
                workdir, keep_container=args.keep_container
            )
            env.update(shim_env)
            local_container = True
        elif args.clusters_config is not None:
            clusters_path = args.clusters_config
        else:
            print(
                "sandbox-proving: pass --clusters-config <ci_clusters.yaml> or --local",
                file=sys.stderr,
            )
            return 2
        env["HPC_CLUSTERS_CONFIG"] = str(clusters_path)
        config = load_cluster_config(clusters_path)
        cluster_name, stanza = select_cluster(config, args.cluster)

        ctx = ChainContext(
            env=env,
            journal_home=journal_home,
            workdir=workdir,
            scratch=scratch,
            experiment_dir=args.experiment_dir.resolve() if args.experiment_dir else None,
            cluster=cluster_name,
            configured_clusters=sorted(config),
            ssh_target=stanza_ssh_target(stanza),
            backend=stanza_backend(stanza),
            remote_path_stanza=stanza,
            goal=args.goal,
            run_name=args.run_name,
            run_ref=run_ref,
            wait_timeout=args.wait_timeout,
            poll_interval=args.poll_interval,
            run_preflight=not args.no_preflight,
            walltime_sec=args.walltime_sec,
        )
        state = run_chain(ctx, sweep=sweep)
    except SandboxRefusal as exc:
        state = ChainState()
        state.record("setup", "driver", "chain setup", False, str(exc))
    finally:
        if local_container and not args.keep_container:
            teardown_local_container()

    meta = {
        "run_ref": run_ref,
        "run_id": ctx.run_id if ctx else None,
        "cluster": ctx.cluster if ctx else None,
        "sweep": sweep,
        "submit_once": env.get("HPC_SUBMIT_ONCE"),
        "journal_home": str(journal_home),
        "started_utc": started_utc,
        "duration_sec": round(time.time() - started, 1),
        "driver": "scripts/run_sandbox_proving.py (U3)",
        "jurisdiction": "rung-2: harness contract only — never cluster-environment truth",
    }
    evidence = build_evidence(meta, state.rows)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_markdown(evidence), encoding="utf-8")

    width = max((len(r["step"]) for r in state.rows), default=0)
    for row in state.rows:
        mark = "PASS" if row["pass"] else "FAIL"
        detail = f"  — {row['detail']}" if row["detail"] and not row["pass"] else ""
        print(f"[{mark}] {row['step']:<{width}}  {row['check']}{detail}")
    print(f"\nevidence: {out_path}\nmarkdown: {md_path}")
    print(f"sandbox-proving: verdict {evidence['verdict'].upper()}")
    return 0 if evidence["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
