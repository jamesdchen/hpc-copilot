"""Contract: every blocking subprocess invocation in ``src/hpc_agent`` is bounded.

The src-side extension of
:mod:`tests.contracts.test_subprocess_timeout_discipline` (which holds the
same line for ``tests/``). The motivating incident is proving run #3's
all-night wedge (2026-07-04): a subprocess call whose timeout could not
fire parked the submit-flow driver for hours. ``infra/remote.py``'s
``_capture_windows`` docstring records the root cause; this contract keeps
every OTHER blocking call site from re-growing the same hazard.

The rule, per call form:

- ``subprocess.run`` / ``call`` / ``check_call`` / ``check_output`` must
  carry a ``timeout=`` kwarg at the call site (a ``**kwargs`` splat does
  NOT count — the bound must be visible to this AST check, same standard
  as the tests-side contract).
- ``subprocess.Popen`` is flagged unconditionally UNLESS the enclosing
  function is enumerated in :data:`_EXEMPT_BY_DESIGN` — whether a Popen is
  bounded (a later ``communicate(timeout=)`` / ``wait(timeout=)``, a
  scheduler wall-clock, a deliberate detach) is not syntactically
  decidable, so every Popen site needs a cited exemption.
- ``<proc>.communicate(...)`` without ``timeout=`` is flagged — the
  Popen-with-communicate face of the same unbounded wait.

``timeout=None`` (or any dynamic expression) at a call site satisfies the
syntactic check, as in the tests-side contract: the contract pins that the
author *addressed* the bound, not the value chosen.

Two lists, with different growth rules:

- :data:`_EXEMPT_BY_DESIGN` — compliant wrappers and deliberate
  unbounded-by-design sites. Each entry carries a citation; entries may
  live forever but adding one is a reviewed design decision.
- :data:`_GRANDFATHERED` — real violations that predate the contract and
  are owned elsewhere or need a non-obvious bound. DO NOT GROW; shrink
  toward empty.
"""

from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR = _REPO_ROOT / "src" / "hpc_agent"

_BLOCKING_FUNCS = {"run", "call", "check_call", "check_output"}

# --- exempt by design: (repo-relative path) -> enclosing function names ----
#
# A finding is exempt when ANY function on its enclosing-def stack matches.
# Every entry must cite WHY the site is bounded (or deliberately unbounded).
_EXEMPT_BY_DESIGN: dict[str, set[str]] = {
    # The kit reference adapter's start_background IS the detached-worker
    # capability under test: a background worker deliberately outlives the call
    # (await_wake carries the bounded timeout_s instead).
    "src/hpc_agent/conformance/adapters/claude_code.py": {"start_background"},
    # The compliant capture wrappers themselves — the S2-wedge fix. Every
    # wait inside is bounded: `_capture_windows` kills on deadline then
    # drains for at most _POST_KILL_DRAIN_SEC; `capture_via_select` bounds
    # the select loop via `_communicate_select(timeout=...)`. These are the
    # seams `ssh_run` funnels through; callers inherit the discipline.
    "src/hpc_agent/infra/remote.py": {"_capture_windows", "capture_via_select"},
    # The cross-platform bounded-capture wrapper (2026-07-05 Hoffman2 orphan
    # fix). The Popen is immediately bounded by `communicate(timeout=...)`, and
    # on timeout the WHOLE process tree is killed (POSIX `os.killpg` / Windows
    # `taskkill /T`) before a bounded drain — so no grandchild (a shelled
    # `hpc-agent` → `ssh`) can hold the stdout pipe past the deadline. Sibling
    # to remote.py's capture seams; the composite-preflight verbs funnel here.
    "src/hpc_agent/infra/bounded_subprocess.py": {"run_capture_bounded"},
    # Deliberately detached workers (the `_spawn_detached` path): the child
    # is MEANT to outlive this process; its lifetime is bounded by the
    # single-lease + doctor-watchdog machinery, not by a parent-side wait.
    "src/hpc_agent/_kernel/lifecycle/detached.py": {"_popen_detached"},
    # tar->ssh streaming push: the local `tar c` Popen's read end feeds
    # `run_capture_bounded(ssh_cmd, timeout_sec=timeout, stdin=...)`, whose
    # tree-kill reaps the ssh grandchild on the deadline; the paired
    # `tar_proc.wait(timeout=timeout)` and the except-arm `tar_proc.kill()`
    # bound the tar half. (Was a bare `subprocess.run(..., timeout=)` — NOT a
    # hard deadline on Windows for an ssh-spawning call; run #7 S2 staging
    # wedge, 2026-07-05. See _BOUNDED_RUNNER_REQUIRED below.)
    # The engine stayed in transport/__init__.py when the module became a package.
    "src/hpc_agent/infra/transport/__init__.py": {"_tar_ssh_push"},
    # ssh->tar streaming PULL (latency ranks 2 + 7): the inverse of
    # `_tar_ssh_push`. The `ssh` Popen (the archive SOURCE) feeds the pump into
    # `run_capture_bounded(tar_x_cmd, timeout_sec=timeout, stdin=...)` — the SINK
    # — whose tree-kill reaps the ssh grandchild on the deadline; the paired
    # `ssh_proc.wait(timeout=timeout)` and the except-arm `ssh_proc.kill()` bound
    # the ssh half. Same discipline as `_tar_ssh_push`, mirrored for the pull.
    "src/hpc_agent/infra/transport/_pull.py": {"_pull_transfer"},
    # Cluster-side dispatcher launching the user's payload: runtime is the
    # task's own runtime, bounded by the scheduler's wall-clock (h_rt /
    # --time) on the job, and heartbeat-monitored — a parent-side timeout
    # would re-implement the scheduler's job.
    "src/hpc_agent/execution/mapreduce/dispatch.py": {"main"},
    # Interactive TTY handover to the human's pager (`less`); returns when
    # the human quits. A timeout here would kill the pager mid-read.
    "src/hpc_agent/execution/mapreduce/reduce/tui.py": {"_open_log"},
    # (The phase-1 ssh broker's persistent-channel exemption lived here until
    # the broker was retired + deleted 2026-07-07 — see
    # docs/design/connection-broker.md for the retirement record.)
}

# --- grandfathered real violations: (repo-relative path, function) ---------
#
# Each is a genuine unbounded blocking call that predates this contract.
# DO NOT GROW. Shrink by giving the site a real bound (see the per-entry
# notes) and removing its entry.
_GRANDFATHERED: set[tuple[str, str]] = set()
# Emptied 2026-07-04: the three original entries were fixed the same wave —
# `block_drive._run_block_verb` and `mcp_server._subprocess_cli_runner` now
# route through `infra.remote.capture_via_select` with a per-verb deadline
# from `infra.block_chain.verb_deadline_seconds` (watch-class verbs get their
# spec's wall_clock_budget + slack), and `drive._run_cli_step` passes the same
# deadline as `timeout=` (stdio inherited — no pipe wedge to drain).


# --- must route through the tree-kill bounded runner: (path) -> functions ---
#
# Transport-layer ssh/rsync/tar/scp pushes+pulls where a bare
# `subprocess.run(..., timeout=)` is NOT sufficient: rsync/tar spawn `ssh` as a
# GRANDCHILD and subprocess.run's post-kill `communicate()` is unbounded on
# Windows, so the deadline cannot fire (run #7 S2 staging wedge, 2026-07-05 —
# a detached submit-s2 worker parked with a 0-byte log, stuck in `_tar_ssh_push`
# staging to Hoffman2). The syntactic scan above treats ANY timeout=-bearing
# `subprocess.run` as compliant, so it cannot catch a regrowth here; this list
# forbids blocking `subprocess.*` in these sites outright and asserts
# `run_capture_bounded` is actually wired. See
# `test_transport_ssh_sites_route_through_bounded_runner`.
_BOUNDED_RUNNER_REQUIRED: dict[str, set[str]] = {
    # The ssh/rsync/tar sites are engine functions that stayed in
    # transport/__init__.py when the module became a package.
    "src/hpc_agent/infra/transport/__init__.py": {
        "_remote_preclean",
        "_tar_ssh_push",
        "rsync_push",
        "_rsync_deploy",
        "rsync_pull",
    },
    # The rsync-less PULL engine (latency ranks 2 + 7) replaced the old
    # ``_scp_pull`` with a tar|ssh transfer + a small ssh manifest round-trip;
    # both drive ssh as a GRANDCHILD and must ride the tree-kill bounded runner.
    "src/hpc_agent/infra/transport/_pull.py": {
        "_pull_transfer",
        "_ssh_capture",
    },
}


def _scan_source(source: str) -> list[tuple[int, str, tuple[str, ...]]]:
    """Return raw findings ``(lineno, reason, enclosing-def stack)`` for *source*.

    Pure syntactic scan — exemption/grandfather policy is applied by the
    caller, so the fire-path test can exercise this directly.
    """
    tree = ast.parse(source)
    findings: list[tuple[int, str, tuple[str, ...]]] = []
    stack: list[str] = []

    def _has_timeout(call: ast.Call) -> bool:
        return any(kw.arg == "timeout" for kw in call.keywords)

    def _visit(node: ast.AST) -> None:
        pushed = False
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.append(node.name)
            pushed = True
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                owner = func.value
                if isinstance(owner, ast.Name) and owner.id == "subprocess":
                    if func.attr in _BLOCKING_FUNCS and not _has_timeout(node):
                        findings.append(
                            (node.lineno, f"subprocess.{func.attr} without timeout=", tuple(stack))
                        )
                    elif func.attr == "Popen":
                        findings.append((node.lineno, "subprocess.Popen", tuple(stack)))
                elif func.attr == "communicate" and not _has_timeout(node):
                    findings.append((node.lineno, ".communicate() without timeout=", tuple(stack)))
        for child in ast.iter_child_nodes(node):
            _visit(child)
        if pushed:
            stack.pop()

    _visit(tree)
    return findings


def _classify(
    rel: str, findings: list[tuple[int, str, tuple[str, ...]]]
) -> tuple[list[str], set[tuple[str, str]]]:
    """Split *findings* into (violation messages, matched grandfather keys)."""
    exempt_funcs = _EXEMPT_BY_DESIGN.get(rel, set())
    violations: list[str] = []
    matched: set[tuple[str, str]] = set()
    for lineno, reason, stack in findings:
        if any(name in exempt_funcs for name in stack):
            continue
        grandfather_keys = {(rel, name) for name in stack} & _GRANDFATHERED
        if grandfather_keys:
            matched |= grandfather_keys
            continue
        where = stack[-1] if stack else "<module>"
        violations.append(f"  {rel}:{lineno} in {where}: {reason}")
    return violations, matched


def _iter_src_files() -> list[Path]:
    assert _SRC_DIR.is_dir(), f"src tree not found at {_SRC_DIR}"
    return sorted(_SRC_DIR.rglob("*.py"))


def test_src_blocking_subprocess_calls_are_bounded() -> None:
    """No new unbounded blocking subprocess call in ``src/hpc_agent``.

    New call sites must pass ``timeout=``, route through a compliant seam
    (``infra.remote.ssh_run`` / the capture helpers), or — for a genuinely
    unbounded-by-design site — add a cited :data:`_EXEMPT_BY_DESIGN` entry
    in a reviewed change. Don't append to :data:`_GRANDFATHERED`.
    """
    violations: list[str] = []
    for path in _iter_src_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        try:
            findings = _scan_source(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        violations += _classify(rel, findings)[0]

    assert not violations, (
        "Unbounded blocking subprocess call(s) in src/hpc_agent — the "
        "proving-run-#3 wedge class. Pass timeout=, route through "
        "infra.remote.ssh_run / its capture seams, or (for a deliberate "
        "design) add a cited _EXEMPT_BY_DESIGN entry:\n" + "\n".join(violations)
    )


def test_grandfathered_entries_still_offend() -> None:
    """Keep :data:`_GRANDFATHERED` honest — prune entries that were fixed."""
    live: set[tuple[str, str]] = set()
    for path in _iter_src_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        if all(g_rel != rel for g_rel, _ in _GRANDFATHERED):
            continue
        try:
            findings = _scan_source(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        live |= _classify(rel, findings)[1]

    stale = _GRANDFATHERED - live
    assert not stale, (
        "Stale _GRANDFATHERED entries — the call site was fixed, moved, or "
        "renamed; remove the entry so the set shrinks toward empty:\n"
        + "\n".join(f"  {rel}: {func}" for rel, func in sorted(stale))
    )


def test_rule_fires_on_synthetic_violation() -> None:
    """The repo standard: every lint rule demonstrates its fire path."""
    synthetic = (
        "import subprocess\n"
        "def bad_run():\n"
        "    subprocess.run(['x'])\n"
        "def bad_splat(**kw):\n"
        "    subprocess.check_output(['x'], **kw)\n"
        "def bad_popen():\n"
        "    p = subprocess.Popen(['x'])\n"
        "    p.communicate()\n"
        "def good():\n"
        "    subprocess.run(['x'], timeout=5)\n"
        "    subprocess.check_call(['x'], timeout=None)\n"
    )
    findings = _scan_source(synthetic)
    reasons = {(reason, stack[-1]) for _, reason, stack in findings}
    assert ("subprocess.run without timeout=", "bad_run") in reasons
    assert ("subprocess.check_output without timeout=", "bad_splat") in reasons, (
        "a **kwargs splat must not satisfy the timeout check"
    )
    assert ("subprocess.Popen", "bad_popen") in reasons
    assert (".communicate() without timeout=", "bad_popen") in reasons
    assert not any(stack and stack[-1] == "good" for _, _, stack in findings), (
        "explicit timeout= (any value) satisfies the syntactic check"
    )
    # And the policy layer: an unlisted file turns findings into violations,
    # while grandfathered/exempt functions are filtered.
    violations, _ = _classify("src/hpc_agent/synthetic.py", findings)
    assert len(violations) == 4


def test_exempt_by_design_entries_still_exist() -> None:
    """Cited exemptions must point at real functions — prune on rename/removal."""
    stale: list[str] = []
    for rel, funcs in sorted(_EXEMPT_BY_DESIGN.items()):
        path = _REPO_ROOT / rel
        if not path.is_file():
            stale.append(f"  {rel}: file no longer exists")
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = {
            n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for func in sorted(funcs - defined):
            stale.append(f"  {rel}: no function named {func}")

    assert not stale, "Stale _EXEMPT_BY_DESIGN entries — update or remove:\n" + "\n".join(stale)


def _bounded_runner_audit(func_node: ast.AST) -> tuple[bool, bool]:
    """``(has_blocking_subprocess, calls_run_capture_bounded)`` over *func_node*'s
    whole subtree — nested ``_attempt`` / ``_run`` helpers included."""
    has_blocking = False
    calls_bounded = False
    for n in ast.walk(func_node):
        if not isinstance(n, ast.Call):
            continue
        func = n.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
            and func.attr in _BLOCKING_FUNCS
        ):
            has_blocking = True
        if isinstance(func, ast.Name) and func.id == "run_capture_bounded":
            calls_bounded = True
    return has_blocking, calls_bounded


def test_transport_ssh_sites_route_through_bounded_runner() -> None:
    """Transport ssh/rsync/tar/scp pushes+pulls must use the tree-kill
    ``run_capture_bounded``, never a bare ``subprocess.run(timeout=)`` whose
    deadline can't fire on Windows (run #7 S2 staging wedge, 2026-07-05)."""
    problems: list[str] = []
    for rel, required in sorted(_BOUNDED_RUNNER_REQUIRED.items()):
        path = _REPO_ROOT / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        by_name = {
            n.name: n
            for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        for func in sorted(required):
            node = by_name.get(func)
            if node is None:
                problems.append(f"  {rel}: no function named {func} (renamed? update the set)")
                continue
            has_blocking, calls_bounded = _bounded_runner_audit(node)
            if has_blocking:
                problems.append(
                    f"  {rel}:{func}: blocking subprocess.* present — route the "
                    "ssh/rsync/tar/scp call through run_capture_bounded"
                )
            if not calls_bounded:
                problems.append(
                    f"  {rel}:{func}: no run_capture_bounded call — the bounded-"
                    "runner wiring is missing"
                )
    assert not problems, (
        "Transport ssh sites must funnel through the tree-kill bounded runner "
        "(run #7 S2 staging wedge):\n" + "\n".join(problems)
    )


def test_bounded_runner_audit_fires() -> None:
    """Fire path: the audit flags a bare subprocess.run and a missing wrapper."""
    bad = ast.parse(
        "import subprocess\ndef push():\n    subprocess.run(['ssh', 'x'], timeout=60)\n"
    )
    bad_fn = next(n for n in ast.walk(bad) if isinstance(n, ast.FunctionDef))
    assert _bounded_runner_audit(bad_fn) == (True, False)

    good = ast.parse("def push():\n    return run_capture_bounded(['ssh', 'x'], timeout_sec=60)\n")
    good_fn = next(n for n in ast.walk(good) if isinstance(n, ast.FunctionDef))
    assert _bounded_runner_audit(good_fn) == (False, True)


# ── WS-INPROC in-process-eligibility carve-out (enforcement-map row 7) ──────────
#
# ``block_drive._IN_PROCESS_ELIGIBLE_VERBS`` names the block verbs a driver span
# may dispatch IN-PROCESS instead of spawning a subprocess. The carve-out is
# ENCODED, not blanket: an eligible verb must be a LOCAL, decision/state-only
# block — it must NOT shell ssh (which would wedge the synchronous MCP server) nor
# block on a watch/wait. This guard turns RED the instant an ssh-reaching or
# WATCH verb is planted into the set.

# Side-effect kinds that mean a verb reaches the cluster (ssh transport /
# scheduler mutation / a remote sync). Any of these on an eligible verb is a
# violation.
_CLUSTER_SIDE_EFFECT_KINDS = {"ssh", "scheduler-submit", "sync-pull"}


def _in_process_eligibility_violation(
    verb: str, *, requires_ssh: bool, side_effect_kinds: set[str], is_watch: bool
) -> str | None:
    """Pure policy: why *verb* may NOT be in-process-eligible, or ``None`` if it may.

    Split out so the fire path can exercise the rule without the live registry
    (the same pure-scan / policy split the subprocess-discipline check uses).
    """
    if is_watch:
        return f"{verb} is a WATCH verb — it blocks on a poll and must stay a subprocess"
    if requires_ssh:
        return f"{verb} declares requires_ssh — an ssh-shelling verb must stay a subprocess"
    cluster = side_effect_kinds & _CLUSTER_SIDE_EFFECT_KINDS
    if cluster:
        return f"{verb} declares cluster side-effect(s) {sorted(cluster)} — must stay a subprocess"
    return None


def test_in_process_eligible_verbs_are_local_and_never_watch() -> None:
    """No ssh-reaching / WATCH verb may sit in ``_IN_PROCESS_ELIGIBLE_VERBS``.

    The planted-violation guard for the WS-INPROC carve-out: an in-process span
    reuses the synchronous server's thread, so a member that shells ssh or blocks
    on a watch would wedge it. Each member is validated against its LIVE primitive
    metadata (declared side-effects + requires_ssh) and ``block_chain.WATCH_VERBS``.
    """
    from hpc_agent._kernel.lifecycle import block_drive
    from hpc_agent._kernel.registry.primitive import get_meta, register_single_module
    from hpc_agent.cli._verb_module_map import VERB_MODULE_MAP
    from hpc_agent.infra import block_chain

    violations: list[str] = []
    for verb in sorted(block_drive._IN_PROCESS_ELIGIBLE_VERBS):
        entry = VERB_MODULE_MAP.get(verb)
        assert entry is not None, f"{verb} is not in VERB_MODULE_MAP — cannot dispatch in-process"
        primitive_name, module_name = entry
        register_single_module(module_name)
        meta = get_meta(primitive_name)
        shape = meta.cli
        kinds = {se.kind for se in (meta.side_effects or [])}
        reason = _in_process_eligibility_violation(
            verb,
            requires_ssh=bool(getattr(shape, "requires_ssh", False)),
            side_effect_kinds=kinds,
            is_watch=verb in block_chain.WATCH_VERBS,
        )
        if reason is not None:
            violations.append("  " + reason)

    assert not violations, (
        "In-process-eligible block verb(s) reach the cluster or block on a watch — "
        "the WS-INPROC carve-out is LOCAL/decision-only. Remove them from "
        "_IN_PROCESS_ELIGIBLE_VERBS (they keep the subprocess seam):\n" + "\n".join(violations)
    )


def test_in_process_eligibility_rule_fires_on_planted_ssh_and_watch() -> None:
    """Fire path: an ssh-reaching or WATCH verb planted into the set is rejected."""
    # An ssh-shelling verb (requires_ssh) is rejected.
    assert _in_process_eligibility_violation(
        "aggregate-check", requires_ssh=True, side_effect_kinds={"ssh"}, is_watch=False
    )
    # A scheduler-mutating verb is rejected on its side-effect alone.
    assert _in_process_eligibility_violation(
        "campaign-refill",
        requires_ssh=False,
        side_effect_kinds={"scheduler-submit"},
        is_watch=False,
    )
    # A WATCH verb is rejected even with no declared side-effect.
    assert _in_process_eligibility_violation(
        "campaign-watch", requires_ssh=False, side_effect_kinds=set(), is_watch=True
    )
    # A genuinely local, decision-only verb is accepted.
    assert (
        _in_process_eligibility_violation(
            "campaign-complete",
            requires_ssh=False,
            side_effect_kinds={"writes-campaign-state"},
            is_watch=False,
        )
        is None
    )
