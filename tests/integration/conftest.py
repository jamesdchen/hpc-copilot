"""Shared harness for the integration tier (``pytest -m integration``).

The tier exists because unit tests here fake the ``hpc-agent <verb> --spec``
subprocess seam, so a spec whose *shape* is wrong (``extra="forbid"`` with a
nested required sub-object) passes green and only fails on a real invocation —
the exact class that broke ``block-drive`` (a driver that built ``{"run_id":
...}`` for a block whose spec nests ``run_id`` under a required ``monitor`` /
``aggregate`` / ``submit`` object). See docs/design/block-drive.md §9.

Two seams the tier exercises, both hermetic:

* **spec-contract** — construct the spec an orchestrator actually builds and
  assert it validates against the target verb's LIVE pydantic model
  (``spec_model_for``). This is the deterministic, no-SSH layer.
* **in-process dispatch** — drive a verb through the real ``cli.dispatch.main``
  (``dispatch_envelope``), patching only the SSH chokepoints, and assert the
  real JSON envelope (``ok`` / ``error_code`` / ``category``). This exercises
  parser → ``model_validate`` → primitive dispatch → envelope.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

import hpc_agent
from hpc_agent._kernel.registry.primitive import get_meta

# The three SSH chokepoints every cluster-touching verb funnels through. A test
# patches these (never the higher-level orchestrators) so it exercises the real
# code path up to — and only stubbing — the connection boundary.
SSH_RUN = "hpc_agent.infra.remote.ssh_run"
SSH_STATUS_REPORT = "hpc_agent.infra.cluster_status.ssh_status_report"


@pytest.fixture(scope="session", autouse=True)
def _primitives_registered() -> None:
    """Guarantee the registry is populated before any spec-model lookup."""
    hpc_agent.register_primitives()


def spec_model_for(verb: str) -> type[BaseModel] | None:
    """The live pydantic Spec model a real CLI dispatch validates *verb* against.

    Returns ``get_meta(verb).cli.spec_model`` — the exact class
    ``cli/_dispatch.py`` calls ``.model_validate(raw)`` on — or ``None`` for a
    verb that takes a raw dict / has no spec. A spec-contract test asserts the
    dict an orchestrator builds satisfies ``spec_model_for(verb).model_validate``.
    """
    meta = get_meta(verb)
    cli = getattr(meta, "cli", None)
    return getattr(cli, "spec_model", None) if cli is not None else None


def assert_valid_spec(verb: str, spec: dict[str, Any]) -> None:
    """Assert *spec* validates against *verb*'s live model (the real dispatch gate).

    Fails with the verb + the pydantic error, mirroring the ``spec_invalid``
    envelope a real ``hpc-agent <verb> --spec`` would emit. A verb with no
    ``spec_model`` (raw-dict input) is a no-op — nothing to validate.
    """
    model = spec_model_for(verb)
    if model is None:
        return
    try:
        model.model_validate(spec)
    except Exception as exc:  # noqa: BLE001 — surface as a readable test failure
        raise AssertionError(
            f"{verb}: the spec an orchestrator builds does NOT validate against "
            f"{model.__name__} — a real `hpc-agent {verb}` would emit spec_invalid.\n"
            f"spec={json.dumps(spec, default=str)}\nerror={exc}"
        ) from exc


@pytest.fixture
def dispatch_envelope():
    """Drive a verb through the REAL in-process CLI dispatch; return its envelope.

    Reuses the shipped ``_in_process_cli_runner`` (the MCP warm runner) so the
    test exercises the same parser → ``model_validate`` → primitive → envelope
    path a real invocation does, without a subprocess. Returns a callable
    ``run(verb, spec, experiment_dir=None) -> dict`` yielding the parsed JSON
    envelope. The caller patches the SSH chokepoints (SSH_RUN / SSH_STATUS_REPORT)
    for any cluster-touching verb.
    """
    from hpc_agent._kernel.extension.mcp_server import _in_process_cli_runner

    def run(verb: str, spec: dict[str, Any], experiment_dir: Any = None) -> dict[str, Any]:
        import tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix=f"{verb}-spec-", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(spec, fh)
            spec_path = fh.name
        argv = [verb, "--spec", spec_path]
        if experiment_dir is not None:
            argv += ["--experiment-dir", str(experiment_dir)]
        exit_code, stdout, _stderr = _in_process_cli_runner(argv)
        Path(spec_path).unlink(missing_ok=True)
        envelope: dict[str, Any]
        try:
            envelope = json.loads(stdout)
        except (json.JSONDecodeError, ValueError):
            envelope = {}
        envelope["_exit_code"] = exit_code
        return envelope

    return run
