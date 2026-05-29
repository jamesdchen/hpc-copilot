"""Tests for ``ssh_validation.validate_remote_path_under_scratch`` (#184)."""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.infra.ssh_validation import validate_remote_path_under_scratch


def test_refuses_scratch_root_exact() -> None:
    """remote_path equal to the cluster scratch root (the catastrophic case) is refused."""
    with pytest.raises(errors.SpecInvalid, match="equals the cluster scratch root"):
        validate_remote_path_under_scratch("/u/scratch/j/jamesdc1", "/u/scratch/j/jamesdc1")


def test_refuses_scratch_root_trailing_slash() -> None:
    """Trailing-slash variants don't sneak the bad path through."""
    with pytest.raises(errors.SpecInvalid, match="equals the cluster scratch root"):
        validate_remote_path_under_scratch("/u/scratch/j/jamesdc1/", "/u/scratch/j/jamesdc1")
    with pytest.raises(errors.SpecInvalid, match="equals the cluster scratch root"):
        validate_remote_path_under_scratch("/u/scratch/j/jamesdc1", "/u/scratch/j/jamesdc1/")


def test_refuses_path_outside_scratch() -> None:
    """A path that is not strictly below scratch is refused (e.g. user home)."""
    with pytest.raises(errors.SpecInvalid, match="not strictly below"):
        validate_remote_path_under_scratch("/u/home/jamesdc1/demo", "/u/scratch/j/jamesdc1")


def test_refuses_scratch_prefix_collision() -> None:
    """A path that string-prefix-matches scratch but isn't truly under it is refused."""
    # `/u/scratch/j/jamesdc12/demo` happens to start with `/u/scratch/j/jamesdc1` but
    # is NOT below it — the slash-boundary check catches the false positive.
    with pytest.raises(errors.SpecInvalid, match="not strictly below"):
        validate_remote_path_under_scratch("/u/scratch/j/jamesdc12/demo", "/u/scratch/j/jamesdc1")


def test_accepts_subdir_below_scratch() -> None:
    """The correct shape — `<scratch>/<repo_name>` — is accepted."""
    out = validate_remote_path_under_scratch(
        "/u/scratch/j/jamesdc1/demo-hpc", "/u/scratch/j/jamesdc1"
    )
    assert out == "/u/scratch/j/jamesdc1/demo-hpc"


def test_accepts_deeper_subdir() -> None:
    """A path multiple levels below scratch is also fine."""
    out = validate_remote_path_under_scratch(
        "/u/scratch/j/jamesdc1/demo-hpc/sub", "/u/scratch/j/jamesdc1"
    )
    assert out == "/u/scratch/j/jamesdc1/demo-hpc/sub"


def test_empty_scratch_is_noop() -> None:
    """No declared scratch (e.g. local-only cluster) → only the base shape check runs."""
    out = validate_remote_path_under_scratch("/some/path", "")
    assert out == "/some/path"


def test_base_shape_check_still_runs() -> None:
    """Shape violations (shell metachars) are still refused before the scratch check."""
    with pytest.raises(errors.SpecInvalid, match="disallowed characters"):
        validate_remote_path_under_scratch(
            "/u/scratch/j/jamesdc1/foo;rm -rf /", "/u/scratch/j/jamesdc1"
        )
