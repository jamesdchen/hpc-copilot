#!/usr/bin/env python3
"""Launch ``mutmut run`` under an ``os.wait()`` guard that ignores stray children.

**Why this launcher exists (curated mutation lane ``consent-hint``, run 29618964851).**
mutmut 3.6.0's mutation-run loop reaps its per-mutant worker processes with a bare
``os.wait()`` and then looks the returned pid up in its worker table WITHOUT
guarding the lookup::

    # mutmut/__main__.py, read_one_child_exit_status (nested in _run)
    pid, wait_status = os.wait()
    source_file_mutation_data_by_pid[pid].register_result(...)   # KeyError on a stray pid

``os.wait()`` reaps ANY child of the run process, not only mutmut's mutant
workers. mutmut runs its STATS phase pytest *in-process* (``PytestRunner.run_stats``
does ``pytest.main(...)`` in the run process itself), so any subprocess a covering
test spawns during stats is a direct child of the run process. If such a subprocess
outlives its immediate parent -- e.g. a fixture/plugin probe (``pandoc --version``,
a ``python -m`` warm-up) whose launcher exits while a descendant lingers, so the
descendant reparents to the run process -- then during the mutation-run phase
``os.wait()`` returns that stray pid, the unguarded table lookup raises ``KeyError``,
and mutmut aborts the ENTIRE curated module on the very FIRST reap: every mutant is
left ``exit_code=null`` ("not checked"), the module reads dark, and the curated
zero-signal tripwire (correctly) turns the job RED. The traceback lands on *stderr*,
which the workflow's ``... | tee`` dropped, so it read as a silent ``exit 1``.

**What the guard does.** It records the pids mutmut itself ``os.fork()``s (its mutant
workers -- ``subprocess``/``multiprocessing`` create children via ``_posixsubprocess``
/ their own machinery, NOT Python ``os.fork``, so a covering-test subprocess
descendant is never in this set) and makes ``os.wait()`` reap-and-DROP any pid that
is not one of them. A stray grandchild is still reaped (so it can't zombie), but its
pid is never handed to mutmut's worker-table lookup. Genuine mutant workers are
returned unchanged, so a healthy run behaves EXACTLY as before -- the guard only ever
changes behaviour on the pathological stray-reap that would otherwise crash the run.

POSIX-only (``os.fork`` / ``os.wait``); the curated runner gates mutmut to Linux, so
this launcher only ever runs there. It is a faithful stand-in for ``python -m mutmut
run`` (same ``cli()`` entry point, same argv, same exit semantics) with the guard
installed first.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable


def install_wait_guard() -> None:
    """Patch ``os.fork`` / ``os.wait`` so ``os.wait()`` drops non-mutmut children.

    Idempotent-safe to call once before handing control to mutmut. A no-op on
    platforms without ``os.fork`` / ``os.wait`` (e.g. Windows), where mutmut cannot
    run at all. ``os.fork`` / ``os.wait`` are read via three-arg ``getattr`` (and the
    reassignments carry ``type: ignore``) because they are absent from the win32
    ``os`` stubs mypy type-checks against on this box.
    """
    orig_fork: Callable[[], int] | None = getattr(os, "fork", None)
    orig_wait: Callable[[], tuple[int, int]] | None = getattr(os, "wait", None)
    if orig_fork is None or orig_wait is None:
        return

    forked: set[int] = set()

    def tracking_fork() -> int:
        pid = orig_fork()
        if pid > 0:  # parent: remember the child we just forked
            forked.add(pid)
        return pid

    def guarded_wait() -> tuple[int, int]:
        while True:
            pid, status = orig_wait()
            if pid in forked:
                forked.discard(pid)
                return pid, status
            # A child mutmut never forked (a covering-test subprocess descendant
            # reparented to the run process): reaped by the os.wait() above so it
            # can't zombie, then DROPPED here so mutmut's worker-table lookup never
            # sees an unknown pid. Loop to wait for a genuine worker.

    os.fork = tracking_fork  # type: ignore[attr-defined]
    os.wait = guarded_wait  # type: ignore[attr-defined]


def main() -> None:
    install_wait_guard()
    # Imported here, NOT at module top: mutmut/__main__ hard ``sys.exit(1)``s at
    # import on Windows, and this module must stay importable there (its unit test
    # runs on every platform; the guard's fork behaviour is POSIX-only).
    from mutmut.__main__ import cli

    sys.argv = ["mutmut", "run"]
    cli()  # click group: parses argv, runs the ``run`` command, sets the exit code


if __name__ == "__main__":
    main()
