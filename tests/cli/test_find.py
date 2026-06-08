"""Tests for the ``find`` discovery tier (#306).

``find`` is the middle step between dumping the whole catalog
(``capabilities --full``) and fetching one contract (``describe
<name>``): intent / half-remembered name → a thin ranked candidate
list of ``{name, verb, cli, summary}``. These pin the matching
behaviour, the thin output shape, and the catalog ``summary`` field
the tier ranks on.
"""

from __future__ import annotations

from hpc_agent._kernel.registry.operations import operations_catalog
from hpc_agent.cli.setup import find

from ._helpers import parse_envelope as _parse_envelope
from ._helpers import run_cli as _run_cli

_THIN_KEYS = {"name", "verb", "cli", "summary"}


def _names(result: dict) -> list[str]:
    return [m["name"] for m in result["matches"]]


def test_summary_threaded_into_every_catalog_entry() -> None:
    """The catalog projection carries a ``summary`` (CliShape help) per row.

    ``find`` scans name + summary, so the field must be present on every
    entry — and non-empty for any primitive that declares a CLI help.
    """
    catalog = operations_catalog()
    assert catalog, "registry empty — register_primitives() not run?"
    for entry in catalog:
        assert "summary" in entry, f"{entry['name']} missing summary"
        assert isinstance(entry["summary"], str)
    # find itself declares a help string, so its summary is non-empty.
    by_name = {e["name"]: e for e in catalog}
    assert by_name["find"]["summary"]


def test_fuzzy_name_match_resolves_half_remembered_name() -> None:
    """``submit-batch`` (not a real name) → the real ``submit-flow-batch``."""
    assert "submit-flow-batch" in _names(find(query="submit-batch"))


def test_intent_phrase_keyword_match() -> None:
    """A prose intent phrase matches via the name+summary token scan."""
    hits = _names(find(query="submit a batch"))
    assert any(n.startswith("submit-flow") for n in hits)


def test_matches_are_thin_rows() -> None:
    """Each match carries exactly the thin projection — no schemas/doc body."""
    result = find(query="preflight")
    assert result["matches"], "expected at least one preflight match"
    for row in result["matches"]:
        assert set(row.keys()) == _THIN_KEYS
    assert result["count"] == len(result["matches"])


def test_limit_caps_results() -> None:
    """``limit`` bounds the candidate count."""
    result = find(query="submit", limit=2)
    assert result["count"] <= 2
    assert len(result["matches"]) <= 2


def test_nonpositive_limit_returns_nothing() -> None:
    """A zero or negative limit returns empty — never drops rows via [:-1]."""
    for n in (0, -1, -5):
        result = find(query="submit", limit=n)
        assert result["count"] == 0
        assert result["matches"] == []


def test_blank_query_matches_nothing() -> None:
    """A blank / whitespace query returns empty — it does not dump the catalog."""
    for q in ("", "   ", "\t"):
        result = find(query=q)
        assert result["count"] == 0
        assert result["matches"] == []


def test_find_is_registered_and_agent_facing() -> None:
    """``find`` appears in the catalog as an agent-facing query primitive."""
    entry = next(e for e in operations_catalog() if e["name"] == "find")
    assert entry["verb"] == "query"
    assert entry["agent_facing"] is True


def test_cli_find_emits_ok_envelope() -> None:
    """``hpc-agent find <query>`` returns a well-formed ok envelope."""
    code, stdout, stderr = _run_cli("find", "submit a batch", "--limit", "5")
    assert code == 0, stderr
    env = _parse_envelope(stdout)
    assert env["ok"] is True
    assert env["idempotent"] is True
    matches = env["data"]["matches"]
    assert len(matches) <= 5
    for row in matches:
        assert set(row.keys()) == _THIN_KEYS
