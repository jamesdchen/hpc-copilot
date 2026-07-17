"""Behaviour-pinning coverage for :mod:`hpc_agent.cli._fast_path_cache`.

The 2026-07-17 mutation triage-2 (``docs/plans/mutation-triage-2-2026-07-17.md``)
found fast-path-cache among the curated modules that ran DARK — mutmut aborted
its baseline because the module's only covering set at the time was the SLOW
subprocess ``test_fast_dispatch`` battery (mutmut deselects ``slow`` and can't
instrument a child interpreter), so it produced ZERO verdicts. The dedicated
in-process ``tests/cli/test_fast_path_cache.py`` battery is now the covering set.

fast-path-cache keys the cross-process plugin verdict on a CHEAP fingerprint of
the installed-distribution set. A wrong fingerprint is a correctness hazard: too
STABLE (misses a ``pip install`` of a CLI-reshaping plugin) serves a stale
verdict that mis-shapes a core verb; too VOLATILE (order-sensitive, double-
counting) needlessly re-pays the scan the cache exists to avoid. This file ADDS
the pins the existing battery leaves a mutant free to survive: the
sorted/dedup/suffix-gate invariants of ``installed_dist_signature`` (its one
existing "changes when a dist appears" test moves ``scanned`` too, so it does NOT
isolate the token gate), the malformed-verdict decode guards, and the
undecodable-hit rescan.

Every assertion notes the mutation it kills inline.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

import hpc_agent.cli._fast_path_cache as fpc

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path))
    monkeypatch.delenv("HPC_AGENT_NO_FAST_PATH_CACHE", raising=False)


# ── installed_dist_signature: the sorted / dedup / suffix-gate invariants ───────


def test_signature_is_stable_under_syspath_reorder(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # kills: dropping ``sorted(tokens)`` before the hash. The docstring promises
    # "a mere reorder of sys.path does not [change the signature] (the union is
    # sorted)"; no existing test reorders sys.path, so an unsorted-join mutant
    # survives — and would re-pay the entry_points scan on every process whose
    # sys.path order differs.
    a = tmp_path / "a"
    a.mkdir()
    (a / "pkg-a.dist-info").mkdir()
    b = tmp_path / "b"
    b.mkdir()
    (b / "pkg-b.dist-info").mkdir()

    monkeypatch.setattr(fpc.sys, "path", [str(a), str(b)])
    forward = fpc.installed_dist_signature()
    monkeypatch.setattr(fpc.sys, "path", [str(b), str(a)])
    reversed_order = fpc.installed_dist_signature()
    assert forward == reversed_order


def test_signature_dedups_a_repeated_syspath_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # kills: removing the ``if norm in seen: continue`` / ``seen.add(norm)``
    # dedup. A dir listed twice on sys.path must count ONCE; without dedup the
    # second pass re-increments ``scanned`` and re-appends every token, so the
    # signature drifts for a purely cosmetic path duplication.
    d = tmp_path / "d"
    d.mkdir()
    (d / "pkg.dist-info").mkdir()

    monkeypatch.setattr(fpc.sys, "path", [str(d)])
    once = fpc.installed_dist_signature()
    monkeypatch.setattr(fpc.sys, "path", [str(d), str(d)])
    twice = fpc.installed_dist_signature()
    assert once == twice


@pytest.mark.parametrize("meta", ["pkg.dist-info", "widget.egg-info", "thing.egg-link"])
def test_each_metadata_suffix_is_recognized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, meta: str
) -> None:
    # kills: dropping any member of the
    # ``(".dist-info", ".egg-info", ".egg-link")`` endswith gate. The existing
    # "changes when a dist appears" test PREPENDS a new dir, so it moves
    # ``scanned`` and passes even with a broken gate; here the dir count is held
    # constant so only token DETECTION can move the signature.
    site = tmp_path / "site"
    site.mkdir()
    monkeypatch.setattr(fpc.sys, "path", [str(site)])
    base = fpc.installed_dist_signature()  # empty dir: scanned=1, 0 tokens

    # A non-metadata name (file or dir) must NOT move the signature.
    (site / "plain-file.txt").write_text("x", encoding="utf-8")
    (site / "just-a-dir").mkdir()
    assert fpc.installed_dist_signature() == base, "non-metadata names must be ignored by the gate"

    # A metadata name of this suffix MUST move it (scanned unchanged → token gate).
    if meta.endswith(".egg-link"):
        (site / meta).write_text("path", encoding="utf-8")
    else:
        (site / meta).mkdir()
    assert fpc.installed_dist_signature() != base


def test_one_scannable_dir_among_junk_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # kills: the ``if scanned == 0: raise`` boundary flipping (e.g. ``== 1``).
    # A single real dir among unscannable entries yields scanned>=1, so the
    # fingerprint is trusted (no raise) — the complement of the existing
    # "nothing scannable → raises" test, pinning the boundary from both sides.
    real = tmp_path / "real"
    real.mkdir()
    monkeypatch.setattr(fpc.sys, "path", [str(real), "/nonexistent/does/not/exist"])
    sig = fpc.installed_dist_signature()
    assert isinstance(sig, str) and sig


# ── _decode_verdict: the malformed-entry refusal guards ────────────────────────


@pytest.mark.parametrize(
    "entry",
    [
        None,
        "a string",
        7,
        ["not", "a", "dict"],
        {"reshaped": []},  # conservative missing → get() is None, not a bool
        {"conservative": "yes", "reshaped": []},  # conservative not a bool
        {"conservative": True, "reshaped": "x"},  # reshaped not a list
        {"conservative": True, "reshaped": [1, 2]},  # reshaped elements not str
        {"conservative": True, "reshaped": ["ok", 3]},  # one non-str element
    ],
)
def test_decode_rejects_every_malformed_verdict_shape(entry: object) -> None:
    # kills: any of the four guards in _decode_verdict — the not-a-dict guard,
    # the ``isinstance(conservative, bool)`` check, the ``isinstance(reshaped,
    # list)`` check, and the ``all(isinstance(v, str) ...)`` check. A malformed
    # cache entry must decode to None (→ fresh scan), never a bogus verdict.
    assert fpc._decode_verdict(entry) is None


def test_decode_accepts_valid_and_preserves_bool_and_frozenset() -> None:
    # kills: a mutation of the ``return conservative, frozenset(reshaped)`` tuple
    # (order swap, dropping the frozenset, coercing the bool). A well-formed
    # entry must round-trip to exactly (bool, frozenset[str]).
    assert fpc._decode_verdict({"conservative": True, "reshaped": ["a", "b"]}) == (
        True,
        frozenset({"a", "b"}),
    )
    assert fpc._decode_verdict({"conservative": False, "reshaped": []}) == (False, frozenset())


# ── _read_cache: valid-JSON-but-not-an-object fallback ─────────────────────────


def test_read_cache_rejects_valid_json_that_is_not_an_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # kills: the ``return data if isinstance(data, dict) else {}`` non-dict
    # branch. The existing corrupt-file test writes NON-json (a ValueError);
    # this writes VALID json that parses to a list, exercising the isinstance
    # guard instead. A non-object cache file must degrade to ``{}`` (a miss),
    # not a list that ``.get("signature")`` would blow up on.
    monkeypatch.setattr(fpc, "installed_dist_signature", lambda: "sig-L")
    path = fpc._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert fpc._read_cache() == {}


# ── _write_cache: the sorted on-disk verdict + payload schema ──────────────────


def test_write_cache_persists_sorted_reshaped_under_the_expected_keys() -> None:
    # kills: dropping ``sorted(reshaped)`` in the written payload (frozenset
    # iteration order is nondeterministic, so an unsorted write makes the cache
    # file's bytes vary run-to-run) AND any rename of the payload schema keys.
    fpc._write_cache("sig-T", (True, frozenset({"c", "a", "b"})))
    raw = json.loads(fpc._cache_path().read_text(encoding="utf-8"))
    assert raw["signature"] == "sig-T"
    assert raw["verdict"]["conservative"] is True
    assert raw["verdict"]["reshaped"] == ["a", "b", "c"]  # sorted, not frozenset order


# ── cached_cli_reshaping_verdict: signature-hit with an undecodable verdict ─────


def test_signature_hit_with_undecodable_verdict_falls_through_to_a_fresh_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # kills: the ``if decoded is not None: return decoded`` fall-through guard.
    # A cache whose SIGNATURE matches but whose verdict is corrupt must NOT be
    # trusted — _decode_verdict returns None and the call rescans. A mutant that
    # returns the (None) decode, or skips the None-check, would serve a bogus
    # verdict on a signature hit.
    import hpc_agent._kernel.registry.plugins as plugins_mod

    monkeypatch.setattr(fpc, "installed_dist_signature", lambda: "sig-U")
    path = fpc._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"signature": "sig-U", "verdict": {"conservative": "nope", "reshaped": []}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(plugins_mod, "cli_reshaping_verdict", lambda: (True, frozenset({"z"})))
    assert fpc.cached_cli_reshaping_verdict() == (True, frozenset({"z"}))
