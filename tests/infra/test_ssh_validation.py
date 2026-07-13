"""Tests for ``ssh_validation.validate_remote_path_under_scratch`` (#184) and the
shared sentinel-ack helpers (``wrap_with_ack`` / ``split_ack``, run-12 finding 24)."""

from __future__ import annotations

import pytest

from hpc_agent import errors
from hpc_agent.infra.ssh_validation import (
    split_ack,
    validate_remote_path_under_scratch,
    wrap_with_ack,
)

# --- shared sentinel-ack primitive (docs/design/connection-broker.md) --------

_P = "__HPC_TEST_ACK__="


def test_wrap_with_ack_appends_semicolon_echo_of_exit_code() -> None:
    # ``;``-sequenced (fires regardless of rc) and carries $? — never ``|| true``.
    assert wrap_with_ack("qstat -u me", _P) == 'qstat -u me; echo "__HPC_TEST_ACK__=$?"'


def test_split_ack_present_strips_line_and_returns_rc() -> None:
    clean, rc = split_ack(f"row-a\nrow-b\n{_P}0\n", _P)
    assert rc == 0 and _P not in clean
    # line structure (incl. the last real row's trailing newline) is preserved so
    # a downstream partition on an embedded sentinel still sees original bytes.
    assert clean == "row-a\nrow-b\n"


def test_split_ack_absent_is_none_the_channel_silence_signal() -> None:
    clean, rc = split_ack("row-a\nrow-b\n", _P)
    assert rc is None and clean == "row-a\nrow-b\n"


def test_split_ack_empty_read_is_none() -> None:
    assert split_ack("", _P) == ("", None)


def test_split_ack_non_numeric_payload_is_minus_one() -> None:
    _, rc = split_ack(f"{_P}garbage\n", _P)
    assert rc == -1


def test_split_ack_roundtrips_wrap_with_ack() -> None:
    # wrap_with_ack emits `; echo "<prefix>$?"`; the shell prints `<prefix><rc>`
    # on its own line after the body. split_ack recovers presence + untouched body.
    assert wrap_with_ack("cmd", _P).endswith(f'; echo "{_P}$?"')
    stdout = f"body\n{_P}0\n"  # what the wrapped command's stdout looks like on rc 0
    clean, rc = split_ack(stdout, _P)
    assert rc == 0 and clean == "body\n"


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
