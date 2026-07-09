"""``--harness-adapter`` loading — a dotted-path import of an adapter factory.

The kit is parameterized by ``--harness-adapter <module.path:factory>`` where
*factory* is a ZERO-ARG callable returning the harness's :class:`HarnessAdapter`
(D-K2). Kept out of ``conftest.py`` so the loading logic is importable and
unit-testable without pytest (``tests/conformance_kit/``). Stdlib-only.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from hpc_agent.conformance.adapter import HarnessAdapter

__all__ = ["AdapterLoadError", "load_adapter"]


class AdapterLoadError(ValueError):
    """A ``--harness-adapter`` spec could not be resolved to an adapter."""


def load_adapter(spec: str) -> HarnessAdapter:
    """Resolve ``module.path:factory`` to a harness adapter instance.

    *spec* is a dotted module path, a ``:``, and the name of a zero-arg factory
    in that module. The factory is imported and called; its return value is the
    adapter the kit drives. Every failure mode raises :class:`AdapterLoadError`
    with an actionable message (malformed spec, unimportable module, missing
    attribute, non-callable factory, a factory that raised).
    """
    if not spec or ":" not in spec:
        raise AdapterLoadError(
            f"malformed --harness-adapter {spec!r}: expected 'module.path:factory'"
        )
    module_path, _, attr = spec.partition(":")
    if not module_path or not attr:
        raise AdapterLoadError(
            f"malformed --harness-adapter {spec!r}: expected 'module.path:factory'"
        )
    try:
        module = importlib.import_module(module_path)
    except ImportError as exc:
        raise AdapterLoadError(
            f"--harness-adapter {spec!r}: cannot import module {module_path!r} ({exc})"
        ) from exc
    try:
        factory = getattr(module, attr)
    except AttributeError as exc:
        raise AdapterLoadError(
            f"--harness-adapter {spec!r}: module {module_path!r} has no attribute {attr!r}"
        ) from exc
    if not callable(factory):
        raise AdapterLoadError(
            f"--harness-adapter {spec!r}: {module_path}:{attr} is not callable "
            "(expected a zero-arg factory returning the adapter)"
        )
    try:
        adapter = factory()
    except Exception as exc:  # noqa: BLE001 - surface any factory failure uniformly
        raise AdapterLoadError(
            f"--harness-adapter {spec!r}: factory {attr!r} raised {type(exc).__name__}: {exc}"
        ) from exc
    return cast("HarnessAdapter", adapter)
