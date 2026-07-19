"""U6 — the sandbox anomaly matrix (rung-2 proving, plan §4-U6).

WHY THIS EXISTS (``docs/plans/sandbox-proving-run-2026-07-18.md`` §4-U6)
------------------------------------------------------------------------
The U3 driver (``scripts/run_sandbox_proving.py``) proves the HAPPY path of
the block chain against the throwaway dockerized cluster. The anomaly arms
prove the FAILURE paths the live runs actually live on — each scenario
asserts the CODE-RENDERED BRIEF (never internal state) where a brief exists,
the same relay doctrine the live runs follow:

(a) FAILING-EXECUTOR canary — the fixture's ``failing`` executor variant
    fails every task → the S2 ``canary_failed`` terminator (brief fields:
    ``verify_result.failure_kind`` + a non-empty ``stderr_tail``) → the
    resubmit-FIXED arm (a second fixture with the working ``pi`` executor,
    same ephemeral journal home) driven to terminal success;
(b) MID-WATCH cancel — the run reaches the S3 detached watch, the array is
    cancelled on the cluster (``--scheduler slurm|sge`` switch; the cancel
    grammar mirrors the backend engine's ``scancel``/``qdel`` families) →
    the S3 ``watching_anomaly`` terminator → the reconcile arm settles the
    terminal classification;
(c) STALLED-DRIVER doctor — a driver tick stamps its dead-man's switch, its
    tick process is killed so ``next_tick_due`` lapses, then ``doctor``
    (+ ``--fleet``) runs INSIDE the sandbox namespace → the re-arm proposal
    names the run and the sandbox namespace (and a decoy stall seeded in a
    SECOND namespace is never re-armed — the namespace-coupling pin, the
    U5.5 decoy-namespace twin);
(d) ALERTS-ACK round-trip — a stall surfaces an alert via ``doctor --notify``,
    ``alerts-ack`` advances the watermark (monotonically), and the
    ``attention-queue`` brief no longer lists the alert.

TRUST DOCTRINE (plan §3 — never bends)
--------------------------------------
This driver REFUSES to run unless ``HPC_JOURNAL_DIR`` is set AND ephemeral,
delegating to the ONE shared guard via the U3 driver's
:func:`run_sandbox_proving.require_ephemeral_journal_home` (no inline guard
copy — a driver-local copy would re-open the alias-spelling bypass the
guard's red-team corpus pins closed). Every journal write the scenarios make
lands in that ephemeral home; a sandbox run proves the anomaly briefs RENDER
correctly, never that a human decided anything.

SIBLING CONTRACTS (consumed BY PATH, never as packages)
-------------------------------------------------------
* ``scripts/run_sandbox_proving.py`` (U3, committed on main) — the public
  helpers this matrix reuses: the §3 guard, the fixture/seed bridges, the
  CLI runner + envelope parsing, the spec composers, the journal readers,
  the detached-worker probes, ``ChainContext`` / ``ChainState`` / evidence
  builders, and the ``--local`` container bring-up. Loaded BY PATH with a
  sys.modules probe so the hermetic tests bind ONE driver object.
* ``tests/integration/scheduler/sandbox_fixture.py`` (U1) — reached through
  the driver's sibling loader for the ``executor_variant`` knob the U3
  bridge hard-codes to ``pi``: the matrix passes ``"failing"`` for the
  scenario-(a) canary-fail arm.

USAGE
-----
::

    # CI lane / any docker-capable host with ci_clusters.yaml:
    HPC_JOURNAL_DIR=$(mktemp -d)/journal \\
        python scripts/sandbox_anomaly_matrix.py --clusters-config ci_clusters.yaml

    # Docker-capable dev machine: stand the container up, run, tear down:
    HPC_JOURNAL_DIR=$(mktemp -d)/journal \\
        python scripts/sandbox_anomaly_matrix.py --local

    # Native Windows without docker: the sanctioned binding (plan §7/U7) is
    #     gh workflow run scheduler-integration.yml

Note: dev tooling — lives in ``scripts/``, never shipped in the wheel.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
_DRIVER_MODULE_NAME = "run_sandbox_proving"
_DRIVER_PATH = REPO_ROOT / "scripts" / f"{_DRIVER_MODULE_NAME}.py"

# The fixture executor variants (tests/integration/scheduler/sandbox_fixture.py
# ::_TRAIN_PY_BY_VARIANT): "pi" computes, "failing" raises on every task — the
# scenario-(a) canary-fail arm. Named here, pinned against the sibling by
# tests/scripts/test_sandbox_anomaly_matrix.py.
FAILING_EXECUTOR_VARIANT = "failing"
WORKING_EXECUTOR_VARIANT = "pi"

# The cancel families the matrix composes (plan §4-U6 names slurm|sge; the
# grammar twin lives in infra/backends/_engine.py's cancel builder).
_CANCEL_SCHEDULERS = ("slurm", "sge")

_SCENARIO_NAMES = ("a", "b", "c", "d")

_CONTAINER_NAME_DEFAULT = "slurmci"
_JOB_IDS_TIMEOUT_SEC = 180
_KILL_TICK_SLEEP_SEC = 3.0

# monitor-flow lifecycle states that are §5 anomaly terminators — the watching
# brief's lifecycle_state must be one of these (never a clean 'complete').
_ANOMALY_LIFECYCLES = frozenset({"failed", "abandoned"})


def _load_driver() -> Any:
    """Load the U3 driver BY PATH, binding ONE module object per process.

    The sys.modules probe keeps the hermetic tests (which load this matrix by
    path AND may load the driver themselves) on a single driver instance —
    the same one-object discipline ``sandbox_fixture._load_shared_guard``
    enforces for the §3 guard.
    """
    cached = sys.modules.get(_DRIVER_MODULE_NAME)
    if cached is not None:
        return cached
    if not _DRIVER_PATH.is_file():
        raise ImportError(
            f"U3 driver not found at {_DRIVER_PATH} — the anomaly matrix reuses "
            "its public helpers (plan §4-U6 consumption surface)."
        )
    spec = importlib.util.spec_from_file_location(_DRIVER_MODULE_NAME, _DRIVER_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot import the U3 driver from {_DRIVER_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Importlib contract: register BEFORE exec (a dataclass-bearing module
    # AttributeErrors mid-decoration when exec runs unregistered).
    sys.modules[_DRIVER_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


driver = _load_driver()
SandboxRefusal = driver.SandboxRefusal  # the ONE driver refusal class (reused, never redefined)

# Import-identity anchor (mirrors the driver/fixture discipline): a later
# by-path load of THIS module converges on the one registered object.
# Defensive ``get``: a by-path loader that execs WITHOUT pre-registering
# (violating the importlib contract) still gets a clean no-op here instead of
# a KeyError — the anchor must never break the import.
_self_module = sys.modules.get(__name__)
if _self_module is not None:
    sys.modules.setdefault("sandbox_anomaly_matrix", _self_module)


# ────────────────────────────────────────────────────────────────────────────
# Pure helpers (hermetically testable — no cluster, no docker, no subprocess)
# ────────────────────────────────────────────────────────────────────────────


def require_journal_home(env: Mapping[str, str]) -> Path:
    """The §3 guard, delegated to the U3 driver (which delegates to the ONE
    shared sandbox guard). No inline copy — see the module docstring."""
    home: Path = driver.require_ephemeral_journal_home(env)
    return home


def compose_cancel_command(
    scheduler: str, job_ids: Sequence[str], *, task_range: str | None = None
) -> str:
    """The cluster-side cancel command for *scheduler*, over *job_ids*.

    Mirrors the cancel grammar the backend engine dispatches
    (``infra/backends/_engine.py``'s cancel builder): SLURM cancels via
    ``scancel <id> <id> ...`` (per-id ``<id>_[<range>]`` subscript form when a
    task_range scopes it); SGE cancels via ``qdel <id> <id> ...`` (``qdel
    <ids> -t <range>`` for a range). The kill drill (U4) carries the same
    abstraction behind its ``--scheduler`` switch; the matrix composes it for
    the in-container cancel + records it as evidence.

    # MIRROR: infra/backends/_engine.py cancel grammar (scancel / qdel)
    #   pinned-by tests/scripts/test_sandbox_anomaly_matrix.py::test_compose_cancel_slurm
    """
    if scheduler not in _CANCEL_SCHEDULERS:
        raise SandboxRefusal(
            f"scheduler {scheduler!r} has no cancel grammar here (have: {list(_CANCEL_SCHEDULERS)})"
        )
    ids = [str(j) for j in job_ids if str(j).strip()]
    if not ids:
        raise SandboxRefusal("compose_cancel_command: at least one job id is required")
    if scheduler == "slurm":
        if task_range:
            # scancel's array-subscript form, one call per id (engine parity).
            return " ".join(f"scancel {j}_[{task_range}]" for j in ids)
        return f"scancel {' '.join(ids)}"
    # sge: qdel addresses the bare array id; -t scopes a task range (one range).
    if task_range:
        return f"qdel {' '.join(ids)} -t {task_range}"
    return f"qdel {' '.join(ids)}"


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def assert_canary_failed_brief(result: Mapping[str, Any]) -> list[str]:
    """Problems with a scenario-(a) S2 ``canary_failed`` terminal (empty == pass).

    Asserts the CODE-RENDERED brief, never internal state: the envelope must
    land at the anomaly terminator with a ``verify_result`` that names a
    failure kind AND carries the cluster stderr tail (the relay doctrine: the
    orchestrator surfaces ``stderr_tail`` verbatim, it never fetches the log
    itself).

    # MIRROR: ops/submit_blocks.py canary_failed terminator + verify_result brief
    #   pinned-by tests/scripts/test_sandbox_anomaly_matrix.py::test_canary_failed_brief_good
    """
    problems: list[str] = []
    if not isinstance(result, Mapping):
        return ["canary_failed: terminal result is not an object"]
    if result.get("stage_reached") != "canary_failed":
        problems.append(f"stage_reached={result.get('stage_reached')!r} (expected 'canary_failed')")
    if result.get("needs_decision") is not True:
        problems.append("needs_decision must be true at the anomaly terminator")
    reason = result.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        problems.append("reason must be a non-empty string (the relayed anomaly line)")
    brief = _dict_or_empty(result.get("brief"))
    verify = _dict_or_empty(brief.get("verify_result"))
    if not verify:
        problems.append("brief.verify_result missing — the canary verdict does not ride the brief")
        return problems
    if verify.get("ok") is not False:
        problems.append(f"verify_result.ok={verify.get('ok')!r} (expected false)")
    failure_kind = verify.get("failure_kind")
    if not isinstance(failure_kind, str) or not failure_kind.strip():
        problems.append("verify_result.failure_kind must be a non-empty string")
    elif isinstance(reason, str) and failure_kind not in reason:
        problems.append(f"reason does not name the failure kind {failure_kind!r}")
    stderr_tail = verify.get("stderr_tail")
    if not isinstance(stderr_tail, str) or not stderr_tail.strip():
        problems.append("verify_result.stderr_tail must be a non-empty string (relay doctrine)")
    return problems


def assert_watching_anomaly_brief(result: Mapping[str, Any]) -> list[str]:
    """Problems with a scenario-(b) S3 ``watching_anomaly`` terminal (empty == pass).

    The cancelled array must land at the §5 anomaly terminator: an anomaly
    lifecycle (failed/abandoned — never 'complete'), a human decision owed,
    and NO deterministic successor (recovery is a genuine human branch — the
    block chain maps this terminator to a null next_block).

    # MIRROR: ops/submit_blocks.py watching_anomaly terminator (_S3_ANOMALY_STATES)
    #   pinned-by tests/scripts/test_sandbox_anomaly_matrix.py::test_watching_anomaly_brief_good
    """
    problems: list[str] = []
    if not isinstance(result, Mapping):
        return ["watching_anomaly: terminal result is not an object"]
    if result.get("stage_reached") != "watching_anomaly":
        problems.append(
            f"stage_reached={result.get('stage_reached')!r} (expected 'watching_anomaly')"
        )
    if result.get("needs_decision") is not True:
        problems.append("needs_decision must be true at the anomaly terminator")
    brief = _dict_or_empty(result.get("brief"))
    lifecycle = brief.get("lifecycle_state")
    if not isinstance(lifecycle, str) or lifecycle not in _ANOMALY_LIFECYCLES:
        problems.append(
            f"brief.lifecycle_state={lifecycle!r} (expected one of {sorted(_ANOMALY_LIFECYCLES)})"
        )
    if result.get("next_block") is not None:
        problems.append(
            "next_block must be null at an anomaly terminator (recovery is human-owned)"
        )
    return problems


def assert_terminal_classification_brief(
    data: Mapping[str, Any], *, run_id: str | None = None
) -> list[str]:
    """Problems with a scenario-(b) reconcile classification (empty == pass).

    After the cancel, the reconcile arm re-derives ground truth and settles
    the run: the envelope's ``lifecycle_state`` must be a TERMINAL
    classification (never ``in_flight``) for the right run.
    """
    problems: list[str] = []
    if not isinstance(data, Mapping):
        return ["reconcile: classification data is not an object"]
    lifecycle = data.get("lifecycle_state")
    if not isinstance(lifecycle, str) or not lifecycle.strip():
        problems.append("lifecycle_state must be a non-empty string")
    elif lifecycle == "in_flight":
        problems.append("lifecycle_state is still 'in_flight' — reconcile did not settle the run")
    if run_id is not None and data.get("run_id") != run_id:
        problems.append(f"run_id={data.get('run_id')!r} (expected {run_id!r})")
    if not isinstance(data.get("last_status"), Mapping):
        problems.append("last_status must be an object (the refreshed cluster snapshot)")
    return problems


def assert_doctor_proposal(
    data: Mapping[str, Any], *, run_id: str, namespace: str | Path | None = None
) -> list[str]:
    """Problems with a scenario-(c) doctor brief naming *run_id* (empty == pass).

    The re-arm proposal must surface (``needs_attention``) and name the
    stalled run; when *namespace* is given (the fleet scan), the proposal for
    that run must ALSO name the sandbox namespace (fleet proposals carry
    ``[<experiment_dir>]`` + ``evidence.experiment_dir``).
    """
    problems: list[str] = []
    if not isinstance(data, Mapping):
        return ["doctor: brief data is not an object"]
    if data.get("needs_attention") is not True:
        problems.append("needs_attention must be true (a stalled driver was seeded)")
    stalled = data.get("stalled")
    if not isinstance(stalled, list) or not stalled:
        problems.append("stalled must be a non-empty list")
        return problems
    entry = next((e for e in stalled if isinstance(e, Mapping) and e.get("run_id") == run_id), None)
    if entry is None:
        found = sorted(str(e.get("run_id")) for e in stalled if isinstance(e, Mapping))
        problems.append(f"no stalled proposal names run_id={run_id!r} (found: {found})")
        return problems
    proposal = entry.get("proposal")
    proposal_text = proposal if isinstance(proposal, str) else ""
    if not proposal_text or "stalled" not in proposal_text.lower():
        problems.append("proposal must be the drafted re-arm line ('...stalled...')")
    if namespace is not None:
        evidence = _dict_or_empty(entry.get("evidence"))
        named = str(evidence.get("experiment_dir", "")) or ""
        want = str(namespace)
        if want not in proposal_text and os.path.normcase(want) != os.path.normcase(named):
            problems.append(f"proposal does not name the sandbox namespace {want!r}")
    return problems


def assert_run_not_proposed(data: Mapping[str, Any], *, run_id: str) -> list[str]:
    """The namespace-scoping pin: *run_id* (a decoy from a SECOND namespace)
    must NOT appear in a single-namespace doctor scan (the U5.5 twin)."""
    problems: list[str] = []
    if not isinstance(data, Mapping):
        return ["doctor: brief data is not an object"]
    stalled = data.get("stalled")
    if isinstance(stalled, list) and any(
        isinstance(e, Mapping) and e.get("run_id") == run_id for e in stalled
    ):
        problems.append(f"decoy run {run_id!r} leaked into a foreign namespace's doctor scan")
    return problems


def filter_namespace_proposals(
    data: Mapping[str, Any], experiment_dir: str | Path
) -> list[dict[str, Any]]:
    """The re-arm selection: ONLY the fleet proposals belonging to
    *experiment_dir*'s namespace (the decoy namespace is never re-armed).

    Fleet proposals carry ``evidence.experiment_dir``; an un-attributed entry
    came from the scanned namespace itself and is kept. An attributed entry
    naming a DIFFERENT namespace is dropped — that drop IS the pin.
    """
    if not isinstance(data, Mapping):
        return []
    stalled = data.get("stalled")
    if not isinstance(stalled, list):
        return []
    want = os.path.normcase(str(Path(experiment_dir)))
    kept: list[dict[str, Any]] = []
    for entry in stalled:
        if not isinstance(entry, Mapping):
            continue
        evidence = _dict_or_empty(entry.get("evidence"))
        attributed = evidence.get("experiment_dir")
        if attributed is None or os.path.normcase(str(attributed)) == want:
            kept.append(dict(entry))
    return kept


def find_alert_items(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The ``kind == "alert"`` items in an attention-queue brief."""
    if not isinstance(data, Mapping):
        return []
    items = data.get("items")
    if not isinstance(items, list):
        return []
    return [dict(i) for i in items if isinstance(i, Mapping) and i.get("kind") == "alert"]


def assert_no_alert_items(data: Mapping[str, Any]) -> list[str]:
    """Problems when the attention-queue brief still lists ANY alert (the
    scenario-(d) post-ack assertion — empty == every alert acknowledged)."""
    alerts = find_alert_items(data)
    if alerts:
        stamps = sorted(str(_dict_or_empty(a.get("subject")).get("scope_id", "?")) for a in alerts)
        return [f"attention-queue still lists {len(alerts)} alert(s) past the watermark: {stamps}"]
    return []


def _parse_iso_utc(value: str | None) -> datetime | None:
    """Parse an ISO-8601 UTC stamp (tolerant of a trailing ``Z``; py3.10's
    ``fromisoformat`` predates ``Z`` support). None on any miss."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def advance_ack_watermark(current: str | None, requested: str) -> str:
    """The monotonic ack watermark: advance to *requested* only when it is at
    or past *current* — a stale ack never resurrects acknowledged alerts (the
    ``acknowledge_alerts`` contract, mirrored purely for hermetic pinning).

    # MIRROR: ops/recover/notify.py::acknowledge_alerts monotonic watermark
    #   pinned-by tests/scripts/test_sandbox_anomaly_matrix.py::test_watermark_monotonic
    """
    req_dt = _parse_iso_utc(requested)
    if req_dt is None:
        raise SandboxRefusal(f"requested watermark {requested!r} is not ISO-8601 UTC")
    cur_dt = _parse_iso_utc(current)
    if cur_dt is None or req_dt >= cur_dt:
        return requested
    return str(current)


def build_matrix_evidence(
    meta: Mapping[str, Any], scenarios: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    """The matrix evidence document (mirrors ``run_sandbox_proving.build_evidence``:
    meta + rows + computed verdict), grouped per scenario.

    # MIRROR: scripts/run_sandbox_proving.py::build_evidence evidence shape
    #   pinned-by tests/scripts/test_sandbox_anomaly_matrix.py::test_matrix_evidence_verdict
    """
    flat_rows: list[dict[str, Any]] = []
    failures: list[Any] = []
    scenario_blocks: dict[str, Any] = {}
    # Canonical scenarios first (a-d order), then any extra blocks (a "setup"
    # refusal) — an extra block's failing rows MUST reach the verdict, never
    # silently drop out of the flat row list.
    ordered = [n for n in _SCENARIO_NAMES if n in scenarios] + [
        n for n in scenarios if n not in _SCENARIO_NAMES
    ]
    for name in ordered:
        scenario = scenarios.get(name)
        if not isinstance(scenario, Mapping):
            continue
        rows = [dict(r) for r in scenario.get("rows", []) if isinstance(r, Mapping)]
        flat_rows.extend(rows)
        scenario_failures = [r.get("step") for r in rows if not r.get("pass")]
        failures.extend(scenario_failures)
        scenario_blocks[name] = {
            "description": scenario.get("description", ""),
            "verdict": "fail" if scenario_failures else "pass",
            "failed_steps": scenario_failures,
            "rows": rows,
        }
    return {
        "schema_version": 1,
        "kind": "sandbox-anomaly-matrix-evidence",
        "meta": dict(meta),
        "scenarios": scenario_blocks,
        "rows": flat_rows,
        "verdict": "pass" if not failures else "fail",
        "failed_steps": failures,
    }


def render_matrix_markdown(evidence: Mapping[str, Any]) -> str:
    """The human render — mirrors ``run_sandbox_proving.render_markdown``
    (run-15 §2.3 table shape + the rung-2 disclaimer), one section per scenario.

    # MIRROR: scripts/run_sandbox_proving.py::render_markdown render shape
    #   pinned-by tests/scripts/test_sandbox_anomaly_matrix.py::test_matrix_render_shape
    """
    meta = _dict_or_empty(evidence.get("meta"))
    scenarios = _dict_or_empty(evidence.get("scenarios"))
    lines = [
        "# Sandbox anomaly matrix evidence (U6 — rung 2)",
        "",
        f"- run_ref: `{meta.get('run_ref', '?')}`",
        f"- cluster: `{meta.get('cluster', '?')}` (scheduler: `{meta.get('scheduler', '?')}`)",
        f"- journal_home (ephemeral): `{meta.get('journal_home', '?')}`",
        f"- scenarios: `{meta.get('scenarios', '?')}`",
        f"- started: {meta.get('started_utc', '?')}  duration: {meta.get('duration_sec', '?')}s",
        "",
    ]
    for name, raw_block in scenarios.items():
        block = _dict_or_empty(raw_block)
        if not block:
            continue
        lines += [
            f"## Scenario ({name}) — {block.get('description', '?')}",
            "",
            "| Step | Where | Mechanical check | Pass |",
            "|---|---|---|---|",
        ]
        for row in block.get("rows", []):
            mark = "yes" if row.get("pass") else "**NO**"
            check = row.get("check", "")
            detail = row.get("detail") or ""
            if detail and not row.get("pass"):
                check = f"{check} — {detail}"
            lines.append(f"| {row.get('step', '?')} | {row.get('where', '?')} | {check} | {mark} |")
        lines += ["", f"verdict: **{block.get('verdict', '?')}**", ""]
    lines += [
        f"**Verdict: {evidence.get('verdict', '?')}**",
        "",
        "> Rung-2 jurisdiction (plan §1): this evidence adjudicates the harness",
        "> contract only. It can never certify a default flip, a live-validation",
        "> claim, or any cluster-environment truth — rung 3 keeps that monopoly.",
        "",
    ]
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# Live scenario machinery (cluster path — the hermetic tests never touch this)
# ────────────────────────────────────────────────────────────────────────────


def _iso_z(epoch: float) -> str:
    """ISO-8601 UTC (Z suffix) — the format ``parse_iso_utc_or_none`` reads."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _record_check(
    state: Any, *, step: str, where: str, check: str, problems: Sequence[str], ok_detail: str = ""
) -> bool:
    """Record one assertion row on the scenario's ChainState (empty problems
    == pass). Mirrors the U3 driver's ``_assert_step`` over the public
    ``ChainState.record`` surface."""
    passed = not problems
    state.record(step, where, check, passed, "; ".join(problems) if problems else ok_detail)
    return passed


def _step_cli(
    state: Any,
    ctx: Any,
    *,
    step: str,
    verb: str,
    spec: Mapping[str, Any],
    experiment_dir: Path | None,
    timeout_sec: int = 600,
) -> Any | None:
    """Write the spec, run the verb, record red invocations as a failing row
    (mirrors the driver's private helper over its PUBLIC write_spec/run_cli)."""
    spec_path = driver.write_spec(ctx.scratch, f"{ctx.run_ref}.{step}.{verb}", spec)
    try:
        return driver.run_cli(
            verb, spec_path, experiment_dir=experiment_dir, env=ctx.env, timeout_sec=timeout_sec
        )
    except SandboxRefusal as exc:
        state.record(step, f"{verb} CLI", "CLI invocation ok", False, str(exc))
        return None


def _run_cli_flags(
    state: Any,
    ctx: Any,
    *,
    step: str,
    verb: str,
    flags: Sequence[str],
    experiment_dir: Path | None,
    timeout_sec: int = 600,
) -> Any | None:
    """A flag-style verb (``reconcile`` takes ``--run-id``/``--scheduler``,
    not ``--spec``). Records a red invocation as a failing row, returns the
    CliOutcome on success."""
    argv = [sys.executable, "-m", "hpc_agent", verb, *flags]
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
            env=dict(ctx.env),
        )
    except subprocess.TimeoutExpired as exc:
        state.record(step, f"{verb} CLI", "CLI invocation ok", False, f"timed out: {exc}")
        return None
    outcome = driver.CliOutcome(
        verb=verb,
        rc=proc.returncode,
        envelope=driver.parse_envelope(proc.stdout or ""),
        stdout=proc.stdout or "",
        stderr=proc.stderr or "",
    )
    if not outcome.ok:
        state.record(step, f"{verb} CLI", "CLI invocation ok", False, outcome.describe_failure())
        return None
    return outcome


def build_variant_fixture(
    ctx: Any,
    *,
    step: str,
    state: Any,
    root: Path,
    run_name: str,
    executor_variant: str,
    n_samples: int,
) -> Any | None:
    """Build a fixture experiment with the *executor_variant* knob the U3
    bridge hard-codes to ``pi`` — reached through the driver's sibling loader
    (the U1 fixture carries the ``failing`` variant for this arm)."""
    try:
        module = driver.load_sibling_module(driver.FIXTURE_MODULE_PATH, label="sandbox_fixture")
        build = driver.sibling_entrypoint(
            module, "build_sandbox_experiment", label="sandbox_fixture"
        )
        handle = build(
            root,
            run_name=run_name,
            executor_variant=executor_variant,
            cluster=ctx.cluster,
            goal=ctx.goal,
            n_samples=n_samples,
        )
    except SandboxRefusal as exc:
        state.record(step, "sandbox_fixture", f"{executor_variant} fixture builds", False, str(exc))
        return None
    experiment_dir = Path(str(driver.fixture_handle_value(handle, "experiment_dir"))).resolve()
    if not (experiment_dir / "interview.json").is_file():
        state.record(
            step,
            "sandbox_fixture",
            f"{executor_variant} fixture builds",
            False,
            f"no interview.json at {experiment_dir}",
        )
        return None
    state.record(
        step,
        "sandbox_fixture",
        f"{executor_variant} fixture builds with interview.json materialized",
        True,
        f"experiment_dir={experiment_dir}",
    )
    return handle


def _read_interview_identity(experiment_dir: Path) -> dict[str, Any]:
    """The interview outputs the S1 drive needs (mirrors the driver's read)."""
    interview = driver.read_json(experiment_dir / "interview.json")
    entry_point = _dict_or_empty(interview.get("entry_point"))
    materialized = _dict_or_empty(interview.get("_materialized"))
    executor_cmd = str((_dict_or_empty(materialized.get("entry_point")).get("executor_cmd")) or "")
    task_generator = _dict_or_empty(interview.get("task_generator"))
    total_tasks = int(materialized.get("total_tasks") or interview.get("task_count") or 0)
    profile = str((_dict_or_empty(interview.get("cluster_target")).get("profile")) or "cpu")
    if not executor_cmd:
        raise SandboxRefusal("interview.json carries no _materialized.entry_point.executor_cmd")
    if not task_generator:
        raise SandboxRefusal("interview.json carries no task_generator")
    if total_tasks < 1:
        raise SandboxRefusal("interview.json carries no usable task_count")
    return {
        "executor_run_name": str(entry_point.get("run_name") or "run"),
        "executor_cmd": executor_cmd,
        "task_generator": task_generator,
        "total_tasks": total_tasks,
        "profile": profile,
    }


def _seed_utterance(ctx: Any, experiment_dir: Path, task_generator: Mapping[str, Any]) -> None:
    text = driver.build_utterance_text(ctx.goal, task_generator)
    driver.seed_authorship_utterance(ctx.journal_home, experiment_dir, text, run_ref=ctx.run_ref)


def drive_s1_to_greenlight(
    state: Any, ctx: Any, *, step: str, handle: Any, experiment_dir: Path
) -> tuple[str, dict[str, Any]] | None:
    """S1 walk+resolve → run_id minted → fused greenlight commits. Returns
    (run_id, s1_brief) or None (a failing row was recorded)."""
    try:
        identity = _read_interview_identity(experiment_dir)
        _seed_utterance(ctx, experiment_dir, identity["task_generator"])
    except SandboxRefusal as exc:
        state.record(
            step, "interview/seed", "interview readable + utterance seeded", False, str(exc)
        )
        return None
    run_name = str(driver.fixture_handle_value(handle, "run_name") or ctx.run_name)
    recorded = driver.compute_recorded_resolutions(experiment_dir, identity["executor_run_name"])
    walk = driver.build_walk_spec(
        cluster=ctx.cluster,
        configured_clusters=ctx.configured_clusters,
        goal=ctx.goal,
        task_generator=identity["task_generator"],
        profile=identity["profile"],
        executor_run_name=identity["executor_run_name"],
        walltime_sec=ctx.walltime_sec,
        experiment_dir=experiment_dir,
        recorded=recorded,
    )
    resolve = driver.build_resolve_spec(
        run_name=run_name,
        profile=identity["profile"],
        cluster=ctx.cluster,
        ssh_target=ctx.ssh_target,
        remote_path=driver.stanza_remote_path(ctx.remote_path_stanza, experiment_dir),
        backend=ctx.backend,
        total_tasks=identity["total_tasks"],
        executor_cmd=identity["executor_cmd"],
        walltime_sec=ctx.walltime_sec,
    )
    outcome = _step_cli(
        state,
        ctx,
        step=f"{step}.s1",
        verb="submit-s1",
        spec={"walk": walk, "run_preflight": ctx.run_preflight, "resolve": resolve},
        experiment_dir=experiment_dir,
    )
    if outcome is None:
        return None
    s1_data = outcome.data
    problems = driver.assert_block_envelope(s1_data, verb="submit-s1")
    brief = _dict_or_empty(s1_data.get("brief"))
    resolve_brief = _dict_or_empty(brief.get("resolve"))
    run_id = s1_data.get("run_id") or resolve_brief.get("run_id")
    problems += driver.assert_run_id_minted(run_id, run_name)
    if s1_data.get("stage_reached") != "resolved":
        problems.append(f"stage_reached={s1_data.get('stage_reached')!r} (expected 'resolved')")
    if not _record_check(
        state,
        step=f"{step}.s1",
        where="submit-s1 brief",
        check="run_id minted at stage 'resolved'",
        problems=problems,
    ):
        return None
    run_id = str(run_id)
    # Fused greenlight — the ONE append-decision definition (every gate fires).
    resolved = driver.build_s1_greenlight_resolved(brief)
    shape_problems = driver.provenance_shape_problems(resolved, brief)
    if not _record_check(
        state,
        step=f"{step}.s1-greenlight",
        where="driver self-check",
        check="greenlight resolved is brief-shaped",
        problems=shape_problems,
    ):
        return None
    approve_spec = {
        "run_id": run_id,
        "approve": {
            "scope_kind": "run",
            "scope_id": run_id,
            "block": "submit-s1",
            "response": "y",
            "resolved": resolved,
            "proposal": f"sandbox anomaly-matrix greenlight ({ctx.run_ref}); stage+canary next",
            "evidence_digest": {"resolved": brief.get("resolved")},
            "provenance": {},
        },
    }
    _step_cli(
        state,
        ctx,
        step=f"{step}.s1-greenlight",
        verb="block-drive",
        spec=approve_spec,
        experiment_dir=experiment_dir,
    )
    records = driver.read_jsonl(driver.decision_journal_path(experiment_dir, run_id))
    committed = driver.find_greenlight(records, block="submit-s1", next_block="submit-s2")
    if not _record_check(
        state,
        step=f"{step}.s1-greenlight",
        where="decision journal",
        check="S1 greenlight commits (provenance + authorship gates accept)",
        problems=[] if committed else ["no committed 'y' for submit-s1 → submit-s2"],
    ):
        return None
    return run_id, brief


def _launch_detached_observe(
    state: Any, ctx: Any, *, step: str, verb: str, run_id: str, spec: Mapping[str, Any]
) -> bool:
    """Launch a detached block — but OBSERVE a fused-tick auto-advance first
    (the driver's single-lease discipline, over its public poll helper)."""
    snap = driver.poll_detached_state(ctx, run_id, verb)
    if snap in driver._WORKER_PRESENT_STATES:
        live = snap != "exited_unrecorded"
        return _record_check(
            state,
            step=step,
            where="poll-detached",
            check=f"{verb} already launched by the fused tick (observed, single-lease)",
            problems=[] if live else ["worker exited with no terminal record"],
            ok_detail=f"state={snap}",
        )
    outcome = _step_cli(
        state, ctx, step=step, verb=verb, spec=spec, experiment_dir=ctx.experiment_dir
    )
    if outcome is None:
        return False
    problems = driver.assert_block_envelope(outcome.data, verb=verb)
    if outcome.data.get("stage_reached") != "detached":
        problems.append(
            f"stage_reached={outcome.data.get('stage_reached')!r} (expected 'detached')"
        )
    return _record_check(
        state,
        step=step,
        where=f"{verb} envelope",
        check=f"{verb} detaches (durable worker owns the cluster wait)",
        problems=problems,
    )


def _commit_thin_greenlight(
    state: Any, ctx: Any, *, step: str, run_id: str, block: str, next_block: str, proposal: str
) -> bool:
    """The fused thin greenlight (S2/S3): ``{next_block: ...}`` resolved."""
    approve_spec = {
        "run_id": run_id,
        "approve": {
            "scope_kind": "run",
            "scope_id": run_id,
            "block": block,
            "response": "y",
            "resolved": {"next_block": next_block},
            "proposal": proposal,
            "evidence_digest": {},
            "provenance": {},
        },
    }
    _step_cli(
        state,
        ctx,
        step=step,
        verb="block-drive",
        spec=approve_spec,
        experiment_dir=ctx.experiment_dir,
    )
    records = driver.read_jsonl(driver.decision_journal_path(ctx.experiment_dir, run_id))
    committed = driver.find_greenlight(records, block=block, next_block=next_block)
    return _record_check(
        state,
        step=step,
        where="decision journal",
        check=f"{block} greenlight commits",
        problems=[] if committed else [f"no committed 'y' for {block} → {next_block}"],
    )


def _poll_main_job_ids(experiment_dir: Path, run_id: str, *, timeout_sec: int) -> list[str]:
    """Poll the journal run record until the main array's job ids land (the
    detached S3 worker launches phase-2 asynchronously)."""
    from hpc_agent.state.journal import load_run  # lazy: hermetic imports never reach here

    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        record = load_run(experiment_dir, run_id)
        if record is not None and record.job_ids:
            return list(record.job_ids)
        time.sleep(2)
    return []


def _docker_exec(state: Any, *, step: str, command: str, container: str, what: str) -> bool:
    """Run *command* inside the cluster container (``docker exec``). Records a
    row; a missing docker is a refusing row with the U7 guidance."""
    if not shutil.which("docker"):
        state.record(
            step,
            "container",
            what,
            False,
            "no docker on this host — the sanctioned binding (plan U7) is: "
            "`gh workflow run scheduler-integration.yml` + `gh run watch`.",
        )
        return False
    try:
        proc = subprocess.run(
            ["docker", "exec", container, "bash", "-lc", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        state.record(step, "container exec", what, False, "docker exec timed out (120s)")
        return False
    tail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-300:]
    return _record_check(
        state,
        step=step,
        where="container exec",
        check=what,
        problems=[] if proc.returncode == 0 else [f"rc={proc.returncode}: {tail}"],
        ok_detail=f"$ {command}",
    )


def seed_stalled_run(
    state: Any,
    ctx: Any,
    *,
    step: str,
    run_id: str,
    experiment_dir: Path,
    deadline_offset_sec: float,
) -> bool:
    """Seed a STALLED driver in the sandbox namespace: an ``in_flight`` journal
    record whose dead-man's-switch ``next_tick_due`` lapses (the state a dead
    tick process leaves behind). Records one row."""
    from hpc_agent.state.journal import stamp_tick, upsert_run  # lazy import (live path only)
    from hpc_agent.state.run_record import RunRecord

    now = time.time()
    try:
        upsert_run(
            experiment_dir,
            RunRecord(
                run_id=run_id,
                profile="cpu",
                cluster=ctx.cluster,
                ssh_target=ctx.ssh_target,
                remote_path=driver.stanza_remote_path(ctx.remote_path_stanza, experiment_dir),
                job_name=run_id,
                job_ids=[],
                total_tasks=1,
                submitted_at=_iso_z(now),
                experiment_dir=str(experiment_dir),
                status="in_flight",
            ),
        )
        stamp_tick(
            run_id,
            last_tick_at=_iso_z(now),
            next_tick_due=_iso_z(now + deadline_offset_sec),
            experiment_dir=experiment_dir,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced verbatim as the row's detail
        state.record(
            step, "journal seed", "stalled run seeded", False, f"{type(exc).__name__}: {exc}"
        )
        return False
    state.record(
        step,
        "journal seed",
        "stalled run seeded (in_flight + lapsed next_tick_due)",
        True,
        f"run_id={run_id} next_tick_due=now{deadline_offset_sec:+.0f}s",
    )
    return True


# ── The four scenarios ──────────────────────────────────────────────────────

_SCENARIO_DESCRIPTIONS = {
    "a": "failing-executor canary → canary_failed brief → resubmit-FIXED arm to terminal success",
    "b": "mid-watch cancel → watching_anomaly brief → reconcile terminal classification",
    "c": "stalled driver → doctor (+ --fleet) re-arm proposal names the run + sandbox namespace",
    "d": "alerts-ack round-trip → attention-queue brief drops the acknowledged alert",
}


def run_scenario_a(ctx: Any, *, n_samples: int) -> Any:
    """(a) The failing-executor canary, then the resubmit-FIXED arm."""
    state = ctx.state
    fail_root = ctx.workdir / "experiment-a-fail"
    handle = build_variant_fixture(
        ctx,
        step="a.fixture-failing",
        state=state,
        root=fail_root,
        run_name="sandbox-u6-a-fail",
        executor_variant=FAILING_EXECUTOR_VARIANT,
        n_samples=n_samples,
    )
    if handle is None:
        return state
    experiment_dir = Path(str(driver.fixture_handle_value(handle, "experiment_dir"))).resolve()
    ctx.experiment_dir = experiment_dir
    minted = drive_s1_to_greenlight(
        state, ctx, step="a.failing", handle=handle, experiment_dir=experiment_dir
    )
    if minted is None:
        return state
    run_id, s1_brief = minted
    # S2 stage+canary — pre-stage smoke OFF for the failing arm: the arm exists
    # to prove the CLUSTER-side canary_failed brief; the local smoke gate is
    # rung-1-covered and would (correctly) refuse before the canary fires.
    s2_spec = driver.compose_s2_spec(s1_brief)
    s2_spec["submit"]["submit"]["pre_stage_smoke"] = False
    state.record(
        "a.s2-smoke",
        "s2 spec",
        "pre_stage_smoke disabled for the failing arm (disclosed)",
        True,
        "the canary_failed brief is the cluster-side path under test",
    )
    if not _launch_detached_observe(
        state, ctx, step="a.s2-stage", verb="submit-s2", run_id=run_id, spec=s2_spec
    ):
        return state
    s2_result = driver.wait_for_detached_terminal(
        state, ctx, step="a.s2-canary", verb="submit-s2", run_id=run_id
    )
    if s2_result is None:
        return state
    if not _record_check(
        state,
        step="a.s2-canary",
        where="submit-s2 terminal",
        check="canary_failed brief: failure kind + cluster stderr tail ride the brief",
        problems=assert_canary_failed_brief(s2_result),
    ):
        return state

    # ── resubmit-FIXED arm: working executor, same ephemeral journal home ──
    fixed_root = ctx.workdir / "experiment-a-fixed"
    fixed_handle = build_variant_fixture(
        ctx,
        step="a.fixture-fixed",
        state=state,
        root=fixed_root,
        run_name="sandbox-u6-a-fixed",
        executor_variant=WORKING_EXECUTOR_VARIANT,
        n_samples=n_samples + 1000,
    )
    if fixed_handle is None:
        return state
    fixed_dir = Path(str(driver.fixture_handle_value(fixed_handle, "experiment_dir"))).resolve()
    ctx.experiment_dir = fixed_dir
    fixed_minted = drive_s1_to_greenlight(
        state, ctx, step="a.fixed", handle=fixed_handle, experiment_dir=fixed_dir
    )
    if fixed_minted is None:
        return state
    fixed_run_id, fixed_brief = fixed_minted
    fixed_s2_spec = driver.compose_s2_spec(fixed_brief)
    if not _launch_detached_observe(
        state, ctx, step="a.fixed-s2", verb="submit-s2", run_id=fixed_run_id, spec=fixed_s2_spec
    ):
        return state
    fixed_s2 = driver.wait_for_detached_terminal(
        state, ctx, step="a.fixed-canary", verb="submit-s2", run_id=fixed_run_id
    )
    if fixed_s2 is None:
        return state
    problems = []
    if fixed_s2.get("stage_reached") != "canary_verified":
        problems.append(
            f"stage_reached={fixed_s2.get('stage_reached')!r} (expected canary_verified)"
        )
    if not _record_check(
        state,
        step="a.fixed-canary",
        where="submit-s2 terminal",
        check="FIXED arm: canary verified",
        problems=problems,
    ):
        return state
    if not _commit_thin_greenlight(
        state,
        ctx,
        step="a.fixed-s2-greenlight",
        run_id=fixed_run_id,
        block="submit-s2",
        next_block="submit-s3",
        proposal="FIXED arm canary green; submit main array and watch",
    ):
        return state
    s3_spec = driver.compose_s3_spec(
        fixed_s2_spec, fixed_run_id, _dict_or_empty(fixed_s2.get("brief"))
    )
    if not _launch_detached_observe(
        state, ctx, step="a.fixed-s3", verb="submit-s3", run_id=fixed_run_id, spec=s3_spec
    ):
        return state
    s3_result = driver.wait_for_detached_terminal(
        state, ctx, step="a.fixed-watch", verb="submit-s3", run_id=fixed_run_id
    )
    if s3_result is None:
        return state
    s3_brief = _dict_or_empty(s3_result.get("brief"))
    problems = []
    if s3_result.get("stage_reached") != "watching_terminal":
        problems.append(
            f"stage_reached={s3_result.get('stage_reached')!r} (expected watching_terminal)"
        )
    elif s3_brief.get("lifecycle_state") != "complete":
        problems.append(f"lifecycle_state={s3_brief.get('lifecycle_state')!r} (expected complete)")
    _record_check(
        state,
        step="a.fixed-terminal",
        where="submit-s3 terminal",
        check="FIXED arm reaches terminal success (watching_terminal, complete)",
        problems=problems,
    )
    return state


def run_scenario_b(ctx: Any, *, n_samples: int) -> Any:
    """(b) Mid-watch cancel → watching_anomaly → reconcile classification."""
    state = ctx.state
    handle = build_variant_fixture(
        ctx,
        step="b.fixture",
        state=state,
        root=ctx.workdir / "experiment-b",
        run_name="sandbox-u6-b-watch",
        executor_variant=WORKING_EXECUTOR_VARIANT,
        n_samples=n_samples + 2000,
    )
    if handle is None:
        return state
    experiment_dir = Path(str(driver.fixture_handle_value(handle, "experiment_dir"))).resolve()
    ctx.experiment_dir = experiment_dir
    minted = drive_s1_to_greenlight(
        state, ctx, step="b", handle=handle, experiment_dir=experiment_dir
    )
    if minted is None:
        return state
    run_id, s1_brief = minted
    s2_spec = driver.compose_s2_spec(s1_brief)
    if not _launch_detached_observe(
        state, ctx, step="b.s2", verb="submit-s2", run_id=run_id, spec=s2_spec
    ):
        return state
    s2_result = driver.wait_for_detached_terminal(
        state, ctx, step="b.canary", verb="submit-s2", run_id=run_id
    )
    if s2_result is None:
        return state
    if s2_result.get("stage_reached") != "canary_verified":
        state.record(
            "b.canary",
            "submit-s2 terminal",
            "canary verified",
            False,
            f"stage_reached={s2_result.get('stage_reached')!r}",
        )
        return state
    state.record("b.canary", "submit-s2 terminal", "canary verified", True)
    if not _commit_thin_greenlight(
        state,
        ctx,
        step="b.s2-greenlight",
        run_id=run_id,
        block="submit-s2",
        next_block="submit-s3",
        proposal="canary green; submit main array and watch (anomaly arm will cancel it)",
    ):
        return state
    s3_spec = driver.compose_s3_spec(s2_spec, run_id, _dict_or_empty(s2_result.get("brief")))
    if not _launch_detached_observe(
        state, ctx, step="b.s3-launch", verb="submit-s3", run_id=run_id, spec=s3_spec
    ):
        return state
    job_ids = _poll_main_job_ids(experiment_dir, run_id, timeout_sec=_JOB_IDS_TIMEOUT_SEC)
    if not _record_check(
        state,
        step="b.job-ids",
        where="journal run record",
        check="main array job ids land in the journal",
        problems=[] if job_ids else [f"no job_ids within {_JOB_IDS_TIMEOUT_SEC}s"],
        ok_detail=f"job_ids={job_ids}",
    ):
        return state
    cancel_cmd = compose_cancel_command(ctx.backend, job_ids)
    if not _docker_exec(
        state,
        step="b.cancel",
        command=cancel_cmd,
        container=ctx.container,
        what=f"array cancelled on the cluster ({ctx.backend} grammar)",
    ):
        return state
    s3_result = driver.wait_for_detached_terminal(
        state, ctx, step="b.watch", verb="submit-s3", run_id=run_id
    )
    if s3_result is None:
        return state
    if not _record_check(
        state,
        step="b.anomaly",
        where="submit-s3 terminal",
        check="watching_anomaly brief: anomaly lifecycle, decision owed, null successor",
        problems=assert_watching_anomaly_brief(s3_result),
    ):
        return state
    outcome = _run_cli_flags(
        state,
        ctx,
        step="b.reconcile",
        verb="reconcile",
        flags=["--run-id", run_id, "--scheduler", ctx.backend],
        experiment_dir=experiment_dir,
    )
    if outcome is None:
        return state
    _record_check(
        state,
        step="b.classification",
        where="reconcile envelope",
        check="reconcile settles the terminal classification (not in_flight)",
        problems=assert_terminal_classification_brief(outcome.data, run_id=run_id),
    )
    return state


def run_scenario_c(ctx: Any, *, n_samples: int, decoy_experiment_dir: Path) -> Any:
    """(c) Stalled driver → doctor (+ --fleet) inside the sandbox namespace."""
    state = ctx.state
    handle = build_variant_fixture(
        ctx,
        step="c.fixture",
        state=state,
        root=ctx.workdir / "experiment-c",
        run_name="sandbox-u6-c-stall",
        executor_variant=WORKING_EXECUTOR_VARIANT,
        n_samples=n_samples + 3000,
    )
    if handle is None:
        return state
    experiment_dir = Path(str(driver.fixture_handle_value(handle, "experiment_dir"))).resolve()
    ctx.experiment_dir = experiment_dir
    # S1 resolve mints the run_id + sidecar (no greenlight / submit needed —
    # the watchdog reads the journal record, not the scheduler).
    try:
        identity = _read_interview_identity(experiment_dir)
        _seed_utterance(ctx, experiment_dir, identity["task_generator"])
    except SandboxRefusal as exc:
        state.record("c.s1", "interview/seed", "interview readable", False, str(exc))
        return state
    run_name = str(driver.fixture_handle_value(handle, "run_name") or ctx.run_name)
    recorded = driver.compute_recorded_resolutions(experiment_dir, identity["executor_run_name"])
    walk = driver.build_walk_spec(
        cluster=ctx.cluster,
        configured_clusters=ctx.configured_clusters,
        goal=ctx.goal,
        task_generator=identity["task_generator"],
        profile=identity["profile"],
        executor_run_name=identity["executor_run_name"],
        walltime_sec=ctx.walltime_sec,
        experiment_dir=experiment_dir,
        recorded=recorded,
    )
    resolve = driver.build_resolve_spec(
        run_name=run_name,
        profile=identity["profile"],
        cluster=ctx.cluster,
        ssh_target=ctx.ssh_target,
        remote_path=driver.stanza_remote_path(ctx.remote_path_stanza, experiment_dir),
        backend=ctx.backend,
        total_tasks=identity["total_tasks"],
        executor_cmd=identity["executor_cmd"],
        walltime_sec=ctx.walltime_sec,
    )
    outcome = _step_cli(
        state,
        ctx,
        step="c.s1",
        verb="submit-s1",
        spec={"walk": walk, "run_preflight": ctx.run_preflight, "resolve": resolve},
        experiment_dir=experiment_dir,
    )
    if outcome is None:
        return state
    run_id = str(outcome.data.get("run_id") or "")
    if not run_id:
        state.record("c.s1", "submit-s1 brief", "run_id minted", False, "no run_id on the envelope")
        return state
    state.record("c.s1", "submit-s1 brief", "run_id minted", True, f"run_id={run_id}")

    # Seed the stall: an in_flight record whose next_tick_due lapses, then
    # start a real driver tick and kill it (the scenario's "kill the tick
    # process" leg — the killed driver cannot renew the lapsed deadline).
    if not seed_stalled_run(
        state,
        ctx,
        step="c.seed",
        run_id=run_id,
        experiment_dir=experiment_dir,
        deadline_offset_sec=2,
    ):
        return state
    tick_spec = driver.write_spec(
        ctx.scratch, f"{ctx.run_ref}.c.tick.block-drive", {"workflow": "status", "run_id": run_id}
    )
    tick_argv = [
        sys.executable,
        "-m",
        "hpc_agent",
        "block-drive",
        "--spec",
        str(tick_spec),
        "--experiment-dir",
        str(experiment_dir),
    ]
    tick_ok = False
    try:
        proc = subprocess.Popen(  # noqa: S603 — the tick process the scenario kills
            tick_argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=dict(ctx.env),
        )
        time.sleep(_KILL_TICK_SLEEP_SEC)
        proc.kill()
        proc.wait(timeout=15)
        tick_ok = True
        tick_detail = f"tick process {proc.pid} killed (rc={proc.returncode})"
    except OSError as exc:
        tick_detail = f"tick process could not start: {exc}"
    state.record(
        "c.kill-tick", "driver tick", "driver tick process killed mid-cadence", tick_ok, tick_detail
    )
    # Re-stamp the lapsed deadline (the kill may have beaten any renewal —
    # deterministic lapse regardless of tick-side stamping races).
    from hpc_agent.state.journal import stamp_tick  # lazy import (live path only)

    now = time.time()
    stamp_tick(
        run_id,
        last_tick_at=_iso_z(now - 120),
        next_tick_due=_iso_z(now - 60),
        experiment_dir=experiment_dir,
    )
    time.sleep(0.2)

    # Seed the DECOY stall in a SECOND namespace (same ephemeral home, a
    # different repo_hash) — it must NOT appear in the single-namespace scan.
    decoy_run_id = f"sandbox-u6-c-decoy-{int(now) % 100000}"
    seed_stalled_run(
        state,
        ctx,
        step="c.seed-decoy",
        run_id=decoy_run_id,
        experiment_dir=decoy_experiment_dir,
        deadline_offset_sec=-60,
    )

    now_iso = _iso_z(time.time())
    single = _step_cli(
        state,
        ctx,
        step="c.doctor",
        verb="doctor",
        spec={"now": now_iso},
        experiment_dir=experiment_dir,
    )
    if single is None:
        return state
    if not _record_check(
        state,
        step="c.doctor",
        where="doctor brief",
        check="single-namespace scan proposes re-arm for the sandbox run",
        problems=assert_doctor_proposal(single.data, run_id=run_id),
    ):
        return state
    if not _record_check(
        state,
        step="c.doctor-namespace",
        where="doctor brief",
        check="decoy run from the second namespace is invisible (namespace scoping, U5.5 twin)",
        problems=assert_run_not_proposed(single.data, run_id=decoy_run_id),
    ):
        return state
    fleet = _step_cli(
        state,
        ctx,
        step="c.doctor-fleet",
        verb="doctor",
        spec={"now": now_iso, "fleet": True},
        experiment_dir=experiment_dir,
    )
    if fleet is None:
        return state
    if not _record_check(
        state,
        step="c.doctor-fleet",
        where="doctor brief (fleet)",
        check="fleet proposal names the run AND the sandbox namespace",
        problems=assert_doctor_proposal(fleet.data, run_id=run_id, namespace=experiment_dir),
    ):
        return state
    selected = filter_namespace_proposals(fleet.data, experiment_dir)
    selected_ids = {str(e.get("run_id")) for e in selected}
    _record_check(
        state,
        step="c.rearm-selection",
        where="namespace filter",
        check="re-arm selection keeps the sandbox run, never the decoy namespace",
        problems=(
            []
            if run_id in selected_ids and decoy_run_id not in selected_ids
            else [f"selection={sorted(selected_ids)} (want {run_id!r}, not {decoy_run_id!r})"]
        ),
    )
    return state


def run_scenario_d(ctx: Any, *, n_samples: int) -> Any:
    """(d) Alert surfaced → attention-queue lists it → alerts-ack → gone."""
    state = ctx.state
    handle = build_variant_fixture(
        ctx,
        step="d.fixture",
        state=state,
        root=ctx.workdir / "experiment-d",
        run_name="sandbox-u6-d-alert",
        executor_variant=WORKING_EXECUTOR_VARIANT,
        n_samples=n_samples + 4000,
    )
    if handle is None:
        return state
    experiment_dir = Path(str(driver.fixture_handle_value(handle, "experiment_dir"))).resolve()
    ctx.experiment_dir = experiment_dir
    run_id = f"sandbox-u6-d-{int(time.time()) % 100000}"
    if not seed_stalled_run(
        state,
        ctx,
        step="d.seed",
        run_id=run_id,
        experiment_dir=experiment_dir,
        deadline_offset_sec=-60,
    ):
        return state
    notify = _step_cli(
        state,
        ctx,
        step="d.surface",
        verb="doctor",
        spec={"notify": True, "now": _iso_z(time.time())},
        experiment_dir=experiment_dir,
    )
    if notify is None:
        return state
    state.record(
        "d.surface",
        "doctor --notify",
        "stall surfaced as an alert (loud fallback log)",
        True,
        f"stalled_count={notify.data.get('stalled_count')}",
    )
    before = _step_cli(
        state,
        ctx,
        step="d.queue-before",
        verb="attention-queue",
        spec={},
        experiment_dir=experiment_dir,
    )
    if before is None:
        return state
    alerts_before = find_alert_items(before.data)
    if not _record_check(
        state,
        step="d.queue-before",
        where="attention-queue brief",
        check="the surfaced alert is listed pre-ack",
        problems=[] if alerts_before else ["no kind=alert item before ack"],
    ):
        return state
    newest = max(str(_dict_or_empty(a.get("subject")).get("scope_id", "")) for a in alerts_before)
    ack = _step_cli(
        state,
        ctx,
        step="d.ack",
        verb="alerts-ack",
        spec={"up_to_ts": newest},
        experiment_dir=experiment_dir,
    )
    if ack is None:
        return state
    if not _record_check(
        state,
        step="d.ack",
        where="alerts-ack envelope",
        check="watermark advanced, at least one alert acknowledged",
        problems=(
            []
            if int(ack.data.get("acknowledged_count") or 0) >= 1
            else [f"acknowledged_count={ack.data.get('acknowledged_count')!r}"]
        ),
        ok_detail=f"acknowledged_up_to={ack.data.get('acknowledged_up_to')}",
    ):
        return state
    # Monotonicity leg: an OLDER ack must not lower the watermark (count 0).
    stale_ack = _step_cli(
        state,
        ctx,
        step="d.ack-stale",
        verb="alerts-ack",
        spec={"up_to_ts": "2000-01-01T00:00:00Z"},
        experiment_dir=experiment_dir,
    )
    if stale_ack is not None:
        _record_check(
            state,
            step="d.ack-stale",
            where="alerts-ack envelope",
            check="stale ack is a no-op (watermark monotonic — nothing resurrected)",
            problems=(
                []
                if int(stale_ack.data.get("acknowledged_count") or 0) == 0
                else ["a stale ack moved the watermark backwards"]
            ),
        )
    after = _step_cli(
        state,
        ctx,
        step="d.queue-after",
        verb="attention-queue",
        spec={},
        experiment_dir=experiment_dir,
    )
    if after is None:
        return state
    _record_check(
        state,
        step="d.queue-after",
        where="attention-queue brief",
        check="the acknowledged alert no longer rides the queue",
        problems=assert_no_alert_items(after.data),
    )
    return state


# ────────────────────────────────────────────────────────────────────────────
# CLI entry
# ────────────────────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sandbox_anomaly_matrix.py",
        description=(
            "U6 sandbox anomaly matrix (rung-2 proving): drive the four anomaly "
            "arms (failing canary / mid-watch cancel / stalled doctor / alerts-ack) "
            "against the container cluster, asserting every code-rendered brief."
        ),
    )
    parser.add_argument("--clusters-config", type=Path, default=None)
    parser.add_argument("--cluster", default=None)
    parser.add_argument(
        "--local",
        action="store_true",
        help="stand the ci/slurm container up itself (docker required; on "
        "dockerless hosts this errors with the U7 guidance).",
    )
    parser.add_argument("--keep-container", action="store_true")
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument(
        "--scenarios",
        default=",".join(_SCENARIO_NAMES),
        help=f"comma list of scenarios to run (default all: {','.join(_SCENARIO_NAMES)}).",
    )
    parser.add_argument("--container", default=_CONTAINER_NAME_DEFAULT)
    parser.add_argument("--walltime-sec", type=int, default=driver.DEFAULT_WALLTIME_SEC)
    parser.add_argument("--wait-timeout", type=int, default=1800)
    parser.add_argument("--poll-interval", type=int, default=5)
    parser.add_argument("--no-preflight", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--markdown", type=Path, default=None)
    return parser.parse_args(argv)


def _parse_scenario_selection(raw: str) -> list[str]:
    selected = [s.strip().lower() for s in raw.split(",") if s.strip()]
    unknown = [s for s in selected if s not in _SCENARIO_NAMES]
    if unknown:
        raise SandboxRefusal(
            f"unknown scenario(s) {unknown}; expected a subset of {list(_SCENARIO_NAMES)}"
        )
    # De-dup preserving order.
    seen: list[str] = []
    for name in selected:
        if name not in seen:
            seen.append(name)
    return seen


class _ScenarioContext:
    """Per-scenario state: a fresh ChainContext + its own ChainState."""

    def __init__(self, ctx: Any, state: Any) -> None:
        self._ctx = ctx
        self.state = state

    def __getattr__(self, name: str) -> Any:
        return getattr(self._ctx, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in ("_ctx", "state"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._ctx, name, value)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    started = time.time()
    run_ref = f"sandbox-u6-{time.strftime('%Y%m%d-%H%M%S', time.gmtime(started))}-{os.getpid()}"

    try:
        journal_home = require_journal_home(os.environ)
        scenarios = _parse_scenario_selection(args.scenarios)
    except SandboxRefusal as exc:
        print(f"sandbox-anomaly-matrix: REFUSED — {exc}", file=sys.stderr)
        return 2
    os.environ["HPC_JOURNAL_DIR"] = str(journal_home)

    workdir = (args.workdir or Path(tempfile.mkdtemp(prefix="hpc-anomaly-"))).resolve()
    scratch = workdir / "specs"
    scratch.mkdir(parents=True, exist_ok=True)
    out_path = args.out or (workdir / "anomaly-matrix.json")
    md_path = args.markdown or (workdir / "anomaly-matrix.md")

    env = dict(os.environ)
    env["HPC_JOURNAL_DIR"] = str(journal_home)
    env["HPC_SUBMIT_ONCE"] = "1"
    env["HPC_STATUS_POLL_INTERVAL_SEC"] = driver._ENV_POLL_INTERVAL

    cluster_name: str | None = None
    backend: str | None = None
    scenario_blocks: dict[str, Any] = {}
    local_container = False
    try:
        if args.local:
            clusters_path, shim_env = driver.ensure_local_cluster(
                workdir, keep_container=args.keep_container
            )
            env.update(shim_env)
            local_container = True
        elif args.clusters_config is not None:
            clusters_path = args.clusters_config
        else:
            print(
                "sandbox-anomaly-matrix: pass --clusters-config <ci_clusters.yaml> or --local",
                file=sys.stderr,
            )
            return 2
        env["HPC_CLUSTERS_CONFIG"] = str(clusters_path)
        config = driver.load_cluster_config(clusters_path)
        cluster_name, stanza = driver.select_cluster(config, args.cluster)
        backend = driver.stanza_backend(stanza)
        ssh_target = driver.stanza_ssh_target(stanza)
        n_samples_base = 90_000 + (os.getpid() % 9_000)
        decoy_dir = workdir / "experiment-decoy"
        decoy_dir.mkdir(parents=True, exist_ok=True)
        runners: dict[str, Callable[..., Any]] = {
            "a": run_scenario_a,
            "b": run_scenario_b,
            "c": run_scenario_c,
            "d": run_scenario_d,
        }
        for name in scenarios:
            scenario_ctx = driver.ChainContext(
                env=env,
                journal_home=journal_home,
                workdir=workdir,
                scratch=scratch / name,
                experiment_dir=None,
                cluster=cluster_name,
                configured_clusters=sorted(config),
                ssh_target=ssh_target,
                backend=backend,
                remote_path_stanza=stanza,
                goal=driver.DEFAULT_GOAL,
                run_name=f"sandbox-u6-{name}",
                run_ref=f"{run_ref}-{name}",
                wait_timeout=args.wait_timeout,
                poll_interval=args.poll_interval,
                run_preflight=not args.no_preflight,
                walltime_sec=args.walltime_sec,
            )
            scenario_ctx.container = args.container
            state = driver.ChainState()
            wrapped = _ScenarioContext(scenario_ctx, state)
            try:
                if name == "c":
                    run_scenario_c(
                        wrapped, n_samples=n_samples_base, decoy_experiment_dir=decoy_dir
                    )
                else:
                    runners[name](wrapped, n_samples=n_samples_base)
            except SandboxRefusal as exc:
                state.record(name, "scenario", f"scenario ({name}) setup", False, str(exc))
            scenario_blocks[name] = {
                "description": _SCENARIO_DESCRIPTIONS[name],
                "rows": state.rows,
            }
            (workdir / "scenarios").mkdir(parents=True, exist_ok=True)
            (workdir / "scenarios" / f"{name}.json").write_text(
                json.dumps(
                    {
                        "scenario": name,
                        "description": _SCENARIO_DESCRIPTIONS[name],
                        "rows": state.rows,
                    },
                    indent=2,
                    sort_keys=True,
                    default=str,
                )
                + "\n",
                encoding="utf-8",
            )
    except SandboxRefusal as exc:
        setup_block = scenario_blocks.setdefault(
            "setup", {"description": "matrix setup (refused before any scenario ran)", "rows": []}
        )
        setup_block["rows"].append(
            driver.build_evidence_row("setup", "driver", "matrix setup", False, str(exc))
        )
    finally:
        if local_container and not args.keep_container:
            driver.teardown_local_container()

    meta = {
        "run_ref": run_ref,
        "cluster": cluster_name,
        "scheduler": backend,
        "scenarios": ",".join(scenarios),
        "journal_home": str(journal_home),
        "started_utc": _iso_z(started),
        "duration_sec": round(time.time() - started, 1),
        "driver": "scripts/sandbox_anomaly_matrix.py (U6)",
        "jurisdiction": "rung-2: harness contract only — never cluster-environment truth",
    }
    evidence = build_matrix_evidence(meta, scenario_blocks)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    md_path.write_text(render_matrix_markdown(evidence), encoding="utf-8")

    for row in evidence["rows"]:
        mark = "PASS" if row["pass"] else "FAIL"
        detail = f"  — {row['detail']}" if row["detail"] and not row["pass"] else ""
        print(f"[{mark}] {row['step']:<26}  {row['check']}{detail}")
    print(f"\nevidence: {out_path}\nmarkdown: {md_path}")
    print(f"sandbox-anomaly-matrix: verdict {evidence['verdict'].upper()}")
    return 0 if evidence["verdict"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
