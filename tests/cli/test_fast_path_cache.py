"""Cross-process verdict cache + fast_path_safe handler (latency rank 13).

Two rank-13 mechanisms beyond the per-verb plugin gate:

* :mod:`hpc_agent.cli._fast_path_cache` caches the reduced
  ``cli_reshaping_verdict`` keyed on a cheap fingerprint of the installed
  distribution set, so the ``entry_points()`` scan is not re-paid by every
  ``hpc-agent`` subprocess. It invalidates on any change to that set and
  fails open to a fresh scan.
* ``CliShape.fast_path_safe`` opts a self-contained HANDLER primitive
  (``install-commands``) into the fast path; a registry-introspecting handler
  (``capabilities`` / ``describe``) stays excluded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import hpc_agent.cli._fast_path_cache as fpc


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point the journal home (cache file lives there) at a tmp dir; enable cache."""
    monkeypatch.setenv("HPC_JOURNAL_DIR", str(tmp_path))
    monkeypatch.delenv("HPC_AGENT_NO_FAST_PATH_CACHE", raising=False)


# ── installed_dist_signature ───────────────────────────────────────────────


def test_signature_is_stable_and_nonempty() -> None:
    sig1 = fpc.installed_dist_signature()
    sig2 = fpc.installed_dist_signature()
    assert sig1 == sig2 and sig1


def test_signature_changes_when_a_dist_appears(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A new ``.dist-info`` on ``sys.path`` (a ``pip install``) changes the key."""
    before = fpc.installed_dist_signature()
    fake = tmp_path / "sitepkgs"
    (fake / "brandnew-9.9.dist-info").mkdir(parents=True)
    monkeypatch.syspath_prepend(str(fake))
    assert fpc.installed_dist_signature() != before


def test_signature_raises_when_nothing_scannable(monkeypatch: pytest.MonkeyPatch) -> None:
    """An un-fingerprintable env raises so the caller fails open to a fresh scan."""
    monkeypatch.setattr(fpc.sys, "path", ["", "/does/not/exist/anywhere"])
    with pytest.raises(RuntimeError):
        fpc.installed_dist_signature()


# ── cached_cli_reshaping_verdict ───────────────────────────────────────────


def test_cache_miss_then_hit_avoids_second_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """First call computes + writes; second call reads without re-scanning."""
    import hpc_agent._kernel.registry.plugins as plugins_mod

    calls = {"n": 0}

    def _verdict() -> tuple[bool, frozenset[str]]:
        calls["n"] += 1
        return False, frozenset({"submit-s1"})

    monkeypatch.setattr(plugins_mod, "cli_reshaping_verdict", _verdict)

    first = fpc.cached_cli_reshaping_verdict()
    second = fpc.cached_cli_reshaping_verdict()
    assert first == (False, frozenset({"submit-s1"}))
    assert second == first
    assert calls["n"] == 1, "the verdict must be scanned once, then served from cache"


def test_cache_invalidates_on_signature_change(monkeypatch: pytest.MonkeyPatch) -> None:
    import hpc_agent._kernel.registry.plugins as plugins_mod

    verdicts: list[tuple[bool, frozenset[str]]] = [(False, frozenset()), (True, frozenset())]
    seq = iter(verdicts)
    monkeypatch.setattr(plugins_mod, "cli_reshaping_verdict", lambda: next(seq))

    sigs = iter(["sig-A", "sig-A", "sig-B"])
    monkeypatch.setattr(fpc, "installed_dist_signature", lambda: next(sigs))

    assert fpc.cached_cli_reshaping_verdict() == (False, frozenset())  # miss, writes sig-A
    assert fpc.cached_cli_reshaping_verdict() == (False, frozenset())  # sig-A hit
    assert fpc.cached_cli_reshaping_verdict() == (True, frozenset())  # sig-B miss → rescan


def test_disabled_cache_always_scans(monkeypatch: pytest.MonkeyPatch) -> None:
    import hpc_agent._kernel.registry.plugins as plugins_mod

    monkeypatch.setenv("HPC_AGENT_NO_FAST_PATH_CACHE", "1")
    calls = {"n": 0}

    def _verdict() -> tuple[bool, frozenset[str]]:
        calls["n"] += 1
        return False, frozenset()

    monkeypatch.setattr(plugins_mod, "cli_reshaping_verdict", _verdict)
    fpc.cached_cli_reshaping_verdict()
    fpc.cached_cli_reshaping_verdict()
    assert calls["n"] == 2, "disabled cache must re-scan every call"


def test_signature_error_fails_open_to_fresh_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    import hpc_agent._kernel.registry.plugins as plugins_mod

    def _boom() -> str:
        raise RuntimeError("cannot fingerprint")

    monkeypatch.setattr(fpc, "installed_dist_signature", _boom)
    monkeypatch.setattr(plugins_mod, "cli_reshaping_verdict", lambda: (True, frozenset()))
    assert fpc.cached_cli_reshaping_verdict() == (True, frozenset())


def test_corrupt_cache_file_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    import hpc_agent._kernel.registry.plugins as plugins_mod

    monkeypatch.setattr(fpc, "installed_dist_signature", lambda: "sig-X")
    path = fpc._cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(plugins_mod, "cli_reshaping_verdict", lambda: (False, frozenset({"x"})))
    assert fpc.cached_cli_reshaping_verdict() == (False, frozenset({"x"}))


# ── fast_path_safe handler (install-commands) ──────────────────────────────


def test_install_commands_is_fast_path_safe_handler() -> None:
    """``install-commands`` declares a handler AND opts into the fast path."""
    from hpc_agent._kernel.registry.primitive import get_meta
    from hpc_agent.cli._dispatch import CliShape

    shape = get_meta("install-commands").cli
    assert isinstance(shape, CliShape)
    assert shape.handler is not None
    assert shape.fast_path_safe is True


def test_build_single_verb_parser_builds_fast_path_safe_handler() -> None:
    """The single-verb parser now BUILDS a fast_path_safe handler primitive
    (previously it rejected every handler)."""
    from hpc_agent.cli.parser import build_single_verb_parser

    parser = build_single_verb_parser("install-commands")
    assert parser is not None
    # It parses the verb + its flags and binds a dispatch func.
    ns = parser.parse_args(["install-commands", "--dry-run"])
    assert ns.dry_run is True
    assert callable(ns.func)


def test_registry_introspecting_handlers_stay_excluded() -> None:
    """``capabilities`` / ``describe`` read the whole registry — never fast-pathed."""
    from hpc_agent.cli.parser import build_single_verb_parser

    assert build_single_verb_parser("capabilities") is None
    assert build_single_verb_parser("describe") is None


def test_generator_maps_fast_path_safe_handler() -> None:
    """The generator's render includes ``install-commands`` (map-independent:
    recomputes from the live registry, so GREEN even while the committed map is
    the disclosed-red stale one)."""
    import scripts.build_verb_module_map as gen

    rendered = gen._render()
    assert "'install-commands'" in rendered
    assert "hpc_agent.cli.setup" in rendered
    # A registry-introspecting handler must NOT be rendered.
    assert "'capabilities'" not in rendered
