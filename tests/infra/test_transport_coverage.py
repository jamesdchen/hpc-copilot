"""Behaviour-pinning coverage for the transport plane's safety-critical seams.

The SSH/transfer machinery in :mod:`hpc_agent.infra.transport` is complex and
safety-critical: a wrong deletion or a mis-read manifest corrupts a deploy or
loses data. The landed batches (``test_transport_prune.py``,
``test_transport_delta_cache_checkpoint.py``, ``test_transport_breaker_uniformity.py``,
``test_remote_rsync_fallback.py``, ``test_prune.py``) already pin most of it; this
file closes the residual *covered-but-UNASSERTED* boundary/polarity/operator
mutations a suite run would otherwise let survive — the exact class the
2026-07-17 mutation triage names.

Each test adds an assertion that KILLS a specific surviving mutant, named in the
docstring. Two themes get priority (both flagged in the task):

* the **wrong-deletion polarity** of the prune known-set — a garbled read or a
  runtime-placed file must never turn into a delete; the fail-open direction is
  "treat as UNKNOWN → never prune", never "empty answer → wrongly prunable";
* the **breaker fail-open direction** — a breaker-open on a prune/reseal dial
  degrades in the SAFE direction (no deletion reaches the wire, no new raise).

The paired assertions target functions the existing files exercise but leave
un-pinned at the boundary:

* :func:`hpc_agent.infra.transport._parse_remote_push_manifest` — the
  None-on-ANY-trouble contract (a broken manifest must never read as "remote
  present");
* :func:`hpc_agent.infra.transport._read_prior_push_manifest` — the parse-side
  fail-open (a severed/garbled read yields the EMPTY known set, never a wrong
  non-empty one that would authorize a deletion);
* :func:`hpc_agent.infra.prune.plan_prune` — the byte-accounting + empty-known
  polarity (anomaly bytes never count toward the cap; empty known ⇒ ZERO prunable);
* :func:`hpc_agent.infra.transport._is_runtime_placed` +
  :func:`hpc_agent.infra.transport._prune_manifest_known_extras` — a
  framework-placed remote file is neither pruned nor nagged as an anomaly;
* :func:`hpc_agent.infra.transport._delta_batch_caps` — the env-override floor;
* :func:`hpc_agent.infra.transport._stage_drop_probe_cmd` — the rc-preserving
  ``|| true`` scoping (rsync-absence never fails the stage-drop leg).
"""

from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

import pytest

from hpc_agent.errors import SshCircuitOpen, SshSlotWaitTimeout
from hpc_agent.infra import transport
from hpc_agent.infra.manifest import FileEntry, Manifest
from hpc_agent.infra.prune import plan_prune

if TYPE_CHECKING:
    from pathlib import Path


def _ok(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


_BREAKER_RAISERS = [
    pytest.param(lambda: SshCircuitOpen("login.test: circuit open"), id="circuit-open"),
    pytest.param(lambda: SshSlotWaitTimeout("login.test: slot wait timed out"), id="slot-timeout"),
]


# ── _parse_remote_push_manifest: None-on-ANY-trouble (never claim remote present) ──
#
# The remote hash manifest decides which local files the delta considers ALREADY
# on the cluster and therefore does NOT re-ship. A garbled manifest read as a
# valid (even empty) Manifest would make the delta think files are present when
# they are not → they are silently skipped → a stale/incomplete deploy. So the
# contract is positive-evidence: return a Manifest ONLY when the payload proves
# one, else ``(None, set())`` which routes to the full-copy tar fallback.


def test_parse_remote_manifest_files_not_a_list_is_none() -> None:
    """``files`` present but NOT a list (a dict) → ``(None, set())``. Kills a mutant
    that widens the ``isinstance(data.get("files"), list)`` guard (e.g. to accept a
    dict): a non-list ``files`` would then reach ``Manifest.from_dict`` and either
    crash or fabricate a bogus manifest that the delta trusts as "remote present"."""
    stdout = json.dumps({"files": {"a.txt": {"size": 1, "sha256": "aa"}}})
    assert transport._parse_remote_push_manifest(stdout) == (None, set())


def test_parse_remote_manifest_toplevel_json_list_is_none() -> None:
    """A top-level JSON LIST (not an object) → ``(None, set())``. Kills a mutant
    that drops the ``isinstance(data, dict)`` half of the guard: ``[...].get("files")``
    would raise ``AttributeError`` (uncaught — it is not a ``JSONDecodeError``),
    turning a merely-unusable manifest into a transport crash."""
    assert transport._parse_remote_push_manifest("[1, 2, 3]") == (None, set())


def test_parse_remote_manifest_malformed_files_entry_is_none() -> None:
    """A well-formed ``files`` list whose MEMBER is malformed (missing ``size``) makes
    ``Manifest.from_dict`` raise ``KeyError`` → ``(None, set())``. Kills a mutant that
    removes the ``except (KeyError, TypeError, ValueError)`` swallow (or narrows it):
    a structurally-broken manifest must degrade to the full-copy fallback, never let
    the exception escape and never be trusted as a partial remote state."""
    stdout = json.dumps({"files": [{"path": "a.txt"}]})  # no size/sha256
    manifest, known = transport._parse_remote_push_manifest(stdout)
    assert manifest is None
    assert known == set()


def test_parse_remote_manifest_valid_payload_folds_paths_and_coerces() -> None:
    """The ONLY branch that yields a real manifest: a valid ``files`` list. The
    folded ``paths`` are coerced with ``str`` (a numeric member survives as its
    string form). Kills a mutant that drops the ``{str(p) ...}`` coercion (a raw
    non-string ``known`` would silently never match a real ``str`` remote extra,
    disabling every prune)."""
    stdout = json.dumps(
        {"files": [{"path": "a.txt", "size": 1, "sha256": "aa"}], "paths": ["a.txt", 7]}
    )
    manifest, known = transport._parse_remote_push_manifest(stdout)
    assert manifest is not None
    assert [e.path for e in manifest.entries] == ["a.txt"]
    assert known == {"a.txt", "7"}  # 7 coerced to "7", not left as int


# ── _read_prior_push_manifest: parse-side fail-open (severed read => UNKNOWN) ──────
#
# ``known`` is the set of remote paths the prune is allowed to DELETE (files we
# proved we shipped before). A garbled read must yield the EMPTY set — every
# remote extra then routes to ANOMALY (never deleted). The danger the guards stop
# is the OPPOSITE: a garbled read coerced into a NON-empty wrong set would
# authorize deleting foreign files. These pin the parse branches the breaker
# tests (which only cover the breaker-open path) leave un-exercised.


def _read_prior_with_stdout(stdout: str) -> set[str]:
    from unittest.mock import patch

    with patch("hpc_agent.infra.transport._guarded_ssh_bounded", return_value=_ok(stdout=stdout)):
        return transport._prune._read_prior_push_manifest(
            ssh_target="u@h", remote_path="/r", timeout=30
        )


def test_read_prior_manifest_dict_paths_is_empty_not_wrong_set() -> None:
    """A ``paths`` that is a DICT (not a list) → the EMPTY known set. Kills a mutant
    that drops the ``isinstance(paths, list)`` guard: ``{str(p) for p in {"a": 1}}``
    iterates the dict's KEYS → ``{"a"}`` — a fabricated non-empty known set that
    would authorize pruning a remote file named ``a`` we never actually shipped
    (the wrong-deletion hazard this guard exists to stop)."""
    assert _read_prior_with_stdout(json.dumps({"paths": {"a": 1}})) == set()


def test_read_prior_manifest_toplevel_list_is_empty_not_crash() -> None:
    """A top-level JSON LIST → the EMPTY known set (no crash). Kills a mutant that
    drops the ``isinstance(data, dict) else None`` guard: ``[...].get("paths")``
    would raise ``AttributeError`` (uncaught), turning an unusable manifest into a
    hard failure of the whole push instead of a fail-open skip."""
    assert _read_prior_with_stdout("[1, 2, 3]") == set()


def test_read_prior_manifest_corrupt_or_empty_is_empty_set() -> None:
    """Corrupt JSON and an empty/whitespace read both → the EMPTY known set. Kills a
    mutant that removes the ``JSONDecodeError`` swallow or the ``if not raw`` guard —
    either would crash or (worse) proceed on undefined ``data``. Fail-open: an
    unprovable manifest deletes NOTHING."""
    assert _read_prior_with_stdout("{ not json ]") == set()
    assert _read_prior_with_stdout("   ") == set()


def test_read_prior_manifest_valid_list_is_the_only_nonempty_case() -> None:
    """A genuine ``paths`` LIST is the ONLY input that produces a non-empty known
    set — the exact positive evidence the prune requires before it may delete.
    Members are ``str``-coerced. Kills a mutant that inverts the guard to return
    ``set()`` on a valid list (which would silently disable every prune)."""
    assert _read_prior_with_stdout(json.dumps({"paths": ["a.py", "b/c.py", 9]})) == {
        "a.py",
        "b/c.py",
        "9",
    }


# ── plan_prune: byte accounting + empty-known polarity (wrong-deletion core) ──────


def test_plan_prune_bytes_count_only_prunable_never_anomalies() -> None:
    """The byte cap governs the would-be DELETE size, so it must sum ``prunable``
    only — an anomaly's bytes (a foreign file we will NEVER delete) must not count.
    Kills a mutant that sums over ``extra_entries`` (or the anomalies): a small
    manifest-known file beside a huge anomaly would then breach a low ``max_bytes``
    and REFUSE a legitimate prune — a foreign file's size vetoing our own cleanup."""
    plan = plan_prune(
        [
            FileEntry(path="ours/small.py", size=5, sha256="s"),
            FileEntry(path="foreign/huge.bin", size=10_000, sha256="x"),
        ],
        manifest_known={"ours/small.py"},
        max_bytes=100,
    )
    assert plan.refused is False  # the 10k-byte anomaly must NOT tip the cap
    assert plan.prune_bytes == 5  # ONLY the manifest-known prunable
    assert plan.to_prune == ("ours/small.py",)
    assert plan.anomalies == ("foreign/huge.bin",)  # foreign — never deleted


def test_plan_prune_empty_known_yields_zero_prunable() -> None:
    """Empty known ⇒ ZERO prunable, ZERO prune_bytes, and EVERY extra surfaced as a
    sorted anomaly (the fail-open polarity: nothing proven ours ⇒ nothing deleted).
    Kills a mutant that inverts ``if e.path in known`` (which would route every
    extra into ``prunable`` when the known set is empty — mass wrong deletion) and a
    mutant that drops the ``sorted(...)`` on the anomaly output."""
    extras = [FileEntry(path=f"foreign/f{i}.dat", size=3, sha256="x") for i in (2, 0, 1, 3)]
    plan = plan_prune(extras, manifest_known=set())
    assert plan.prunable == ()  # empty known => ZERO prunable
    assert plan.to_prune == ()
    assert plan.prune_bytes == 0
    assert plan.refused is False
    assert plan.anomalies == (
        "foreign/f0.dat",
        "foreign/f1.dat",
        "foreign/f2.dat",
        "foreign/f3.dat",
    )  # ALL surfaced, sorted


# ── _is_runtime_placed: prefix-for-dirs vs exact-for-files (framework-safe) ───────


def test_is_runtime_placed_dir_prefix_and_file_exact_boundaries() -> None:
    """A framework-placed path is recognized by PREFIX for a directory pattern
    (``hpc_agent/``) and by EXACT match for a file pattern
    (``.hpc/_hpc_dispatch.py``). Two boundary mutants die here:

    * ``hpc_agentX/y.py`` must be False — kills a mutant that matches the dir
      pattern with ``startswith("hpc_agent")`` (no trailing ``/``), which would
      swallow an unrelated sibling like ``hpc_agent_notes/``;
    * ``.hpc/_hpc_dispatch.pyc`` must be False — kills a mutant that matches the
      FILE pattern by prefix instead of equality, which would over-claim a
      compiled sibling.

    Over-claiming here is a wrong-deletion-adjacent hazard: a mis-classified file
    is silently dropped from BOTH the prune candidates and the anomaly surface."""
    # Directory patterns match by prefix (the framework subtree).
    assert transport._is_runtime_placed("hpc_agent") is True
    assert transport._is_runtime_placed("hpc_agent/execution/mapreduce/metrics_io.py") is True
    assert transport._is_runtime_placed(".hpc/templates/common/hpc_preamble.sh") is True
    # File patterns match ONLY by equality.
    assert transport._is_runtime_placed(".hpc/_hpc_dispatch.py") is True
    assert transport._is_runtime_placed(".hpc/_hpc_dispatch.pyc") is False  # exact, not prefix
    # A sibling that merely shares the dir-pattern's leading text is NOT placed.
    assert transport._is_runtime_placed("hpc_agentX/y.py") is False
    assert transport._is_runtime_placed("hpc_agent_notes/readme.md") is False
    # An ordinary pushed file is never runtime-placed.
    assert transport._is_runtime_placed("src/mod.py") is False


# ── _prune_manifest_known_extras: a runtime-placed extra is neither pruned nor nagged ─


@pytest.fixture
def _capture_prune_legs(monkeypatch: pytest.MonkeyPatch) -> dict[str, list]:
    """Capture the trailing leg (the combined prune+reseal tail vs the standalone
    seal) without touching the network — mirrors ``test_transport_prune.capture_legs``."""
    reseal: list[dict] = []
    seal: list[list[str]] = []

    def _fake_reseal(*, ssh_target, remote_path, prune_paths, seal_paths, timeout):  # type: ignore[no-untyped-def]
        reseal.append({"prune": list(prune_paths), "seal": list(seal_paths)})

    def _fake_seal(*, ssh_target, remote_path, paths, timeout):  # type: ignore[no-untyped-def]
        seal.append(list(paths))

    monkeypatch.setattr(transport, "_prune_and_reseal", _fake_reseal)
    monkeypatch.setattr(transport, "_write_push_manifest", _fake_seal)
    return {"reseal": reseal, "seal": seal}


def test_runtime_placed_extra_is_never_pruned_nor_surfaced(
    tmp_path: Path,
    _capture_prune_legs: dict[str, list],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A remote extra that is a ``deploy_runtime``-placed framework file (rides its
    OWN deploy leg, never the repo push manifest) is filtered from the candidate set
    up front: NOT pruned (no combined tail), NOT surfaced as an anomaly, and the
    manifest is still sealed with just the base. Kills a mutant that drops the
    ``and not _is_runtime_placed(p)`` filter — the framework's own dispatcher/stub
    would then be classified an ANOMALY (nagged forever) or, with a stale ``known``,
    wrongly PRUNED (deleting the live preamble under every array task)."""
    runtime_extra = "hpc_agent/execution/mapreduce/metrics_io.py"
    remote = Manifest(entries=(FileEntry(path=runtime_extra, size=10, sha256="s"),))

    transport._prune_manifest_known_extras(
        ssh_target="host",
        remote_path="/remote/proj",
        local_path=tmp_path,
        remote_manifest=remote,
        known=set(),
        extra=(runtime_extra,),
        seal_paths=["keep.py"],
        timeout=30,
    )

    assert _capture_prune_legs["reseal"] == []  # never pruned
    assert _capture_prune_legs["seal"] == [["keep.py"]]  # sealed with base only
    # No journal record and NOT nagged as an anomaly.
    assert not (tmp_path / ".hpc" / "deploy_prune.jsonl").exists()
    err = capsys.readouterr().err
    assert "ANOMALY" not in err
    assert runtime_extra not in err


# ── breaker fail-open direction: a breaker-open PRUNE reaches ZERO deletions ───────


@pytest.mark.parametrize("raise_factory", _BREAKER_RAISERS)
def test_prune_and_reseal_breaker_open_deletes_nothing_and_never_raises(raise_factory) -> None:
    """The wrong-deletion × breaker intersection: on a breaker-open (or slot give-up)
    the prune+reseal dial degrades in the SAFE direction — the inner ``_ssh_bounded``
    that carries the remote ``rm`` is NEVER invoked (guarded_call fast-fails before
    it), so NO deletion reaches the wire, and NO exception escapes (fail-open).

    Kills two mutants at once: (a) dropping ``SshCircuitOpen``/``SshSlotWaitTimeout``
    from the ``contextlib.suppress(...)`` (the breaker-open would then propagate as a
    fail-LOUD raise on a best-effort prune); (b) any reordering that would run the
    delete leg before the breaker consult."""
    from unittest.mock import patch

    ssh_calls: list = []

    def _spy_ssh(*a, **kw):  # noqa: ANN002, ANN003
        ssh_calls.append((a, kw))
        return _ok()

    def _raise_guard(_target, _fn, **_kw):  # noqa: ANN001
        raise raise_factory()

    with (
        patch("hpc_agent.infra.transport._ssh_bounded", side_effect=_spy_ssh),
        patch("hpc_agent.infra.transport.guarded_call", side_effect=_raise_guard),
    ):
        # No exception escapes (fail-open) …
        transport._prune._prune_and_reseal(
            ssh_target="u@h",
            remote_path="/r",
            prune_paths=["victim/should_not_be_deleted.py"],
            seal_paths=["keep.py"],
            timeout=30,
        )
    # … and the delete leg never reached the wire.
    assert ssh_calls == []


# ── _delta_batch_caps: env override + the max(1, …) floor ─────────────────────────


def test_delta_batch_caps_default_override_and_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ship-batch caps read their two env knobs and FLOOR each at 1. Kills a
    mutant that drops the ``max(1, …)`` floor: a ``0`` (or negative) override would
    yield a 0-file / 0-byte cap — a batch that can never accept a file, wedging the
    delta ship loop or emitting degenerate single-file batches forever."""
    monkeypatch.delenv("HPC_DELTA_BATCH_MAX_FILES", raising=False)
    monkeypatch.delenv("HPC_DELTA_BATCH_MAX_BYTES", raising=False)
    assert transport._delta_batch_caps() == (
        transport._delta._DELTA_BATCH_MAX_FILES,
        transport._delta._DELTA_BATCH_MAX_BYTES,
    )
    monkeypatch.setenv("HPC_DELTA_BATCH_MAX_FILES", "7")
    monkeypatch.setenv("HPC_DELTA_BATCH_MAX_BYTES", "999")
    assert transport._delta_batch_caps() == (7, 999)
    # Floor: a non-positive override can never produce a 0/negative cap.
    monkeypatch.setenv("HPC_DELTA_BATCH_MAX_FILES", "0")
    monkeypatch.setenv("HPC_DELTA_BATCH_MAX_BYTES", "-5")
    assert transport._delta_batch_caps() == (1, 1)


# ── _stage_drop_probe_cmd: the rc-preserving `|| true` scoping ─────────────────────


def test_stage_drop_probe_never_fails_the_leg_on_rsync_absence() -> None:
    """The stage-drop leg drops a stale staging dir AND probes for a login-node
    rsync on the SAME round-trip. The probe is wrapped ``&& {{ command -v rsync …
    && printf … || true; }}`` so the leg's returncode stays the ``rm``'s: a genuine
    drop failure still surfaces, but rsync-ABSENCE (``command -v rsync`` exits
    non-zero) is swallowed by ``|| true`` and never misread as a failed drop.

    Kills a mutant that drops the ``|| true`` guard: on an rsync-less node the probe
    group would exit non-zero, the whole leg would return non-zero, and a perfectly
    healthy stage drop would abort the push. The token is emitted only on the
    success side of the probe's ``&&`` (positive evidence — absence reads as
    'no remote rsync', the conservative default)."""
    cmd = transport._stage_drop_probe_cmd("/r.hpc_stage")
    assert cmd.startswith("rm -rf /r.hpc_stage && ")  # the drop is the rc-bearing head
    assert "command -v rsync" in cmd
    assert "|| true" in cmd  # rsync-absence never fails the leg
    # The token is printed on the SUCCESS side of the probe's `&&`, never uncondi-
    # tionally — so its ABSENCE (severed read or no rsync) is the safe default.
    assert f"rsync >/dev/null 2>&1 && printf %s {transport._RSYNC_PROBE_TOKEN!r}".replace(
        "'", ""
    ) in cmd.replace("'", "")
