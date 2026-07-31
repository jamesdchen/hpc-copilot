"""The deploy cache must answer from the DISK, not from its own memory (U5).

``deploy_runtime``'s content-hash cache (#242) skips any file whose sha and
package version match the cluster-side manifest ``.hpc/.deploy_state.json``.
That manifest is a claim about what a PAST deploy wrote — and until U5 it was
never checked against what is actually there now.

That is what made the 2026-07-30 dropout PERMANENT rather than merely
unfortunate. Once the combiner was gone while the manifest survived, every
later deploy re-read the manifest, concluded "already current", shipped
nothing, and re-affirmed the lie. No amount of resubmitting would have fixed
it. And the manifest outlives the file it attests by construction: it is
rewritten on every deploy that changes anything (fresh mtime) while a cache-hit
file is never rewritten at all (frozen mtime) — precisely the discrimination a
scratch reaper applies.

The fix reads presence+sha in the prelude ssh ``deploy_runtime`` already makes,
so the evidence costs nothing, and lets it override the cache for the combiner.

Lives here rather than under ``tests/infra`` because the contract under test is
:mod:`hpc_agent.execution.mapreduce.deployed_artifact`'s — the transport is the
consumer.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from hpc_agent.execution.mapreduce import deployed_artifact as D
from hpc_agent.infra import transport


@pytest.fixture(autouse=True)
def _cache_is_the_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HPC_SSH_NO_BACKOFF", "1")
    monkeypatch.delenv("HPC_NO_DEPLOY_CACHE", raising=False)


def _probe_line(token: str) -> str:
    return f"{D.COMBINER_PROBE_PREFIX} {token}\n"


def _run_deploy(prelude_stdout: str) -> tuple[list[str], str, str | None]:
    """Deploy with the prelude ssh returning *prelude_stdout*.

    Returns ``(dst_rels_shipped, prelude_cmd, manifest_written)``.
    """
    captured: dict[str, Any] = {"dst_rels": [], "manifest": None}

    def _capture(*, ssh_target: str, remote_path: str, items: Any) -> None:
        captured["dst_rels"] = [it.dst_rel for it in items]

    def _capture_manifest(*, ssh_target: str, remote_path: str, content: str) -> None:
        captured["manifest"] = content

    with (
        patch(
            "hpc_agent.infra.transport.ssh_run",
            return_value=SimpleNamespace(returncode=0, stdout=prelude_stdout, stderr=""),
        ) as mock_ssh,
        patch("hpc_agent.infra.transport._deploy_transfer", side_effect=_capture),
        patch("hpc_agent.infra.transport._write_deploy_manifest", side_effect=_capture_manifest),
    ):
        transport.deploy_runtime(ssh_target="u@c", remote_path="/p", scheduler="sge")
    return captured["dst_rels"], mock_ssh.call_args[0][0], captured["manifest"]


def _current_manifest_json() -> str:
    return json.dumps(transport._local_deploy_manifest(scheduler="sge"))


# ── the probe rides the prelude that already runs ───────────────────────────


def test_the_presence_probe_costs_no_extra_round_trip() -> None:
    """One ssh in the prelude, as before — the probe is folded into it."""
    with (
        patch(
            "hpc_agent.infra.transport.ssh_run",
            return_value=SimpleNamespace(
                returncode=0,
                stdout=_probe_line(D.local_combiner_sha()) + _current_manifest_json(),
                stderr="",
            ),
        ) as mock_ssh,
        patch("hpc_agent.infra.transport._deploy_transfer"),
        patch("hpc_agent.infra.transport._write_deploy_manifest"),
    ):
        transport.deploy_runtime(ssh_target="u@c", remote_path="/p", scheduler="sge")

    assert mock_ssh.call_count == 1
    prelude = mock_ssh.call_args[0][0]
    assert D.COMBINER_PROBE_PREFIX in prelude
    assert f"/p/{D.COMBINER_REL}" in prelude
    # It must still carry the mkdir prep and the manifest read it always did.
    assert "mkdir -p" in prelude
    assert transport._DEPLOY_MANIFEST_REL in prelude


def test_the_probe_line_does_not_disturb_the_manifest_parse() -> None:
    """A full cache hit stays a full cache hit — nothing ships."""
    stdout = _probe_line(D.local_combiner_sha()) + _current_manifest_json()
    dst_rels, _prelude, manifest_written = _run_deploy(stdout)

    assert dst_rels == []
    assert manifest_written is None


# ── the override ───────────────────────────────────────────────────────────


def test_an_absent_combiner_overrides_a_cache_hit() -> None:
    """The dropout, closed. The manifest claims current; the disk says gone."""
    stdout = _probe_line("absent") + _current_manifest_json()
    dst_rels, _prelude, _manifest = _run_deploy(stdout)

    assert dst_rels == [D.COMBINER_REL], dst_rels


def test_a_stale_combiner_overrides_a_cache_hit() -> None:
    """Positively-read wrong bytes are re-shipped too.

    A manifest that survived a partial/torn transfer can attest bytes that
    never fully landed; the sha read from the disk is the only thing that
    settles it.
    """
    stdout = _probe_line("0" * 64) + _current_manifest_json()
    dst_rels, _prelude, _manifest = _run_deploy(stdout)

    assert dst_rels == [D.COMBINER_REL], dst_rels


def test_an_unhashable_combiner_does_not_force_a_re_ship() -> None:
    """Absence of a sha is absence of evidence.

    A login node with no ``sha256sum`` / ``shasum`` / ``openssl`` reports the
    file PRESENT with an unknown digest. Forcing on that would make every such
    host a permanent cache miss — paying the re-ship forever to learn nothing.
    """
    stdout = _probe_line("unknown") + _current_manifest_json()
    dst_rels, _prelude, _manifest = _run_deploy(stdout)

    assert dst_rels == []


def test_no_probe_line_leaves_the_cache_exactly_as_it_was() -> None:
    """Fail-open: an old prelude or a severed channel changes no decision."""
    dst_rels, _prelude, manifest_written = _run_deploy(_current_manifest_json())

    assert dst_rels == []
    assert manifest_written is None


def test_the_override_does_not_duplicate_an_already_shipping_combiner() -> None:
    """On a full cache MISS the combiner ships once, not twice."""
    dst_rels, _prelude, _manifest = _run_deploy(_probe_line("absent"))

    assert dst_rels.count(D.COMBINER_REL) == 1
    # …and everything else still ships, as a cache miss always did.
    assert sorted(dst_rels) == sorted(transport._local_deploy_manifest(scheduler="sge")["files"])
