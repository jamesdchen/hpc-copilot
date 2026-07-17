"""Candidate input-data-root detection (``ops/detect_input_data.py``) — the
shared engine for the data-leg deepening option (a): scan the repo, DISCLOSE
data-shaped candidates for the human to confirm, NEVER silently mint/capture.

Pins:
* a planted ``data/`` dir is surfaced as an unconfirmed candidate (name +
  extension signals);
* the exclude vocabulary is honored (``.hpc`` / ``.venv`` / cluster output dirs
  are never candidates);
* DVC pointers name a data target;
* detection NEVER mints a manifest or writes anything (detect-discloses-only);
* detection is FAIL-OPEN — an unreadable/erroring walk yields ``[]``, never a
  raise (the disclosure-path never-blocking pin).
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import hpc_agent.ops.detect_input_data as did
import hpc_agent.state.data_manifest as sdm
from hpc_agent.ops.detect_input_data import (
    CandidateDataRoot,
    detect_candidate_data_roots,
)
from tests.contracts.never_blocking import assert_never_blocking


def _plant(root: Path, rel: str, text: str = "x") -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ── the detection engine ──────────────────────────────────────────────────────


def test_detects_planted_data_dir_as_unconfirmed_candidate(tmp_path: Path) -> None:
    """A planted ``data/`` dir holding a ``.csv`` is surfaced as a candidate with
    BOTH the conventional-name and data-extension reasons."""
    _plant(tmp_path, "data/train.csv")
    _plant(tmp_path, "main.py", "print(1)")  # code, not data

    candidates = detect_candidate_data_roots(tmp_path)

    assert [c.path for c in candidates] == ["data"]
    assert set(candidates[0].reasons) == {"conventional-name", "data-extension"}


def test_detects_non_conventional_dir_by_data_extension(tmp_path: Path) -> None:
    """A NON-conventional dir name is a candidate purely by holding data-shaped
    files (the odd-name case the residual note warns about is still caught when
    the extension is telltale)."""
    _plant(tmp_path, "corpus/shard-0.parquet")

    candidates = detect_candidate_data_roots(tmp_path)

    assert [c.path for c in candidates] == ["corpus"]
    assert candidates[0].reasons == ("data-extension",)


def test_conventional_dir_without_data_extension_still_flagged(tmp_path: Path) -> None:
    """A conventionally-named dir (``inputs/``) is a candidate even when its files
    carry no telltale extension — the name alone proposes it for confirmation."""
    _plant(tmp_path, "inputs/notes.txt")

    candidates = detect_candidate_data_roots(tmp_path)

    assert [c.path for c in candidates] == ["inputs"]
    assert candidates[0].reasons == ("conventional-name",)


def test_dvc_pointer_names_a_data_target(tmp_path: Path) -> None:
    """A top-level ``<name>.dvc`` pointer names ``<name>`` as a (possibly
    not-yet-pulled) data target."""
    _plant(tmp_path, "bigdata.dvc", "outs:\n- md5: abc\n  path: bigdata\n")

    candidates = detect_candidate_data_roots(tmp_path)

    assert [c.path for c in candidates] == ["bigdata"]
    assert candidates[0].reasons == ("dvc-pointer",)


def test_code_only_repo_yields_no_candidates(tmp_path: Path) -> None:
    """A repo of only code / config yields NO candidates — no data-shaped signal,
    no false positives (keeps the fully-declared regression clean)."""
    _plant(tmp_path, "src/model.py", "print(1)")
    _plant(tmp_path, "configs/base.yaml", "lr: 0.1")
    _plant(tmp_path, "README.md", "# hi")

    assert detect_candidate_data_roots(tmp_path) == []


def test_excluded_trees_are_never_candidates(tmp_path: Path) -> None:
    """The shared exclude vocabulary is honored — the ``.hpc`` control tree, a
    ``.venv``, and cluster run-output dirs (``results/``) are never surfaced,
    even when they contain data-shaped files."""
    _plant(tmp_path, ".hpc/data_manifest.json", "{}")
    _plant(tmp_path, ".venv/lib/data/x.csv")  # dotdir + venv
    _plant(tmp_path, "results/out.csv")  # protected cluster output dir
    _plant(tmp_path, "data/real.csv")  # the one real candidate

    candidates = detect_candidate_data_roots(tmp_path)

    assert [c.path for c in candidates] == ["data"]


def test_detection_never_mints_or_writes(tmp_path: Path) -> None:
    """detect-discloses-candidates-NEVER-auto-mints: running detection over a
    data-shaped tree leaves NO manifest and writes nothing under ``.hpc``."""
    _plant(tmp_path, "data/train.csv")

    detect_candidate_data_roots(tmp_path)

    # No manifest minted, no data-identity fabricated — capture stays the human's.
    assert sdm.read_manifest(tmp_path) is None
    assert not (tmp_path / ".hpc").exists()
    assert sdm.data_identity(tmp_path) is None


def test_detection_is_fail_open_on_an_erroring_walk(tmp_path: Path) -> None:
    """FAIL-OPEN: if the underlying walk raises, detection returns ``[]`` rather
    than propagating — detection feeds a DISCLOSURE path and must never crash a
    submit."""
    _plant(tmp_path, "data/train.csv")

    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("simulated unreadable tree")

    with mock.patch.object(did, "iter_exclude_filtered_files", side_effect=_boom):
        assert detect_candidate_data_roots(tmp_path) == []


def test_detection_source_never_raises() -> None:
    """The disclosure-path never-blocking pin: the detector's source carries no
    ``raise`` (a future gate/refusal trips this)."""
    assert_never_blocking(detect_candidate_data_roots)


def test_candidate_as_brief_is_json_safe() -> None:
    """The code-rendered projection is a plain JSON-safe dict (path + reasons)."""
    c = CandidateDataRoot(path="data", reasons=("conventional-name", "data-extension"))
    assert c.as_brief() == {
        "path": "data",
        "reasons": ["conventional-name", "data-extension"],
    }
