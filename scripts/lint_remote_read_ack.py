"""CI lint: a remote ``.stdout`` read must be ack-gated (positive-evidence).

Companion to ``lint_no_raw_ssh.py``. Where that keeps the *agent-facing*
surfaces from offering a raw-ssh affordance, this keeps the **ssh consumers in
core** from reading a remote ``.stdout`` as a *settled result* without the
positive-evidence ack that proves the remote shell ran the command to
completion.

Why it matters (run-12 finding 24): a severed SSH channel — NAT idle-drop
(~100s), the asyncssh idle reaper, or an expired remote deadline — delivers
``rc 0`` with truncated / empty stdout that *masquerades as a valid empty
read*. A consumer that trusts that stdout treats "the channel died" as "the
command returned nothing" — the silence-as-success class. The only guard is an
AFFIRMATIVE token: a command wrapped by
:func:`hpc_agent.infra.ssh_validation.wrap_with_ack` echoes a sentinel carrying
its exit code LAST, and :func:`~hpc_agent.infra.ssh_validation.split_ack`
(which ``scheduler_query_ran`` composes) reads the token back — its ABSENCE is
positive proof the read is UNKNOWN, never a settled "empty". See
``docs/design/connection-broker.md`` (sentinel-ack ruling + transport
inventory) and ``docs/plans/upstream-fixes-2026-07.md`` (spec B3′).

What it flags
-------------

A **function** in a scanned ``src/hpc_agent`` module that BOTH

* calls ``ssh_run(...)`` (bare ``ssh_run(`` or ``remote.ssh_run(``), and
* reads a ``.stdout`` attribute in the same function body,

UNLESS the function's module routes remote reads through one of the canonical
ack helpers — ``split_ack`` / ``wrap_with_ack`` / ``scheduler_query_ran`` — in
which case the whole module is treated as ack-aware (see below). A flagged
function whose ack-free read is a *legitimate* advisory / affirmative-token /
sentinel-clean read is cleared by a cited entry in :data:`ALLOWLIST`, keyed by
``<scan-root-relative path>::<function>``.

Deliberately NOT flagged
------------------------

* A ``.stdout`` that is NOT on an ssh_run result (e.g. a local
  ``subprocess.run`` result, or a ``Popen`` pipe pumped by ``_tar_ssh_push``).
  The candidate rule is intentionally coarse — "same function calls ssh_run AND
  reads some ``.stdout``" — and the ALLOWLIST absorbs the legitimately-fine
  ones; no dataflow is attempted.
* Any function in a module that references an ack helper. Granularity is
  per-module for the *ack detection* (a module that ack-routes ANY read is
  trusted as ack-aware — the ack-clean sibling reads like
  ``cluster_status.ssh_marker_scan`` ride the module's positive-evidence
  posture), and per-function for the *allowlist keys*. This is the "same
  function (or module) also references an ack helper" rule (spec B3′); it is
  what lets ``cluster_status`` / ``aggregate/runner`` / ``reconcile`` /
  ``verify_submitted`` be detected clean rather than allowlisted.
* ``infra/ssh_validation.py`` itself — it DEFINES the helpers.
* ``infra/remote.py`` — the ``ssh_run`` seam definition reads its subprocess
  ``.stdout`` internally but never *calls* ``ssh_run(...)`` (only ``def
  ssh_run`` + ``engine_ssh_run``), so it is not a candidate.

Scope
-----

Every ``src/hpc_agent/**/*.py`` file (tests live outside ``src`` and are not
scanned). ``infra/ssh_validation.py`` is skipped.

ALLOWLIST escape valve
----------------------

A genuine advisory / sentinel-clean read adds a cited entry to
:data:`ALLOWLIST` (scan-root-relative ``path::function``) — the same escape
valve ``lint_no_raw_ssh.py`` / ``lint_backend_boundary.py`` use. The seeded
entries below are drawn from ``connection-broker.md``'s "Seams left
sentinel-clean" list plus the B3 low-severity observation sites named in spec
B3′ (verify_canary absent-vs-unreadable, inspect-deployment, stray-sweep /
dir-digest advisory reads).

Every violation surfaces a ``path:lineno: remote-read not ack-gated: ...`` line
and the script exits 1. The fire path is pinned by
``tests/scripts/test_lint_remote_read_ack.py``.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCAN_ROOT = REPO / "src"
_PKG_ROOT = SCAN_ROOT / "hpc_agent"

# The canonical ack helpers. A module that references any of these (as a call
# name, attribute, or import) is treated as ack-aware for the whole module.
ACK_HELPERS: frozenset[str] = frozenset(
    {
        "split_ack",
        "wrap_with_ack",
        "scheduler_query_ran",
    }
)

# The module that DEFINES the helpers — skipped entirely.
_SKIP_RELPATH = "hpc_agent/infra/ssh_validation.py"

# Cited exemptions: scan-root-relative ``path::function`` of an ack-free remote
# read that is a legitimate advisory / affirmative-token / sentinel-clean read.
# Add an entry only as a reviewed decision citing why the read cannot report a
# false settled result. Authority: docs/design/connection-broker.md ("Seams
# left sentinel-clean") + docs/plans/upstream-fixes-2026-07.md spec B3′.
ALLOWLIST: frozenset[str] = frozenset(
    {
        # memo fingerprint (O3, latency rank 17): validates stdout to EXACTLY one
        # 64-hex line; any truncation/severed-channel deviation -> None -> the
        # memo goes inert (never serves a cached verdict) - sentinel-clean.
        "hpc_agent/ops/aggregate_flow.py::_remote_tree_fingerprint",
        # Advisory task-log fetch: reads log tails for human display, not a
        # run-state verdict; an empty/truncated read degrades to "no logs",
        # never a settled success.
        "hpc_agent/infra/cluster_logs.py::fetch_task_logs",
        # Advisory GPU-availability scan (qstat parse) — a best-effort
        # scheduling hint that returns None on any failure, not a run-state
        # verdict.
        "hpc_agent/infra/gpu.py::_run_qstat",
        # inspect-deployment advisory cluster snapshot read (B3 low-severity
        # observation site); the transport wrapper for a human-facing inspect.
        "hpc_agent/infra/inspect/_common.py::run",
        # Preflight uv probe — an empty/failed read FAILS the preflight closed
        # (positive check: rc!=0 or empty stdout ⇒ not-ready), never reads
        # absence as present.
        "hpc_agent/infra/runtime_preflight.py::runtime_uv_preflight",
        # Advisory dir-digest read (B3 low-severity per spec B3′) — a
        # best-effort directory census that fails open, not a state verdict.
        "hpc_agent/ops/dir_digest.py::_digest_remote",
        # inspect-deployment advisory read (B3 low-severity per spec B3′).
        "hpc_agent/ops/inspect_deployment.py::inspect_deployment",
        # Cluster-announce marker read (crash-only monitoring, G1 Phase 1): a
        # truncated/empty readdir yields FEWER announcements, never a false
        # terminal — the watch treats a missing announcement as "not yet",
        # not "done".
        "hpc_agent/ops/monitor/announce.py::read_announcements",
        # migrate census (M-CENSUS): same positive-evidence _ANNOUNCE_IDS_ACK
        # discipline as read_announcements — echoes the ack on cd-success and
        # reads only when present, so a severed channel yields present=False,
        # never a false-empty done-set. Custom ack token the matcher misses.
        "hpc_agent/ops/monitor/announce.py::read_announced_task_ids",
        # Per-host BATCHED census (F4, latency-elimination 2.6): self-ack-gated on
        # ``_BATCH_ACK`` (echoed only after the batch shell ran) — an absent batch
        # ack degrades EVERY run to not-present (fall-through), never a spurious
        # zero, so it carries the same severed-vs-empty protection as the per-run
        # reader above; a missing per-run ``present`` row reads "not yet", not "done".
        "hpc_agent/ops/monitor/announce.py::read_announcements_batch",
        # Affirmative-token remote-python resolution: the command ends
        # ``|| echo python3`` so a token is always emitted; absence is a
        # transport failure, not a settled empty answer.
        "hpc_agent/ops/monitor/watcher_install.py::_resolve_remote_python",
        # Advisory cron-binary capability probe (content-classified,
        # best-effort); an unreadable probe surfaces as "binary unusable".
        "hpc_agent/ops/monitor/watcher_install.py::_probe_cron_binary",
        # Affirmative-token install verify (``grep -Fq <marker> && echo YES ||
        # echo NO``) — sentinel-clean per connection-broker.md
        # (monitor.watcher_install); keyed on the YES token, not exit code.
        "hpc_agent/ops/monitor/watcher_install.py::_cron_has_marker",
        # Affirmative echo-ok round-trip: success is keyed on the literal "ok"
        # token in stdout, so a truncated/empty read is not-ok (positive
        # evidence), never a settled pass.
        "hpc_agent/ops/preflight/check.py::_cluster_ssh_echo_check",
        # Preflight combined cluster probe (best-effort diagnostic): a failed
        # read surfaces as a not-ok check, never a settled success.
        "hpc_agent/ops/preflight/check.py::_cluster_combined_probe",
        # Stray-sweep advisory ps read (B3 low-severity per spec B3′) — a
        # best-effort orphan scan; an empty read reaps nothing, never a verdict.
        "hpc_agent/ops/recover/stray_sweep.py::stray_sweep",
        # verify_canary absent-vs-unreadable (B3 low-severity per spec B3′):
        # remote checkpoint existence probe; degrades conservatively, never
        # reads a truncated read as "no checkpoint → settled".
        "hpc_agent/ops/verify_canary.py::_verify_remote_checkpoint",
        # verify_canary absent-vs-unreadable (B3 low-severity per spec B3′):
        # canary exit-code sentinel read; conservative on absence.
        "hpc_agent/ops/verify_canary.py::_read_canary_exit_code",
        # verify_canary metrics-fingerprint sha read — best-effort
        # (``sha.stdout.strip() or None``), an advisory fingerprint sample, not
        # a run-state verdict.
        "hpc_agent/ops/verify_canary.py::verify_canary",
    }
)


def _call_name(func: ast.expr) -> str | None:
    """Return the callee name for ``ssh_run(...)`` / ``x.ssh_run(...)``."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _module_has_ack(tree: ast.AST) -> bool:
    """True iff the module references an ack helper (call, attribute, import).

    Per-module granularity: a module that ack-routes ANY remote read is trusted
    as ack-aware, so its sentinel-clean sibling reads ride that posture without
    an ALLOWLIST entry (spec B3′ "same function (or module)").
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in ACK_HELPERS:
            return True
        if isinstance(node, ast.Attribute) and node.attr in ACK_HELPERS:
            return True
        if isinstance(node, ast.ImportFrom) and any(
            alias.name in ACK_HELPERS for alias in node.names
        ):
            return True
    return False


class _Scope:
    """A function (or the module) accumulating ssh_run / .stdout evidence."""

    __slots__ = ("name", "lineno", "has_ssh", "has_stdout")

    def __init__(self, name: str, lineno: int) -> None:
        self.name = name
        self.lineno = lineno
        self.has_ssh = False
        self.has_stdout = False


def _candidate_scopes(tree: ast.Module) -> list[_Scope]:
    """Return every scope (function or module) reading an ssh_run ``.stdout``.

    Evidence is attributed to the INNERMOST enclosing scope, so a closure's
    ssh_run call is charged to the closure, not its outer function. A candidate
    has BOTH an ssh_run call and a ``.stdout`` read in its own body.
    """
    module_scope = _Scope("<module>", 1)
    scopes: list[_Scope] = [module_scope]

    def walk(node: ast.AST, scope: _Scope) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_scope = _Scope(child.name, child.lineno)
                scopes.append(child_scope)
                walk(child, child_scope)
                continue
            if isinstance(child, ast.Call) and _call_name(child.func) == "ssh_run":
                scope.has_ssh = True
            if isinstance(child, ast.Attribute) and child.attr == "stdout":
                scope.has_stdout = True
            walk(child, scope)

    walk(tree, module_scope)
    return [s for s in scopes if s.has_ssh and s.has_stdout]


def _relpath(path: Path, scan_root: Path) -> str:
    """Scan-root-relative posix path (forward slashes, for ALLOWLIST keys)."""
    try:
        return path.resolve().relative_to(scan_root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def lint_file(path: Path, scan_root: Path | None = None) -> list[tuple[int, str]]:
    """Return ``(lineno, message)`` per un-ack-gated remote read in *path*.

    A file whose module ack-routes (references an ack helper) returns ``[]``
    without further inspection. ``ssh_validation.py`` returns ``[]`` too.
    Keys are relative to *scan_root* (defaults to :data:`SCAN_ROOT`) so the
    ALLOWLIST matches whether the real tree or a tmp fixture is scanned.
    """
    root = scan_root if scan_root is not None else SCAN_ROOT
    rel = _relpath(path, root)
    if rel.endswith(_SKIP_RELPATH):
        return []
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    if _module_has_ack(tree):
        return []
    findings: list[tuple[int, str]] = []
    for scope in _candidate_scopes(tree):
        key = f"{rel}::{scope.name}"
        if key in ALLOWLIST:
            continue
        findings.append(
            (
                scope.lineno,
                f"remote-read not ack-gated: {scope.name!r} calls ssh_run(...) and "
                f"reads .stdout without routing it through wrap_with_ack / split_ack "
                f"/ scheduler_query_ran. A severed channel returns rc 0 with "
                f"truncated stdout that masquerades as a valid empty read (run-12 "
                f"finding 24). Route the read through the ack helpers, or add a cited "
                f"ALLOWLIST entry ({key!r}) if this is an advisory / sentinel-clean "
                f"read.",
            )
        )
    findings.sort(key=lambda f: f[0])
    return findings


def iter_targets(scan_root: Path) -> list[Path]:
    """Yield every scanned ``hpc_agent`` Python source file under *scan_root*."""
    pkg = scan_root / "hpc_agent"
    if not pkg.exists():
        return []
    return sorted(p for p in pkg.rglob("*.py") if p.is_file())


def main(scan_root: Path | None = None) -> int:
    root = scan_root if scan_root is not None else SCAN_ROOT
    failures = 0
    for path in iter_targets(root):
        for lineno, hint in lint_file(path, root):
            try:
                disp = path.resolve().relative_to(REPO).as_posix()
            except ValueError:
                disp = path.as_posix()
            print(f"{disp}:{lineno}: {hint}")
            failures += 1
    if failures:
        print(
            f"\n{failures} un-ack-gated remote read(s). A severed SSH channel "
            f"returns rc 0 with truncated stdout that reads as a valid empty result "
            f"(run-12 finding 24). Route the read through "
            f"wrap_with_ack / split_ack / scheduler_query_ran so the ack's absence "
            f"is positive UNKNOWN, or add a cited ALLOWLIST entry in "
            f"scripts/lint_remote_read_ack.py for an advisory / sentinel-clean read.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
