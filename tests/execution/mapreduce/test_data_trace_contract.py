"""Tests for the data-trace emission contract (T2).

Covers the four testable surfaces of ``data_trace_contract``:

* constants equality / lock-step pins (dispatcher shares the strings),
* the freshest-transport selection (mtime order),
* the tolerant JSONL read,
* the fallback-rule decision table, and
* the closed source-tier set.

Toy fixtures only — text ``_trace.jsonl`` files, no quant vocabulary.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from hpc_agent.execution.mapreduce import data_trace_contract as dtc
from hpc_agent.execution.mapreduce import dispatch


# --------------------------------------------------------------------------
# Constants + lock-step pins
# --------------------------------------------------------------------------
def test_transport_filename_value():
    assert dtc.TRACE_TRANSPORT_FILENAME == "_trace.jsonl"


def test_digest_env_var_value():
    assert dtc.TRACE_DIGEST_ENV_VAR == "HPC_TRACE_DIGESTS"


def test_dispatcher_shares_constants_lockstep():
    # The standalone cluster dispatcher hardcodes copies (it cannot import
    # this module at cluster runtime); they MUST match — the _EXIT precedent.
    assert dispatch._TRACE_TRANSPORT_FILENAME == dtc.TRACE_TRANSPORT_FILENAME
    assert dispatch._TRACE_DIGEST_ENV_VAR == dtc.TRACE_DIGEST_ENV_VAR


def test_source_tier_values():
    assert dtc.TRACE_SOURCE_FIELD == "source"
    assert dtc.TRACE_SOURCE_RUNNER == "runner"
    assert dtc.TRACE_SOURCE_ENGINE == "engine"
    assert dtc.TRACE_SOURCE_DRAFT == "draft"


def test_source_tiers_closed_set():
    # The closed set, descending trust order — pinned so a fourth tier is a
    # reviewed edit, never an accident.
    assert dtc.TRACE_SOURCE_TIERS == ("runner", "engine", "draft")


def test_receipt_grade_is_runner_only():
    # A10/A11: only runner-observed records are receipt-grade.
    assert dtc.RECEIPT_GRADE_SOURCES == ("runner",)
    assert dtc.TRACE_SOURCE_RUNNER in dtc.TRACE_SOURCE_TIERS


def test_read_source_kinds_closed():
    assert dtc.READ_TRANSPORT == "transport"
    assert dtc.READ_STORE == "store"


# --------------------------------------------------------------------------
# The fallback-rule decision table
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("consumer", "is_local", "expected"),
    [
        ("authoring", True, ("transport", "store")),
        ("authoring", False, ("transport",)),
        ("verification", True, ("store",)),
        ("verification", False, ("store",)),
    ],
)
def test_resolve_read_order_table(consumer, is_local, expected):
    assert dtc.resolve_read_order(consumer, is_local_run=is_local) == expected


def test_resolve_read_order_authoring_always_tries_transport_first():
    for is_local in (True, False):
        order = dtc.resolve_read_order("authoring", is_local_run=is_local)
        assert order[0] == dtc.READ_TRANSPORT


def test_resolve_read_order_verification_never_reads_transport():
    for is_local in (True, False):
        order = dtc.resolve_read_order("verification", is_local_run=is_local)
        assert dtc.READ_TRANSPORT not in order


def test_resolve_read_order_unknown_class_raises():
    with pytest.raises(ValueError, match="unknown trace consumer_class"):
        dtc.resolve_read_order("reference", is_local_run=True)


# --------------------------------------------------------------------------
# local_scope
# --------------------------------------------------------------------------
def test_local_scope_truncates_to_12():
    assert dtc.local_scope("0123456789abcdef") == ("local", "0123456789ab")


def test_local_scope_short_sha_passthrough():
    assert dtc.local_scope("abc") == ("local", "abc")


# --------------------------------------------------------------------------
# Freshest-transport selection
# --------------------------------------------------------------------------
def _write_trace(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


def test_find_freshest_none_when_absent(tmp_path):
    assert dtc.find_freshest_transport(tmp_path) is None


def test_find_freshest_missing_root(tmp_path):
    assert dtc.find_freshest_transport(tmp_path / "nope") is None


def test_find_freshest_picks_newest_mtime(tmp_path):
    old = tmp_path / "arm_a" / dtc.TRACE_TRANSPORT_FILENAME
    new = tmp_path / "arm_b" / dtc.TRACE_TRANSPORT_FILENAME
    _write_trace(old, [{"stage": "load", "seq": 0}])
    _write_trace(new, [{"stage": "load", "seq": 0}])
    # Force a clear mtime ordering regardless of write speed / fs resolution.
    old_t = time.time() - 100
    new_t = time.time()
    os.utime(old, (old_t, old_t))
    os.utime(new, (new_t, new_t))

    assert dtc.find_freshest_transport(tmp_path) == new


def test_read_freshest_returns_records_of_newest(tmp_path):
    old = tmp_path / "run1" / dtc.TRACE_TRANSPORT_FILENAME
    new = tmp_path / "run2" / dtc.TRACE_TRANSPORT_FILENAME
    _write_trace(old, [{"stage": "old", "seq": 0, "source": "draft"}])
    _write_trace(new, [{"stage": "new", "seq": 0, "source": "runner"}])
    os.utime(old, (time.time() - 50, time.time() - 50))

    result = dtc.read_freshest_transport(tmp_path, scope=("run", "r-1"))
    assert result.path == new
    assert [r["stage"] for r in result.records] == ["new"]
    # Source tier surfaced as-is, untouched.
    assert result.records[0][dtc.TRACE_SOURCE_FIELD] == "runner"
    assert result.scope == ("run", "r-1")


def test_read_freshest_empty_when_no_transport(tmp_path):
    result = dtc.read_freshest_transport(tmp_path)
    assert result.path is None
    assert result.records == []
    assert result.scope is None


# --------------------------------------------------------------------------
# Tolerant read
# --------------------------------------------------------------------------
def test_read_transport_records_skips_junk(tmp_path):
    path = tmp_path / dtc.TRACE_TRANSPORT_FILENAME
    path.write_text(
        "\n".join(
            [
                json.dumps({"stage": "load", "seq": 0}),
                "",  # blank line
                "not json at all",  # unparseable — process still appending
                json.dumps([1, 2, 3]),  # valid JSON but not an object
                json.dumps({"stage": "scale", "seq": 1}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    records = dtc.read_transport_records(path)
    assert [r["stage"] for r in records] == ["load", "scale"]


def test_read_transport_records_missing_file(tmp_path):
    assert dtc.read_transport_records(tmp_path / "gone.jsonl") == []


def test_read_transport_records_preserves_order(tmp_path):
    path = tmp_path / dtc.TRACE_TRANSPORT_FILENAME
    _write_trace(path, [{"seq": i} for i in range(5)])
    records = dtc.read_transport_records(path)
    assert [r["seq"] for r in records] == [0, 1, 2, 3, 4]
