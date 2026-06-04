"""Tests for the ``check-task-generator-mismatch`` validate verb (WS5 #9).

Pins the canonical-JSON compare at hpc-submit Step 3: match/mismatch on
content (not Python ``==`` or key order), the vacuous-match
no-cached-generator path, the sha256 digests, and the str/dict input
duality (CLI passes JSON strings; the in-process path passes dicts).
"""

from __future__ import annotations

import hashlib
import json

from hpc_agent.ops import check_task_generator_mismatch as ctgm


def _sha(value: object) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TestCanonicalJson:
    def test_key_order_independent(self) -> None:
        a = ctgm.canonical_json({"b": 1, "a": 2})
        b = ctgm.canonical_json({"a": 2, "b": 1})
        assert a == b == '{"a":2,"b":1}'

    def test_nested_key_order_independent(self) -> None:
        a = ctgm.canonical_json({"k": "x", "params": {"z": 1, "a": 2}})
        b = ctgm.canonical_json({"params": {"a": 2, "z": 1}, "k": "x"})
        assert a == b


class TestMatch:
    def test_identical_dicts_match(self) -> None:
        gen = {"kind": "items_x_seeds", "params": {"seeds": 100}}
        out = ctgm.check_task_generator_mismatch(
            caller_task_generator=gen, cached_task_generator=gen
        )
        assert out["match"] is True
        assert out["reason"] == "identical"

    def test_key_order_difference_still_matches(self) -> None:
        out = ctgm.check_task_generator_mismatch(
            caller_task_generator={"kind": "x", "params": {"a": 1, "b": 2}},
            cached_task_generator={"params": {"b": 2, "a": 1}, "kind": "x"},
        )
        assert out["match"] is True
        assert out["reason"] == "identical"
        # Same content ⇒ same canonical ⇒ same sha256.
        assert out["caller"]["sha256"] == out["cached"]["sha256"]


class TestMismatch:
    def test_divergent_seed_count_mismatches(self) -> None:
        caller = {"kind": "items_x_seeds", "params": {"seeds": 100}}
        cached = {"kind": "items_x_seeds", "params": {"seeds": 8}}
        out = ctgm.check_task_generator_mismatch(
            caller_task_generator=caller, cached_task_generator=cached
        )
        assert out["match"] is False
        assert out["reason"] == "divergent"
        # Both shapes surfaced for the task_generator_mismatch envelope.
        assert out["caller"]["sha256"] == _sha(caller)
        assert out["cached"]["sha256"] == _sha(cached)
        assert out["caller"]["sha256"] != out["cached"]["sha256"]

    def test_different_kind_mismatches(self) -> None:
        out = ctgm.check_task_generator_mismatch(
            caller_task_generator={"kind": "cartesian_product"},
            cached_task_generator={"kind": "items_x_seeds"},
        )
        assert out["match"] is False
        assert out["reason"] == "divergent"


class TestNoCachedGenerator:
    def test_none_cached_is_vacuous_match(self) -> None:
        out = ctgm.check_task_generator_mismatch(
            caller_task_generator={"kind": "x"}, cached_task_generator=None
        )
        assert out["match"] is True
        assert out["reason"] == "no_cached_generator"
        assert out["cached"] is None
        assert out["caller"]["canonical"] == '{"kind":"x"}'

    def test_omitted_cached_defaults_to_none(self) -> None:
        out = ctgm.check_task_generator_mismatch(caller_task_generator={"kind": "x"})
        assert out["match"] is True
        assert out["reason"] == "no_cached_generator"


class TestStringInputDuality:
    def test_json_string_inputs_parsed(self) -> None:
        # The CLI path passes JSON object strings, not dicts.
        out = ctgm.check_task_generator_mismatch(
            caller_task_generator='{"kind":"x","params":{"seeds":100}}',
            cached_task_generator='{"params":{"seeds":8},"kind":"x"}',
        )
        assert out["match"] is False
        assert out["reason"] == "divergent"

    def test_string_and_dict_compare_equal(self) -> None:
        # A string and an equivalent dict canonicalize to the same form.
        out = ctgm.check_task_generator_mismatch(
            caller_task_generator='{"a":1,"b":2}',
            cached_task_generator={"b": 2, "a": 1},
        )
        assert out["match"] is True


class TestOutputShape:
    def test_keys_present(self) -> None:
        out = ctgm.check_task_generator_mismatch(
            caller_task_generator={"k": 1}, cached_task_generator={"k": 2}
        )
        assert set(out) == {"match", "reason", "caller", "cached"}
        assert set(out["caller"]) == {"canonical", "sha256"}
        assert set(out["cached"]) == {"canonical", "sha256"}
