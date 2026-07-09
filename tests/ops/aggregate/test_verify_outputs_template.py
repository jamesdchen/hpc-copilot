"""``verify_per_task_outputs`` template substitution.

Only ``{task_id}`` is recognised — via ``str.replace``, mirroring
``_reducer_contract.format_output_rel`` — so any other brace in a
user-supplied template (a literal ``{horizon}`` results dir, a stray
``{...}``) must pass through untouched instead of raising
``KeyError``/``IndexError`` from ``str.format``.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

from hpc_agent.ops.aggregate.runner import verify_per_task_outputs

_SIDECAR_JSON = json.dumps(
    {"sidecar_schema_version": 1, "task_count": 2, "wave_map": {"0": [0, 1]}}
)


def _completed(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["ssh"], returncode=returncode, stdout=stdout, stderr=""
    )


def test_extra_braces_in_template_do_not_raise() -> None:
    """A named non-task_id placeholder and a positional-looking ``{}`` both
    survive substitution as literals."""
    scripts: list[str] = []

    def fake_ssh_run(cmd, *, ssh_target, **_kw):
        if cmd.startswith("cat "):
            return _completed(stdout=_SIDECAR_JSON)
        scripts.append(cmd)
        return _completed(stdout="MISSING:results/h{horizon}/metrics.1.json\n")

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh_run):
        missing = verify_per_task_outputs(
            ssh_target="user@host",
            remote_path="/exp",
            run_id="run_abcd1234",
            wave=0,
            template="results/h{horizon}/metrics.{task_id}.json",
        )

    # Substituted per-task paths keep the foreign brace verbatim.
    assert "results/h{horizon}/metrics.0.json" in scripts[0]
    assert "results/h{horizon}/metrics.1.json" in scripts[0]
    assert missing == ["results/h{horizon}/metrics.1.json"]


def test_bare_positional_braces_do_not_raise() -> None:
    """``{}`` in a template would raise IndexError under str.format."""

    def fake_ssh_run(cmd, *, ssh_target, **_kw):
        if cmd.startswith("cat "):
            return _completed(stdout=_SIDECAR_JSON)
        return _completed(stdout="")

    with patch("hpc_agent.infra.remote.ssh_run", side_effect=fake_ssh_run):
        missing = verify_per_task_outputs(
            ssh_target="user@host",
            remote_path="/exp",
            run_id="run_abcd1234",
            wave=0,
            template="results/{}/metrics.{task_id}.json",
        )

    assert missing == []
