"""Cross-domain schema-version manifest + compatibility-check helper.

Five JSON shapes in the codebase carry a ``schema_version`` field and
five readers each implement a private "I read N, what does the doc
say?" check:

* ``slash_commands/session.py`` — session journal (``SCHEMA_VERSION = 1``)
* ``hpc_mapreduce/job/blacklist.py`` — SEGV blacklist (``SCHEMA_VERSION = 1``)
* ``hpc_mapreduce/job/runtime_prior.py`` — runtime priors (``SCHEMA_VERSION = 1``)
* ``hpc_mapreduce/job/calibration.py`` — calibration prediction sidecar (``schema_version = 1`` literal)
* ``hpc_mapreduce/reduce/status.py`` — status rollup (``schema_version = 2`` literal)

Plus the per-run sidecar (``hpc_mapreduce/job/runs.py``,
``SIDECAR_SCHEMA_VERSION = 2``) which has been coordinated with
``map/dispatch.py`` since the P0 v2 fix.

This module collects supported-version sets in one place. Writers keep
their per-module ``SCHEMA_VERSION`` (= the version they emit). Readers
call :func:`compatibility_check` so the *supported reader range* is one
manifest, not a string-comparison sprinkle. Bumping a schema's
supported range is a one-line manifest edit + writer constant bump.

Why a domain string rather than the version constant itself
-----------------------------------------------------------

The reader doesn't usually want to compare against a single number — a
v2 reader of the per-run sidecar should accept v1 too (back-compat) but
v3 only after an explicit reader update. The manifest stores the full
*supported* tuple so this stays declarative.

The check raises :class:`slash_commands.errors.SchemaIncompat` (a typed
HpcError so the CLI maps it to ``ok:false`` with
``error_code="schema_incompat"``) on mismatch. Soft-warn variants live
in the per-module readers — those are advisory and shouldn't tank the
operation.
"""

from __future__ import annotations

from typing import Mapping

# Domain → set of supported versions (writer + back-compat readers).
#
# Writers always emit the *highest* listed version. Readers accept any
# version in the tuple. Bumping = append the new version, optionally
# drop the oldest after a release-note migration window.
_MANIFEST: Mapping[str, tuple[int, ...]] = {
    # Per-run sidecar — v1→v2 migration landed in the P0 fix; both must
    # remain readable while old runs are still in flight.
    "sidecar": (1, 2),
    # SEGV blacklist — only v1 has shipped.
    "blacklist": (1,),
    # Runtime priors — only v1 has shipped.
    "runtime_prior": (1,),
    # Calibration prediction sidecar — only v1 has shipped.
    "calibration_prediction": (1,),
    # Status rollup — v1 was the legacy shape; v2 is the cmd_sha-keyed
    # one shipping today. Readers must tolerate both because long
    # campaigns may have rolled their first wave under v1.
    "status_rollup": (1, 2),
    # Slash-command session journal — only v1 has shipped.
    "session": (1,),
}


def supported_versions(domain: str) -> tuple[int, ...]:
    """Return the supported-version tuple for *domain*.

    Raises :class:`KeyError` if *domain* is unknown — the manifest is
    deliberately a closed enumeration so a typo in a caller fails
    loudly rather than silently widening the supported set.
    """
    return _MANIFEST[domain]


def compatibility_check(domain: str, found: int) -> None:
    """Raise :class:`SchemaIncompat` if *found* is not supported.

    *domain* must be a key in :data:`_MANIFEST`; otherwise a
    :class:`KeyError` propagates. *found* is the integer
    ``schema_version`` (or ``sidecar_schema_version`` etc.) read from
    the on-disk JSON.

    Successful return means the caller may proceed with reads; the
    per-module reader still applies any field-level back-compat
    backfills (e.g. v1 sidecars without ``wave_map``).
    """
    # Local import: ``slash_commands.errors`` imports nothing from
    # ``hpc_mapreduce`` so this is safe, but the import is inside the
    # function so module load order is robust to future refactors.
    from slash_commands import errors as _errors

    supported = _MANIFEST[domain]
    if found in supported:
        return
    raise _errors.SchemaIncompat(
        f"{domain}: on-disk schema_version={found!r} not in "
        f"supported={list(supported)}; please upgrade the writer "
        "or migrate the file."
    )


def is_compatible(domain: str, found: int) -> bool:
    """Non-raising sibling of :func:`compatibility_check`.

    For readers that prefer to soft-skip an incompatible record (e.g.
    the journal rebuilder, which scans every file and shouldn't tank
    on one stale entry) rather than raise. Returns ``True`` iff
    *found* is in :data:`_MANIFEST` ``[domain]``.
    """
    return found in _MANIFEST[domain]


__all__ = ["compatibility_check", "is_compatible", "supported_versions"]
