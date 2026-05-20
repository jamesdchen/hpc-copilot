"""``validate-executor-signatures`` primitive — static cross-check between
``tasks.py.resolve(i)`` kwargs and the user's executor function signature.

Catches the SEGMENT_CHOICES bug class: a campaign that submits with
fabricated kwarg values which the executor would reject at runtime.

Approach: ``inspect.signature(executor_function).parameters``; for the
first ``sample_n_tasks`` task indices, verify every kwarg key maps to
a real parameter and every ``Literal``-typed parameter receives a
value in the allowed set. Findings are agent-actionable (each carries
``suggested_fix`` and an ``evidence`` dict naming the task index and
parameter).

Skips silently (info finding) if the executor module fails to import
— project-side import-time side effects, missing optional deps, etc.
"""

from __future__ import annotations

import importlib
import inspect
from typing import TYPE_CHECKING, Literal, get_args, get_origin

from hpc_agent._internal.primitive import primitive
from hpc_agent._schema_models.validators.validate_executor_signatures import (
    ValidateExecutorSignaturesResult,
    ValidateExecutorSignaturesSpec,
)
from hpc_agent._schema_models.workflows.validate_campaign import ValidatorFinding

if TYPE_CHECKING:
    from pathlib import Path

_VALIDATOR = "validate-executor-signatures"


@primitive(
    name=_VALIDATOR,
    verb="validate",
    side_effects=[],
    idempotent=True,
    agent_facing=True,
)
def validate_executor_signatures(
    experiment_dir: Path,
    *,
    spec: ValidateExecutorSignaturesSpec,
) -> ValidateExecutorSignaturesResult:
    """Cross-check ``tasks.py`` kwargs against the executor function's signature.

    Returns a :class:`ValidateExecutorSignaturesResult` whose
    ``findings`` is the list of detected issues (empty list = pass).
    Common ``code`` values:

    * ``tasks_py_missing`` — campaign hasn't been interviewed yet.
    * ``tasks_py_import_error`` — tasks.py exists but raises on import.
    * ``executor_module_import_error`` — module path is wrong or
      module fails on import (info-level; signature check skipped).
    * ``executor_function_not_found`` — function name typo.
    * ``missing_parameter`` — kwarg not in signature (and no ``**kwargs``).
    * ``literal_value_not_allowed`` — value isn't in the parameter's
      ``Literal`` set (the SEGMENT_CHOICES bug class).
    * ``resolve_returned_non_dict`` — tasks.py contract violation.
    """
    findings: list[ValidatorFinding] = []
    tasks_py = experiment_dir / spec.tasks_py_path

    if not tasks_py.is_file():
        findings.append(
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="warning",
                code="tasks_py_missing",
                message=f"tasks.py not found at {tasks_py}",
                suggested_fix="Run /interview to generate tasks.py.",
                evidence={"path": str(tasks_py)},
            )
        )
        return ValidateExecutorSignaturesResult(findings=findings)

    from hpc_agent import load_tasks_module

    try:
        tasks_module = load_tasks_module(tasks_py)
    except Exception as exc:  # noqa: BLE001 — validator surfaces, doesn't crash
        findings.append(
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="error",
                code="tasks_py_import_error",
                message=f"failed to import tasks.py: {exc}",
                evidence={"path": str(tasks_py)},
            )
        )
        return ValidateExecutorSignaturesResult(findings=findings)

    try:
        executor_mod = importlib.import_module(spec.executor_module)
    except Exception as exc:  # noqa: BLE001
        findings.append(
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="info",
                code="executor_module_import_error",
                message=f"failed to import {spec.executor_module}: {exc}",
                suggested_fix=(
                    f"Verify {spec.executor_module} is on PYTHONPATH and imports "
                    "cleanly. Signature check skipped."
                ),
                evidence={"module": spec.executor_module},
            )
        )
        return ValidateExecutorSignaturesResult(findings=findings)

    fn = getattr(executor_mod, spec.executor_function, None)
    if fn is None or not callable(fn):
        findings.append(
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="error",
                code="executor_function_not_found",
                message=(f"{spec.executor_module}.{spec.executor_function} is not callable"),
                suggested_fix=(
                    f"Verify {spec.executor_function} is a top-level function on "
                    f"{spec.executor_module}."
                ),
                evidence={
                    "module": spec.executor_module,
                    "function": spec.executor_function,
                },
            )
        )
        return ValidateExecutorSignaturesResult(findings=findings)

    parameters = inspect.signature(fn).parameters
    accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values())

    try:
        n = int(tasks_module.total())
    except Exception as exc:  # noqa: BLE001
        findings.append(
            ValidatorFinding(
                validator=_VALIDATOR,
                severity="error",
                code="tasks_py_contract_error",
                message=f"tasks.total() raised: {type(exc).__name__}: {exc}",
                suggested_fix=(
                    "tasks.total() must return an int. Inspect the module for "
                    "import-time failures or a broken total() implementation."
                ),
                evidence={"tasks_py_path": spec.tasks_py_path},
            )
        )
        return ValidateExecutorSignaturesResult(findings=findings)
    for i in range(min(n, spec.sample_n_tasks)):
        try:
            kwargs = tasks_module.resolve(i)
        except Exception as exc:  # noqa: BLE001 — validator boundary
            findings.append(
                ValidatorFinding(
                    validator=_VALIDATOR,
                    severity="error",
                    code="tasks_py_contract_error",
                    message=f"tasks.resolve({i}) raised: {type(exc).__name__}: {exc}",
                    suggested_fix=(
                        "tasks.resolve(i) must return a dict for every i in "
                        "range(tasks.total()). Inspect the module for an "
                        "off-by-one or a broken resolve() branch."
                    ),
                    evidence={"tasks_py_path": spec.tasks_py_path, "task_id": i},
                )
            )
            return ValidateExecutorSignaturesResult(findings=findings)
        if not isinstance(kwargs, dict):
            findings.append(
                ValidatorFinding(
                    validator=_VALIDATOR,
                    severity="error",
                    code="resolve_returned_non_dict",
                    message=(
                        f"tasks.resolve({i}) returned {type(kwargs).__name__}; "
                        "the framework requires a dict so kwargs can be **-unpacked"
                    ),
                    evidence={"task_id": i},
                )
            )
            continue
        for key, value in kwargs.items():
            if key not in parameters:
                if accepts_kwargs:
                    continue
                findings.append(
                    ValidatorFinding(
                        validator=_VALIDATOR,
                        severity="error",
                        code="missing_parameter",
                        message=(
                            f"tasks[{i}] passes kwarg {key!r} but "
                            f"{spec.executor_module}.{spec.executor_function} "
                            "has no such parameter"
                        ),
                        suggested_fix=(
                            f"Either remove {key!r} from tasks.resolve(...) or add "
                            f"it as a parameter to {spec.executor_function}."
                        ),
                        evidence={
                            "task_id": i,
                            "param_name": key,
                            "available_params": list(parameters.keys()),
                        },
                    )
                )
                continue
            param = parameters[key]
            ann = param.annotation
            if get_origin(ann) is Literal:
                allowed = get_args(ann)
                if value not in allowed:
                    findings.append(
                        ValidatorFinding(
                            validator=_VALIDATOR,
                            severity="error",
                            code="literal_value_not_allowed",
                            message=(
                                f"tasks[{i}] passes {key}={value!r} but parameter "
                                f"is annotated Literal{list(allowed)}"
                            ),
                            suggested_fix=(
                                f"Change tasks.resolve({i})[{key!r}] to one of {list(allowed)}."
                            ),
                            evidence={
                                "task_id": i,
                                "param_name": key,
                                "value": value,
                                "allowed": list(allowed),
                            },
                        )
                    )

    return ValidateExecutorSignaturesResult(findings=findings)
