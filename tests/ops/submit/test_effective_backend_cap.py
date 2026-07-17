"""Pin :func:`hpc_agent.ops.submit_flow._effective_cap_for_backend_name`.

Mutation triage-2 (``docs/plans/mutation-triage-2-2026-07-17.md``, Top-3 Unit 1)
found this backend->concurrency-cap resolver at **19/19 mutants survived, 0 test
files referencing it**: it is covered-but-never-asserted. A wrong effective cap
silently over- or under-submits against a real scheduler (it feeds
:func:`_is_multiwave_sweep` and :func:`_main_submission_plan`), so the exact
resolution rule is load-bearing.

The resolver collects up to two ceilings — the backend CLASS's
``max_array_size`` (only when the name is registered) and the cluster's declared
``_cluster_array_cap`` — and returns the ``min`` of whatever it collected, or
``None`` when it collected nothing. These tests pin every branch of that rule so
a mutation on the ``min`` operator, the ``is not None`` guard, the registration
gate, or the empty-list default is killed.

The three collaborators are injected by monkeypatch so the assertion is a pure
in-process unit with no SSH, no clusters.yaml, and no live backend instance —
exactly the ``store``-free unit the triage memo asked for. The ``from
hpc_agent.infra.backends import ...`` inside the SUT re-reads the module
attributes at call time, so patching the module attributes takes effect.
"""

from __future__ import annotations

import pytest

import hpc_agent.infra.backends as backends_mod
import hpc_agent.ops.submit_flow as submit_flow
from hpc_agent.ops.submit_flow import _effective_cap_for_backend_name


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    registered: bool,
    backend_cap: int | None,
    cluster_cap: int | None,
    class_raises: bool = False,
) -> None:
    """Inject the resolver's three collaborators with controlled values.

    ``registered`` decides whether ``"fake"`` is in ``registered_backend_names``;
    ``backend_cap`` is the fake class's ``max_array_size``; ``cluster_cap`` is
    what ``_cluster_array_cap`` returns. ``class_raises`` makes
    ``get_backend_class`` raise (the ``except Exception`` path).
    """
    names = frozenset({"fake"}) if registered else frozenset()
    monkeypatch.setattr(backends_mod, "registered_backend_names", lambda: names)

    fake_cls = type("_FakeBackend", (), {"max_array_size": backend_cap})

    def _get_class(name: str) -> type:
        if class_raises:
            raise RuntimeError("boom")
        return fake_cls

    monkeypatch.setattr(backends_mod, "get_backend_class", _get_class)
    monkeypatch.setattr(submit_flow, "_cluster_array_cap", lambda cluster: cluster_cap)


def test_returns_min_when_backend_is_the_smaller_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # backend 50 < cluster 100 -> the resolver must pick 50 (the MIN), not 100,
    # not the first-collected (backend), not the sum. Paired with the next test
    # to force a genuine min (neither "always backend" nor "always cluster").
    _install(monkeypatch, registered=True, backend_cap=50, cluster_cap=100)
    assert _effective_cap_for_backend_name("fake", "c1") == 50


def test_returns_min_when_cluster_is_the_smaller_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    # cluster 100 < backend 256 -> must pick 100. A max()-mutant would yield 256;
    # a "return caps[0]" (backend-first) mutant would yield 256; a sum would 356.
    _install(monkeypatch, registered=True, backend_cap=256, cluster_cap=100)
    assert _effective_cap_for_backend_name("fake", "c1") == 100


def test_none_when_no_cap_is_known(monkeypatch: pytest.MonkeyPatch) -> None:
    # Unregistered backend AND no cluster cap -> caps is empty -> None. Kills a
    # mutant that returns min([]) unguarded (ValueError) or a non-None default.
    _install(monkeypatch, registered=False, backend_cap=None, cluster_cap=None)
    assert _effective_cap_for_backend_name("nope", "c1") is None


def test_backend_cap_only_when_cluster_declares_none(monkeypatch: pytest.MonkeyPatch) -> None:
    # Registered backend cap present, cluster cap None -> the backend cap flows
    # through unchanged. Pins that a missing cluster cap does not zero the result.
    _install(monkeypatch, registered=True, backend_cap=256, cluster_cap=None)
    assert _effective_cap_for_backend_name("fake", None) == 256


def test_cluster_cap_only_when_backend_unregistered(monkeypatch: pytest.MonkeyPatch) -> None:
    # Backend name not registered -> its cap is never collected; cluster cap is
    # the sole ceiling. Pins the ``in registered_backend_names()`` gate.
    _install(monkeypatch, registered=False, backend_cap=999, cluster_cap=100)
    assert _effective_cap_for_backend_name("nope", "c1") == 100


def test_cluster_cap_only_when_backend_class_declares_no_max(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Registered, but the class's ``max_array_size`` is None -> the ``is not
    # None`` guard drops it and only the cluster cap survives. Pins that guard.
    _install(monkeypatch, registered=True, backend_cap=None, cluster_cap=100)
    assert _effective_cap_for_backend_name("fake", "c1") == 100


def test_none_when_backend_class_and_cluster_both_declare_no_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Registered but no class cap, and no cluster cap -> nothing collected -> None.
    _install(monkeypatch, registered=True, backend_cap=None, cluster_cap=None)
    assert _effective_cap_for_backend_name("fake", "c1") is None


def test_backend_lookup_failure_is_swallowed_cluster_cap_survives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``get_backend_class`` raising must be caught (the ``except Exception``
    # branch) and must NOT discard the cluster cap collected afterwards.
    _install(monkeypatch, registered=True, backend_cap=256, cluster_cap=100, class_raises=True)
    assert _effective_cap_for_backend_name("fake", "c1") == 100
