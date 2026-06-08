"""Pin: an unknown CLI verb yields a compact "did you mean" line, never the
full ~70-verb argparse dump.

Stock argparse answers an invalid subcommand by printing the usage line
(every verb) plus ``invalid choice: 'X' (choose from <every verb again>)`` —
the whole CLI surface twice, a content-free tax on a spawned worker's
context. ``_HpcArgumentParser`` collapses that to one line, and ``metavar``
keeps the usage line from enumerating every verb.
"""

from __future__ import annotations

import pytest

from hpc_agent.cli.parser import build_parser


def _verb_error(argv: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(argv)
    assert exc.value.code == 2
    return capsys.readouterr().err


def test_unknown_verb_is_not_a_full_choice_dump(capsys: pytest.CaptureFixture[str]) -> None:
    err = _verb_error(["totally-not-a-verb"], capsys)
    assert "unknown command 'totally-not-a-verb'" in err
    # The whole point: stop dumping every verb on a miss.
    assert "choose from" not in err
    # A 3-item "did you mean" carries at most two commas; the old dump carried
    # ~70. Pin that the surface stays tiny.
    assert err.count(",") <= 3


def test_name_vs_verb_trap_suggests_the_real_verb(capsys: pytest.CaptureFixture[str]) -> None:
    # The exact misfire the user hit: the primitive's registry/doc name is not
    # the CLI verb, but it is lexically close to it.
    assert "preflight" in _verb_error(["check-preflight"], capsys)
    assert "discover" in _verb_error(["discover-executors"], capsys)
