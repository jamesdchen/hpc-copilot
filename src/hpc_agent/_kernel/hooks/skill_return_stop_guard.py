"""``Stop`` hook — block ending the turn over an unfetched sub-skill return.

This is *harness-mediated*, not a CLI ``@primitive``: Claude Code runs it as a
``command`` hook wired into ``~/.claude/settings.json``'s ``hooks.Stop`` array
(see :func:`hpc_agent.agent_assets.install_agent_assets`). It is invoked when
the agent is about to end its turn, receives the Stop payload as JSON on
**stdin**, and may emit ``{"decision": "block", "reason": ...}`` on **stdout**
to make the agent continue instead.

Why it exists
-------------
The sub-skill `Then stop`/hand-back prose at every composition boundary is
advisory: a parent composing ``Skill(<sub>)`` can still end its turn right
after the sub-skill's ``emit-skill-return``, leaving the committed envelope
unfetched and the parent procedure stalled until a human types "keep going"
(empirical: 2026-06-10 demo, ``hpc-wrap-entry-point``). Prose lowers the
frequency; nothing harness-side *prevented* the stop — ``PostToolUse`` hooks
only run when a tool call happens, and the failure mode is precisely that no
further tool call happens. ``Stop`` is the one hook event that fires at the
exact failure point, so this guard turns "stopped over a pending return" into
a deterministic continuation.

Behaviour
---------
On a Stop event, the guard scans ``<cwd>/.hpc/_returns/`` AND every directory
the emitter recorded in its breadcrumb (see
:func:`hpc_agent.cli.skill_returns.known_return_dirs`) for a committed envelope
of any skill in :data:`hpc_agent.cli.skill_returns._KNOWN_SKILLS`. Scanning the
breadcrumb is what lets the guard fire when the emit ran with an
``--experiment-dir`` other than the harness cwd — a Stop payload carries no
command, so unlike the autofetch sibling the guard cannot recover that dir by
parsing. If one or more envelopes exist, it blocks the stop with a reason
instructing the agent to ``fetch-skill-return`` each pending skill (from the
directory its envelope was found in) and continue the parent skill's next step.

The condition is **self-healing**: ``fetch-skill-return`` deletes the
committed file by default, so after the agent follows the reason the guard has
nothing left to block on — including for a stale envelope left over by an
older session, which gets flushed by exactly one block-fetch-continue cycle.

The rejector → completer split (RULED 2026-07-12)
-------------------------------------------------
``docs/design/stop-hook-completer.md`` rules this guard a HYBRID: the FETCH is
code's (the autofetch sibling already reads the same envelope) but the "continue
the parent skill's next step" is judgment. When the harness declares the
``stop-hook-append`` capability (both the proceeding and the on-block bits — the
injection rides a BLOCKED stop, D2), :func:`_completer_output` reads each envelope
through the ONE shared reader
(:func:`hpc_agent._kernel.hooks.skill_return_autofetch.read_committed_envelope`),
injects it via ``systemMessage``, and CLEARS the committed file (completing the
fetch) — while the output still BOUNCES for the parent-skill continuation only.
Absent/unknown (the default, since no harness declares it) the guard degrades to
:func:`_rejector_output` byte-for-byte: today's fetch-then-continue bounce.

Loop safety & defensiveness
---------------------------
* If the payload carries ``stop_hook_active`` (Claude Code's marker that this
  stop is already a continuation forced by a Stop hook), the guard is a
  no-op — it can block a given stop at most once, never loop.
* For a malformed payload, an unreadable directory, or no pending envelope it
  is a **clean no-op** — prints nothing, exits ``0``. It never raises and
  never exits non-zero.
* In a directory with no ``.hpc/_returns/`` (any non-hpc project — the hook is
  installed user-globally) the scan is a handful of ``is_file`` misses.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

# Single source of truth for the set of sub-skills that emit a return envelope.
# Re-exported from the CLI primitive so this hook and the verbs can never drift.
from hpc_agent.cli.skill_returns import _KNOWN_SKILLS, _committed_path, known_return_dirs

__all__ = ["build_hook_output", "main", "pending_skill_returns"]


def pending_skill_returns(experiment_dir: Path) -> list[str]:
    """Known skills with a committed (not merely staged) envelope on disk.

    Order follows :data:`_KNOWN_SKILLS`. Filesystem errors on any single probe
    are swallowed — a skill we cannot stat is treated as not pending.
    """
    pending: list[str] = []
    for skill in _KNOWN_SKILLS:
        try:
            if _committed_path(experiment_dir, skill).is_file():
                pending.append(skill)
        except OSError:
            continue
    return pending


def build_hook_output(payload: Any) -> dict[str, Any] | None:
    """Pure core: map a Stop *payload* to a block decision, or ``None``.

    Returns ``None`` (→ caller prints nothing, the stop proceeds) when:

    * *payload* is not a mapping.
    * ``stop_hook_active`` is truthy — this stop is already a hook-forced
      continuation; blocking again would loop.
    * no known skill has a committed envelope under the resolved
      experiment dir.

    Otherwise returns the Claude Code Stop hook-output shape::

        {"decision": "block", "reason": "<fetch instructions>"}
    """
    if not isinstance(payload, dict):
        return None

    if payload.get("stop_hook_active"):
        return None

    cwd = payload.get("cwd")
    cwd_dir = Path(cwd) if isinstance(cwd, str) and cwd else Path(os.getcwd())

    # Scan ``cwd`` first, then every directory the emitter recorded committing a
    # return to (the breadcrumb). The emit command's ``--experiment-dir`` may
    # differ from the harness cwd — exactly the case the autofetch sibling
    # handles by parsing the command, which a Stop payload (no command) cannot.
    # Scanning the breadcrumb closes that gap so the guard cannot miss a pending
    # return committed under a non-cwd experiment dir.
    candidate_dirs: list[Path] = [cwd_dir]
    seen = {cwd_dir.expanduser().resolve()}
    for d in known_return_dirs():
        resolved = d.expanduser().resolve()
        if resolved not in seen:
            seen.add(resolved)
            candidate_dirs.append(d)

    # First dir that has each pending skill wins, so a skill is fetched once
    # from the location its envelope actually lives in.
    pending_by_skill: dict[str, Path] = {}
    for cand in candidate_dirs:
        for skill in pending_skill_returns(cand):
            pending_by_skill.setdefault(skill, cand)
    if not pending_by_skill:
        # No committed envelope (never emitted, or already fetched — the
        # self-healing no-op). Both rejector and completer are silent here.
        return None

    # The RULED split (docs/design/stop-hook-completer.md, 2026-07-12): code
    # completes the FETCH (inject the envelope, clear the file — the judgment-free
    # half the autofetch sibling already does in code) and the bounce survives
    # ONLY for the parent-skill continuation (the judgment half). Capability-gated
    # (D1/D2): the injection rides a BLOCKED stop (the continuation always bounces),
    # so it needs the harness's `append_on_block` confirmation. Absent/unknown —
    # the default, since no harness declares it — the whole guard degrades to the
    # REJECTOR EXACTLY (today's fetch-then-continue bounce).
    try:
        from hpc_agent.ops.harness_capabilities import (
            detect_stop_hook_append,
            detect_stop_hook_append_on_block,
        )

        completer_active = (
            detect_stop_hook_append() is True and detect_stop_hook_append_on_block() is True
        )
    except Exception:
        completer_active = False

    if completer_active:
        completed = _completer_output(pending_by_skill)
        if completed is not None:
            return completed
        # Reading/clearing every envelope failed → fall through to the rejector,
        # which tells the model to fetch them itself (invariant 4: degrade to the
        # rejector, never claim an un-injected fetch).

    return _rejector_output(pending_by_skill)


def _rejector_output(pending_by_skill: dict[str, Path]) -> dict[str, Any]:
    """Today's REJECTOR shape — the capability-absent (dark) default (D1).

    Byte-identical to the pre-completer bounce: the model runs
    ``fetch-skill-return`` for each pending envelope, then continues the parent
    skill. This is what the completer degrades to wherever the
    ``stop-hook-append`` capability is absent/unknown (or a completer read fails).
    """
    # Forward-slash form: the agent will paste this into a Git Bash command,
    # where a bare backslash path invites the \U-escape-collapse bug class
    # (agent_assets._hook_python). shlex.quote still covers spaces. Each skill
    # carries the --experiment-dir of the directory its envelope was found in.
    fetches = " && ".join(
        f"hpc-agent fetch-skill-return --skill {skill} "
        f"--experiment-dir {shlex.quote(found_dir.as_posix())}"
        for skill, found_dir in pending_by_skill.items()
    )
    reason = (
        f"Sub-skill return envelope(s) committed but not fetched: "
        f"{', '.join(pending_by_skill)}. Run `{fetches}`, then continue the parent "
        "skill's next step — a sub-skill composition boundary is not the end "
        "of the turn."
    )
    return {"decision": "block", "reason": reason}


def _completer_output(pending_by_skill: dict[str, Path]) -> dict[str, Any] | None:
    """The COMPLETER shape (D1/D2): inject the envelopes in code, bounce only for
    the parent-skill continuation.

    For each pending skill, read its envelope through the ONE shared reader the
    autofetch sibling uses (:func:`skill_return_autofetch.read_committed_envelope`)
    and CLEAR the committed file (completing the fetch — mirroring
    ``fetch-skill-return``'s default clear, so a later stop is silent). The
    envelopes ride ONE ``systemMessage``; the output ALSO carries
    ``{"decision":"block", ...}`` for the parent-skill continuation — the judgment
    the model must author. A skill whose read or clear fails is left on the
    model-fetch path (its fetch command stays in the reason).

    Returns ``None`` when NOT ONE envelope could be injected in code — the caller
    then emits the full rejector (invariant 4: never claim an un-injected fetch).
    """
    from hpc_agent._kernel.hooks.skill_return_autofetch import read_committed_envelope

    injected: list[tuple[str, str]] = []
    model_fetch: dict[str, Path] = {}
    for skill, found_dir in pending_by_skill.items():
        envelope = read_committed_envelope(found_dir, skill)
        if envelope is None:
            model_fetch[skill] = found_dir
            continue
        # Complete the fetch: clear the committed file so a later stop is silent
        # (fetch-skill-return clears by default). A failed clear means the fetch
        # is NOT complete → keep the skill on the model-fetch path.
        cleared = False
        with contextlib.suppress(OSError):
            _committed_path(found_dir, skill).unlink()
            cleared = True
        if not cleared:
            model_fetch[skill] = found_dir
            continue
        injected.append((skill, envelope))

    if not injected:
        return None  # nothing completed in code → the caller emits the rejector

    system_message = (
        "hpc-agent skill-return completer — code-fetched the sub-skill return "
        "envelope(s) (model-untouched; the fetch is done, do NOT re-run "
        "fetch-skill-return for these):\n"
        + "\n".join(f"[{skill}] {envelope}" for skill, envelope in injected)
    )

    if model_fetch:
        fetches = " && ".join(
            f"hpc-agent fetch-skill-return --skill {skill} "
            f"--experiment-dir {shlex.quote(found_dir.as_posix())}"
            for skill, found_dir in model_fetch.items()
        )
        reason = (
            f"Fetched {', '.join(s for s, _ in injected)} in code (above). Still "
            f"un-fetched: {', '.join(model_fetch)} — run `{fetches}`, then continue "
            "the parent skill's next step — a sub-skill composition boundary is not "
            "the end of the turn."
        )
    else:
        # The judgment half: only the parent-skill continuation remains.
        reason = (
            "Sub-skill return envelope(s) fetched in code (above). Continue the "
            "parent skill's next step — a sub-skill composition boundary is not the "
            "end of the turn."
        )
    return {"systemMessage": system_message, "decision": "block", "reason": reason}


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint the harness invokes — read stdin, maybe print, never crash.

    Reads the Stop payload from stdin, runs :func:`build_hook_output`, and
    prints the resulting JSON to stdout when non-``None``. Any unexpected
    error is swallowed and reported as a clean no-op (exit ``0``): a broken
    guard must degrade to today's behaviour (the stop proceeds, the manual
    "keep going" seam remains), never wedge the harness. ``argv`` is accepted
    for symmetry with other entrypoints but is unused.
    """
    del argv
    try:
        raw = sys.stdin.read()
    except OSError:
        return 0

    try:
        payload = json.loads(raw) if raw.strip() else None
    except (json.JSONDecodeError, ValueError):
        return 0

    try:
        output = build_hook_output(payload)
    except Exception:
        return 0

    if output is not None:
        print(json.dumps(output), flush=True)
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via the harness
    raise SystemExit(main())
