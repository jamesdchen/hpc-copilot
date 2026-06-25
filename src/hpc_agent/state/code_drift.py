"""The single definition of executor / code drift between a prior run and the
about-to-submit code.

``cmd_sha`` is pure PARAMETER identity (#207): it deliberately excludes the
executor command and the ``tasks.py`` bytes. So "same swept parameters, changed
code" is param-identical but NOT a valid replay target — detecting it is the job
of this module, the *code*-identity check that rides alongside the param-identity
dedup.

The predicate previously lived inline in TWO places — the layer-1 (run_id) gate
in ``ops/submit/runner.py`` and the layer-2 (cmd_sha) scan in
``state/runs.py::find_run_by_cmd_sha`` — and was fixed twice (#351 sub-bug #5
landed in ``find_run_by_cmd_sha`` first, then again at the layer-1 gate when the
common same-machine COMPLETE-redo case turned out to bypass layer 2). Two copies
of one rule is exactly how a fix lands in one and misses the other. This is the
one home; both layers feed it their own recorded-vs-current values.

The predicate is intentionally conservative: an empty/absent recorded value is
NOT drift. We cannot prove a pre-#351 record (which never stamped ``executor`` /
``tasks_py_sha``) changed, so the check stays off rather than firing falsely.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["CodeDrift", "detect_code_drift"]


@dataclass(frozen=True)
class CodeDrift:
    """Outcome of comparing a prior run's recorded code identity to the
    about-to-submit code.

    ``drifted_executor`` / ``drifted_tasks_py_sha`` carry the *recorded* value
    that differs (for the caller's warning / return), and are ``None`` when that
    dimension did not drift — so a caller can branch per-dimension without
    re-deriving which one moved.
    """

    executor_changed: bool
    code_changed: bool
    drifted_executor: str | None
    drifted_tasks_py_sha: str | None

    @property
    def drifted(self) -> bool:
        """True when either dimension changed."""
        return self.executor_changed or self.code_changed


def detect_code_drift(
    *,
    recorded_executor: str | None,
    recorded_tasks_py_sha: str | None,
    current_executor: str | None,
    current_tasks_py_sha: str | None,
) -> CodeDrift:
    """Compare a prior run's recorded code identity to the current submission.

    A dimension counts as drifted only when BOTH the recorded and the current
    value are non-empty AND differ — an absent value on either side disables
    that dimension's check (we cannot prove it changed). This is the symmetric
    rule both dedup layers apply; the layers differ only in WHERE they read the
    recorded values from (layer 1: the prior run's journal ``RunRecord``; layer
    2: the matched sidecar dict).
    """
    executor_changed = bool(
        current_executor and recorded_executor and str(recorded_executor) != str(current_executor)
    )
    code_changed = bool(
        current_tasks_py_sha
        and recorded_tasks_py_sha
        and str(recorded_tasks_py_sha) != str(current_tasks_py_sha)
    )
    return CodeDrift(
        executor_changed=executor_changed,
        code_changed=code_changed,
        drifted_executor=str(recorded_executor) if executor_changed else None,
        drifted_tasks_py_sha=str(recorded_tasks_py_sha) if code_changed else None,
    )
