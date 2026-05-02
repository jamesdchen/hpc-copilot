"""Tests for ``hpc_mapreduce.campaign.campaign_dir``."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hpc_mapreduce.campaign import campaign_dir

if TYPE_CHECKING:
    from pathlib import Path


def test_campaign_dir_returns_canonical_path(tmp_path: Path) -> None:
    p = campaign_dir(tmp_path, "ml_ridge_q1")
    assert p == tmp_path / ".hpc" / "campaigns" / "ml_ridge_q1"
    assert p.is_dir()


def test_campaign_dir_is_idempotent(tmp_path: Path) -> None:
    p1 = campaign_dir(tmp_path, "ml_ridge_q1")
    p2 = campaign_dir(tmp_path, "ml_ridge_q1")
    assert p1 == p2 and p1.is_dir()


def test_campaign_dir_accepts_str_experiment_dir(tmp_path: Path) -> None:
    p = campaign_dir(str(tmp_path), "ml_ridge_q1")
    assert p.is_dir()


def test_campaign_dir_rejects_empty_id(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="non-empty"):
        campaign_dir(tmp_path, "")


def test_campaign_dir_rejects_path_separator(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="filesystem-safe"):
        campaign_dir(tmp_path, "evil/../../escape")


def test_campaign_dir_rejects_dot_segments(tmp_path: Path) -> None:
    for bad in (".", ".."):
        with pytest.raises(ValueError, match="filesystem-safe"):
            campaign_dir(tmp_path, bad)
