"""CLI dispatcher smoke tests for the ``memory`` domain (interview + recall).

These are in-process tests that drive :func:`dispatch_primitive` against
the production ``interview`` and ``recall`` primitives. They guard the
specific kwarg-filter behaviour the registry-driven migration relies on:

* ``recall_campaigns(roots, *, spec)`` exposes ``--root`` / ``--limit`` /
  ``--task-kind`` / etc. as CLI flags; ``arg_pre`` re-maps them into
  ``{spec, roots}``; the leftover raw flag values (``root``, ``limit``,
  ``task_kind``, …) must be dropped by the dispatcher's signature filter
  rather than forwarded as unknown kwargs.

The earlier dispatcher implementation forwarded the raw values, which
TypeError'd on ``recall_campaigns(roots, *, spec, root=..., limit=...)``.
The fix (signature-based filtering on ``_filter_to_signature``) is now
on the foundation branch; this test pins the contract.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from hpc_agent.cli._dispatch import dispatch_primitive


def _capsys_envelope(captured: pytest.CaptureResult[str]) -> dict[str, Any]:
    """Return the parsed JSON envelope on stdout (must be exactly one line)."""
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected one envelope, got: {captured.out!r}"
    return json.loads(lines[0])


def test_recall_dispatch_filters_cli_only_flags(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """``--root``/``--limit``/etc. are CLI flags; the primitive expects roots+spec.

    The dispatcher's signature filter must drop the raw ``root``,
    ``limit``, ``task_kind``, ``operator``, ``since``,
    ``include_runtime``, ``include_generator_stats`` kwargs that
    don't appear in ``recall_campaigns``'s signature — otherwise the
    primitive call would raise ``TypeError: got unexpected keyword argument``.
    """
    # Empty roots dir → no interview.json files → empty result is fine.
    ns = argparse.Namespace(
        root=str(tmp_path),
        limit=20,
        task_kind=None,
        operator=None,
        since=None,
        include_runtime=False,
        include_generator_stats=False,
    )
    rc = dispatch_primitive("recall", ns)

    assert rc == 0
    env = _capsys_envelope(capsys.readouterr())
    assert env["ok"] is True
    assert "data" in env
    data = env["data"]
    # Envelope shape pinned by RecallResult — campaigns/total_matching/showing/rollup.
    assert data["total_matching"] == 0
    assert data["showing"] == 0
    assert data["campaigns"] == []
    assert "rollup" in data
    assert data["rollup"]["count"] == 0


def test_interview_dispatch_resolves_campaign_dir(
    capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """End-to-end: write a tasks.py + interview spec, dispatch ``interview``.

    Pins the spec-loading + ``arg_pre`` (--campaign-dir → Path.resolve())
    path for the interview primitive.
    """
    campaign_dir = tmp_path / "camp"
    campaign_dir.mkdir()
    # Minimal tasks.py with three tasks so total() == 3 matches intent.
    tasks_py = campaign_dir / "tasks.py"
    tasks_py.write_text(
        "_T = [{'i': 0}, {'i': 1}, {'i': 2}]\n"
        "def total() -> int: return len(_T)\n"
        "def resolve(i: int) -> dict: return _T[i]\n",
        encoding="utf-8",
    )
    spec_path = tmp_path / "intent.json"
    spec_path.write_text(
        json.dumps(
            {
                "goal": "smoke test",
                "task_count": 3,
                "produced_by": {"kind": "human", "operator": "tester"},
            }
        ),
        encoding="utf-8",
    )

    ns = argparse.Namespace(
        spec=spec_path,
        campaign_dir=str(campaign_dir),
    )
    rc = dispatch_primitive("interview", ns)

    assert rc == 0
    env = _capsys_envelope(capsys.readouterr())
    assert env["ok"] is True
    data = env["data"]
    assert data["total_tasks"] == 3
    assert "interview.json" in data["artifacts"]
    # campaign_dir resolved to absolute path via arg_pre.
    assert data["campaign_dir"] == str(campaign_dir.resolve())
