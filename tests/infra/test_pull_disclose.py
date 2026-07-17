"""Pin :func:`hpc_agent.infra.transport._pull._disclose_pull_batch`.

Mutation triage-2 (``docs/plans/mutation-triage-2-2026-07-17.md``, Top-3 Unit 2)
found this at **12/12 mutants survived, 0 test files referencing it**. It is the
human-facing per-batch pull disclosure — a consent-adjacent honesty surface —
and nothing pins its content, so a mutated field, counter, or the MiB math would
ship silently.

These tests assert the EXACT disclosed line for a known batch (so a swapped
``index``/``total``, a changed ``n_files``, or a mutated byte->MiB divisor /
one-decimal format all flip an assertion) and that the line goes to stderr, not
stdout. The function only READS its arguments and prints; the tests never touch
the transport module's source.
"""

from __future__ import annotations

import pytest

from hpc_agent.infra.transport._pull import _disclose_pull_batch

_MiB = 1024 * 1024


def test_exact_disclosure_line_for_a_known_batch(capsys: pytest.CaptureFixture[str]) -> None:
    # 2.5 MiB == 2_621_440 bytes; index/total distinct so "batch 2/5" pins order.
    _disclose_pull_batch(index=2, total=5, n_files=3, batch_bytes=int(2.5 * _MiB))
    err = capsys.readouterr().err.strip()
    assert err == (
        "[transport] content-hash PULL: fetching batch 2/5 "
        "(3 file(s), 2.5 MB); landed batches are durable so a "
        "died-mid-pull retry fetches only the remainder."
    )


def test_index_over_total_not_transposed(capsys: pytest.CaptureFixture[str]) -> None:
    # A mutant swapping the two counters would print "batch 5/2".
    _disclose_pull_batch(index=2, total=5, n_files=1, batch_bytes=_MiB)
    err = capsys.readouterr().err
    assert "fetching batch 2/5 " in err
    assert "batch 5/2" not in err


def test_n_files_is_disclosed(capsys: pytest.CaptureFixture[str]) -> None:
    _disclose_pull_batch(index=1, total=1, n_files=7, batch_bytes=_MiB)
    assert "(7 file(s)," in capsys.readouterr().err


def test_bytes_shown_as_binary_megabytes(capsys: pytest.CaptureFixture[str]) -> None:
    # 5_000_000 bytes / 1024^2 == 4.768... -> "4.8 MB". A decimal (10^6) divisor
    # would render "5.0 MB"; this kills that mutant.
    _disclose_pull_batch(index=1, total=1, n_files=1, batch_bytes=5_000_000)
    err = capsys.readouterr().err
    assert "4.8 MB" in err
    assert "5.0 MB" not in err


def test_megabytes_carry_exactly_one_decimal(capsys: pytest.CaptureFixture[str]) -> None:
    # Exactly 1 MiB -> "1.0 MB". Kills a ``:.0f`` mutant ("1 MB") and a ``:.2f``
    # mutant ("1.00 MB").
    _disclose_pull_batch(index=1, total=1, n_files=1, batch_bytes=_MiB)
    err = capsys.readouterr().err
    assert "1.0 MB" in err
    assert "1.00 MB" not in err
    assert "1 MB)" not in err


def test_line_goes_to_stderr_not_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    _disclose_pull_batch(index=1, total=2, n_files=1, batch_bytes=_MiB)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "[transport] content-hash PULL: fetching batch 1/2 " in captured.err
