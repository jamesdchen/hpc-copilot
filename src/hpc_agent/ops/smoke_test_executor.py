"""``smoke-test-executor``: composite primitive — import-and-``compute`` probe.

WS5 #2. Collapses ``hpc-build-executor``'s inline ``python -c "import
argparse, importlib.util, sys; ... m.compute(...)"`` smoke test — the
one its own execution-style header forbids (the permission classifier
hard-blocks arbitrary ``python -c`` patterns) — into one deterministic
CLI verb the skill can branch on.

The probe imports the freshly-scaffolded executor module from a file
path and calls its ``compute(args)`` entry point with a minimal
``argparse.Namespace(output_file=...)``, exactly as the new-contract
dispatcher (``.hpc/cli.py``) would at runtime. There is no ``__main__``
block to exercise — ``compute`` IS the entry point — so a bare import is
not a sufficient smoke test; we must actually call ``compute``.

Why a real subprocess (not ``exec_module`` in-process): the executor is
unreviewed user code. It may ``sys.exit``, segfault, spin, or leak
global state. Running it in a child process keeps a crashing/exiting
module from taking down the CLI, lets us bound it with a timeout, and
gives us a clean stdout/stderr capture to tail back to the agent. The
child runs the canonical four-line load-and-call recipe the SKILL.md
used to inline; the parent reports ``{exit_code, stdout_tail,
stderr_tail}`` so the skill branches deterministically (non-zero
exit_code = fix-then-retry).

I/O contracts:

* Input: see ``hpc_agent/schemas/smoke_test_executor.input.json``.
* Output: a ``dict`` matching ``schemas/smoke_test_executor.output.json``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from hpc_agent._kernel.registry.primitive import SideEffect, primitive
from hpc_agent.cli._dispatch import CliArg, CliShape

__all__ = [
    "build_probe_argv",
    "smoke_test_executor",
]

# How many trailing characters of each captured stream to return. The
# tail is the actionable part for fix-then-retry (the traceback's final
# frames land here); a full dump would bloat the envelope the agent reads.
_TAIL_CHARS: int = 2000


def _probe_source(module_path: str, output_file: str) -> str:
    """Return the child-process Python that loads *module_path* and calls ``compute``.

    This is the exact load-and-call recipe the ``hpc-build-executor``
    SKILL.md inlined as ``python -c``: spec-from-file-location → module
    construction → register in ``sys.modules`` (so dataclass / pickle
    lookups by module name resolve) → ``exec_module`` → ``compute`` with
    a minimal Namespace. The values are interpolated via ``repr`` so a
    path containing quotes can't break out of the literal.
    """
    return (
        "import argparse, importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('m', {module_path!r})\n"
        "m = importlib.util.module_from_spec(spec)\n"
        "sys.modules['m'] = m\n"
        "spec.loader.exec_module(m)\n"
        f"m.compute(argparse.Namespace(output_file={output_file!r}))\n"
    )


def build_probe_argv(*, module_path: str, output_file: str) -> list[str]:
    """Build the argv for the child probe: ``<this python> -c <recipe>``.

    Uses ``sys.executable`` (not a bare ``python``) so the child runs the
    same interpreter — and therefore sees the same installed framework —
    as the CLI process. Separated out so the test can pin the recipe
    without spawning a real subprocess.
    """
    return [sys.executable, "-c", _probe_source(module_path, output_file)]


def _decode(raw: str | bytes | None) -> str:
    """Coerce a captured stream (``TimeoutExpired`` may carry bytes) to ``str``."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", errors="replace")
    return raw


def _tail(text: str) -> str:
    """Return the last :data:`_TAIL_CHARS` characters of *text*."""
    return text[-_TAIL_CHARS:]


@primitive(
    name="smoke-test-executor",
    verb="validate",
    # Honest declaration: the probe executes unreviewed user code in a
    # child process (``runs``) and that code is expected to write
    # ``--output-file`` (``filesystem``). Neither touches the cluster, so
    # requires_ssh stays False.
    side_effects=[
        SideEffect("runs", "user executor's compute(args) in a child python -c"),
        SideEffect("filesystem", "<output_file> (whatever the executor writes)"),
    ],
    idempotent=True,
    cli=CliShape(
        help=(
            "Smoke-test a scaffolded executor: import its module from a file "
            "path and call compute(Namespace(output_file=...)) in a subprocess. "
            "Returns {exit_code, stdout_tail, stderr_tail}; non-zero exit_code "
            "means fix-then-retry."
        ),
        verb="smoke-test-executor",
        args=(
            CliArg(
                "--module-path",
                type=str,
                required=True,
                help="Absolute path to the executor .py file to import and probe.",
            ),
            CliArg(
                "--output-file",
                type=str,
                default=None,
                help=(
                    "Path passed to compute() as Namespace.output_file. When "
                    "omitted, a throwaway file inside a private per-invocation "
                    "temp directory is used (0700, unique name) — never a fixed "
                    "shared path, so a co-tenant on a login node can't pre-plant "
                    "a symlink or collide. The smoke test only checks the module "
                    "imports and compute() runs clean, not the artifact."
                ),
            ),
        ),
        requires_ssh=False,
    ),
    agent_facing=True,
)
def smoke_test_executor(
    *,
    module_path: str | Path,
    output_file: str | None = None,
    timeout_sec: float = 60.0,
) -> dict[str, Any]:
    """Import *module_path* and call its ``compute`` in a child process.

    Returns a dict matching ``schemas/smoke_test_executor.output.json``;
    the CLI dispatcher wraps it in a SuccessEnvelope. *module_path*
    accepts both ``str`` (the CLI path) and ``Path`` (the in-process
    path) and is coerced internally.

    When *output_file* is ``None`` (the default), the probe writes to a
    throwaway file inside a private per-invocation temp directory
    (``tempfile.mkdtemp`` — mode 0700, unique name) removed on return. A
    fixed shared path like ``/tmp/smoke.csv`` was a symlink/collision
    hazard on multi-tenant login nodes; a private dir closes it. An
    explicit *output_file* is honoured verbatim (the caller owns it).

    The probe never raises on a failing executor — a crash, non-zero
    ``sys.exit``, or timeout all surface in the returned ``exit_code`` +
    ``stderr_tail`` so the skill branches deterministically. A timeout
    yields ``exit_code: null`` with ``timed_out: true`` and a timeout
    marker appended to ``stderr_tail``.
    """
    module_path_str = str(module_path)
    scratch_dir: str | None = None
    if output_file is None:
        scratch_dir = tempfile.mkdtemp(prefix="hpc-smoke-")
        output_file = str(Path(scratch_dir) / "smoke.csv")
    argv = build_probe_argv(module_path=module_path_str, output_file=str(output_file))

    started = time.monotonic()
    try:
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout_sec,
            )
        except subprocess.TimeoutExpired as exc:
            # A module that spins (or blocks on input) must not hang the CLI.
            # Surface the partial output the child managed to emit before the
            # kill so a traceback-before-hang is still actionable.
            stdout_tail = _tail(_decode(exc.stdout))
            stderr_tail = _tail(
                _decode(exc.stderr) + f"\n[smoke-test-executor] timed out after {timeout_sec}s"
            )
            return {
                "exit_code": None,
                "stdout_tail": stdout_tail,
                "stderr_tail": stderr_tail,
                "timed_out": True,
                "elapsed_sec": time.monotonic() - started,
            }

        return {
            "exit_code": proc.returncode,
            "stdout_tail": _tail(proc.stdout or ""),
            "stderr_tail": _tail(proc.stderr or ""),
            "timed_out": False,
            "elapsed_sec": time.monotonic() - started,
        }
    finally:
        # Remove the private scratch dir we minted (a caller-supplied
        # output_file is theirs to keep — only our own temp dir is cleaned).
        if scratch_dir is not None:
            shutil.rmtree(scratch_dir, ignore_errors=True)
