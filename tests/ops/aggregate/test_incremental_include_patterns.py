"""Pin :func:`hpc_agent.ops.aggregate_flow._incremental_include_patterns`.

Mutation triage-2 (``docs/plans/mutation-triage-2-2026-07-17.md``, Top-3 Unit 3)
found this at **14/14 mutants survived**, with only one shallow existing test
(``test_flow_incremental_pull.py``) that is out of the sweep's ``tests_dir`` and
never isolates the wave-filename regex or the ``is_dir`` guard. This helper picks
the rsync ``--include`` globs for the incremental ``_combiner/`` pull; a wrong
pattern silently drops or over-pulls per-wave result data (the F08/F09 class), so
its exact contract is data-correctness-load-bearing.

Contract (from the source):
  * ``combined_waves`` empty  -> ``None`` (unfiltered pull), taking precedence
    even when local wave files exist.
  * ``combined_waves`` non-empty but no local ``wave_<N>.json`` partial present
    (dir missing, dir is a file, or only non-partial names) -> ``None``.
  * ``combined_waves`` non-empty AND at least one ``wave_<N>.json`` partial is
    present locally -> exactly ``["wave_*.json", "wave_*.runtime.json"]``.

The regex ``_WAVE_PARTIAL_NAME_RE`` is anchored ``^wave_(\\d+)\\.json$``, so a
runtime sidecar (``wave_3.runtime.json``) and a digit-less ``wave_.json`` must
NOT count as local partials. These cases pin that anchoring in isolation, which
the existing shallow test never does (it always has plain partials present).
Read-only: the tests build tmp dirs and never touch the source.
"""

from __future__ import annotations

from pathlib import Path

from hpc_agent.ops.aggregate_flow import _incremental_include_patterns

_TWO_GLOBS = ["wave_*.json", "wave_*.runtime.json"]


def test_none_when_combined_waves_empty_even_with_local_partials(tmp_path: Path) -> None:
    # The empty-combined_waves gate fires FIRST: None even though a partial
    # exists locally. Kills a mutant flipping ``if not combined_waves``.
    local = tmp_path / "_combiner"
    local.mkdir()
    (local / "wave_0.json").write_text("{}")
    assert _incremental_include_patterns(local, []) is None


def test_none_when_combiner_dir_absent(tmp_path: Path) -> None:
    # Non-empty waves but nothing pulled yet (dir missing) -> unfiltered pull.
    local = tmp_path / "_combiner"
    assert _incremental_include_patterns(local, [0, 1, 2]) is None


def test_none_when_combiner_path_is_a_file(tmp_path: Path) -> None:
    # A path that exists but is NOT a directory must take the is_dir()==False
    # branch -> None (the shallow existing test never exercises a non-dir path).
    local = tmp_path / "_combiner"
    local.write_text("not a dir")
    assert _incremental_include_patterns(local, [0, 1, 2]) is None


def test_none_when_only_runtime_sidecar_present(tmp_path: Path) -> None:
    # ``wave_3.runtime.json`` must NOT match the anchored partial regex, so with
    # no plain partial present the result is None. Pins the ``\\.json$`` anchor.
    local = tmp_path / "_combiner"
    local.mkdir()
    (local / "wave_3.runtime.json").write_text("{}")
    assert _incremental_include_patterns(local, [3]) is None


def test_none_when_only_nonpartial_names_present(tmp_path: Path) -> None:
    # Names that don't match ``^wave_(\\d+)\\.json$`` don't count as local
    # partials: a digit-less ``wave_.json``, a non-wave json, and a txt file.
    local = tmp_path / "_combiner"
    local.mkdir()
    (local / "wave_.json").write_text("{}")  # \\d+ requires >=1 digit
    (local / "combined.json").write_text("{}")
    (local / "notes.txt").write_text("x")
    assert _incremental_include_patterns(local, [0, 1]) is None


def test_two_globs_when_a_single_partial_is_present(tmp_path: Path) -> None:
    # One matching ``wave_<N>.json`` is enough to switch to the two-glob filter.
    # Multi-digit id pins the ``\\d+`` group. Order + exact contents are asserted
    # so a swapped/dropped/renamed glob flips it.
    local = tmp_path / "_combiner"
    local.mkdir()
    (local / "wave_12.json").write_text("{}")
    patterns = _incremental_include_patterns(local, [12])
    assert patterns == _TWO_GLOBS
    assert patterns is not None
    assert patterns[0] == "wave_*.json"
    assert patterns[1] == "wave_*.runtime.json"
    assert len(patterns) == 2


def test_two_globs_ignores_runtime_sidecar_when_a_partial_coexists(tmp_path: Path) -> None:
    # A runtime sidecar alongside a real partial does not change the result — the
    # partial alone drives the switch. Guards against a regression that would let
    # the sidecar be (mis)counted; result is still exactly the two globs.
    local = tmp_path / "_combiner"
    local.mkdir()
    (local / "wave_0.json").write_text("{}")
    (local / "wave_0.runtime.json").write_text("{}")
    assert _incremental_include_patterns(local, [0, 1, 2, 3]) == _TWO_GLOBS
